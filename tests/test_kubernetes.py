"""Kubernetes and logs as first-class connections: problems, events, context, tools, and answer checks."""
import datetime as dt
import ssl

import pytest
import pytest_asyncio
from fake_kube import TOKEN, FakeKube, deployment, pod, serve

from app.context.render import render_context
from app.diagnosis.problems import find_problems
from app.integrations.kubernetes import KubernetesClient, summarize_pod
from app.investigation.tools import ToolRunner
from app.investigation.verification import verify_answer
from app.net_guard import TargetPolicy
from app.sources.base import SourceError

LOCAL = TargetPolicy(allow_loopback=True)


def snapshot(pods=(), deployments=(), nodes=()):
    from app.integrations.kubernetes import summarize_deployment, summarize_node

    return {"pods": [summarize_pod(item) for item in pods], "deployments": [summarize_deployment(item) for item in deployments],
            "nodes": [summarize_node(item) for item in nodes], "events": [], "forbidden": []}


@pytest.fixture
def kube():
    server, url = serve()
    yield url
    server.shutdown()


@pytest_asyncio.fixture
async def kube_only(console, kube):
    """A company with a Kubernetes cluster and nothing else: no metric source."""
    client = await console.company("KubeCo", "o@kube.example")
    org_id = (await client.get("/api/auth/me")).json()["org_id"]
    FakeKube.state.crash()
    saved = await client.post("/api/integrations", json={"kind": "kubernetes", "name": "docker-desktop", "config": {"url": kube}, "secrets": {"token": TOKEN}})
    assert saved.status_code == 201, saved.text
    engine = await console.ctx.engines.get(org_id)
    await engine.refresh_now()
    return client, org_id, engine


# Problems from cluster state -------------------------------------------------------
def test_problems_are_keyed_on_the_workload_not_the_pod():
    pods = [pod("shop", f"payment-5b5568b987-{suffix}", "payment", waiting="CrashLoopBackOff", last_reason="Error", exit_code=1, restarts=5, ready=False)
            for suffix in ("aaaaa", "bbbbb", "ccccc")]
    problems = find_problems(snapshot(pods, [deployment("shop", "payment", 3, 0)]))
    assert len(problems) == 1, "one crash-looping deployment is one problem, with no second 'unavailable' problem for it"
    problem = problems[0]
    assert problem["fingerprint"] == "k8s:shop/deployment/payment:crashloop"
    assert problem["severity"] == "critical" and len(problem["pods"]) == 3
    assert "Deployment shop/payment crash-looping" in problem["title"] and "exit code 1" in problem["title"]


def test_each_failure_signature_is_recognised():
    now = dt.datetime.now(dt.timezone.utc)
    cases = {
        "image_pull": pod("a", "web-5b5568b987-1", "web", waiting="ImagePullBackOff", ready=False),
        "config_error": pod("a", "api-5b5568b987-1", "api", waiting="CreateContainerConfigError", ready=False),
        "oomkilled": pod("a", "cache-5b5568b987-1", "cache", last_reason="OOMKilled", exit_code=137, restarts=2, limits={"memory": "512Mi"}),
        "unschedulable": pod("a", "big-5b5568b987-1", "big", phase="Pending", ready=False, unschedulable="0/1 nodes are available: 1 Insufficient memory."),
        "not_ready": pod("a", "slow-5b5568b987-1", "slow", ready=False),
    }
    for expected, item in cases.items():
        problems = find_problems(snapshot([item]), now)
        assert [p["problem"] for p in problems] == [expected], expected
    fresh = pod("a", "new-5b5568b987-1", "new", phase="Pending", ready=False, unschedulable="0/1 nodes", created_minutes_ago=0.5)
    assert find_problems(snapshot([fresh]), now) == [], "a pod that was just created gets time to be scheduled"


def test_deployment_and_node_problems():
    nodes = [{"metadata": {"name": "n1"}, "spec": {}, "status": {"conditions": [{"type": "Ready", "status": "False", "message": "kubelet stopped posting"}]}},
             {"metadata": {"name": "n2"}, "spec": {}, "status": {"conditions": [{"type": "Ready", "status": "True"}, {"type": "DiskPressure", "status": "True"}]}}]
    problems = find_problems(snapshot([], [deployment("a", "down", 2, 0), deployment("a", "half", 4, 2), deployment("a", "stuck", 2, 1, stuck=True),
                                           deployment("a", "fine", 2, 2), deployment("a", "scaled-to-zero", 0, 0)], nodes))
    found = {p["fingerprint"]: p["severity"] for p in problems}
    assert found == {
        "k8s:a/deployment/down:unavailable": "critical", "k8s:a/deployment/half:degraded": "warning",
        "k8s:a/deployment/stuck:rollout_stuck": "critical", "k8s:node/n1:not_ready": "critical", "k8s:node/n2:pressure": "warning",
    }
    assert problems[0]["severity"] == "critical", "worst first"


# The client --------------------------------------------------------------------------
async def test_client_snapshot_logs_and_forbidden_kinds(kube):
    FakeKube.state.crash()
    FakeKube.state.forbid = {"nodes"}
    client = KubernetesClient({"url": kube}, {"token": TOKEN}, LOCAL)
    assert "can list pods" in (await client.test())["detail"]
    snap = await client.snapshot()
    assert snap["forbidden"] == ["nodes"] and snap["pods"][0]["workload"] == "payment"
    crashed = await client.pod_logs("shop", "payment-5b5568b987-h5zzq", previous=True, lines=10)
    assert "connection refused postgres:5432" in crashed["lines"][-1]
    assert (await client.pod_logs("shop", "payment-5b5568b987-h5zzq", previous=True, lines=1))["lines"] == [crashed["lines"][-1]]
    FakeKube.state.logs.pop(("shop", "payment-5b5568b987-h5zzq", True))
    assert "previous container" in (await client.pod_logs("shop", "payment-5b5568b987-h5zzq", previous=True))["note"]
    await client.close()
    with pytest.raises(SourceError) as error:
        await KubernetesClient({"url": kube}, {"token": "wrong"}, LOCAL).test()
    assert error.value.kind == "auth"


def _tls_material(tmp_path):
    """A private CA and a server certificate for kubernetes.docker.internal, like Docker Desktop's."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = dt.datetime.now(dt.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-kubernetes-ca")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key()).serial_number(1)
          .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False, key_encipherment=False,
                                       data_encipherment=False, key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "kube-apiserver")])).issuer_name(ca_name)
            .public_key(key.public_key()).serial_number(2).not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("kubernetes.docker.internal")]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    cert_file, key_file = tmp_path / "server.pem", tmp_path / "server.key"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    return context, ca.public_bytes(serialization.Encoding.PEM).decode()


async def test_private_ca_and_tls_server_name(tmp_path):
    """Docker Desktop's API certificate names kubernetes.docker.internal, but a container reaches it by another address."""
    server_tls, ca_pem = _tls_material(tmp_path)
    server, url = serve(server_tls)
    try:
        untrusted = KubernetesClient({"url": url}, {"token": TOKEN}, LOCAL)
        with pytest.raises(SourceError) as error:
            await untrusted.test()
        assert error.value.kind == "tls" and "CA certificate" in error.value.message
        await untrusted.close()
        wrong_name = KubernetesClient({"url": url, "ca_cert": ca_pem}, {"token": TOKEN}, LOCAL)
        with pytest.raises(SourceError):
            await wrong_name.test()
        await wrong_name.close()
        trusted = KubernetesClient({"url": url, "ca_cert": ca_pem, "tls_server_name": "kubernetes.docker.internal"}, {"token": TOKEN}, LOCAL)
        assert (await trusted.test())["ok"]
        await trusted.close()
        with pytest.raises(SourceError):
            KubernetesClient({"url": url, "ca_cert": "not a certificate"}, {"token": TOKEN}, LOCAL)
    finally:
        server.shutdown()


async def test_connection_form_rules(console, kube):
    client = await console.company("FormCo", "o@form.example")
    bad = await client.post("/api/integrations", json={"kind": "kubernetes", "name": "k", "config": {"url": kube, "ca_cert": "hello"}, "secrets": {}})
    assert bad.status_code == 422 and "PEM" in bad.json()["detail"]
    bad = await client.post("/api/integrations", json={"kind": "kubernetes", "name": "k", "config": {"url": kube, "tls_server_name": "a b"}, "secrets": {}})
    assert bad.status_code == 422
    tested = await client.post("/api/integrations/test", json={"kind": "kubernetes", "name": "k", "config": {"in_cluster": True}, "secrets": {}})
    assert tested.json()["ok"] is False and "inside a Kubernetes cluster" in tested.json()["error"]
    saved = await client.post("/api/integrations", json={"kind": "kubernetes", "name": "k", "config": {"url": kube}, "secrets": {"token": TOKEN}})
    assert saved.status_code == 201
    again = await client.post("/api/integrations", json={"kind": "kubernetes", "name": "k", "config": {"url": kube, "namespace": "shop"}, "secrets": {"token": ""}})
    assert again.json()["has_secret"] is True, "saving again without retyping the token keeps it"


# A cluster with no metric source -----------------------------------------------------
async def test_cluster_alone_gives_a_full_context(console, kube_only):
    client, org_id, engine = kube_only
    context = engine.context
    assert context["health"]["status"] == "ok" and context["coverage"]["has_metrics"] is False
    assert context["problems"][0]["fingerprint"] == "k8s:shop/deployment/payment:crashloop"
    assert "Kubernetes workload is failing" in context["headline"]["text"]
    text = render_context(context)
    assert text.index("PROBLEMS RIGHT NOW (1)") < text.index("KUBERNETES DETAIL"), "what is broken comes first"
    assert "No metric source" not in text and "available: logs, Kubernetes" in text
    assert "FIRING ALERTS" not in text, "metric sections are left out when there are no metrics"
    events = await console.ctx.db.all("SELECT kind, title FROM events WHERE org_id = ?", (org_id,))
    assert [row["kind"] for row in events] == ["k8s_started"]
    assert events[0]["title"].startswith("Already active when the console connected."), "the first poll can't date a problem"

    viewer = (await client.get("/api/context")).json()
    assert viewer["problems"][0]["kind"] == "k8s"


async def test_cluster_problems_start_and_clear_as_events(console, kube_only):
    client, org_id, engine = kube_only
    FakeKube.state.heal()
    engine._integrations_at = 0
    await engine.refresh_now()
    assert engine.context["problems"] == []
    FakeKube.state.down = True
    FakeKube.state.crash()
    engine._integrations_at = 0
    await engine.refresh_now()
    FakeKube.state.down = False
    engine._integrations_at = 0
    await engine.refresh_now()
    FakeKube.state.down = True
    engine._integrations_at = 0
    await engine.refresh_now()
    assert engine.context["health"]["status"] == "unreachable"
    kinds = [row["kind"] for row in await console.ctx.db.all("SELECT kind FROM events WHERE org_id = ? ORDER BY id", (org_id,))]
    assert kinds == ["k8s_started", "k8s_cleared", "k8s_started"], "an unreachable cluster doesn't clear its problems"


async def test_tools_follow_what_is_connected(console, kube_only):
    client, org_id, engine = kube_only
    runner = ToolRunner(engine, console.ctx.db, console.ctx.box, console.ctx.config)
    names = {tool["name"] for tool in await runner.definitions()}
    assert {"get_kubernetes_state", "get_pod_logs", "describe_object"} <= names
    assert not names & {"run_instant_query", "run_range_query", "list_metrics"}, "no metric tools without a metric source"
    ok, text = await runner.run("run_instant_query", {"query": "kubernetes.pods{x≠''} | summarize count()"})
    assert not ok and "No metric source is connected" in text and "get_pod_logs" in text, "the error points to the right tool"
    ok, text = await runner.run("get_pod_logs", {"namespace": "shop", "pod": "payment", "previous": True})
    assert ok and "connection refused postgres:5432" in text and "h5zzq" in text, "a workload name finds its worst pod"
    ok, text = await runner.run("describe_object", {"namespace": "shop", "name": "payment"})
    assert ok and "CrashLoopBackOff" in text and "BackOff" in text
    ok, text = await runner.run("get_kubernetes_state", {"resource": "problems"})
    assert ok and "crash-looping" in text
    ok, text = await runner.run("no_such_tool", {})
    assert not ok and "get_pod_logs" in text


async def test_answer_that_ignores_the_crash_is_flagged():
    problems = [{"kind": "k8s", "severity": "critical", "title": "Deployment shop/payment crash-looping (12 restarts, exit code 1)",
                 "detail": {"workload": "payment", "pods": ["payment-5b5568b987-h5zzq"]}}]
    ignored = ("## Summary\nNo problem is visible.\n## Likely root cause\nUnknown.\nConfidence: Unknown\n"
               "## Evidence\n- No recent errors in logs (Query: `invalid parameter \"query\"`).\n## Competing explanation\nUnknown.")
    result = await verify_answer("what is wrong with my app", ignored, [], None, problems)
    assert any("doesn't address" in reason and "shop/payment" in reason for reason in result["reasons"])
    assert any("error message as if it were data" in reason for reason in result["reasons"])
    assert result["missed_problems"]
    found = ("## Summary\npayment crashes on start.\n## Likely root cause\nThe payment app can't reach postgres:5432.\nConfidence: High\n"
             "## Evidence\n- `{namespace=\"shop\"} |= \"ERROR\"` shows connection refused.\n## Competing explanation\nA bad image.")
    result = await verify_answer("what is wrong", found, ["postgres:5432"], None, problems)
    assert result["missed_problems"] == [] and result["confidence_final"] == "High", "a log query isn't run as PromQL"


# Diagnoses and crash logs ------------------------------------------------------------
def _problem(item, **extra):
    from app.diagnosis.problems import find_problems as find

    problems = find(snapshot([item]))
    assert problems, "expected a problem"
    return {**problems[0], **extra}


def test_diagnoses_name_the_cause_with_evidence():
    from app.diagnosis.rules import diagnose

    crash = _problem(pod("shop", "payment-5b5568b987-1", "payment", waiting="CrashLoopBackOff", last_reason="Error", exit_code=1, restarts=12, ready=False))
    result = diagnose(crash, ["2026-11-07T00:17:40Z payment starting", "2026-11-07T00:17:41Z ERROR: connection refused postgres:5432"])
    assert result["cause"] == "The application exits on start with an error: ERROR: connection refused postgres:5432"
    assert result["confidence"] == "high" and "last exit code 1: a general error in the application" in result["evidence"]
    assert "kubectl -n shop logs deployment/payment --previous --tail=100" in result["commands"]

    oom = _problem(pod("a", "cache-5b5568b987-1", "cache", waiting="CrashLoopBackOff", last_reason="OOMKilled", exit_code=137, limits={"memory": "512Mi"}))
    assert diagnose(oom)["cause"] == "The container runs out of memory at its 512Mi limit and is killed (OOMKilled)."

    probe = _problem(pod("a", "api-5b5568b987-1", "api", waiting="CrashLoopBackOff", last_reason="Error", exit_code=137))
    events = [{"namespace": "a", "name": "api-5b5568b987-1", "reason": "Unhealthy", "message": "Liveness probe failed: HTTP probe failed with statuscode: 500"}]
    assert "liveness probe" in diagnose(probe, [], events)["cause"]

    missing = _problem(pod("a", "web-5b5568b987-1", "web", waiting="CreateContainerConfigError", ready=False))
    missing["facts"]["message"] = 'secret "db-password" not found'
    assert diagnose(missing)["cause"] == "The pod refers to a Secret named db-password that doesn't exist in this namespace."

    pull = _problem(pod("a", "web-5b5568b987-1", "web", waiting="ImagePullBackOff", ready=False, image="shop/web:9.9"))
    pull["facts"]["message"] = "Back-off pulling image: manifest unknown"
    assert "doesn't exist in the registry" in diagnose(pull)["cause"]

    pending = _problem(pod("a", "big-5b5568b987-1", "big", phase="Pending", ready=False, unschedulable="0/3 nodes are available: 3 Insufficient memory."))
    assert diagnose(pending)["cause"] == "No node can take the pod: not enough free memory."


async def test_crash_logs_and_diagnosis_reach_the_context_once_per_restart(console, kube_only):
    client, org_id, engine = kube_only
    problem = engine.context["problems"][0]
    assert problem["detail"]["diagnosis"]["cause"].endswith("connection refused postgres:5432")
    assert any("connection refused postgres:5432" in line for line in problem["detail"]["logs"]["lines"])
    text = render_context(engine.context)
    assert "Diagnosis (computed from cluster state, not by a model)" in text and "<untrusted_logs>" in text
    assert text.index("connection refused postgres:5432") < text.index("KUBERNETES DETAIL")
    engine._integrations_at = 0
    await engine.refresh_now()
    assert FakeKube.state.log_requests == ["shop/payment-5b5568b987-h5zzq:previous"], "logs are read again only after a restart"
    detail = (await console.ctx.db.one("SELECT detail FROM events WHERE org_id = ?", (org_id,)))["detail"]
    assert "connection refused" in detail and "untrusted" not in detail and '"logs"' not in detail, "events keep the diagnosis, not the log lines"


def test_small_model_mode():
    from app.ai_settings import is_small_model
    from app.investigation.prompts import build_system, compact_tools
    from app.investigation.tools import TOOL_DEFINITIONS

    assert is_small_model("qwen2.5:3b") and is_small_model("llama3.1:8b") and is_small_model("phi3:mini")
    assert not is_small_model("qwen2.5:14b") and not is_small_model("llama3.2:latest") and not is_small_model("claude-sonnet-5-5")
    assert [tool["name"] for tool in compact_tools(TOOL_DEFINITIONS)] == ["get_pod_logs", "describe_object", "query_logs", "run_instant_query"]
    compact = build_system("ctx", [], [], True, False, compact=True)
    assert len(compact) < len(build_system("ctx", [], [], True, False)) and "## Likely root cause" in compact


# The Nov 6 test, replayed ------------------------------------------------------------
async def _set_model(client, url, model):
    settings = (await client.get("/api/ai/settings")).json()
    settings["ollama"].update(url=url, model=model, use_tools=True)
    for key in ("key_source", "key_configured"):
        settings["claude"].pop(key, None)
    response = await client.put("/api/ai/settings", json={k: settings[k] for k in ("provider", "max_rounds", "ollama", "claude")})
    assert response.status_code == 200, response.text


async def _investigate(client, prompt):
    import json

    conversation = (await client.post("/api/conversations", json={})).json()
    run = await client.post(f"/api/conversations/{conversation['id']}/messages", json={"prompt": prompt})
    assert run.status_code == 202, run.text
    async with client.stream("GET", f"/api/runs/{run.json()['run_id']}/stream") as response:
        [line async for line in response.aiter_lines()]
    detail = (await client.get(f"/api/conversations/{conversation['id']}")).json()
    message = [item for item in detail["messages"] if item["role"] == "assistant"][-1]
    calls = (await client.get("/api/llm-calls")).json()
    first = (await client.get(f"/api/llm-calls/{calls[-1]['id']}")).json()
    return message, json.loads(message["verification"]), json.dumps(first["request"])


async def test_replay_of_the_nov_6_investigation(console, kube_only, fake_models):
    """Loki-free cluster with a crash-looping payment app and no Prometheus, as on Nov 6."""
    client, org_id, engine = kube_only
    await _set_model(client, fake_models, "careless:3b")
    message, verification, request = await _investigate(client, "what is wrong with my app?")
    assert message["status"] == "complete"
    assert "connection refused postgres:5432" in request and "PROBLEMS RIGHT NOW (1)" in request, "the crash log went to the model"
    assert "You are a site reliability engineer. Find why" in request, "a 3B model gets the short prompt"
    tool_calls = await console.ctx.db.all("SELECT name, ok, result FROM tool_calls WHERE org_id = ?", (org_id,))
    assert tool_calls[0]["name"] == "query_logs" or "get_pod_logs" in tool_calls[0]["result"], "a wrong guess is pointed to the right tool"
    assert any("doesn't address" in reason for reason in verification["reasons"]), "an answer that ignores the crash is flagged"

    await _set_model(client, fake_models, "careful:7b")
    message, verification, request = await _investigate(client, "what is wrong with my app?")
    assert "postgres:5432" in message["content"] and verification["confidence_final"] == "High" and verification["missed_problems"] == []
