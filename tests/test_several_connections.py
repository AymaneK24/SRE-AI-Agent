"""Several log stores and clusters side by side, and the main connection of each kind."""
import pytest_asyncio
from fake_kube import TOKEN, FakeKube, serve

from app.context.render import render_context
from app.investigation.tools import ToolRunner


@pytest_asyncio.fixture
async def two_clusters(console):
    server, url = serve()
    client = await console.company("TwoCo", "o@two.example")
    org_id = (await client.get("/api/auth/me")).json()["org_id"]
    FakeKube.state.crash()
    ids = []
    for name in ("prod-eu", "prod-us"):
        saved = await client.post("/api/integrations", json={"kind": "kubernetes", "name": name, "config": {"url": url}, "secrets": {"token": TOKEN}})
        assert saved.status_code == 201, saved.text
        ids.append(saved.json()["id"])
    engine = await console.ctx.engines.get(org_id)
    await engine.refresh_now()
    yield client, engine, ids
    server.shutdown()


async def test_a_second_cluster_is_added_not_swapped(two_clusters):
    client, engine, ids = two_clusters
    rows = (await client.get("/api/integrations")).json()
    assert [row["name"] for row in rows] == ["prod-eu", "prod-us"]
    clusters = [item for item in engine.context["connections"] if item["kind"] == "kubernetes"]
    assert [item["name"] for item in clusters] == ["prod-eu", "prod-us"]
    assert [item["main"] for item in clusters] == [True, False], "the oldest is main until someone chooses"
    assert engine.context["integrations"]["kubernetes"]["name"] == "prod-eu"


async def test_problems_from_each_cluster_stay_apart(two_clusters):
    _, engine, ids = two_clusters
    fingerprints = [item["fingerprint"] for item in engine.k8s_problems]
    assert len(fingerprints) == len(set(fingerprints)), "the same workload in two clusters is two problems"
    assert {item["cluster"] for item in engine.k8s_problems} == {"prod-eu", "prod-us"}
    assert all(item["title"].endswith(f"({item['cluster']})") for item in engine.k8s_problems)
    text = render_context(engine.context)
    assert f"connection_id {ids[1]}" in text and 'KUBERNETES DETAIL "prod-us"' in text


async def test_main_connection_can_be_changed(two_clusters):
    client, engine, ids = two_clusters
    changed = await client.put("/api/connections/main", json={"kind": "kubernetes", "id": ids[1]})
    assert changed.status_code == 200, changed.text
    await engine.refresh_now()
    clusters = [item for item in engine.context["connections"] if item["kind"] == "kubernetes"]
    assert [item["main"] for item in clusters] == [False, True]
    assert engine.context["main"]["kubernetes"] == ids[1]
    missing = await client.put("/api/connections/main", json={"kind": "kubernetes", "id": 99999})
    assert missing.status_code == 404


async def test_tools_can_name_a_cluster(console, two_clusters):
    _, engine, ids = two_clusters
    runner = ToolRunner(engine, console.ctx.db, console.ctx.box, console.ctx.config)
    ok, text = await runner.run("get_kubernetes_state", {"resource": "problems", "connection_id": ids[1]})
    assert ok and "prod-us" in text and "prod-eu" not in text
    ok, text = await runner.run("get_kubernetes_state", {"resource": "pods", "connection_id": 424242})
    assert ok and "no kubernetes connection 424242" in text and "prod-eu" in text, "a made-up id uses the main cluster and says so"


async def test_saving_by_name_updates_and_renaming_onto_another_is_refused(two_clusters):
    client, _, ids = two_clusters
    rows = (await client.get("/api/integrations")).json()
    first = rows[0]
    again = await client.post("/api/integrations", json={"kind": "kubernetes", "name": "prod-eu", "config": {"url": first["config"]["url"], "namespace": "shop"}, "secrets": {}})
    assert again.status_code == 201 and again.json()["id"] == ids[0] and again.json()["has_secret"]
    clash = await client.post("/api/integrations", json={"kind": "kubernetes", "id": ids[0], "name": "prod-us", "config": {"url": first["config"]["url"]}, "secrets": {}})
    assert clash.status_code == 409
    assert len((await client.get("/api/integrations")).json()) == 2


# One problem, one notification ------------------------------------------------------------
async def test_a_prometheus_alert_about_a_known_workload_joins_it(console, prom_server):
    from conftest import FakeProm

    server, url = serve()
    try:
        client = await console.company("DedupCo", "o@dedup.example")
        org_id = (await client.get("/api/auth/me")).json()["org_id"]
        FakeProm.state = FakeProm.state.__class__()
        FakeKube.state.crash()
        assert (await client.post("/api/integrations", json={"kind": "kubernetes", "name": "aks", "config": {"url": url}, "secrets": {"token": TOKEN}})).status_code == 201
        assert (await client.post("/api/sources", json={"name": "prom", "poll_seconds": 5, "config": {"host": "127.0.0.1", "port": prom_server.server_address[1]}, "secrets": {}})).status_code == 201
        engine = await console.ctx.engines.get(org_id)
        await engine.refresh_now()
        sent = []
        engine.on_event = lambda org, event, detail: sent.append(event) or _done()
        crash = next(item for item in engine.k8s_problems if item["problem"] == "crashloop")
        pod = crash["pods"][0]
        FakeProm.state.extra_alerts = [{"labels": {"alertname": "KubePodCrashLooping", "severity": "warning", "namespace": crash["namespace"], "pod": pod},
                                        "annotations": {"summary": "crash looping"}, "state": "firing", "activeAt": "2026-01-01T00:00:00Z", "value": "1"}]
        await engine.refresh_now()
        context = engine.context
        assert not [item for item in context["problems"] if item["kind"] == "alert"], "the alert isn't a second problem"
        problem = next(item for item in context["problems"] if item["fingerprint"] == crash["fingerprint"])
        assert problem["detail"]["also_firing"] == ["KubePodCrashLooping"]
        assert context["alerts"]["firing"][0]["merged_into"] == crash["fingerprint"]
        assert "alert" not in context["headline"]["text"], "it isn't counted twice in the headline"
        assert sent == [], "and nobody is notified a second time"
        FakeProm.state.extra_alerts = []
        await engine.refresh_now()
        assert sent == [], "its end isn't announced either: the problem it joined is still there"
    finally:
        server.shutdown()


async def _done():
    return None
