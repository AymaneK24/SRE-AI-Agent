"""False-alarm and expected labels end: after their time, after a change to the same service, when the problem gets
worse, when its rule changes, or when it lasts longer than ever. The alert then goes out and says why."""
import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from app import analysis, noise
from app.db import Database
from app.verdicts import Verdicts, targets


def _iso(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _now():
    return datetime.now(timezone.utc)


async def _setup(console, **settings):
    client = await console.company("LabelCo", "l@test.example")
    org_id = (await client.get("/api/auth/me")).json()["org_id"]
    assert (await client.post("/api/org/channels", json={"kind": "email", "name": "mail", "recipients": ["l@test.example"], "min_severity": "warning"})).status_code == 201
    body = {"grace_critical_s": 0, "grace_warning_s": 0, "group_window_s": 0, "max_per_hour": 50, "flapping": False, "respect_verdicts": True, **settings}
    assert (await client.put("/api/org/noise", json=body)).status_code == 200
    notifier = console.ctx.notifier
    notifier.noise = noise.NoiseControl()
    return client, org_id, notifier


def _mails(console):
    return [message for message in console.outbox if message["To"] == "l@test.example" and "WARNING" in message["Subject"]]


def _body(message):
    return "\n".join(part.get_content() for part in message.walk() if part.get_content_type() == "text/plain")


EVENT = {"kind": "signal_started", "severity": "warning", "title": "Disk use is over its limit: payments-db", "fingerprint": "signal:1:disk:payments-db"}
DETAIL = {"labels": {"namespace": "payments", "statefulset": "payments-db", "instance": "10.0.4.2:9100"}, "value": 82.0, "query": "disk_used_percent", "limit": 80}


def _event():
    return {**EVENT, "ts": _iso(_now())}


async def _record(db, org_id, ts, kind="signal_started", detail=None):
    return await db.insert("INSERT INTO events(org_id, ts, kind, severity, fingerprint, title, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (org_id, ts, kind, EVENT["severity"], EVENT["fingerprint"], EVENT["title"], json.dumps(detail if detail is not None else DETAIL)))


async def _label(client, verdict="false_positive"):
    response = await client.put("/api/issues/verdict", json={"title": EVENT["title"], "verdict": verdict, "note": "The nightly backup."})
    assert response.status_code == 200, response.text


async def _row(db, org_id):
    return await db.one("SELECT * FROM issue_verdicts WHERE org_id = ? AND issue_key = ?", (org_id, EVENT["title"]))


async def test_a_label_still_mutes_until_something_ends_it(console):
    client, org_id, notifier = await _setup(console)
    await _record(console.ctx.db, org_id, _iso(_now() - timedelta(hours=3)))
    await _label(client)
    await notifier.handle_event(org_id, _event(), dict(DETAIL))
    # Within 20% of the highest value seen when it was labelled: still the same old problem.
    await notifier.handle_event(org_id, _event(), {**DETAIL, "value": 90.0})
    assert not _mails(console)
    assert not (await _row(console.ctx.db, org_id))["ended_at"]


async def test_a_label_ends_when_its_time_is_up_and_the_alert_says_why(console):
    client, org_id, notifier = await _setup(console, label_days=7)
    await _label(client)
    await console.ctx.db.execute("UPDATE issue_verdicts SET decided_at = ? WHERE org_id = ?", (_iso(_now() - timedelta(days=8)), org_id))
    await notifier.handle_event(org_id, _event(), dict(DETAIL))
    mails = _mails(console)
    assert len(mails) == 1
    assert "that label ended: it was set for 7 days and wasn't renewed" in _body(mails[0])
    row = await _row(console.ctx.db, org_id)
    assert row["ended_reason"] == "expired" and row["ended_at"]
    audit = await console.ctx.db.all("SELECT action, actor FROM audit_log WHERE org_id = ? AND action = 'verdict_ended'", (org_id,))
    assert audit == [{"action": "verdict_ended", "actor": "console"}]


async def test_a_label_kept_until_something_changes_has_no_time_limit(console):
    client, org_id, notifier = await _setup(console, label_days=0)
    await _label(client)
    await console.ctx.db.execute("UPDATE issue_verdicts SET decided_at = ? WHERE org_id = ?", (_iso(_now() - timedelta(days=400)), org_id))
    await notifier.handle_event(org_id, _event(), dict(DETAIL))
    assert not _mails(console)


async def test_a_deploy_to_the_same_service_ends_the_label(console):
    client, org_id, notifier = await _setup(console)
    await _label(client)
    # A change elsewhere doesn't matter; one to payments-db does.
    await console.ctx.db.insert("INSERT INTO change_events(org_id, ts, origin, service, title) VALUES (?, ?, 'GitHub', 'checkout', 'Deployment of v2 to prod')", (org_id, _iso(_now())))
    await notifier.handle_event(org_id, _event(), dict(DETAIL))
    assert not _mails(console)
    await console.ctx.db.insert("INSERT INTO change_events(org_id, ts, origin, service, title) VALUES (?, ?, 'Helm', '', 'Helm upgrade payments-db 3.8.0')", (org_id, _iso(_now())))
    await notifier.handle_event(org_id, _event(), dict(DETAIL))
    mails = _mails(console)
    assert len(mails) == 1 and "Helm upgrade payments-db 3.8.0" in _body(mails[0])
    assert (await _row(console.ctx.db, org_id))["ended_reason"] == "change"


async def test_a_value_well_above_anything_seen_before_ends_the_label(console):
    client, org_id, notifier = await _setup(console)
    await _record(console.ctx.db, org_id, _iso(_now() - timedelta(hours=3)))
    await _label(client)
    assert json.loads((await _row(console.ctx.db, org_id))["peak"])["value"] == 82.0
    await notifier.handle_event(org_id, _event(), {**DETAIL, "value": 99.0})
    mails = _mails(console)
    assert len(mails) == 1 and "above the highest seen when it was labelled (82)" in _body(mails[0])
    assert (await _row(console.ctx.db, org_id))["ended_reason"] == "behaviour"


async def test_a_changed_query_or_limit_ends_the_label(console):
    client, org_id, notifier = await _setup(console)
    await _record(console.ctx.db, org_id, _iso(_now() - timedelta(hours=3)))
    await _label(client)
    await notifier.handle_event(org_id, _event(), {**DETAIL, "limit": 95})
    assert len(_mails(console)) == 1
    assert (await _row(console.ctx.db, org_id))["ended_reason"] == "rule_changed"


async def test_renewing_starts_a_new_period_and_mutes_again(console):
    client, org_id, notifier = await _setup(console, label_days=7)
    await _label(client)
    await console.ctx.db.execute("UPDATE issue_verdicts SET decided_at = ? WHERE org_id = ?", (_iso(_now() - timedelta(days=8)), org_id))
    await notifier.handle_event(org_id, _event(), dict(DETAIL))
    assert len(_mails(console)) == 1
    renewed = await client.post("/api/issues/verdict/renew", json={"title": EVENT["title"]})
    assert renewed.status_code == 200, renewed.text
    row = await _row(console.ctx.db, org_id)
    assert not row["ended_at"] and row["renewed_at"] and row["decided_at"] < row["renewed_at"]
    await notifier.handle_event(org_id, _event(), dict(DETAIL))
    assert len(_mails(console)) == 1
    assert (await client.post("/api/issues/verdict/renew", json={"title": "No such issue"})).status_code == 404
    actions = [row["action"] for row in await console.ctx.db.all("SELECT action FROM audit_log WHERE org_id = ? ORDER BY id", (org_id,))]
    assert actions[-1] == "issue_verdict_renewed"


async def test_labelling_again_clears_an_ended_label(console):
    client, org_id, notifier = await _setup(console, label_days=7)
    await _label(client)
    await console.ctx.db.execute("UPDATE issue_verdicts SET decided_at = ? WHERE org_id = ?", (_iso(_now() - timedelta(days=8)), org_id))
    await notifier.handle_event(org_id, _event(), dict(DETAIL))
    await _label(client, "expected")
    row = await _row(console.ctx.db, org_id)
    assert row["verdict"] == "expected" and not row["ended_at"] and row["ended_reason"] == ""


async def test_a_muted_problem_that_lasts_longer_than_ever_is_sent_while_it_is_still_active(console):
    client, org_id, notifier = await _setup(console)
    db = console.ctx.db
    now = _now()
    # Before the label: it lasted 20 minutes, then cleared.
    await _record(db, org_id, _iso(now - timedelta(hours=5)))
    await _record(db, org_id, _iso(now - timedelta(hours=5) + timedelta(minutes=20)), kind="signal_cleared")
    await _label(client)
    # Now it started again an hour ago and hasn't stopped.
    await _record(db, org_id, _iso(now - timedelta(minutes=60)))
    assert await notifier.watch_labels(now) == 1
    await asyncio.gather(*list(notifier._waiting))
    mails = _mails(console)
    assert len(mails) == 1 and "longer than any time before it was labelled (20 minutes)" in _body(mails[0])
    # Nothing more to send on the next round.
    assert await notifier.watch_labels(now) == 0


async def test_a_muted_problem_still_within_its_usual_length_stays_quiet(console):
    client, org_id, notifier = await _setup(console)
    db = console.ctx.db
    now = _now()
    await _record(db, org_id, _iso(now - timedelta(hours=5)))
    await _record(db, org_id, _iso(now - timedelta(hours=5) + timedelta(minutes=40)), kind="signal_cleared")
    await _label(client)
    await _record(db, org_id, _iso(now - timedelta(minutes=30)))
    assert await notifier.watch_labels(now) == 0
    assert not _mails(console)


async def test_an_ended_label_shows_as_not_reviewed_with_its_reason():
    data = {"issues": [{"title": "A", "occurrences": 2, "active": True}], "patterns": [], "summary": {}}
    ended = {"A": {"verdict": "false_positive", "note": "", "decided_by": "pat", "ended_at": "2026-11-01T00:00:00Z", "ended_text": "Marked a false alarm by pat on 2026-10-01; that label ended: a deploy."}}
    issue = analysis.apply_verdicts(data, ended)["issues"][0]
    assert issue["verdict"] is None and "that label ended" in issue["verdict_ended"]
    assert data["summary"]["active_real_issues"] == 1 and data["summary"]["dismissed"] == 0


def test_the_names_a_problem_is_about_skip_generic_ones():
    names = targets({"labels": {"namespace": "default", "pod": "payments-api-7d9f6c5b8d-x2k4q", "job": "api"}})
    assert "payments-api" in names and "default" not in names and "api" not in names


async def test_a_label_without_a_row_is_no_label(console):
    assert await Verdicts(console.ctx.db).check(1, "nothing", None, 30) is None


def test_upgrading_from_version_10_keeps_labels(tmp_path):
    from app import schema
    path = tmp_path / "console.db"
    conn = sqlite3.connect(path)
    conn.executescript(schema.sqlite_statements().replace(schema.V11_COLUMNS, "").replace(schema.V12_COLUMNS, ""))
    conn.execute("INSERT INTO issue_verdicts(org_id, issue_key, verdict, note, decided_by, decided_at) VALUES (1, 'Old', 'false_positive', '', 'pat', '2026-01-01T00:00:00Z')")
    conn.execute("PRAGMA user_version=10")
    conn.commit()
    conn.close()
    db = Database(path, "")
    assert db._all("SELECT issue_key, ended_at, ended_reason, peak FROM issue_verdicts") == [{"issue_key": "Old", "ended_at": None, "ended_reason": "", "peak": "{}"}]
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == schema.SCHEMA_VERSION
    db.close()


# A length chosen per label ----------------------------------------------------
async def _label_for(client, hours, verdict="false_positive"):
    response = await client.put("/api/issues/verdict", json={"title": EVENT["title"], "verdict": verdict, "expires_hours": hours})
    assert response.status_code == 200, response.text


async def test_a_label_can_last_a_few_hours(console):
    client, org_id, notifier = await _setup(console)
    await _label_for(client, 6)
    await console.ctx.db.execute("UPDATE issue_verdicts SET decided_at = ? WHERE org_id = ?", (_iso(_now() - timedelta(hours=5)), org_id))
    await notifier.handle_event(org_id, _event(), dict(DETAIL))
    assert not _mails(console)
    await console.ctx.db.execute("UPDATE issue_verdicts SET decided_at = ? WHERE org_id = ?", (_iso(_now() - timedelta(hours=7)), org_id))
    await notifier.handle_event(org_id, _event(), dict(DETAIL))
    mails = _mails(console)
    assert len(mails) == 1 and "it was set for 6 hours and wasn't renewed" in _body(mails[0])


async def test_its_own_length_wins_over_the_organization_default(console):
    client, org_id, notifier = await _setup(console, label_days=7)
    await _label_for(client, 0)  # Until something changes.
    await console.ctx.db.execute("UPDATE issue_verdicts SET decided_at = ? WHERE org_id = ?", (_iso(_now() - timedelta(days=60)), org_id))
    await notifier.handle_event(org_id, _event(), dict(DETAIL))
    assert not _mails(console)
    await _label_for(client, 24 * 90)
    await console.ctx.db.execute("UPDATE issue_verdicts SET decided_at = ? WHERE org_id = ?", (_iso(_now() - timedelta(days=60)), org_id))
    await notifier.handle_event(org_id, _event(), dict(DETAIL))
    assert not _mails(console)


async def test_renewing_keeps_the_chosen_length(console):
    client, org_id, notifier = await _setup(console)
    await _label_for(client, 2)
    assert (await client.post("/api/issues/verdict/renew", json={"title": EVENT["title"]})).status_code == 200
    row = await _row(console.ctx.db, org_id)
    assert row["duration_hours"] == 2
    await console.ctx.db.execute("UPDATE issue_verdicts SET renewed_at = ? WHERE org_id = ?", (_iso(_now() - timedelta(hours=3)), org_id))
    await notifier.handle_event(org_id, _event(), dict(DETAIL))
    assert "it was set for 2 hours" in _body(_mails(console)[0])


async def test_a_length_must_be_between_an_hour_and_a_year(console):
    client, org_id, notifier = await _setup(console)
    for bad in (-1, 24 * 366):
        response = await client.put("/api/issues/verdict", json={"title": EVENT["title"], "verdict": "false_positive", "expires_hours": bad})
        assert response.status_code == 422
    # A real problem has no length.
    assert (await client.put("/api/issues/verdict", json={"title": EVENT["title"], "verdict": "real", "expires_hours": 5})).status_code == 200
    assert (await _row(console.ctx.db, org_id))["duration_hours"] is None


def test_upgrading_from_version_11_keeps_labels(tmp_path):
    from app import schema
    path = tmp_path / "console.db"
    conn = sqlite3.connect(path)
    conn.executescript(schema.sqlite_statements().replace(schema.V12_COLUMNS, ""))
    conn.execute("INSERT INTO issue_verdicts(org_id, issue_key, verdict, note, decided_by, decided_at) VALUES (1, 'Old', 'expected', '', 'pat', '2026-01-01T00:00:00Z')")
    conn.execute("PRAGMA user_version=11")
    conn.commit()
    conn.close()
    db = Database(path, "")
    assert db._all("SELECT issue_key, duration_hours FROM issue_verdicts") == [{"issue_key": "Old", "duration_hours": None}]
    db.close()
