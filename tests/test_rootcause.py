"""Root cause ranking: which problems share a cause, which one is the root, and why. Time alone never links."""
from app import rootcause
from app.context.render import focus_context, render_context
from app.dependencies import Links, env_hosts


def _pod(namespace, workload, name, node="node-1", labels=None, env=None):
    return {"namespace": namespace, "workload": workload, "workload_kind": "Deployment", "name": name, "node": node,
            "labels": labels or {"app": workload}, "env_hosts": env_hosts([{"env": env or []}])}


def _cluster():
    pods = [
        _pod("payments", "postgres-primary", "postgres-primary-0", labels={"app.kubernetes.io/name": "postgres-primary"}),
        _pod("payments", "payments-api", "payments-api-7d9f6c5b8d-x2k4q", env=[{"name": "DATABASE_URL", "value": "postgres://app:s3cret@postgres-primary:5432/app"}]),
        _pod("shop", "checkout-web", "checkout-web-5c8f7d6b9-p9m2t", node="node-2", env=[{"name": "PAYMENTS_URL", "value": "http://payments-api.payments:8080"}]),
        _pod("search", "search-indexer", "search-indexer-6b5d8-zz9kq", node="node-3"),
    ]
    services = [
        {"namespace": "payments", "name": "postgres-primary", "selector": {"app": "postgres-primary"}},  # the label it used to have
        {"namespace": "payments", "name": "payments-api", "selector": {"app": "payments-api"}},
        {"namespace": "shop", "name": "checkout-web", "selector": {"app": "checkout-web"}},
    ]
    return {"pods": pods, "deployments": [], "services": services}


def _k8s(namespace, workload, problem, since, severity="critical", logs=None, kind="Deployment"):
    detail = {"namespace": namespace, "workload": workload, "workload_kind": kind, "problem": problem}
    if logs:
        detail["logs"] = {"lines": logs}
    return {"fingerprint": f"k8s:{namespace}/{kind.lower()}/{workload}:{problem}", "kind": "k8s", "severity": severity,
            "title": f"{kind} {namespace}/{workload} {problem}", "detail": detail, "since": since}


def _alert(name, labels, since, severity="critical"):
    return {"fingerprint": f"alert:{name}", "kind": "alert", "severity": severity, "title": f"Alert firing: {name}",
            "detail": {"labels": labels, "activeAt": since}, "since": since}


DB = _k8s("payments", "postgres-primary", "unavailable", "2026-11-12T22:14:30Z")
API = _k8s("payments", "payments-api", "crash_loop", "2026-11-12T22:16:05Z",
           logs=["dial tcp 10.0.41.17:5432: connect: connection refused", "FATAL: cannot reach postgres-primary:5432"])
WEB = _alert("HighErrorRate", {"job": "checkout-web", "namespace": "shop"}, "2026-11-12T22:16:40Z")


def test_the_database_is_the_root_and_the_others_are_its_symptoms():
    groups = rootcause.analyze([WEB, API, DB], [_cluster()])
    assert len(groups) == 1
    group = groups[0]
    assert group["root"] == DB["fingerprint"] and group["verdict"] == "likely"
    roles = {member["fingerprint"]: member["role"] for member in group["members"]}
    assert roles == {DB["fingerprint"]: "root", API["fingerprint"]: "symptom", WEB["fingerprint"]: "symptom"}
    api = next(member for member in group["members"] if member["fingerprint"] == API["fingerprint"])
    assert any("configured to call payments/postgres-primary" in reason for reason in api["reasons"])
    assert any("mentions postgres-primary:5432" in reason for reason in api["reasons"])
    assert "Likely one cause: payments/postgres-primary" in group["summary"]
    # The secret in DATABASE_URL never reaches the reasons.
    assert "s3cret" not in str(groups)


def test_problems_that_only_happen_at_the_same_time_are_not_linked():
    backup = {"fingerprint": "signal:1:disk:node-9", "kind": "signal", "severity": "warning", "title": "Disk use is over its limit: node-9",
              "detail": {"labels": {"instance": "10.0.9.9:9100"}, "value": 91}, "since": "2026-11-12T22:15:00Z"}
    indexer = _k8s("search", "search-indexer", "crash_loop", "2026-11-12T22:15:30Z", logs=["out of memory"])
    assert rootcause.analyze([DB, backup, indexer], [_cluster()]) == []


def test_an_alert_and_a_cluster_problem_about_the_same_workload_are_one_problem():
    crash_alert = _alert("KubePodCrashLooping", {"namespace": "payments", "pod": "payments-api-7d9f6c5b8d-x2k4q"}, "2026-11-12T22:16:00Z")
    groups = rootcause.analyze([crash_alert, API], [_cluster()])
    assert len(groups) == 1 and groups[0]["verdict"] == "same"
    assert "same problem on payments/payments-api" in groups[0]["summary"]


def test_a_failing_node_is_the_root_of_the_workloads_running_on_it():
    node = {"fingerprint": "k8s:node/node-2:not_ready", "kind": "k8s", "severity": "critical", "title": "Node node-2 is NotReady",
            "detail": {"namespace": "", "workload": "node-2", "workload_kind": "Node"}, "since": "2026-11-12T22:10:00Z"}
    web = _k8s("shop", "checkout-web", "unavailable", "2026-11-12T22:11:00Z")
    groups = rootcause.analyze([web, node], [_cluster()])
    assert groups[0]["root"] == node["fingerprint"]
    assert any("runs on node node-2" in reason for member in groups[0]["members"] for reason in member["reasons"])


def test_a_change_just_before_the_root_started_is_named():
    change = {"ts": "2026-11-12T22:13:10Z", "origin": "Helm", "service": "", "title": "Helm upgrade postgres-primary 3.8.0"}
    elsewhere = {"ts": "2026-11-12T22:12:00Z", "origin": "GitHub", "service": "search", "title": "Deployment of search v9"}
    group = rootcause.analyze([WEB, API, DB], [_cluster()], [elsewhere, change])[0]
    assert group["change"]["title"] == "Helm upgrade postgres-primary 3.8.0"
    assert "A change came 1 min before it started: Helm upgrade postgres-primary 3.8.0." in group["summary"]


def test_something_that_started_well_before_is_not_called_the_symptom():
    early_api = {**API, "since": "2026-11-12T21:30:00Z"}
    groups = rootcause.analyze([early_api, DB], [_cluster()])
    # The API was failing 45 minutes before the database: the database can't explain it.
    assert all(group["root"] != DB["fingerprint"] or group["verdict"] != "likely" for group in groups)


def test_onset_uses_when_the_condition_began_not_when_it_was_noticed():
    problem = _alert("HighLatency", {}, "2026-11-12T22:20:00Z")
    problem["detail"]["activeAt"] = "2026-11-12T22:15:00Z"
    assert rootcause.onset(problem).isoformat().startswith("2026-11-12T22:15")


def test_configured_hosts_skip_secrets_and_keep_only_names():
    hosts = env_hosts([{"env": [
        {"name": "DATABASE_URL", "value": "postgres://user:pa55@db.payments.svc.cluster.local:5432/app"},
        {"name": "API_KEY", "value": "redis:6379"},
        {"name": "REDIS_HOST", "value": "cache"},
        {"name": "GREETING", "value": "hello"},
    ]}])
    assert hosts == ["db.payments.svc.cluster.local", "cache"]
    links = Links([{"pods": [_pod("payments", "db", "db-0")], "services": [{"namespace": "payments", "name": "db", "selector": {"app": "db"}}]}])
    assert links.resolve("db.payments.svc.cluster.local", "shop") == {("payments", "db")}


def test_the_model_reads_groups_as_hypotheses_and_a_focused_symptom_keeps_its_root():
    groups = rootcause.analyze([WEB, API, DB], [_cluster()])
    context = {"version": 3, "as_of": None, "health": {"status": "ok"}, "problems": [DB, API, WEB], "groups": groups, "sources": []}
    text = render_context(context)
    assert "PROBLEMS THAT LOOK CONNECTED" in text and "hypothesis" in text and "root: Deployment payments/postgres-primary" in text
    focused = focus_context(context, [API["fingerprint"]])
    assert [member["fingerprint"] for member in focused["groups"][0]["members"]] == [DB["fingerprint"], API["fingerprint"]]
    assert "[not in the person's selection]" in render_context(focused)


# Messages ---------------------------------------------------------------------
def _items(*problems):
    return [{"fingerprint": p["fingerprint"], "title": p["title"], "severity": p["severity"]} for p in problems]


def test_a_message_leads_with_the_likely_cause_and_lists_every_problem():
    groups = rootcause.analyze([WEB, API, DB], [_cluster()])
    other = {"fingerprint": "alert:DiskFull", "title": "Alert firing: DiskFull", "severity": "warning"}
    lines, covering = rootcause.message_lines(_items(DB, API, WEB) + [other], groups)
    text = "\n".join(lines)
    assert lines[0] == f"Likely cause: {DB['title']}"
    assert API["title"] in text and WEB["title"] in text and "configured to call" in text
    assert "Also started, not linked to the above:\n- Alert firing: DiskFull" in text
    assert covering is None  # DiskFull isn't explained, so the headline doesn't claim one cause for all.
    lines, covering = rootcause.message_lines(_items(DB, API, WEB), groups)
    assert covering["root"] == DB["fingerprint"]


def test_a_grouped_message_never_cuts_a_critical_problem():
    items = [{"fingerprint": f"w{i}", "title": f"Warning {i}", "severity": "warning"} for i in range(20)]
    items += [{"fingerprint": f"c{i}", "title": f"Critical {i}", "severity": "critical"} for i in range(3)]
    lines, _ = rootcause.message_lines(items, [])
    text = "\n".join(lines)
    assert all(f"Critical {i}" in text for i in range(3))
    assert "- and 11 more" in text


def test_a_single_alert_names_its_likely_cause():
    groups = rootcause.analyze([WEB, API, DB], [_cluster()])
    assert rootcause.cause_line(API["fingerprint"], groups).startswith(f"Likely cause: {DB['title']} (")
    assert rootcause.cause_line(DB["fingerprint"], groups) == ""


async def test_the_alert_email_is_organized_by_cause(console):
    from app import noise

    class _Engine:
        def __init__(self, groups):
            self._active, self.context = {}, {"groups": groups}

        def still_active(self, fingerprint):
            return True

    class _Engines:
        def __init__(self, engine):
            self.engine = engine

        async def get(self, org_id):
            return self.engine

    client = await console.company("CauseCo", "c@test.example")
    org_id = (await client.get("/api/auth/me")).json()["org_id"]
    assert (await client.post("/api/org/channels", json={"kind": "email", "name": "mail", "recipients": ["c@test.example"], "min_severity": "warning"})).status_code == 201
    assert (await client.put("/api/org/noise", json={"grace_critical_s": 0, "grace_warning_s": 0, "group_window_s": 1, "flapping": False})).status_code == 200
    notifier = console.ctx.notifier
    notifier.noise = noise.NoiseControl()
    notifier.engines = _Engines(_Engine(rootcause.analyze([WEB, API, DB], [_cluster()])))
    import asyncio
    from app.db import utcnow
    events = [{"kind": f"{p['kind']}_started", "severity": p["severity"], "title": p["title"], "fingerprint": p["fingerprint"], "ts": utcnow()} for p in (DB, API, WEB)]
    await asyncio.gather(*(notifier.handle_event(org_id, event, {}) for event in events))
    mails = [m for m in console.outbox if m["To"] == "c@test.example" and "CRITICAL" in m["Subject"]]
    assert len(mails) == 1
    assert "payments/postgres-primary is the likely cause of 2 other problems" in mails[0]["Subject"]
    body = "\n".join(part.get_content() for part in mails[0].walk() if part.get_content_type() == "text/plain")
    assert f"Likely cause: {DB['title']}" in body and API["title"] in body and WEB["title"] in body
