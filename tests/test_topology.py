"""The service dependency map: reading it from Kubernetes and metrics, and telling causes from symptoms."""
from app.integrations.kubernetes import summarize_pod
from app.topology import Topology, env_hosts, link_problems, parse_observed, public_map, summarize_service


def _pod(namespace, workload, env=(), labels=None):
    raw = {"metadata": {"namespace": namespace, "name": f"{workload}-5b5568b987-abcde", "labels": {"pod-template-hash": "5b5568b987", "app": workload, **(labels or {})},
                        "ownerReferences": [{"kind": "ReplicaSet", "name": f"{workload}-5b5568b987"}]},
           "spec": {"containers": [{"name": "app", "env": [{"name": key, "value": value} for key, value in env]}]},
           "status": {"phase": "Running", "containerStatuses": []}}
    return summarize_pod(raw)


def _service(namespace, name, app=None):
    return summarize_service({"metadata": {"namespace": namespace, "name": name}, "spec": {"selector": {"app": app or name}}})


def _shop():
    """checkout calls payments, payments calls postgres and Stripe, frontend calls checkout."""
    pods = [
        _pod("shop", "frontend", [("CHECKOUT_URL", "http://checkout:8080/api")]),
        _pod("shop", "checkout", [("PAYMENTS_ADDR", "payments.shop.svc.cluster.local:9000"), ("LOG_LEVEL", "info")]),
        _pod("shop", "payments", [("DATABASE_URL", "postgres://app:s3cr3t-pass@postgres-primary.data:5432/payments"),
                                  ("STRIPE_API", "https://api.stripe.com/v1"), ("DB_PASSWORD", "postgres-primary")]),
        _pod("data", "postgres-primary"),
        _pod("shop", "worker", [("REDIS_HOST", "redis"), ("KAFKA_BROKERS", "kafka-0.kafka:9092,kafka-1.kafka:9092")]),
    ]
    services = [_service("shop", "frontend"), _service("shop", "checkout"), _service("shop", "payments"), _service("data", "postgres-primary")]
    return {"pods": pods, "deployments": [], "services": services}


def _map():
    topology = Topology()
    topology.add_cluster(1, "prod-eu", _shop(), main=True)
    return topology


def test_hosts_are_read_from_the_environment_without_keeping_any_value():
    containers = [{"env": [
        {"name": "DATABASE_URL", "value": "postgres://app:hunter2@db.prod:5432/x"},
        {"name": "API_TOKEN", "value": "https://tokens.example.com"},
        {"name": "DB_PASSWORD", "value": "orders-db"},
        {"name": "MODE", "value": "production"},
        {"name": "CACHE_HOST", "value": "redis"},
        {"name": "BROKERS", "value": "kafka-0:9092, kafka-1:9092"},
        {"name": "ADDR", "value": "10.0.0.7:8080"},
        {"name": "FROM_SECRET", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}},
    ]}]
    hosts = env_hosts(containers)
    assert hosts == ["db.prod", "redis", "kafka-0", "kafka-1"]
    assert not any("hunter2" in host or "app:" in host for host in hosts), "credentials in a URL are dropped"
    assert "orders-db" not in hosts and "tokens.example.com" not in hosts, "secret-looking variables are never read"
    assert "production" not in hosts, "a plain word counts as a host only under a host-like name"


def test_the_map_links_workloads_through_their_services():
    topology = _map()
    name = topology.name
    edges = {(name(edge["from"]), name(edge["to"])): edge for edge in topology.edges.values()}
    assert set(edges) == {("shop/frontend", "shop/checkout"), ("shop/checkout", "shop/payments"),
                          ("shop/payments", "data/postgres-primary"), ("shop/payments", "api.stripe.com")}
    assert edges[("shop/payments", "data/postgres-primary")]["sources"] == ["config"]
    assert edges[("shop/payments", "data/postgres-primary")]["hosts"] == ["postgres-primary.data"]
    assert topology.nodes["ext:api.stripe.com"]["kind"] == "External"
    # Names inside the cluster that match no Service, such as redis and kafka here, aren't invented.
    assert not any("redis" in node or "kafka" in node for node in topology.nodes)
    assert topology._resolve_host(1, "shop", "redis.db") == [] and topology._resolve_host(1, "shop", "api.example.fr") == ["ext:api.example.fr"]
    assert topology.nodes[topology.node_for("data", "postgres-primary")]["services"] == ["postgres-primary"]


def test_observed_calls_add_rates_and_new_edges():
    topology = _map()
    total = {"result": [
        {"metric": {"client": "worker", "server": "payments"}, "value": [0, "4"]},
        {"metric": {"client": "checkout", "server": "payments"}, "value": [0, "10"]},
        {"metric": {"client": "payments", "server": "fraud-check"}, "value": [0, "2"]},
        {"metric": {"client": "user", "server": "frontend"}, "value": [0, "50"]},
        {"metric": {"client": "payments", "server": "api.stripe.com"}, "value": [0, "3"]},
    ]}
    failed = {"result": [{"metric": {"client": "checkout", "server": "payments"}, "value": [0, "2.5"]}]}
    calls = parse_observed("traces", total, failed, ("client",), ("server",))
    topology.add_observed("traces", calls)
    name = topology.name
    edges = {(name(edge["from"]), name(edge["to"])): edge for edge in topology.edges.values()}
    seen = edges[("shop/checkout", "shop/payments")]
    assert seen["sources"] == ["config", "traces"] and seen["rps"] == 10 and seen["error_ratio"] == 0.25
    assert edges[("shop/worker", "shop/payments")]["sources"] == ["traces"]
    assert topology.nodes["svc:fraud-check"]["kind"] == "Service", "a traced service with no workload still appears"
    assert "svc:api.stripe.com" not in topology.nodes and edges[("shop/payments", "api.stripe.com")]["sources"] == ["config", "traces"]
    assert not any(node.endswith(":user") or node == "svc:user" for node in topology.nodes), "the trace root isn't a service"
    # Istio names the namespace, so the same name in two namespaces is told apart.
    istio = parse_observed("istio", {"result": [{"metric": {"source_workload": "frontend", "source_workload_namespace": "shop",
                                                            "destination_workload": "postgres-primary", "destination_workload_namespace": "data"}, "value": [0, "1"]}]},
                           None, ("source_workload_namespace", "source_workload"), ("destination_workload_namespace", "destination_workload"))
    topology.add_observed("istio", istio)
    assert ("shop/frontend", "data/postgres-primary") in {(name(edge["from"]), name(edge["to"])) for edge in topology.edges.values()}


def _problem(namespace, workload, severity="critical", title=None):
    return {"kind": "k8s", "severity": severity, "title": title or f"Deployment {namespace}/{workload} is crash-looping",
            "detail": {"namespace": namespace, "workload": workload, "workload_kind": "Deployment"}}


def test_the_deepest_broken_dependency_is_the_likely_cause():
    topology = _map()
    current = {
        "k8s:checkout": _problem("shop", "checkout", "warning"),
        "k8s:payments": _problem("shop", "payments"),
        "k8s:postgres": _problem("data", "postgres-primary"),
        "alert:frontend": {"kind": "alert", "severity": "warning", "title": "Alert firing: HighLatency",
                           "detail": {"labels": {"namespace": "shop", "deployment": "frontend"}}},
        "rule:x": {"kind": "rule", "severity": "warning", "title": "Rule failing: x", "detail": {}},
    }
    links = link_problems(topology, current)
    assert "rule:x" not in links, "a problem that isn't about a workload stays off the map"
    for symptom in ("k8s:checkout", "k8s:payments", "alert:frontend"):
        assert links[symptom]["caused_by"]["fingerprint"] == "k8s:postgres", symptom
    assert links["alert:frontend"]["caused_by"]["path"] == ["shop/frontend", "shop/checkout", "shop/payments", "data/postgres-primary"]
    assert "caused_by" not in links["k8s:postgres"]
    assert sorted(links["k8s:postgres"]["affects"]) == ["shop/checkout", "shop/frontend", "shop/payments"]
    assert links["k8s:postgres"]["impact"] == ["shop/checkout", "shop/frontend", "shop/payments"]
    drawn = public_map(topology, current, links, "now")
    roles = {node["name"]: node["role"] for node in drawn["nodes"] if node["role"]}
    assert roles == {"postgres-primary": "cause", "payments": "symptom", "checkout": "symptom", "frontend": "symptom"}
    assert {node["name"]: node["status"] for node in drawn["nodes"]}["frontend"] == "warning"


def test_unrelated_problems_and_loops_are_not_linked():
    topology = _map()
    alone = link_problems(topology, {"k8s:checkout": _problem("shop", "checkout"), "k8s:worker": _problem("shop", "worker")})
    assert not any("caused_by" in link for link in alone.values()), "worker doesn't call checkout"
    # A loop of broken workloads has no clear cause.
    topology.add_observed("traces", [{"client": "postgres-primary", "server": "payments", "rps": 1, "errors": 0}])
    loop = link_problems(topology, {"k8s:payments": _problem("shop", "payments"), "k8s:postgres": _problem("data", "postgres-primary")})
    assert not any("caused_by" in link for link in loop.values())


# End to end, with a cluster -----------------------------------------------------------
import asyncio  # noqa: E402

import pytest_asyncio  # noqa: E402
import pytest  # noqa: E402
from fake_kube import TOKEN, FakeKube, deployment, pod, serve  # noqa: E402


def _outage():
    """payment calls postgres through its Service; postgres is down, so payment crash-loops too. frontend is fine."""
    state = FakeKube.state
    with state.lock:
        state.deployments = [deployment("shop", "payment", 1, 0), deployment("shop", "postgres", 1, 0), deployment("shop", "frontend")]
        state.pods = [
            pod("shop", "payment-5b5568b987-aaaaa", "payment", waiting="CrashLoopBackOff", last_reason="Error", exit_code=1, restarts=9, ready=False,
                env={"DATABASE_URL": "postgres://app:pw@postgres:5432/pay", "DB_PASSWORD": "x"}),
            pod("shop", "postgres-5b5568b987-bbbbb", "postgres", waiting="CrashLoopBackOff", last_reason="Error", exit_code=1, restarts=12, ready=False),
            pod("shop", "frontend-5b5568b987-ccccc", "frontend", env={"PAYMENT_URL": "http://payment.shop:8080", "STRIPE": "https://api.stripe.com"}),
        ]
        state.services = [
            {"metadata": {"namespace": "shop", "name": name}, "spec": {"type": "ClusterIP", "selector": {"app": name}, "ports": [{"port": 8080}]}}
            for name in ("payment", "postgres", "frontend")]
        state.events = []


@pytest.fixture
def kube():
    server, url = serve()
    yield url
    server.shutdown()


@pytest_asyncio.fixture
async def outage(console, kube):
    client = await console.company("MapCo", "o@map.example")
    org_id = (await client.get("/api/auth/me")).json()["org_id"]
    _outage()
    saved = await client.post("/api/integrations", json={"kind": "kubernetes", "name": "prod-eu", "config": {"url": kube}, "secrets": {"token": TOKEN}})
    assert saved.status_code == 201, saved.text
    engine = await console.ctx.engines.get(org_id)
    return client, org_id, engine


async def test_the_map_page_data_shows_the_cause_and_its_symptom(console, outage):
    client, org_id, engine = outage
    await engine.refresh_now()
    data = (await client.get("/api/dependencies")).json()
    nodes = {node["name"]: node for node in data["nodes"]}
    assert nodes["postgres"]["role"] == "cause" and nodes["payment"]["role"] == "symptom" and nodes["frontend"]["status"] == "ok"
    assert nodes["api.stripe.com"]["kind"] == "External" and data["observed"] is False and data["sources"] == ["config"]
    edges = {(edge["from"].split("/")[-1], edge["to"].split("/")[-1]): edge for edge in data["edges"]}
    assert set(edges) == {("frontend", "payment"), ("payment", "postgres"), ("frontend", "ext:api.stripe.com")}
    assert edges[("payment", "postgres")]["hosts"] == ["postgres"]
    assert "pw" not in str(data) and "app:" not in str(data), "no value from the environment reaches the page"
    payment = nodes["payment"]["problems"][0]
    assert payment["caused_by"]["path"] == ["shop/payment", "shop/postgres"] and "postgres" in payment["caused_by"]["title"]
    assert nodes["postgres"]["problems"][0]["affects"] == ["shop/payment"]
    # The Overview's problems carry the same lead, and the model is told it.
    context = (await client.get("/api/context")).json()
    by_title = {item["title"]: item for item in context["problems"]}
    payment_problem = next(item for title, item in by_title.items() if "payment" in title)
    assert payment_problem["detail"]["caused_by"]["fingerprint"].startswith("k8s:shop/deployment/postgres")
    assert payment_problem["detail"]["map_node"].endswith("shop/payment")
    from app.context.render import render_context
    text = render_context(engine.context)
    assert "Dependency map: likely a symptom of" in text and "This is a likely root cause" in text


async def test_the_model_can_read_the_map(console, outage):
    from app.investigation.tools import ToolRunner
    client, org_id, engine = outage
    await engine.refresh_now()
    runner = ToolRunner(engine, console.ctx.db, console.ctx.box, console.ctx.config)
    assert "get_dependencies" in {tool["name"] for tool in await runner.definitions()}
    ok, text = await runner.run("get_dependencies", {"name": "shop/payment"})
    assert ok and '"shop/postgres"' in text and "crash" in text.lower() and "configuration" in text
    ok, text = await runner.run("get_dependencies", {"name": "nope"})
    assert not ok and "Known:" in text


async def test_viewers_do_not_see_the_map(console, outage):
    from test_security import _invite
    client, org_id, engine = outage
    viewer = await _invite(console, client, "v@map.example", "viewer")
    assert (await viewer.get("/api/dependencies")).status_code == 403
    context = (await viewer.get("/api/context")).json()
    assert "caused_by" not in str(context) and "map_node" not in str(context)


async def _sent(console, org_id):
    while await console.ctx.db.all("SELECT id FROM events WHERE handled = 0 AND org_id = ?", (org_id,)):
        await asyncio.sleep(0.05)
    return [row["title"] for row in await console.ctx.db.all("SELECT title, status FROM deliveries WHERE org_id = ? AND status = 'sent' ORDER BY id", (org_id,))]


async def test_alerts_name_the_likely_cause_or_hold_the_symptom(console, outage, monkeypatch):
    from app import notifications
    client, org_id, engine = outage
    sent_texts = []

    async def capture(self, channel, message):
        sent_texts.append(message["text"])

    monkeypatch.setattr(notifications.Notifier, "_send", capture)
    assert (await client.post("/api/org/channels", json={"kind": "email", "name": "mail", "recipients": ["o@map.example"], "min_severity": "warning"})).status_code == 201
    # No grouping, so each problem has its own message.
    assert (await client.put("/api/org/noise", json={"group_window_s": 0})).status_code == 200
    await engine.refresh_now()
    titles = await _sent(console, org_id)
    assert len(titles) == 2
    payment = next(text for text in sent_texts if "payment is" in text.split("\n")[0] or "shop/payment " in text.split("\n")[0])
    assert "Likely caused by: Deployment shop/postgres" in payment and "shop/payment → shop/postgres" in payment
    postgres = next(text for text in sent_texts if text is not payment)
    assert "Also failing because of it: shop/payment." in postgres


async def test_holding_symptoms_sends_only_the_cause(console, outage, monkeypatch):
    client, org_id, engine = outage
    assert (await client.post("/api/org/channels", json={"kind": "email", "name": "mail", "recipients": ["o@map.example"], "min_severity": "warning"})).status_code == 201
    assert (await client.put("/api/org/noise", json={"group_window_s": 0, "hold_symptoms": True})).json()["hold_symptoms"] is True
    await engine.refresh_now()
    titles = await _sent(console, org_id)
    assert len(titles) == 1 and "postgres" in titles[0]
    held = await console.ctx.db.all("SELECT title FROM deliveries WHERE org_id = ? AND status = 'suppressed:symptom'", (org_id,))
    assert len(held) == 1 and "payment" in held[0]["title"]
    assert (await client.get("/api/org/noise")).json()["week"]["held_back"]["symptom"] == 1


async def test_a_grouped_message_leads_with_the_likely_cause(console, outage, monkeypatch):
    from app import notifications
    client, org_id, engine = outage
    texts = []

    async def capture(self, channel, message):
        texts.append(message)

    monkeypatch.setattr(notifications.Notifier, "_send", capture)
    assert (await client.post("/api/org/channels", json={"kind": "email", "name": "mail", "recipients": ["o@map.example"], "min_severity": "warning"})).status_code == 201
    assert (await client.put("/api/org/noise", json={"group_window_s": 1})).status_code == 200
    await engine.refresh_now()
    for _ in range(100):
        if texts:
            break
        await asyncio.sleep(0.05)
    assert len(texts) == 1
    message = texts[0]
    assert "postgres" in message["subject"].split(":", 1)[1].split(" and ")[0], message["subject"]
    assert "Likely cause: Deployment shop/postgres" in message["text"] and "(likely because of Deployment shop/postgres" in message["text"]


async def test_calls_from_a_newly_added_metric_source_are_read_on_the_next_poll(console, outage):
    client, org_id, engine = outage
    await engine.refresh_now()
    assert engine._observed_at > 0, "the first poll read observed calls, with no metric source yet"
    await engine.reload_sources()  # What adding or changing a source does.
    assert engine._observed_at == 0.0
