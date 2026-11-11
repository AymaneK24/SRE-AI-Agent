"""The console keeps watching and keeps its promises across failures and restarts."""
import asyncio
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import PG_URL, Console, FakeProm, _reset_postgres
from test_investigations import _company_with_source, _set_ollama

from app.db import Database, utcnow
from app.main import create_app
from app.notifications import RETRY_AFTER_S


class FlakyHook(BaseHTTPRequestHandler):
    """A chat webhook that can be told to refuse messages."""
    received: list = []
    status = 200

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if FlakyHook.status == 200:
            FlakyHook.received.append(json.loads(body))
        self.send_response(FlakyHook.status)
        self.end_headers()


@pytest.fixture
def flaky_hook():
    FlakyHook.received, FlakyHook.status = [], 200
    server = ThreadingHTTPServer(("127.0.0.1", 0), FlakyHook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/hook"
    server.shutdown()


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
    conn.executescript(schema.sqlite_statements().replace(schema.V9_COLUMNS, "").replace(schema.V10_TABLES.format(**schema.SQLITE_TYPES), ""))
    conn.execute("INSERT INTO events(org_id, ts, kind, severity, fingerprint, title) VALUES (1, 'now', 'alert_started', 'critical', 'alert:x', 'Old alert')")
    conn.execute("PRAGMA user_version=8")
    conn.commit()
    conn.close()
    db = Database(path, "")
    assert db._all("SELECT title, handled FROM events") == [{"title": "Old alert", "handled": 1}]
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == schema.SCHEMA_VERSION
    db.close()


# Durable alerts -------------------------------------------------------------
async def test_a_recovery_reaches_the_channel_that_heard_the_problem_even_after_a_restart(make_config, prom_server, flaky_hook):
    if PG_URL:
        _reset_postgres()
    app = create_app(make_config())
    async with app.router.lifespan_context(app):
        first = Console(app)
        client = await first.company("TestCo", "o@test.example")
        org_id = (await client.get("/api/auth/me")).json()["org_id"]
        assert (await client.post("/api/org/channels", json={"kind": "slack", "name": "ops", "url": flaky_hook, "min_severity": "warning"})).status_code == 201
        await client.post("/api/sources", json={"name": "lab", "poll_seconds": 5, "config": {"host": "127.0.0.1", "port": prom_server.server_address[1]}, "secrets": {}})
        engine = await first.ctx.engines.get(org_id)
        await engine.refresh_now()
        FakeProm.state.firing = True
        await engine.refresh_now()
        await _wait_for(lambda: asyncio.sleep(0, len(FlakyHook.received) == 1))
        await _wait_for(lambda: _all_handled(first.ctx.db))
    app = create_app(make_config())
    async with app.router.lifespan_context(app):
        second = Console(app)
        engine = await second.ctx.engines.get(org_id)
        await engine.refresh_now()
        FakeProm.state.firing = False
        await engine.refresh_now()
        await _wait_for(lambda: asyncio.sleep(0, len(FlakyHook.received) == 2))
        assert "RESOLVED" in FlakyHook.received[1]["text"] and "HighErrorRate" in FlakyHook.received[1]["text"]
        assert await second.ctx.db.all("SELECT fingerprint FROM alert_sent WHERE org_id = ?", (org_id,)) == []


async def _all_handled(db):
    return not await db.all("SELECT id FROM events WHERE handled = 0")


async def _slack_with_firing_alert(console, prom_server, url):
    client, org_id, engine = await _company_with_source(console, prom_server)
    made = await client.post("/api/org/channels", json={"kind": "slack", "name": "ops", "url": url, "min_severity": "warning"})
    assert made.status_code == 201, made.text
    FakeProm.state.firing = True
    await engine.refresh_now()
    return client, org_id, made.json()["id"]


async def test_a_message_the_channel_refused_is_delivered_later_and_says_it_is_late(console, prom_server, flaky_hook):
    FlakyHook.status = 503
    client, org_id, channel_id = await _slack_with_firing_alert(console, prom_server, flaky_hook)
    db, notifier = console.ctx.db, console.ctx.notifier

    async def queued():
        return await db.all("SELECT * FROM delivery_queue WHERE org_id = ?", (org_id,))

    row = (await _wait_for(queued, 15))[0]
    assert row["attempts"] == 0 and row["next_try_at"] > utcnow()
    assert [item["status"] for item in await db.all("SELECT status FROM deliveries WHERE org_id = ?", (org_id,))] == ["retrying"]
    assert await notifier.retry_due() == 0 and not FlakyHook.received, "not before its wait is over"
    # Still refused when its turn comes: it waits longer.
    await db.execute("UPDATE delivery_queue SET next_try_at = ?", ("2000-01-01T00:00:00.000Z",))
    assert await notifier.retry_due() == 0
    row = (await queued())[0]
    assert row["attempts"] == 1 and row["next_try_at"] > utcnow()
    # The service is back.
    FlakyHook.status = 200
    await db.execute("UPDATE delivery_queue SET next_try_at = ?", ("2000-01-01T00:00:00.000Z",))
    assert await notifier.retry_due() == 1
    text = FlakyHook.received[0]["text"]
    assert text.startswith("Delayed message.") and "HighErrorRate" in text
    assert await queued() == []
    delivery = (await db.all("SELECT status, error FROM deliveries WHERE org_id = ?", (org_id,)))[0]
    assert delivery["status"] == "sent" and "late" in delivery["error"]
    week = (await client.get("/api/org/noise")).json()["week"]
    assert week["messages_sent"] == 1 and week["retrying"] == 0


async def test_a_message_is_given_up_after_its_last_retry_or_when_its_channel_is_removed(console, prom_server, flaky_hook):
    FlakyHook.status = 503
    client, org_id, channel_id = await _slack_with_firing_alert(console, prom_server, flaky_hook)
    db, notifier = console.ctx.db, console.ctx.notifier

    async def queued():
        return await db.all("SELECT * FROM delivery_queue WHERE org_id = ?", (org_id,))

    await _wait_for(queued, 15)
    await db.execute("UPDATE delivery_queue SET next_try_at = ?, attempts = ?", ("2000-01-01T00:00:00.000Z", len(RETRY_AFTER_S) - 1))
    await notifier.retry_due()
    assert await queued() == []
    delivery = (await db.all("SELECT status, error FROM deliveries WHERE org_id = ?", (org_id,)))[0]
    assert delivery["status"] == "failed" and "Given up" in delivery["error"]
    # A test message is never queued: the person pressing the button sees the result at once.
    result = (await client.post(f"/api/org/channels/{channel_id}/test")).json()
    assert result["status"] == "failed" and await queued() == []
    # A queued message whose channel is deleted is dropped.
    await db.insert("INSERT INTO delivery_queue(org_id, channel_id, delivery_id, fingerprint, message, attempts, first_tried_at, next_try_at) VALUES (?, 12345, 1, 'x', '{}', 0, ?, ?)",
                    (org_id, utcnow(), "2000-01-01T00:00:00.000Z"))
    await notifier.retry_due()
    assert await queued() == []


async def test_an_address_the_service_refuses_for_good_is_not_retried(console, prom_server, flaky_hook):
    FlakyHook.status = 404
    client, org_id, channel_id = await _slack_with_firing_alert(console, prom_server, flaky_hook)
    db = console.ctx.db

    async def failed():
        return await db.all("SELECT error FROM deliveries WHERE org_id = ? AND status = 'failed'", (org_id,))

    rows = await _wait_for(failed)
    assert "404" in rows[0]["error"] and await db.all("SELECT id FROM delivery_queue") == []


def test_a_queued_message_keeps_its_charts_and_report():
    from app.notifications import _delayed, _pack, _unpack
    message = {"subject": "[CRITICAL] x", "text": "t", "body_html": "<p>b</p>", "images": {"chart1": b"\x89PNG"}, "attachments": [{"filename": "r.pdf", "data": b"%PDF", "mime": "application/pdf"}]}
    assert _unpack(_pack(message)) == message
    late = _delayed(message, "2026-11-11T10:02:00.000Z")
    assert late["subject"] == "[DELAYED] [CRITICAL] x" and "10:02 UTC" in late["text"] and "10:02 UTC" in late["body_html"]
    assert message["subject"] == "[CRITICAL] x", "the queued copy isn't changed"


# The console watches itself ---------------------------------------------------
async def test_health_says_when_polling_is_stuck_and_a_loop_that_ended_is_started_again(console, prom_server):
    client, org_id, engine = await _company_with_source(console, prom_server)
    anon = console.client()
    assert (await anon.get("/healthz")).json() == {"status": "ok"}
    # Sources that don't answer are the customer's outage, not the console's: it stays healthy.
    prom_server.shutdown()
    prom_server.server_close()
    await engine.refresh_now()
    assert engine.context["health"]["status"] == "unreachable" and (await anon.get("/healthz")).status_code == 200
    # A loop that hasn't come round long after it was due is stuck.
    engine._loop_beat -= 3600
    response = await anon.get("/healthz")
    assert response.status_code == 503 and response.json() == {"status": "stalled"}
    engine._loop_beat += 3600
    # A loop that ended is started again by the watchdog.
    engine._task.cancel()
    await asyncio.gather(engine._task, return_exceptions=True)
    assert engine.loop_ended and console.ctx.engines.revive() == 1
    assert not engine._task.done() and not engine.loop_ended
    assert console.ctx.engines.revive() == 0
    assert (await anon.get("/healthz")).status_code == 200


# Staying responsive ---------------------------------------------------------
async def test_checking_a_password_does_not_pause_everything_else():
    from app.auth import hash_password_async, verify_password_async
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    task = asyncio.create_task(ticker())
    stored = await hash_password_async("a-Strong-Passphrase-2026")
    before = ticks
    assert await verify_password_async(stored, "a-Strong-Passphrase-2026") is True
    assert await verify_password_async(stored, "wrong") is False and await verify_password_async("", "anything") is False
    task.cancel()
    assert ticks - before >= 3, "other work kept running while the password was checked"


def test_the_rate_limiter_forgets_keys_with_no_recent_attempts(monkeypatch):
    from app import auth
    now = [1000.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: now[0])
    limiter = auth.RateLimiter()
    assert limiter.count("never-seen", 60) == 0 and not limiter._hits, "asking about a key doesn't store it"
    assert limiter.allow("a", 2, 60) and limiter.allow("a", 2, 60) and not limiter.allow("a", 2, 60)
    now[0] += 61
    assert limiter.count("a", 60) == 0 and "a" not in limiter._hits
    assert limiter.allow("a", 2, 60)
    # Addresses that tried once and never came back are dropped after a day.
    for index in range(limiter.SWEEP_EVERY):
        limiter.record(f"stranger-{index}")
    now[0] += limiter.KEEP_S + 1
    for _ in range(limiter.SWEEP_EVERY):
        limiter.allow("busy", 10**6, 60)
    assert set(limiter._hits) == {"busy"}


async def test_clusters_are_read_at_the_same_time_and_one_failing_does_not_hide_the_others(console, monkeypatch):
    from app.context import integrations as engine_module
    from app.sources.base import SourceError
    client = await console.company("ManyCo", "m@test.example")
    org_id = (await client.get("/api/auth/me")).json()["org_id"]
    for index in range(3):
        await console.ctx.db.insert("INSERT INTO integrations(org_id, kind, name, config, secret_enc, enabled, created_at, updated_at) VALUES (?, 'kubernetes', ?, '{}', '', 1, ?, ?)",
                                    (org_id, f"cluster-{index}", utcnow(), utcnow()))

    class Slow:
        def __init__(self, name):
            self.name = name

        async def snapshot(self):
            await asyncio.sleep(0.4)
            if self.name == "cluster-1":
                raise SourceError("This cluster didn't answer.", "timeout")
            return {"pods": [], "deployments": [], "nodes": [], "events": [], "forbidden": []}

        async def close(self):
            pass

    monkeypatch.setattr(engine_module, "build_integration", lambda row, box, config: Slow(row["name"]))
    engine = await console.ctx.engines.get(org_id)
    await engine.integrations_changed()
    started = asyncio.get_running_loop().time()
    await engine.refresh_now()
    took = asyncio.get_running_loop().time() - started
    assert took < 1.0, f"three clusters of 0.4 s each were read one after another ({took:.1f} s)"
    states = {item["name"]: item["status"] for item in engine.context["connections"]}
    assert states == {"cluster-0": "ok", "cluster-1": "error", "cluster-2": "ok"}
    assert sorted(engine.k8s_snapshots) == sorted(row["id"] for row in await console.ctx.db.all("SELECT id FROM integrations WHERE org_id = ? AND name <> 'cluster-1'", (org_id,)))


async def test_a_record_removed_during_a_request_gives_a_clear_answer_not_a_server_error(console):
    from app.db import RowMissing
    client = await console.company("GoneCo", "g@test.example")
    try:
        await console.ctx.db.get("SELECT id FROM users WHERE id = ?", (987654,))
        raise AssertionError("a missing row must be reported")
    except RowMissing:
        pass
    assert (await console.ctx.db.get("SELECT count(*) AS n FROM users WHERE id = ?", (987654,)))["n"] == 0
    # The account is deleted while its session is still open: the next request says so plainly.
    me = (await client.get("/api/auth/me")).json()
    real = console.ctx.db.get

    async def gone(sql, args=()):
        if "FROM organizations WHERE id" in sql:
            raise RowMissing("gone")
        return await real(sql, args)

    console.ctx.db.get = gone
    response = await client.get("/api/org")
    console.ctx.db.get = real
    assert me["org_id"] and response.status_code == 404 and "no longer exists" in response.json()["detail"]


# Heartbeat ------------------------------------------------------------------
class Beats(BaseHTTPRequestHandler):
    """The outside service that expects a call every few minutes."""
    calls: list = []
    status = 200

    def log_message(self, *args):
        pass

    def do_GET(self):
        Beats.calls.append(self.path)
        self.send_response(Beats.status)
        self.end_headers()


@pytest.fixture
def beat_server():
    Beats.calls, Beats.status = [], 200
    server = ThreadingHTTPServer(("127.0.0.1", 0), Beats)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/ping/secret-check-id"
    server.shutdown()


async def test_the_heartbeat_is_sent_while_watching_and_held_back_when_checks_fail(console, prom_server, beat_server):
    client, org_id, engine = await _company_with_source(console, prom_server)
    heartbeat = console.ctx.heartbeat
    assert (await client.get("/api/org/heartbeat")).json() == {"enabled": False, "every_minutes": 5, "url_set": False, "url_host": "", "last_status": "never",
                                                                "last_at": None, "last_ok_at": None, "last_error": ""}
    assert (await client.put("/api/org/heartbeat", json={"enabled": True})).status_code == 422, "on needs an address"
    assert (await client.put("/api/org/heartbeat", json={"enabled": True, "url": "ftp://nope"})).status_code == 422
    saved = await client.put("/api/org/heartbeat", json={"enabled": True, "url": beat_server, "every_minutes": 2})
    assert saved.status_code == 200 and saved.json()["url_set"] is True and saved.json()["url_host"] == "127.0.0.1"
    assert "secret-check-id" not in saved.text, "the address is a secret and is never sent back"
    stored = await console.ctx.db.get("SELECT value FROM settings WHERE org_id = ? AND key = 'heartbeat'", (org_id,))
    assert "secret-check-id" not in stored["value"], "and it is stored encrypted"
    # Watching: one heartbeat now, and no second one before its turn.
    assert await heartbeat.run_due() == 1 and Beats.calls == ["/ping/secret-check-id"]
    assert await heartbeat.run_due() == 0 and len(Beats.calls) == 1
    state = (await client.get("/api/org/heartbeat")).json()
    assert state["last_status"] == "ok" and state["last_ok_at"]
    # Sources that don't answer are the customer's outage. The console is working, so the heartbeat continues.
    prom_server.shutdown()
    prom_server.server_close()
    await engine.refresh_now()
    heartbeat._last.clear()
    assert await heartbeat.run_due() == 1 and len(Beats.calls) == 2
    # The console can't complete its own checks: no heartbeat, so the outside service raises the alarm.
    engine.failed_polls_in_a_row = 1
    heartbeat._last.clear()
    assert await heartbeat.run_due() == 1 and len(Beats.calls) == 2
    state = (await client.get("/api/org/heartbeat")).json()
    assert state["last_status"] == "held" and "couldn't be completed" in state["last_error"] and state["last_ok_at"]
    # A test is sent either way, so the address can be checked.
    assert (await client.post("/api/org/heartbeat/test")).json() == {"status": "ok", "error": ""}
    Beats.status = 500
    assert (await client.post("/api/org/heartbeat/test")).json()["status"] == "failed"
    # Keeping the saved address while changing something else, and turning it off.
    off = await client.put("/api/org/heartbeat", json={"enabled": False, "every_minutes": 10})
    assert off.json()["url_set"] is True and off.json()["every_minutes"] == 10
    heartbeat._last.clear()
    assert await heartbeat.run_due() == 0
    audit = await console.ctx.db.all("SELECT detail FROM audit_log WHERE org_id = ? AND action = 'heartbeat_changed'", (org_id,))
    assert len(audit) == 2 and all("secret-check-id" not in row["detail"] for row in audit)


async def test_only_admins_set_the_heartbeat(console):
    from test_security import _invite
    admin = await console.admin()
    viewer = await _invite(console, admin, "v@test.example", "viewer")
    assert (await viewer.get("/api/org/heartbeat")).status_code == 403
    assert (await viewer.put("/api/org/heartbeat", json={"enabled": False})).status_code == 403
    assert (await viewer.post("/api/org/heartbeat/test")).status_code == 403
    assert (await console.client().get("/api/org/heartbeat")).status_code == 401


async def test_the_console_says_on_its_channels_when_it_cannot_complete_its_checks(console, prom_server, flaky_hook):
    from app.context.state import BLIND_AFTER_POLLS
    client, org_id, engine = await _company_with_source(console, prom_server)
    assert (await client.post("/api/org/channels", json={"kind": "slack", "name": "ops", "url": flaky_hook, "min_severity": "warning"})).status_code == 201
    real = engine.poll
    broken = True

    async def poll():
        if broken:
            raise RuntimeError("something inside the console broke")
        return await real()

    engine.poll = poll
    for _ in range(BLIND_AFTER_POLLS):
        engine._wake.set()
        await asyncio.sleep(0.15)
    assert engine.failed_polls_in_a_row >= BLIND_AFTER_POLLS and engine.still_active("console:polling")
    await _wait_for(lambda: asyncio.sleep(0, len(FlakyHook.received) == 1))
    assert "[CRITICAL] The console can't complete its checks" in FlakyHook.received[0]["text"] and "may go unnoticed" in FlakyHook.received[0]["text"]
    # More failures don't repeat it.
    engine._wake.set()
    await asyncio.sleep(0.2)
    assert len(FlakyHook.received) == 1
    # It works again: the same channel hears that too.
    broken = False
    engine._wake.set()
    await _wait_for(lambda: asyncio.sleep(0, len(FlakyHook.received) == 2))
    assert "[RESOLVED] The console completes its checks again" in FlakyHook.received[1]["text"]
    assert engine.failed_polls_in_a_row == 0 and not engine.still_active("console:polling")
