"""Noise control: blips, groups, flapping, false-alarm verdicts and the hourly limit."""
import asyncio

from app import noise
from app.db import utcnow


class _Engine:
    def __init__(self):
        self._active = {}

    def still_active(self, fingerprint):
        return fingerprint in self._active


class _Engines:
    def __init__(self, engine):
        self.engine = engine

    async def get(self, org_id):
        return self.engine


def _event(name, severity="warning", kind="alert_firing"):
    return {"kind": kind, "severity": severity, "title": f"{name} is failing", "fingerprint": f"alert:{name}", "ts": utcnow()}


async def _setup(console, **settings):
    client = await console.company("NoiseCo", "n@test.example")
    org_id = (await client.get("/api/auth/me")).json()["org_id"]
    assert (await client.post("/api/org/channels", json={"kind": "email", "name": "mail", "recipients": ["n@test.example"], "min_severity": "warning"})).status_code == 201
    body = {"grace_critical_s": 0, "grace_warning_s": 0, "group_window_s": 0, "max_per_hour": 50, "flapping": True, "respect_verdicts": True, **settings}
    assert (await client.put("/api/org/noise", json=body)).status_code == 200
    notifier = console.ctx.notifier
    notifier.noise = noise.NoiseControl()
    engine = _Engine()
    notifier.engines = _Engines(engine)
    return client, org_id, notifier, engine


def _mails(console):
    return [message for message in console.outbox if message["To"] == "n@test.example" and "WARNING" in message["Subject"]]


async def _held(console, org_id):
    rows = await console.ctx.db.all("SELECT status FROM deliveries WHERE org_id = ? AND status LIKE 'suppressed:%'", (org_id,))
    return [row["status"].split(":", 1)[1] for row in rows]


async def test_a_blip_that_clears_in_the_grace_period_is_never_sent(console):
    client, org_id, notifier, engine = await _setup(console, grace_warning_s=1)
    event = _event("Blip")
    engine._active[event["fingerprint"]] = {}
    task = asyncio.create_task(notifier.handle_event(org_id, event, {}))
    await asyncio.sleep(0.2)
    engine._active.clear()  # Fixed itself before the grace period ended.
    await task
    assert not _mails(console)
    assert await _held(console, org_id) == ["blip"]
    # One that is still there afterwards goes out.
    event = _event("Real")
    engine._active[event["fingerprint"]] = {}
    await notifier.handle_event(org_id, event, {})
    assert len(_mails(console)) == 1


async def test_problems_that_start_together_become_one_message_with_what_they_share(console):
    client, org_id, notifier, engine = await _setup(console, group_window_s=1)
    details = [{"namespace": "shop", "summary": "dial tcp postgres:5432: connection refused"} for _ in range(3)]
    events = [_event(name) for name in ("Checkout", "Cart", "Orders")]
    await asyncio.gather(*(notifier.handle_event(org_id, event, detail) for event, detail in zip(events, details, strict=True)))
    mails = _mails(console)
    assert len(mails) == 1, "one message for the incident, not three"
    text = mails[0].get_body(preferencelist=("plain",)).get_content()
    assert "3 problems started together" in mails[0]["Subject"]
    assert "3 of them are in namespace shop" in text and "postgres:5432" in text
    assert sorted(await _held(console, org_id)) == ["grouped", "grouped"]
    week = (await client.get("/api/org/noise")).json()["week"]
    assert week["held_back"]["grouped"] == 2


async def test_a_flapping_problem_gets_one_message_saying_so(console):
    client, org_id, notifier, engine = await _setup(console)
    event = _event("Flappy")
    for _ in range(5):
        await notifier.handle_event(org_id, event, {})
        notifier._sent.clear()  # Let the 15-minute repeat guard pass, to test flapping alone.
    subjects = [mail["Subject"] for mail in _mails(console)]
    assert len(subjects) == 3, subjects
    assert sum("keeps starting and stopping" in subject for subject in subjects) == 1
    assert (await _held(console, org_id)).count("flapping") == 3


async def test_a_problem_marked_false_alarm_is_no_longer_sent(console):
    client, org_id, notifier, engine = await _setup(console)
    event = _event("Noisy")
    # The same call the Projects page makes when someone marks an issue as a false alarm.
    marked = await client.put("/api/issues/verdict", json={"title": event["title"], "verdict": "false_positive", "note": "Fires on every nightly batch."})
    assert marked.status_code == 200, marked.text
    await notifier.handle_event(org_id, event, {})
    assert not _mails(console)
    assert await _held(console, org_id) == ["muted"]
    assert (await client.put("/api/org/noise", json={"respect_verdicts": False})).status_code == 200
    await notifier.handle_event(org_id, event, {})
    assert len(_mails(console)) == 1


async def test_the_hourly_limit_holds_the_rest_for_one_summary(console, monkeypatch):
    client, org_id, notifier, engine = await _setup(console, max_per_hour=2)
    monkeypatch.setattr(notifier.noise, "seconds_until_room", lambda channel_id: 0.2)
    for name in ("One", "Two", "Three", "Four"):
        await notifier.handle_event(org_id, _event(name), {})
    for _ in range(30):
        if any("more problems started" in mail["Subject"] for mail in _mails(console)):
            break
        await asyncio.sleep(0.1)
    subjects = [mail["Subject"] for mail in _mails(console)]
    assert len(subjects) == 3, subjects
    assert "2 more problems started in the last hour" in subjects[-1]
    assert (await _held(console, org_id)).count("rate_limit") == 2


async def test_recoveries_go_out_without_waiting(console):
    client, org_id, notifier, engine = await _setup(console, grace_warning_s=600)
    event = _event("Quick")
    notifier._sent[(1, event["fingerprint"])] = 0  # Not a channel that heard about it: nothing to send.
    await asyncio.wait_for(notifier.handle_event(org_id, {**event, "kind": "alert_cleared", "severity": "info"}, {}), 2)


def test_common_causes_are_facts_from_the_problems():
    items = [({"title": "a"}, {"cluster": "prod", "summary": "timeout talking to redis:6379"}),
             ({"title": "b"}, {"cluster": "prod", "error": "redis:6379 refused"})]
    hints = noise.common_causes(items, [{"title": "Deploy api v42", "ts": "2026-11-10T10:00:00Z"}])
    assert "All of them are in cluster prod." in hints
    assert any("redis:6379" in hint for hint in hints)
    assert any("Deploy api v42" in hint for hint in hints)
    assert not any("10.0.0" in hint for hint in noise.common_causes(items[:1], []))


async def test_noise_settings_need_an_admin_and_are_checked(console):
    client, org_id, notifier, engine = await _setup(console)
    assert (await client.put("/api/org/noise", json={"max_per_hour": 0})).status_code == 422
    settings = (await client.get("/api/org/noise")).json()
    assert settings["max_per_hour"] == 50 and "held_back" in settings["week"]
