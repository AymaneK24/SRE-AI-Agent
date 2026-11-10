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
