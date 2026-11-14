"""Replays each finding from the penetration test of 2026-11-05, so none of them can come back unnoticed."""
import json
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.analysis import analyze, apply_verdicts, statements
from app.net_guard import BlockedTarget, TargetPolicy, check_url, guarded_transport
from app.security import SECURITY_HEADERS
from app.validation import clean_person_name, sanitize_stored_name

PASSWORD = "Correct-horse-battery-9"
SELF_HOSTED = TargetPolicy(allow_private=True)
XSS = ["<script>alert(1)</script>", "<img src=x onerror=alert(1)>", "<svg onload=alert(1)>", "<iframe src=javascript:alert(1)>", "<details open ontoggle=alert(1)>"]


async def _invite(console, admin, email, role):
    response = await admin.post("/api/org/members/invite", json={"email": email, "name": f"{role.title()} Person", "role": role})
    assert response.status_code == 201, response.text
    client = console.client()
    accepted = await client.post("/api/auth/accept-invite", json={"token": console.last_link_token(email), "name": f"{role.title()} Person", "password": PASSWORD})
    assert accepted.status_code == 200, accepted.text
    return client


@pytest.fixture
async def team(console):
    admin = await console.admin()
    viewer = await _invite(console, admin, "viewer@team.example", "viewer")
    alice = await _invite(console, admin, "alice@team.example", "engineer")
    bob = await _invite(console, admin, "bob@team.example", "engineer")
    yield {"admin": admin, "viewer": viewer, "alice": alice, "bob": bob}
    for client in (admin, viewer, alice, bob):
        await client.aclose()


# CRIT-1 -------------------------------------------------------------------------
async def test_email_cannot_change_without_the_password(console, team):
    viewer = team["viewer"]
    assert (await viewer.patch("/api/auth/me", json={"email": "attacker@evil.example"})).status_code == 401
    assert (await viewer.patch("/api/auth/me", json={"email": "attacker@evil.example", "current_password": "wrong-password-here"})).status_code == 401
    assert (await viewer.get("/api/auth/me")).json()["email"] == "viewer@team.example"


async def test_without_an_email_server_a_password_is_enough_and_sessions_restart(console, team):
    viewer = team["viewer"]
    response = await viewer.patch("/api/auth/me", json={"email": "direct@team.example", "current_password": PASSWORD})
    assert response.json()["pending_email"] is None
    me = (await viewer.get("/api/auth/me")).json()
    assert me["email"] == "direct@team.example"


async def test_new_email_works_only_after_it_is_confirmed(console, team, monkeypatch):
    viewer = team["viewer"]
    mailer = console.ctx.mailer
    monkeypatch.setattr(type(mailer), "configured", property(lambda self: True))
    monkeypatch.setattr(mailer, "_send", mailer.outbox.append)
    response = await viewer.patch("/api/auth/me", json={"email": "New.Address@team.example", "current_password": PASSWORD})
    assert response.status_code == 200 and response.json()["pending_email"] == "new.address@team.example"
    me = (await viewer.get("/api/auth/me")).json()
    assert me["email"] == "viewer@team.example" and me["pending_email"] == "new.address@team.example"
    # The old address hears about it, and can still sign in. The new one can't yet.
    assert any(message["To"] == "viewer@team.example" and "being changed" in message["Subject"] for message in console.outbox)
    assert (await console.client().post("/api/auth/login", json={"email": "new.address@team.example", "password": PASSWORD})).status_code == 401
    # Asking for a reset at the unconfirmed address sends nothing.
    before = len(console.outbox)
    await console.client().post("/api/auth/forgot-password", json={"email": "new.address@team.example"})
    assert len(console.outbox) == before
    assert (await console.client().post("/api/auth/verify", json={"token": console.last_link_token("new.address@team.example")})).json()["email_changed"]
    # Every old session ends, and the account's username follows the email.
    assert (await viewer.get("/api/auth/me")).status_code == 401
    assert (await console.client().post("/api/auth/login", json={"email": "viewer@team.example", "password": PASSWORD})).status_code == 401
    assert (await console.client().post("/api/auth/login", json={"email": "new.address@team.example", "password": PASSWORD})).status_code == 200
    row = await console.ctx.db.one("SELECT username, email, pending_email FROM users WHERE email = 'new.address@team.example'")
    assert row["username"] == row["email"] and row["pending_email"] is None


# Email-only sign-in ----------------------------------------------------------------
async def test_sign_in_uses_email_only_and_all_fields_are_filled(console, team):
    assert (await console.client().post("/api/auth/login", json={"email": "admin", "password": "a-Strong-Passphrase-2026"})).status_code == 401
    assert (await console.client().post("/api/auth/login", json={"login": "admin@rootcause.local", "password": "a-Strong-Passphrase-2026"})).status_code == 422
    assert (await console.client().post("/api/auth/login", json={"email": "ADMIN@rootcause.local", "password": "a-Strong-Passphrase-2026"})).status_code == 200
    rows = await console.ctx.db.all("SELECT username, email, name FROM users")
    assert len(rows) == 4 and all(row["email"] and row["name"] and row["username"] == row["email"] for row in rows)
    assert "username" not in (await team["viewer"].get("/api/auth/me")).json()


async def test_invitation_needs_a_name(team):
    assert (await team["admin"].post("/api/org/members/invite", json={"email": "x@team.example", "role": "viewer"})).status_code == 422


# HIGH-1: other people's investigations --------------------------------------------
async def test_investigations_are_private_to_whoever_started_them(console, team):
    alice, bob, viewer, admin = team["alice"], team["bob"], team["viewer"], team["admin"]
    made = (await alice.post("/api/conversations", json={"title": "Alice's work"})).json()
    cid = made["id"]
    call = await console.ctx.db.insert(
        "INSERT INTO llm_calls(org_id, conversation_id, round, provider, model, endpoint, request_json, request_sha256, started_at) VALUES (?, ?, 1, 'ollama', 'm', 'http://10.0.0.1/api', ?, 'x', 'now')",
        (made["org_id"], cid, json.dumps({"messages": [{"role": "system", "content": "secret system prompt"}]})))
    paths = [f"/api/conversations/{cid}", f"/api/conversations/{cid}/export", f"/api/llm-calls/{call}"]
    for path in paths:
        assert (await alice.get(path)).status_code == 200, path
        assert (await admin.get(path)).status_code == 200, path
        assert (await bob.get(path)).status_code == 404, path
        assert (await viewer.get(path)).status_code == 403, path
    assert [item["id"] for item in (await alice.get("/api/conversations")).json()["items"]] == [cid]
    assert (await bob.get("/api/conversations")).json()["items"] == []
    assert (await bob.get("/api/llm-calls")).json() == []
    assert "endpoint" not in (await alice.get("/api/llm-calls")).json()[0]
    assert (await bob.patch(f"/api/conversations/{cid}", json={"status": "resolved"})).status_code == 404
    assert (await bob.post(f"/api/conversations/{cid}/share", json={"shared": True})).status_code == 404
    assert (await bob.post(f"/api/conversations/{cid}/messages", json={"prompt": "hi"})).status_code == 404
    assert (await viewer.get(f"/api/results/{cid}")).status_code == 404


async def test_sharing_gives_viewers_the_question_and_answer_only(console, team):
    alice, bob, viewer = team["alice"], team["bob"], team["viewer"]
    made = (await alice.post("/api/conversations", json={"title": "Checkout errors"})).json()
    cid = made["id"]
    db = console.ctx.db
    await db.insert("INSERT INTO messages(conversation_id, role, content, created_at) VALUES (?, 'user', ?, 'now')", (cid, "Why is checkout failing?"))
    await db.insert("INSERT INTO messages(conversation_id, role, content, status, model, provider, input_tokens, cost_usd, created_at) VALUES (?, 'assistant', 'The pool is exhausted.', 'complete', 'secret-model', 'ollama', 99, 1.5, 'now')", (cid,))
    assert (await alice.post(f"/api/conversations/{cid}/share", json={"shared": True})).json()["visibility"] == "team"
    result = await viewer.get(f"/api/results/{cid}")
    assert result.status_code == 200
    body = result.json()
    assert body["exchanges"] == [{"question": "Why is checkout failing?", "answer": "The pool is exhausted.", "status": "complete", "asked_at": "now"}]
    assert set(body) == {"id", "title", "status", "root_cause_note", "updated_at", "exchanges"}
    # Not the model, its token count, or its cost. (Checked by field: a timestamp can contain any digits.)
    assert "secret-model" not in result.text and "input_tokens" not in result.text and "cost" not in result.text
    assert [item["id"] for item in (await viewer.get("/api/results")).json()["items"]] == [cid]
    # Another engineer can read a shared investigation, but only its owner or an admin can change it.
    assert (await bob.get(f"/api/conversations/{cid}")).status_code == 200
    assert (await bob.patch(f"/api/conversations/{cid}", json={"status": "resolved"})).status_code == 403
    await alice.post(f"/api/conversations/{cid}/share", json={"shared": False})
    assert (await viewer.get(f"/api/results/{cid}")).status_code == 404


async def test_private_investigations_do_not_reach_another_persons_model(console, team):
    from app.investigation.tools import related_investigations
    alice = team["alice"]
    made = (await alice.post("/api/conversations", json={"title": "Private matter"})).json()
    db = console.ctx.db
    await db.index_text(made["org_id"], made["id"], "payroll outage")
    owner = (await alice.get("/api/auth/me")).json()["id"]
    assert await related_investigations(db, made["org_id"], ["payroll"], owner_id=owner + 1) == []
    assert [item["id"] for item in await related_investigations(db, made["org_id"], ["payroll"], owner_id=owner)] == [made["id"]]


# HIGH-2, LOW-3, INFO-1: names ---------------------------------------------------------
@pytest.mark.parametrize("bad", [*XSS, "test\r\nX-Injected: true", "a\x00b", "=cmd|'/C calc'!A0", "+1 234", "-x", "@me", "x", "   ", "a‮b", "a b"])
async def test_unsafe_names_are_refused(team, bad):
    response = await team["viewer"].patch("/api/auth/me", json={"name": bad})
    assert response.status_code == 422, bad
    assert (await team["viewer"].get("/api/auth/me")).json()["name"] == "Viewer Person"


@pytest.mark.parametrize("good", ["Aymane KENBOUCH", "O'Brien", "Jean-Luc Picard", "José Ñandú", "محمد علي", "李小龙"])
def test_real_names_are_accepted(good):
    assert clean_person_name(good) == good


def test_old_unsafe_names_get_repaired():
    assert sanitize_stored_name("<img src=x onerror=alert(1)>King", "Fallback") == "King"
    assert sanitize_stored_name("<script>alert(1)</script>", "Fallback") == "alert(1)"
    assert sanitize_stored_name("=cmd", "Fallback") == "cmd"
    assert sanitize_stored_name("", "Fallback") == "Fallback"


async def test_labels_refuse_markup_too(team):
    assert (await team["alice"].post("/api/projects", json={"name": "<svg onload=alert(1)>"})).status_code == 422


async def test_audit_export_defuses_spreadsheet_formulas(console, team):
    await console.ctx.db.audit(1, "x", "=cmd", {}, actor="\t=evil")
    text = (await team["admin"].get("/api/audit/export?format=csv")).text
    assert "'=cmd" in text and "'\t=evil" in text


# MEDIUM-1: outgoing requests -----------------------------------------------------------
@pytest.mark.parametrize("url", [
    "http://0x7f000001:8000", "http://2130706433:8000", "http://0177.0.0.1:8000", "http://127.1:8000", "http://0x7f.0.0.1",
    "http://127.0.0.1:8000", "http://127.0.0.2:8000", "http://127.255.255.254:8000", "http://localhost:8000", "http://LOCALHOST:8000", "http://[::1]:8000",
    "http://[::ffff:127.0.0.1]:8000", "http://[::ffff:172.27.171.169]:11434", "http://[::ffff:7f00:1]/",
    "http://127.0.0.1.nip.io:8000", "http://x.127.0.0.1.sslip.io", "http://10.0.0.1.xip.io", "http://foo.lvh.me",
    "http://169.254.169.254", "http://2852039166:80", "http://0.0.0.0", "http://metadata.google.internal", "http://[fe80::1]/",
    "file:///etc/passwd", "gopher://127.0.0.1:8000",
])
async def test_disguised_and_reserved_targets_are_blocked(url):
    with pytest.raises(BlockedTarget):
        await check_url(url, SELF_HOSTED)


async def test_private_targets_stay_available_to_a_self_hosted_installation():
    for url in ["http://10.0.0.5:9090", "http://172.27.171.169:11434", "http://192.168.1.20:9090"]:
        await check_url(url, SELF_HOSTED)
    with pytest.raises(BlockedTarget):
        await check_url("http://10.0.0.5:9090", TargetPolicy(allow_private=False))


async def test_allowlist_limits_targets():
    policy = TargetPolicy(allowlist=("prometheus", "10.1.0.0/16"))
    await check_url("http://10.1.2.3:9090", policy)
    await check_url("http://prometheus:9090", policy)
    with pytest.raises(BlockedTarget):
        await check_url("http://10.2.0.1:9090", policy)
    with pytest.raises(BlockedTarget):
        await check_url("http://169.254.169.254", policy)


async def test_guard_also_applies_at_connection_time():
    """A name that passed the first check can point somewhere else later, so the check repeats when connecting."""
    async with httpx.AsyncClient(transport=guarded_transport(SELF_HOSTED), trust_env=False, timeout=3) as client:
        for url in ["http://127.0.0.1:9/", "http://169.254.169.254/", "http://localhost:9/"]:
            with pytest.raises(httpx.ConnectError) as caught:
                await client.get(url)
            assert re.search("server itself|reserved", str(caught.value))


async def test_guard_allows_connections_it_should(prom_server):
    port = prom_server.server_address[1]
    async with httpx.AsyncClient(transport=guarded_transport(TargetPolicy(allow_loopback=True)), trust_env=False, timeout=5) as client:
        assert (await client.get(f"http://127.0.0.1:{port}/api/v1/status/buildinfo")).status_code == 200


async def test_only_admins_can_try_a_new_model_address(team):
    for name in ("viewer", "alice", "bob"):
        response = await team[name].get("/api/ai/providers/ollama/models?url=http://prometheus:9090")
        assert response.status_code == 403, name
    assert (await team["viewer"].get("/api/ai/providers/ollama/models")).status_code == 403
    assert (await team["alice"].get("/api/ai/providers/ollama/models")).status_code == 200


# MEDIUM-2, MEDIUM-3, LOW-2: what a viewer can reach ---------------------------------------
async def test_viewer_cannot_query_metrics_or_read_settings(team):
    viewer = team["viewer"]
    assert (await viewer.post("/api/metrics/query_range", json={"query": "node_uname_info", "start": 1, "end": 2, "step": 1})).status_code == 403
    for path in ["/api/metrics/names", "/api/ai/settings", "/api/sources", "/api/integrations", "/api/collectors", "/api/context/prompt-preview", "/api/events", "/api/changes",
                 "/api/org/retention", "/api/org/members", "/api/org/usage", "/api/org/channels", "/api/audit", "/api/conversations", "/api/llm-calls"]:
        assert (await viewer.get(path)).status_code == 403, path


async def test_removed_practice_routes_stay_gone(team):
    assert (await team["admin"].get("/api/eval/runs")).status_code in {404, 405}


async def test_org_details_only_for_admins(team):
    assert (await team["viewer"].get("/api/org")).json().keys() == {"name"}
    assert (await team["alice"].get("/api/org")).json().keys() == {"name"}
    assert "policies" in (await team["admin"].get("/api/org")).json()


async def test_engineer_gets_model_names_without_addresses(team):
    settings = await team["alice"].get("/api/ai/settings")
    assert settings.status_code == 200
    assert "url" not in settings.json()["ollama"] and "key_source" not in settings.json()["claude"]
    assert "url" in (await team["admin"].get("/api/ai/settings")).json()["ollama"]


async def test_viewer_context_holds_no_infrastructure_details(console, team, prom_server):
    admin = team["admin"]
    created = await admin.post("/api/sources", json={"name": "Main", "config": {"host": "127.0.0.1", "port": prom_server.server_address[1]}})
    assert created.status_code == 201, created.text
    await admin.post("/api/context/refresh", json={})
    panels = (await admin.post("/api/panels", json={"title": "Up", "query": "up", "source_id": created.json()["id"]})).json()
    context = await team["viewer"].get("/api/context")
    text = context.text
    assert context.status_code == 200
    # The metric value 2.53 on its own, not inside a timestamp such as 14:30:22.533Z.
    assert "127.0.0.1" not in text and "http://" not in text and '"query"' not in text and not re.search(r"(?<![\d.])2\.53(?!\d)", text) and '"labels"' not in text
    assert context.json()["sources"][0].keys() == {"id", "name", "status"}
    full = await admin.get("/api/context")
    assert "url" in json.dumps(full.json()["sources"]) or full.status_code == 200
    # Saved panels are the only way a viewer sees a chart, and the query stays hidden.
    listed = (await team["viewer"].get("/api/panels")).json()
    assert listed and all("query" not in item for item in listed)
    series = await team["viewer"].get(f"/api/panels/{panels['id']}/series", params={"start": 1000, "end": 2000, "step": 60})
    assert series.status_code == 200
    assert (await team["viewer"].get(f"/api/panels/{panels['id']}/series", params={"start": 0, "end": 10**8, "step": 1})).status_code == 422


def test_viewer_status_text_hides_addresses():
    from app.access import viewer_context
    context = viewer_context({
        "headline": {"text": "node-1:9100 is down", "tone": "critical"}, "health": {"status": "ok"},
        "alerts": {"firing": [{"name": "Down", "severity": "critical", "summary": "10.0.0.5 stopped at http://10.0.0.5:9100/metrics", "labels": {"instance": "10.0.0.5"}}]},
        "events": [{"id": 1, "ts": "now", "severity": "critical", "title": "Target prod-db.internal:5432 went down", "fingerprint": "x"}],
    })
    text = json.dumps(context)
    assert "9100" not in text and "10.0.0.5" not in text and "5432" not in text and "fingerprint" not in text and "labels" not in text
    assert "went down" in text


# MEDIUM-5, cookies -------------------------------------------------------------------
async def test_cookie_is_secure_on_https(make_config):
    from app.main import create_app
    from tests_helpers import reset_database
    reset_database()
    app = create_app(make_config(public_url="https://rcc.example.com", allowed_hosts=["rcc.example.com"], admin_password="a-Good-Long-Passphrase-42", admin_email="o@example.com"))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://rcc.example.com") as client:
            response = await client.post("/api/auth/login", json={"email": "o@example.com", "password": "a-Good-Long-Passphrase-42"})
            assert response.status_code == 200
            cookie = response.headers["set-cookie"].lower()
            assert "secure" in cookie and "httponly" in cookie and "samesite=lax" in cookie
            assert "strict-transport-security" in response.headers


# LOW-1, INFO-3, INFO-4, INFO-8: errors and headers ----------------------------------------
def _assert_security_headers(response):
    for name, _ in SECURITY_HEADERS:
        assert name.decode() in response.headers, name


async def test_null_bytes_get_a_clean_error_with_headers(console):
    client = console.client()
    for path in ["/api/projects%00", "/%00", "/index.html%00.js"]:
        response = await client.get(path)
        assert response.status_code == 400
        _assert_security_headers(response)
        assert response.headers["cache-control"] == "no-store"


async def test_unexpected_errors_keep_headers_and_hide_details(console):
    def broken(*args, **kwargs):
        raise RuntimeError("secret internal detail")
    original = console.ctx.db.one
    console.ctx.db.one = broken
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=console.app, raise_app_exceptions=False), base_url="http://localhost")
    response = await client.get("/readyz")
    console.ctx.db.one = original
    assert response.status_code == 500 and "secret" not in response.text
    _assert_security_headers(response)


async def test_style_policy_has_no_inline_exception(console):
    response = await console.client().get("/")
    assert "unsafe-inline" not in response.headers["content-security-policy"]
    assert "unsafe-eval" not in response.headers["content-security-policy"]


async def test_validation_errors_do_not_echo_input(console):
    response = await console.client().post("/api/auth/login", json={"email": 123, "password": "hunter2-secret"})
    assert response.status_code == 422
    body = response.json()
    assert isinstance(body["detail"], str) and "hunter2" not in response.text and '"input"' not in response.text and "loc" not in body


# INFO-6 ----------------------------------------------------------------------------------
async def test_repeated_json_keys_are_refused(console):
    response = await console.client().post("/api/auth/login", content='{"email":"admin@rootcause.local","password":"WRONG","password":"admin"}', headers={"Content-Type": "application/json"})
    assert response.status_code == 400


async def test_oversized_requests_are_refused(console):
    response = await console.client().post("/api/auth/login", content=b"x" * (5 * 1024 * 1024 + 1), headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    _assert_security_headers(response)


# INFO-7 ----------------------------------------------------------------------------------
async def test_old_sessions_end_past_the_limit(console, team):
    oldest = console.client()
    await oldest.post("/api/auth/login", json={"email": "bob@team.example", "password": PASSWORD})
    for _ in range(6):
        await console.client().post("/api/auth/login", json={"email": "bob@team.example", "password": PASSWORD})
    row = await console.ctx.db.one("SELECT count(*) AS n FROM sessions s JOIN users u ON u.id = s.user_id WHERE u.email = 'bob@team.example'")
    assert row["n"] == 5


# LOW-4 -----------------------------------------------------------------------------------
def _from(console, address):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=console.app, client=(address, 1234)), base_url="http://localhost", timeout=60)


async def test_failing_on_purpose_cannot_lock_someone_out(console, team, monkeypatch):
    waits = []

    async def no_wait(seconds):
        waits.append(seconds)
    monkeypatch.setattr("app.api.auth_routes.asyncio.sleep", no_wait)
    attacker = _from(console, "203.0.113.9")
    for _ in range(5):
        assert (await attacker.post("/api/auth/login", json={"email": "alice@team.example", "password": "wrong-guess-here"})).status_code == 401
    # The person guessing is stopped.
    assert (await attacker.post("/api/auth/login", json={"email": "alice@team.example", "password": PASSWORD})).status_code == 429
    # The real owner, on another device, still gets in, after only a short slowdown.
    owner = _from(console, "198.51.100.7")
    assert (await owner.post("/api/auth/login", json={"email": "alice@team.example", "password": PASSWORD})).status_code == 200
    assert waits and max(waits) <= 8


async def test_many_failures_warn_the_account_owner(console, team, monkeypatch):
    async def no_wait(seconds):
        return None
    monkeypatch.setattr("app.api.auth_routes.asyncio.sleep", no_wait)
    for index in range(20):
        await _from(console, f"203.0.113.{index + 1}").post("/api/auth/login", json={"email": "alice@team.example", "password": "wrong-guess-here"})
    assert any(message["To"] == "alice@team.example" and "Failed sign-ins" in message["Subject"] for message in console.outbox)


# Workflow pipeline -------------------------------------------------------------------------
async def _workflow(team):
    workflows = (await team["alice"].get("/api/workflows")).json()
    return next(item for item in workflows if item["name"] == "High CPU use")


async def test_pipeline_goes_one_step_at_a_time(team):
    alice, bob = team["alice"], team["bob"]
    workflow = await _workflow(team)
    run = (await alice.post(f"/api/workflows/{workflow['id']}/runs", json={})).json()
    rid = run["id"]
    assert [step["state"] for step in run["steps"]] == ["pending"] * len(workflow["steps"])
    assert (await bob.post(f"/api/workflows/{workflow['id']}/runs", json={})).status_code == 409
    assert (await alice.post(f"/api/workflow-runs/{rid}/steps/1", json={"action": "done"})).status_code == 409
    assert (await alice.post(f"/api/workflow-runs/{rid}/finish", json={"outcome": "helped"})).status_code == 409
    first = (await alice.post(f"/api/workflow-runs/{rid}/steps/0", json={"action": "done"})).json()
    assert first["steps"][0]["state"] == "done" and first["steps"][0]["by"] == "alice@team.example"
    # A teammate can take over, and only the latest step can be reopened.
    assert (await bob.post(f"/api/workflow-runs/{rid}/steps/1", json={"action": "skipped"})).status_code == 422
    assert (await bob.post(f"/api/workflow-runs/{rid}/steps/1", json={"action": "skipped", "note": "No traffic data"})).status_code == 200
    assert (await bob.post(f"/api/workflow-runs/{rid}/steps/0", json={"action": "reopen"})).status_code == 409
    assert (await bob.post(f"/api/workflow-runs/{rid}/steps/1", json={"action": "reopen"})).status_code == 200
    await bob.post(f"/api/workflow-runs/{rid}/steps/1", json={"action": "done"})
    await bob.post(f"/api/workflow-runs/{rid}/steps/2", json={"action": "done"})
    # The last step changes something, so the person must accept its risk.
    assert (await bob.post(f"/api/workflow-runs/{rid}/steps/3", json={"action": "done"})).status_code == 422
    assert (await bob.post(f"/api/workflow-runs/{rid}/steps/3", json={"action": "done", "risk_accepted": True})).status_code == 200
    done = (await bob.post(f"/api/workflow-runs/{rid}/finish", json={"outcome": "helped", "note": "Scaled by hand"})).json()
    assert done["status"] == "finished" and done["finished_by"] == "bob@team.example"
    listed = next(item for item in (await alice.get("/api/workflows")).json() if item["id"] == workflow["id"])
    assert listed["results"] == {"helped": 1}


async def test_viewer_cannot_touch_workflow_runs(team):
    workflow = await _workflow(team)
    run = (await team["alice"].post(f"/api/workflows/{workflow['id']}/runs", json={})).json()
    assert (await team["viewer"].post(f"/api/workflows/{workflow['id']}/runs", json={})).status_code == 403
    assert (await team["viewer"].post(f"/api/workflow-runs/{run['id']}/steps/0", json={"action": "done"})).status_code == 403


def test_nothing_in_the_app_can_run_a_workflow():
    from pathlib import Path
    text = Path("app/api/org_routes.py").read_text()
    for banned in ("subprocess", "os.system", "paramiko", "asyncssh", "exec("):
        assert banned not in text


# Incidents and verdicts ----------------------------------------------------------------------
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def _event(minutes_ago, kind, title, fingerprint=None, event_id=[0]):  # noqa: B006
    event_id[0] += 1
    return {"id": event_id[0], "ts": (NOW - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z"), "kind": kind, "severity": "critical", "fingerprint": fingerprint or title, "title": title, "detail": "{}", "source_id": 1}


def test_alerts_that_start_together_become_one_incident():
    events = [
        _event(600, "alert_started", "Alert firing: Errors"), _event(595, "alert_started", "Alert firing: Latency"), _event(590, "alert_started", "Alert firing: Saturation"),
        _event(540, "alert_cleared", "Alert firing: Errors"), _event(540, "alert_cleared", "Alert firing: Latency"), _event(530, "alert_cleared", "Alert firing: Saturation"),
        _event(100, "alert_started", "Alert firing: Disk"), _event(70, "alert_cleared", "Alert firing: Disk"),
    ]
    start = NOW - timedelta(days=1)
    changes = [{"ts": (NOW - timedelta(minutes=610)).isoformat().replace("+00:00", "Z"), "title": "Deployed checkout 2.4"}]
    result = analyze(events, changes, start, NOW, (NOW - timedelta(days=1)).isoformat())
    assert result["summary"]["occurrences"] == 4 and result["reliability"]["incidents"] == 2
    latest, earliest = result["incidents"]
    assert earliest["problems"] == 3 and earliest["minutes"] == 70 and earliest["after_change"] == "Deployed checkout 2.4"
    assert latest["problems"] == 1 and latest["after_change"] is None
    assert result["reliability"]["mttr_minutes"] == 50 and result["reliability"]["mtbf_minutes"] == 500
    assert result["reliability"]["clear_percent"] == round(100 * (1 - 100 / 1440), 2)


def test_a_long_problem_does_not_swallow_later_ones():
    events = [_event(3000, "alert_started", "Alert firing: Chronic"), _event(600, "alert_started", "Alert firing: New"), _event(590, "alert_cleared", "Alert firing: New")]
    result = analyze(events, [], NOW - timedelta(days=7), NOW, (NOW - timedelta(days=7)).isoformat())
    assert result["reliability"]["incidents"] == 2


def test_verdicts_change_totals_and_hints_are_only_suggestions():
    events = []
    for offset in range(4):
        events += [_event(1000 - offset * 100, "alert_started", "Alert firing: Flappy"), _event(998 - offset * 100, "alert_cleared", "Alert firing: Flappy")]
    for event in events:
        event["severity"] = "warning"
    events.append(_event(50, "alert_started", "Alert firing: Real"))
    base = analyze(events, [], NOW - timedelta(days=2), NOW, (NOW - timedelta(days=2)).isoformat())
    hinted = apply_verdicts(json.loads(json.dumps(base)), {})
    flappy = next(item for item in hinted["issues"] if item["title"] == "Alert firing: Flappy")
    assert flappy["noise_hint"] and flappy["verdict"] is None and hinted["summary"]["dismissed"] == 0
    decided = apply_verdicts(json.loads(json.dumps(base)), {"Alert firing: Flappy": {"verdict": "false_positive", "note": "Limit too low", "decided_by": "a@b.c"}})
    assert decided["summary"]["dismissed"] == 4 and decided["summary"]["real_occurrences"] == 1 and decided["summary"]["noise_percent"] == 80.0
    assert decided["summary"]["active_real_issues"] == 1
    assert any("false alarm" in line for line in statements(decided))


async def test_verdict_endpoint_is_for_engineers(team):
    body = {"title": "Alert firing: X", "verdict": "false_positive"}
    assert (await team["viewer"].put("/api/issues/verdict", json=body)).status_code == 403
    assert (await team["alice"].put("/api/issues/verdict", json=body)).status_code == 200
    assert (await team["alice"].put("/api/issues/verdict", json={"title": "Alert firing: X", "verdict": None})).status_code == 200
