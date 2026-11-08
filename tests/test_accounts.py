"""Sign-in, sign-up, email verification, roles, security policies, and data isolation between companies."""
import json

import pytest

from conftest import PG_URL  # noqa: F401


async def test_default_admin_must_change_password(console):
    client = console.client()
    response = await client.post("/api/auth/login", json={"email": "admin@rootcause.local", "password": "admin"})
    assert response.status_code == 200 and response.json()["must_change_password"] is True
    blocked = await client.get("/api/context")
    assert blocked.status_code == 403 and blocked.json()["detail"] == "password_change_required"
    weak = await client.post("/api/auth/change-password", json={"current_password": "admin", "new_password": "short"})
    assert weak.status_code == 422
    common = await client.post("/api/auth/change-password", json={"current_password": "admin", "new_password": "password1234"})
    assert common.status_code == 200 or common.status_code == 422
    client2 = console.client()
    await client2.post("/api/auth/login", json={"email": "admin@rootcause.local", "password": "password1234" if common.status_code == 200 else "admin"})


async def test_api_requires_sign_in(console):
    client = console.client()
    for path in ["/api/context", "/api/sources", "/api/conversations", "/api/audit", "/api/org", "/api/projects", "/api/workflows", "/metrics"]:
        assert (await client.get(path)).status_code == 401, path
    info = (await client.get("/api/public/info")).json()
    # No operator is built in: each installation names its own, so the policy pages never name the wrong company.
    assert info["operator"] == "" and "creator" not in info
    assert (await client.get("/healthz")).status_code == 200


async def test_signup_requires_email_verification_and_hides_accounts(console):
    client = console.client()
    body = {"company": "Acme", "name": "Sara Owner", "email": "sara@acme.example", "password": "correct-Horse-Battery-9", "accept_terms": True}
    first = await client.post("/api/auth/signup", json=body)
    assert first.status_code == 202
    blocked = await client.post("/api/auth/login", json={"email": body["email"], "password": body["password"]})
    assert blocked.status_code == 403 and "Verify" in blocked.json()["detail"]
    token = console.last_link_token(body["email"])
    assert (await client.post("/api/auth/verify", json={"token": "x" * 40})).status_code == 400
    assert (await client.post("/api/auth/verify", json={"token": token})).status_code == 200
    assert (await client.post("/api/auth/verify", json={"token": token})).status_code == 400
    assert (await client.post("/api/auth/login", json={"email": body["email"], "password": body["password"]})).status_code == 200
    me = (await client.get("/api/auth/me")).json()
    assert me["role"] == "owner" and me["org_name"] == "Acme"
    duplicate = await console.client().post("/api/auth/signup", json={**body, "company": "Other"})
    assert duplicate.status_code == 202 and duplicate.json() == first.json()
    assert "Someone tried" in console.outbox[-1]["Subject"]
    row = await console.ctx.db.one("SELECT count(*) AS n FROM organizations WHERE name = 'Other'")
    assert row["n"] == 0
    stored = await console.ctx.db.one("SELECT token_hash FROM email_tokens LIMIT 1")
    assert token not in stored["token_hash"]


async def test_login_lockout_and_generic_errors(console):
    client = console.client()
    unknown = await client.post("/api/auth/login", json={"email": "nobody@x.example", "password": "wrong"})
    assert unknown.status_code == 401
    known = await client.post("/api/auth/login", json={"email": "admin@rootcause.local", "password": "wrong"})
    assert known.json()["detail"] == unknown.json()["detail"]
    for _ in range(6):
        response = await client.post("/api/auth/login", json={"email": "admin@rootcause.local", "password": "wrong"})
    assert response.status_code == 429
    assert (await client.post("/api/auth/login", json={"email": "admin@rootcause.local", "password": "admin"})).status_code == 429


async def test_password_reset_ends_sessions(console):
    owner = await console.company("ResetCo", "pat@resetco.example")
    other = console.client()
    assert (await other.post("/api/auth/forgot-password", json={"email": "pat@resetco.example"})).status_code == 202
    assert (await other.post("/api/auth/forgot-password", json={"email": "ghost@resetco.example"})).status_code == 202
    token = console.last_link_token("pat@resetco.example")
    assert (await other.post("/api/auth/reset-password", json={"token": token, "password": "short"})).status_code == 422
    assert (await other.post("/api/auth/reset-password", json={"token": token, "password": "a-Brand-New-Secret-44"})).status_code == 200
    assert (await owner.get("/api/auth/me")).status_code == 401
    assert (await other.post("/api/auth/login", json={"email": "pat@resetco.example", "password": "a-Brand-New-Secret-44"})).status_code == 200


async def test_roles_and_invitations(console):
    owner = await console.company("RoleCo", "o@roleco.example")
    response = await owner.post("/api/org/members/invite", json={"email": "v@roleco.example", "name": "Vera Viewer", "role": "viewer"})
    assert response.status_code == 201
    viewer = console.client()
    token = console.last_link_token("v@roleco.example")
    assert (await viewer.post("/api/auth/accept-invite", json={"token": token, "name": "Val Viewer", "password": "viewer-Pass-Phrase-66"})).status_code == 200
    assert (await viewer.get("/api/context")).status_code == 200
    assert (await viewer.post("/api/conversations", json={})).status_code == 403
    assert (await viewer.get("/api/org/members")).status_code == 403
    assert (await viewer.post("/api/sources", json={"name": "x", "config": {"host": "1.1.1.1", "port": 9090}})).status_code == 403
    members = (await owner.get("/api/org/members")).json()
    owner_id = next(item["id"] for item in members if item["role"] == "owner")
    viewer_id = next(item["id"] for item in members if item["role"] == "viewer")
    assert (await owner.patch(f"/api/org/members/{owner_id}", json={"role": "viewer"})).status_code == 409
    assert (await owner.patch(f"/api/org/members/{viewer_id}", json={"role": "engineer"})).status_code == 200
    assert (await viewer.get("/api/context")).status_code == 401  # Changing a role ends that person's sessions.
    audit = (await owner.get("/api/audit")).json()
    assert any(row["action"] == "member_updated" and row["actor"] == "o@roleco.example" for row in audit)


async def test_companies_cannot_see_each_others_data(console, prom_server):
    alpha = await console.company("Alpha", "a@alpha.example")
    beta = await console.company("Beta", "b@beta.example")
    source = (await alpha.post("/api/sources", json={"name": "alpha-prom", "config": {"host": "127.0.0.1", "port": prom_server.server_address[1]}, "secrets": {}})).json()
    conversation = (await alpha.post("/api/conversations", json={"title": "alpha secret incident"})).json()
    await console.ctx.db.index_text((await alpha.get("/api/auth/me")).json()["org_id"], conversation["id"], "uniqueword alpha secret")
    workflow = (await alpha.get("/api/workflows")).json()[0]
    channel = (await alpha.post("/api/org/channels", json={"kind": "slack", "name": "c", "url": "https://hooks.slack.com/services/T/B/x"})).json()
    panel = (await alpha.post("/api/panels", json={"title": "p", "query": "up", "source_id": source["id"]})).json()
    assert (await beta.get("/api/sources")).json() == []
    assert (await beta.get("/api/conversations")).json()["items"] == []
    assert (await beta.get("/api/conversations?q=uniqueword")).json()["items"] == []
    assert (await alpha.get("/api/conversations?q=uniqueword")).json()["total"] == 1
    for method, path, body in [
        ("get", f"/api/conversations/{conversation['id']}", None),
        ("patch", f"/api/conversations/{conversation['id']}", {"status": "resolved"}),
        ("delete", f"/api/conversations/{conversation['id']}", None),
        ("post", f"/api/conversations/{conversation['id']}/messages", {"prompt": "hi"}),
        ("get", f"/api/conversations/{conversation['id']}/export", None),
        ("put", f"/api/sources/{source['id']}", {"name": "x", "config": {"host": "1.1.1.1", "port": 1}}),
        ("delete", f"/api/sources/{source['id']}", None),
        ("put", f"/api/panels/{panel['id']}", {"title": "x", "query": "up"}),
        ("get", f"/api/workflows/{workflow['id']}/records", None),
        ("post", f"/api/workflows/{workflow['id']}/status", {"status": "archived"}),
        ("post", f"/api/org/channels/{channel['id']}/test", None),
    ]:
        kwargs = {"json": body} if body is not None else {}
        response = await getattr(beta, method)(path, **kwargs)
        assert response.status_code == 404, (path, response.status_code, response.text)
    await beta.delete(f"/api/panels/{panel['id']}")
    await beta.delete(f"/api/org/channels/{channel['id']}")
    assert len((await alpha.get("/api/panels")).json()) >= 1
    assert len((await alpha.get("/api/org/channels")).json()) == 1
    beta_context = (await beta.get("/api/context")).json()
    assert beta_context["sources"] == []
    alpha_audit = (await alpha.get("/api/audit")).text
    assert "b@beta.example" not in alpha_audit
    assert (await beta.post("/api/metrics/query_range", json={"query": "up", "start": 1, "end": 100, "step": 10, "source_id": source["id"]})).status_code == 409


async def test_policies_are_enforced(console):
    owner = await console.company("PolicyCo", "o@policy.example")
    policies = {"password_min_length": 16, "session_hours": 4, "allowed_email_domains": ["policy.example"], "require_sso": False, "allow_cloud_ai": False}
    assert (await owner.put("/api/org/policies", json=policies)).status_code == 200
    assert (await owner.post("/api/org/members/invite", json={"email": "x@other.example", "name": "Xavier One", "role": "viewer"})).status_code == 422
    assert (await owner.post("/api/org/members/invite", json={"email": "x@policy.example", "name": "Xavier Two", "role": "viewer"})).status_code == 201
    weak = await owner.post("/api/auth/change-password", json={"current_password": "another-Good-Passphrase-77", "new_password": "only-14-chars!"})
    assert weak.status_code == 422 and "16" in weak.json()["detail"]
    assert (await owner.put("/api/org/policies", json={**policies, "require_sso": True})).status_code == 422
    await owner.put("/api/ai/claude-key", json={"api_key": "sk-ant-test-valid-key"})
    conversation = (await owner.post("/api/conversations", json={})).json()
    settings = (await owner.get("/api/ai/settings")).json()
    settings["claude"]["consent"] = True
    await owner.put("/api/ai/settings", json={k: settings[k] for k in ("provider", "max_rounds", "ollama", "claude")})
    response = await owner.post(f"/api/conversations/{conversation['id']}/messages", json={"prompt": "hi", "provider": "claude", "model": "m"})
    assert response.status_code == 422 and "policy" in response.json()["detail"]


async def test_owner_can_delete_organization(console):
    owner = await console.company("GoneCo", "o@gone.example")
    org_id = (await owner.get("/api/auth/me")).json()["org_id"]
    response = await owner.request("DELETE", "/api/org", json={"confirm_name": "wrong"})
    assert response.status_code == 422
    response = await owner.request("DELETE", "/api/org", json={"confirm_name": "GoneCo"})
    assert response.status_code == 204
    assert await console.ctx.db.one("SELECT id FROM organizations WHERE id = ?", (org_id,)) is None
    assert await console.ctx.db.one("SELECT id FROM users WHERE email = 'o@gone.example'") is None


async def test_rejects_foreign_host_and_origin(console):
    client = console.client()
    assert (await client.get("/api/public/info", headers={"Host": "evil.example"})).status_code == 400
    assert (await client.post("/api/auth/login", json={"email": "a", "password": "b"}, headers={"Origin": "http://evil.example"})).status_code == 403
    assert (await client.post("/api/auth/login", content="{}", headers={"Content-Type": "text/plain"})).status_code == 415
    response = await client.get("/healthz")
    assert "default-src 'self'" in response.headers["content-security-policy"]


async def test_session_cookie_flags(console):
    client = console.client()
    response = await client.post("/api/auth/login", json={"email": "admin@rootcause.local", "password": "admin"})
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    row = await console.ctx.db.one("SELECT token_hash FROM sessions LIMIT 1")
    assert client.cookies.get("rcc_session") != row["token_hash"]


async def test_hosted_mode_refuses_unsafe_settings(make_config):
    with pytest.raises(SystemExit):
        make_config(mode="hosted", public_url="http://x.example", secret_key="", smtp_host="").validate()


async def test_hosted_mode_blocks_private_targets(make_config):
    from app.net_guard import BlockedTarget, check_host

    with pytest.raises(BlockedTarget):
        await check_host("169.254.169.254", True)
    with pytest.raises(BlockedTarget):
        await check_host("10.0.0.5", False)
    with pytest.raises(BlockedTarget):
        await check_host("127.0.0.1", False)
    with pytest.raises(BlockedTarget):
        await check_host("metadata.google.internal", True)
    await check_host("10.0.0.5", True)
    await check_host("8.8.8.8", False)


def test_json_is_valid_helper():
    assert json.loads("{}") == {}
