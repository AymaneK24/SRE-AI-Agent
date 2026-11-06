"""Projects, recurring issues, patterns, PDF reports, and upgrading an older database."""
import io
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from pypdf import PdfReader

from app.analysis import analyze
from app.config import Config
from app.db import Database
from app.reports import build_pdf
from conftest import PG_URL, FakeProm

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
START = NOW - timedelta(days=30)


class Log:
    def __init__(self):
        self.events = []

    def add(self, at, kind, fingerprint, title, severity="warning", detail="{}"):
        self.events.append({"id": len(self.events) + 1, "ts": at.isoformat().replace("+00:00", "Z"), "kind": kind, "severity": severity,
                            "fingerprint": fingerprint, "title": title, "detail": detail, "source_id": 1})

    def problem(self, at, minutes, name="Alert firing: HighCPU", fingerprint=None, severity="warning"):
        fingerprint = fingerprint or f"alert:1:{name}"
        self.add(at, "alert_started", fingerprint, name, severity)
        if minutes is not None:
            self.add(at + timedelta(minutes=minutes), "alert_cleared", fingerprint, f"Alert resolved: {name}", "info")


def days_ago(days, hour=12, minute=0):
    return (NOW - timedelta(days=days)).replace(hour=hour, minute=minute)


def test_recurring_issues_are_counted_and_timed():
    log = Log()
    log.problem(days_ago(10), 20)
    log.problem(days_ago(5), 40)
    log.problem(days_ago(1), None, severity="critical")
    log.problem(days_ago(3), 15, name="Alert firing: DiskFull")
    result = analyze(log.events, [], START, NOW, log.events[0]["ts"])
    high = next(issue for issue in result["issues"] if issue["title"] == "Alert firing: HighCPU")
    assert high["occurrences"] == 3 and high["recurring"] and high["active"] and high["severity"] == "critical"
    assert high["average_minutes"] == 30.0
    assert result["summary"] == {"occurrences": 4, "issues": 2, "recurring": 1, "critical": 1, "active": 1,
                                 "problem_minutes": result["summary"]["problem_minutes"], "average_clear_minutes": 25.0}
    assert sum(day["warning"] + day["critical"] for day in result["daily"]) == 4


def test_a_restart_doesnt_count_as_a_new_occurrence():
    log = Log()
    log.problem(days_ago(4), None)
    log.add(days_ago(3), "alert_started", "alert:1:Alert firing: HighCPU", "Already active when the console connected. Alert firing: HighCPU")
    log.add(days_ago(2), "alert_cleared", "alert:1:Alert firing: HighCPU", "Alert resolved: HighCPU", "info")
    issue = analyze(log.events, [], START, NOW)["issues"][0]
    assert issue["occurrences"] == 1 and not issue["active"] and issue["total_minutes"] == 2880


def test_a_problem_from_before_the_period_counts_only_its_time_inside():
    log = Log()
    log.problem(START - timedelta(days=2), 2 * 1440 + 60)
    log.problem(START - timedelta(days=5), 30, name="Alert firing: Old")
    result = analyze(log.events, [], START, NOW)
    assert [issue["title"] for issue in result["issues"]] == ["Alert firing: HighCPU"]
    assert result["issues"][0]["total_minutes"] == 60


def _types(result):
    return {pattern["type"] for pattern in result["patterns"]}


def test_quiet_data_produces_no_patterns():
    log = Log()
    log.problem(days_ago(20, 3), 300)
    log.problem(days_ago(2, 15), 60)
    assert analyze(log.events, [], START, NOW)["patterns"] == []


def test_detects_flapping_and_time_of_day():
    log = Log()
    for day in (20, 15, 10, 5):
        log.problem(days_ago(day, 2, 10), 3)
    result = analyze(log.events, [], START, NOW)
    assert {"flapping", "time_of_day"} <= _types(result)
    timing = next(pattern for pattern in result["patterns"] if pattern["type"] == "time_of_day")
    assert timing["evidence"] == {"in_window": 4, "total": 4, "from_hour_utc": 0}


def test_detects_problems_that_happen_together_and_which_starts_first():
    log = Log()
    for day in (20, 14, 8):
        log.problem(days_ago(day, 9), 30, name="Alert firing: DbPool")
        log.problem(days_ago(day, 9, 5), 30, name="Alert firing: HighErrorRate")
    log.problem(days_ago(2, 18), 10, name="Alert firing: DbPool")
    pattern = next(item for item in analyze(log.events, [], START, NOW)["patterns"] if item["type"] == "together")
    assert pattern["evidence"]["times"] == 3
    assert "Alert firing: DbPool usually starts first, about 5.0 minutes before" in pattern["detail"]


def test_detects_problems_after_changes_but_not_before_them():
    log = Log()
    changes = []
    for day in (12, 6):
        at = days_ago(day, 14)
        changes.append({"ts": at.isoformat().replace("+00:00", "Z"), "title": f"Deploy {day}"})
        log.problem(at + timedelta(minutes=10), 20)
    result = analyze(log.events, changes, START, NOW)
    assert "after_change" in _types(result)
    late = [{"ts": (days_ago(day, 14) + timedelta(minutes=40)).isoformat().replace("+00:00", "Z"), "title": "Later"} for day in (12, 6)]
    assert "after_change" not in _types(analyze(log.events, late, START, NOW))


def test_detects_trends_and_long_running_problems():
    log = Log()
    log.problem(days_ago(25), 30)
    for day in (10, 8, 6, 4):
        log.problem(days_ago(day), 30)
    assert "worsening" in _types(analyze(log.events, [], START, NOW))
    long_log = Log()
    long_log.problem(days_ago(25), None)
    assert "chronic" in _types(analyze(long_log.events, [], START, NOW))


async def _project(client, prom_server, name="Payments"):
    source = (await client.post("/api/sources", json={"kind": "prometheus", "name": "prod", "poll_seconds": 15, "config": {"scheme": "http", "host": "127.0.0.1", "port": prom_server.server_address[1]}, "secrets": {}})).json()
    made = await client.post("/api/projects", json={"name": name, "description": "Card payments", "source_ids": [source["id"]]})
    assert made.status_code == 201, made.text
    return made.json()["id"], source["id"]


async def _seed_events(console, org_id, source_id):
    log = Log()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    for day in (9, 6, 3):
        log.problem(now - timedelta(days=day), 25, name="Alert firing: HighErrorRate", severity="critical")
    log.problem(now - timedelta(days=2), 12, name="Alert firing: <b>Odd & name</b>")
    for event in log.events:
        await console.ctx.db.insert("INSERT INTO events(org_id, source_id, ts, kind, severity, fingerprint, title, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                    (org_id, source_id, event["ts"], event["kind"], event["severity"], event["fingerprint"], event["title"], "{}"))


async def test_project_groups_its_sources_only(console, prom_server):
    FakeProm.state.firing = True
    admin = await console.admin()
    project_id, source_id = await _project(admin, prom_server)
    other = (await admin.post("/api/sources", json={"kind": "prometheus", "name": "lab", "poll_seconds": 15, "config": {"scheme": "http", "host": "127.0.0.1", "port": prom_server.server_address[1]}, "secrets": {}})).json()
    org_id = (await admin.get("/api/auth/me")).json()["org_id"]
    await _seed_events(console, org_id, source_id)
    await console.ctx.db.insert("INSERT INTO events(org_id, source_id, ts, kind, severity, fingerprint, title, detail) VALUES (?, ?, ?, 'alert_started', 'critical', 'x', 'Alert firing: LabOnly', '{}')",
                                (org_id, other["id"], datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")))
    view = (await admin.get(f"/api/projects/{project_id}?days=30")).json()
    assert [source["name"] for source in view["sources"]] == ["prod"]
    titles = [issue["title"] for issue in view["issues"]]
    assert "Alert firing: HighErrorRate" in titles and "Alert firing: LabOnly" not in titles
    listed = (await admin.get("/api/projects")).json()
    assert listed[0]["summary"]["recurring"] == 1

    # Removing a source takes it out of the project, and an unknown source is refused.
    assert (await admin.put(f"/api/projects/{project_id}", json={"name": "Payments", "source_ids": [999999]})).status_code == 422
    assert (await admin.delete(f"/api/sources/{source_id}")).status_code < 300
    assert (await admin.get(f"/api/projects/{project_id}")).json()["sources"] == []
    await admin.aclose()


async def test_report_is_a_fixed_snapshot_with_a_readable_pdf(console, prom_server, fake_models):
    admin = await console.admin()
    project_id, source_id = await _project(admin, prom_server)
    org_id = (await admin.get("/api/auth/me")).json()["org_id"]
    await _seed_events(console, org_id, source_id)
    report = (await admin.post(f"/api/projects/{project_id}/reports", json={"days": 30})).json()
    assert report["summary"]["occurrences"] == 4 and report["ai_notes"] == ""

    # Events recorded later don't change a saved report.
    await _seed_events(console, org_id, source_id)
    saved = (await admin.get(f"/api/projects/{project_id}/reports")).json()[0]
    assert saved["summary"]["occurrences"] == 4

    settings = (await admin.get("/api/ai/settings")).json()
    settings["ollama"]["url"] = fake_models
    settings["ollama"]["model"] = "llama3.2:latest"
    assert (await admin.put("/api/ai/settings", json={"provider": "ollama", "max_rounds": 3, "ollama": settings["ollama"], "claude": settings["claude"]})).status_code == 200
    noted = await admin.post(f"/api/projects/{project_id}/reports/{report['id']}/notes", json={})
    assert noted.status_code == 200, noted.text
    assert noted.json()["ai_model"] == "llama3.2:latest" and noted.json()["ai_notes"]
    # Every prompt is logged, including this one.
    calls = (await admin.get("/api/llm-calls")).json()
    calls = calls["items"] if isinstance(calls, dict) else calls
    assert calls and "HighErrorRate" in (await admin.get(f"/api/llm-calls/{calls[0]['id']}")).text

    response = await admin.get(f"/api/projects/{project_id}/reports/{report['id']}/pdf")
    assert response.status_code == 200 and response.headers["content-type"] == "application/pdf"
    assert "attachment" in response.headers["content-disposition"] and "Payments-report" in response.headers["content-disposition"]
    text = " ".join(page.extract_text() for page in PdfReader(io.BytesIO(response.content)).pages)
    for expected in ("Payments", "Recurring issues", "Alert firing: HighErrorRate", "Patterns", "AI notes", "llama3.2:latest", "Data and method", "<b>Odd & name</b>"):
        assert expected in text, expected
    await admin.aclose()


async def test_viewers_can_read_but_not_change_projects(console, prom_server):
    admin = await console.admin()
    project_id, _ = await _project(admin, prom_server)
    report = (await admin.post(f"/api/projects/{project_id}/reports", json={"days": 7})).json()
    await admin.post("/api/org/members/invite", json={"email": "view@example.com", "name": "Vic Viewer", "role": "viewer"})
    viewer = console.client()
    assert (await viewer.post("/api/auth/accept-invite", json={"token": console.last_link_token("view@example.com"), "name": "Vic", "password": "Correct-horse-battery-staple-1"})).status_code == 200
    assert (await viewer.get(f"/api/projects/{project_id}")).status_code == 200
    # A report stays with the team until someone shares it.
    assert (await viewer.get(f"/api/projects/{project_id}/reports")).json() == []
    assert (await viewer.get(f"/api/projects/{project_id}/reports/{report['id']}/pdf")).status_code == 404
    assert (await admin.post(f"/api/projects/{project_id}/reports/{report['id']}/share", json={"shared": True})).status_code == 200
    assert (await viewer.get(f"/api/projects/{project_id}/reports/{report['id']}/pdf")).status_code == 200
    assert set((await viewer.get(f"/api/projects/{project_id}/reports")).json()[0]) == {"id", "project_id", "days", "created_at", "period", "summary"}
    assert (await viewer.post("/api/projects", json={"name": "No"})).status_code == 403
    assert (await viewer.post(f"/api/projects/{project_id}/reports", json={"days": 7})).status_code == 403
    assert (await viewer.delete(f"/api/projects/{project_id}")).status_code == 403
    await admin.aclose()
    await viewer.aclose()


async def test_projects_stay_within_their_company(console, prom_server):
    a = await console.company("Alpha Co", "a@alpha.example")
    b = await console.company("Beta Co", "b@beta.example")
    project_id, source_id = await _project(a, prom_server)
    report = (await a.post(f"/api/projects/{project_id}/reports", json={"days": 7})).json()
    assert (await b.get("/api/projects")).json() == []
    for method, path in [("GET", f"/api/projects/{project_id}"), ("DELETE", f"/api/projects/{project_id}"), ("GET", f"/api/projects/{project_id}/reports"),
                         ("GET", f"/api/projects/{project_id}/reports/{report['id']}/pdf")]:
        assert (await b.request(method, path)).status_code == 404, path
    assert (await b.post("/api/projects", json={"name": "Stolen", "source_ids": [source_id]})).status_code == 422
    await a.aclose()
    await b.aclose()


def test_pdf_handles_empty_data_and_hostile_text():
    data = analyze([], [], START, NOW)
    data.update(project={"name": "<script>Empty & new</script>", "description": "日本語 </para>"}, sources=[], root_causes=[], workflows=[], health="no_sources", company="Ünïcode Co")
    pdf = build_pdf({"data": data, "created_at": "2026-11-04T12:00:00.000Z", "created_by": "admin", "ai_notes": "## Notes\n- **Bold** <font>x</font> & `code`\n\nDone", "ai_model": "m", "ai_cost_usd": None, "ai_check": {"unmatched": ["99%"]}})
    text = " ".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)
    assert "<script>Empty & new</script>" in text and "No problems were recorded" in text and "<font>x</font>" in text and "99%" in text


@pytest.mark.skipif(bool(PG_URL), reason="This test builds a SQLite file.")
def _old_sqlite_schema(version):
    """The tables as version 2 or 3 of the app created them."""
    from app import schema
    old = schema.sqlite_statements().replace(schema.V4_TABLES.format(**schema.SQLITE_TYPES), "").replace(schema.V5_TABLES.format(**schema.SQLITE_TYPES), "")
    for new, previous in (("  username TEXT NOT NULL UNIQUE,\n  email TEXT NOT NULL UNIQUE,\n  name TEXT NOT NULL,\n  pending_email TEXT,\n", "  username TEXT UNIQUE,\n  email TEXT UNIQUE,\n  name TEXT NOT NULL DEFAULT '',\n"),
                          ("  owner_id BIGINT,\n  visibility TEXT NOT NULL DEFAULT 'private',\n  shared_by TEXT NOT NULL DEFAULT '',\n  shared_at TEXT,\n", "")):
        assert new in old
        old = old.replace(new, previous)
    if version == 2:
        old = old.replace(schema.PROJECT_TABLES.format(**schema.SQLITE_TYPES), "") + "CREATE TABLE licenses (id INTEGER PRIMARY KEY, org_id BIGINT);"
    return old + "CREATE TABLE eval_runs (id INTEGER PRIMARY KEY);"


@pytest.mark.parametrize("version", [2, 3])
def test_upgrading_keeps_existing_data(tmp_path, version):
    from app import schema
    path = tmp_path / "console.db"
    conn = sqlite3.connect(path)
    conn.executescript(_old_sqlite_schema(version))
    conn.execute("INSERT INTO organizations(name, slug, contact_email, policies, created_at) VALUES ('Kept', 'kept', '', '{}', 'now')")
    conn.executemany("INSERT INTO users(org_id, username, email, name, password_hash, role, is_platform_admin, created_at) VALUES (1, ?, ?, ?, 'hash', ?, ?, 'now')", [
        ("admin", None, "Administrator", "owner", 1),
        (None, "King@Example.com", "<img src=x onerror=alert(1)>", "viewer", 0),
        (None, "pat.lee@example.com", "", "engineer", 0),
        (None, "crlf@example.com", "Bad\r\nX-Injected: true", "viewer", 0),
    ])
    conn.executemany("INSERT INTO conversations(org_id, created_by, trigger, title, created_at, updated_at) VALUES (1, ?, ?, 't', 'now', 'now')",
                     [("admin", "manual"), ("pat.lee@example.com", "manual"), ("automatic", "auto")])
    conn.execute(f"PRAGMA user_version={version}")
    conn.commit()
    conn.close()
    db = Database(path, "")
    assert db._all("SELECT name FROM organizations")[0]["name"] == "Kept"
    assert db._all("SELECT count(*) AS n FROM projects")[0]["n"] == 0
    assert not db._all("SELECT name FROM sqlite_master WHERE name IN ('licenses', 'eval_runs')")
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == schema.SCHEMA_VERSION
    users = db._all("SELECT id, username, email, name FROM users ORDER BY id")
    assert [user["email"] for user in users] == ["admin@rootcause.local", "king@example.com", "pat.lee@example.com", "crlf@example.com"]
    assert all(user["username"] == user["email"] and user["name"] for user in users)
    assert users[0]["name"] == "Administrator" and users[1]["name"] == "King" and users[2]["name"] == "Pat Lee"
    assert users[3]["name"] == "Bad X-Injected: true"
    rows = db._all("SELECT owner_id, visibility FROM conversations ORDER BY id")
    assert rows == [{"owner_id": users[0]["id"], "visibility": "private"}, {"owner_id": users[2]["id"], "visibility": "private"}, {"owner_id": None, "visibility": "team"}]
    db.close()
    assert not list(tmp_path.glob("console.v*")), "older data must not be moved aside"


def test_public_address_is_accepted_automatically():
    config = Config(public_url="http://10.1.2.3:8000", allowed_hosts=["localhost"])
    assert "10.1.2.3" in config.accepted_hosts and json.dumps(config.accepted_hosts)
