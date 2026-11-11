"""The console keeps watching and keeps its promises across failures and restarts."""
import asyncio
import sqlite3

from conftest import PG_URL, Console, FakeProm, _reset_postgres
from test_investigations import _company_with_source, _set_ollama

from app.db import Database, utcnow
from app.main import create_app


async def _wait_for(check, seconds=10.0):
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        result = await check()
        if result:
            return result
        await asyncio.sleep(0.1)
    raise AssertionError("timed out waiting")


async def _sent(db, org_id):
    return [row["title"] for row in await db.all("SELECT title FROM deliveries WHERE org_id = ? AND status = 'sent' ORDER BY id", (org_id,))]


async def _email_channel(client, address):
    response = await client.post("/api/org/channels", json={"kind": "email", "name": "mail", "recipients": [address], "min_severity": "warning"})
    assert response.status_code == 201, response.text


async def test_one_failed_poll_does_not_end_monitoring(console, prom_server):
    client, org_id, engine = await _company_with_source(console, prom_server)
    db = console.ctx.db
    real = db.all
    failed = []

    async def fails_once(sql, args=()):
        if "change_events" in sql and not failed:
            failed.append(sql)
            raise RuntimeError("the database restarted")
        return await real(sql, args)

    db.all = fails_once
    engine._wake.set()
    await _wait_for(lambda: asyncio.sleep(0, bool(failed)))
    await asyncio.sleep(0.2)
    db.all = real
    assert not engine._task.done(), "the polling loop must survive a failed poll"
    assert engine.poll_failures == 1 and engine.context["stale"] is True
    assert "try again" in engine.context["health"]["error"]
    version = engine.context["version"]
    FakeProm.state.firing = True
    engine._wake.set()
    await _wait_for(lambda: asyncio.sleep(0, engine.context["version"] > version))
    assert engine.context["alerts"]["firing"] and engine.context["health"]["error"] is None


async def test_an_alert_still_waiting_at_a_restart_is_sent_afterwards(make_config, prom_server):
    if PG_URL:
        _reset_postgres()
    app = create_app(make_config(alert_grace_s=30))
    async with app.router.lifespan_context(app):
        first = Console(app)
        client = await first.company("TestCo", "o@test.example")
        org_id = (await client.get("/api/auth/me")).json()["org_id"]
        await _email_channel(client, "o@test.example")
        source = await client.post("/api/sources", json={"name": "lab", "poll_seconds": 5, "config": {"host": "127.0.0.1", "port": prom_server.server_address[1]}, "secrets": {}})
        assert source.status_code == 201, source.text
        engine = await first.ctx.engines.get(org_id)
        await engine.refresh_now()
        FakeProm.state.firing = True
        await engine.refresh_now()  # The alert starts, and its message waits out the grace period.
        await asyncio.sleep(0.2)
        assert not await _sent(first.ctx.db, org_id)
    # The console restarts before the grace period ends.
    app = create_app(make_config())
    async with app.router.lifespan_context(app):
        second = Console(app)
        engine = await second.ctx.engines.get(org_id)
        await engine.refresh_now()
        titles = await _wait_for(lambda: _sent(second.ctx.db, org_id))
        assert len(titles) == 1 and "HighErrorRate" in titles[0]
    # After another restart it isn't sent again: it was already announced.
    app = create_app(make_config())
    async with app.router.lifespan_context(app):
        third = Console(app)
        engine = await third.ctx.engines.get(org_id)
        await engine.refresh_now()
        await asyncio.sleep(0.5)
        assert len(await _sent(third.ctx.db, org_id)) == 1


async def test_a_source_that_stops_answering_is_announced_after_the_grace_period(console, prom_server):
    client, org_id, engine = await _company_with_source(console, prom_server)
    await _email_channel(client, "o@test.example")
    assert (await client.put("/api/org/noise", json={"grace_critical_s": 1})).status_code == 200
    prom_server.shutdown()
    prom_server.server_close()
    await engine.refresh_now()
    titles = await _wait_for(lambda: _sent(console.ctx.db, org_id))
    assert any("lab stopped answering" in title for title in titles)
    held = await console.ctx.db.all("SELECT status FROM deliveries WHERE org_id = ? AND status = 'suppressed:blip'", (org_id,))
    assert not held, "a source that is still down is not a blip"


async def test_an_alertmanager_alert_is_sent_and_investigated_once_without_a_source(console, fake_models):
    client = await console.company("HookCo", "h@test.example")
    org_id = (await client.get("/api/auth/me")).json()["org_id"]
    await _email_channel(client, "h@test.example")
    await _set_ollama(client, fake_models)
    assert (await client.put("/api/org/auto-investigate", json={"enabled": True, "per_hour": 5})).status_code == 200
    # With a grace period, as in a real installation: Alertmanager already waited before it called.
    assert (await client.put("/api/org/noise", json={"grace_critical_s": 30})).status_code == 200
    path = (await client.post("/api/org/webhooks/rotate")).json()["alertmanager"].replace("http://localhost", "")
    response = await console.client().post(path, json={"alerts": [{"status": "firing", "labels": {"alertname": "DiskFull", "severity": "critical"}}]})
    assert response.status_code == 202 and response.json()["accepted"] == 1
    db = console.ctx.db

    async def finished():
        rows = await db.all("SELECT m.status FROM messages m JOIN conversations c ON c.id = m.conversation_id WHERE c.org_id = ? AND m.role = 'assistant'", (org_id,))
        return rows if rows and all(row["status"] != "running" for row in rows) else None

    answers = await _wait_for(finished, 20)
    assert [row["status"] for row in answers] == ["complete"]
    conversations = await db.all("SELECT trigger FROM conversations WHERE org_id = ?", (org_id,))
    assert conversations == [{"trigger": "auto"}], "one investigation, and it ran"
    titles = await _wait_for(lambda: _sent(db, org_id))
    assert any("DiskFull" in title for title in titles)


async def test_an_alertmanager_alert_starts_nothing_twice_when_a_source_is_connected(console, prom_server, fake_models):
    client, org_id, engine = await _company_with_source(console, prom_server)
    await _set_ollama(client, fake_models)
    assert (await client.put("/api/org/auto-investigate", json={"enabled": True, "per_hour": 5})).status_code == 200
    path = (await client.post("/api/org/webhooks/rotate")).json()["alertmanager"].replace("http://localhost", "")
    anon = console.client()
    response = await anon.post(path, json={"alerts": [{"status": "firing", "labels": {"alertname": "DiskFull", "severity": "critical"}}]})
    assert response.status_code == 202
    await asyncio.sleep(0.5)
    # The source's own poll is what announces and investigates, so the webhook leaves no empty investigation behind.
    assert await console.ctx.db.all("SELECT id FROM conversations WHERE org_id = ?", (org_id,)) == []
    for body in ([1, 2], {"alerts": "nope"}):
        assert (await anon.post(path, json=body)).status_code == 422
    assert (await anon.post(path, json={"alerts": ["text", {"status": "firing", "labels": "bad"}]})).json() == {"accepted": 1}
    changes = path.replace("/alertmanager/", "/changes/")
    assert (await anon.post(changes, json=["not", "an", "object"])).status_code == 422


async def test_an_investigation_cut_off_by_a_restart_is_marked_as_failed(make_config):
    if PG_URL:
        _reset_postgres()
    app = create_app(make_config())
    async with app.router.lifespan_context(app):
        first = Console(app)
        client = await first.company("TestCo", "o@test.example")
        conversation = (await client.post("/api/conversations", json={})).json()
        await first.ctx.db.insert(
            "INSERT INTO messages(conversation_id, role, content, status, provider, model, created_at) VALUES (?, 'assistant', '', 'running', 'ollama', 'm', ?)",
            (conversation["id"], utcnow()))
    app = create_app(make_config())
    async with app.router.lifespan_context(app):
        rows = await Console(app).ctx.db.all("SELECT status, error FROM messages WHERE conversation_id = ?", (conversation["id"],))
        assert rows[0]["status"] == "failed" and "restarted" in rows[0]["error"]


def test_upgrading_from_version_8_keeps_old_events_as_handled(tmp_path):
    from app import schema
    path = tmp_path / "console.db"
    conn = sqlite3.connect(path)
    conn.executescript(schema.sqlite_statements().replace(schema.V9_COLUMNS, ""))
    conn.execute("INSERT INTO events(org_id, ts, kind, severity, fingerprint, title) VALUES (1, 'now', 'alert_started', 'critical', 'alert:x', 'Old alert')")
    conn.execute("PRAGMA user_version=8")
    conn.commit()
    conn.close()
    db = Database(path, "")
    assert db._all("SELECT title, handled FROM events") == [{"title": "Old alert", "handled": 1}]
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == schema.SCHEMA_VERSION
    db.close()
