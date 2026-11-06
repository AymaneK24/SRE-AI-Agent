"""Customer installations and the safety checks for a public deployment."""
import pytest

from app.config import Config
from app.main import create_app
from conftest import PG_URL, _reset_postgres


def test_public_deployment_refuses_unsafe_settings(tmp_path):
    safe = {
        "mode": "hosted", "data_dir": tmp_path, "public_url": "https://rcc.example.com", "secret_key": "x" * 32,
        "database_url": "postgresql://rcc@db/rcc", "smtp_host": "smtp.example.com", "smtp_from": "no-reply@example.com",
        "admin_password": "a-Good-Long-Passphrase-42", "admin_email": "owner@example.com", "allowed_hosts": ["rcc.example.com"],
    }
    Config(**safe).validate()
    for name, value in [("admin_password", ""), ("admin_email", ""), ("secret_key", ""), ("database_url", ""), ("public_url", "http://rcc.example.com")]:
        with pytest.raises(SystemExit):
            Config(**{**safe, name: value}).validate()


async def test_shared_service_limits_alert_recipients(make_config):
    from app.db import Database
    from app.notifications import validate_channel
    if PG_URL:
        _reset_postgres()
    config = make_config(mode="hosted")
    db = Database(config.db_path, config.database_url)
    org = await db.insert("INSERT INTO organizations(name, slug, contact_email, policies, created_at) VALUES ('A', 'a', '', '{}', '2026-01-01')")
    for address in ["me@gmail.com", "ops@acme.example"]:
        await db.insert("INSERT INTO users(org_id, username, email, name, role, email_verified_at, created_at) VALUES (?, ?, ?, 'n', 'owner', '2026-01-01', '2026-01-01')", (org, address, address))
    check = lambda *to: validate_channel(db, config, org, "email", {"recipients": list(to)})  # noqa: E731
    assert await check("me@gmail.com") is None
    assert await check("oncall@acme.example") is None
    assert "verified member" in await check("stranger@gmail.com")
    assert "verified member" in await check("someone@other.example")
    db.close()


async def test_one_company_cannot_reach_another_companys_records(console, prom_server):
    a = await console.company("Alpha Co", "a@alpha.example")
    b = await console.company("Beta Co", "b@beta.example")
    source = (await a.post("/api/sources", json={"kind": "prometheus", "name": "alpha-prom", "poll_seconds": 15, "config": {"scheme": "http", "host": "127.0.0.1", "port": prom_server.server_address[1]}, "secrets": {}})).json()
    panel = (await a.post("/api/panels", json={"title": "Alpha", "query": "up", "unit": "short", "source_id": source["id"]})).json()
    conversation = (await a.post("/api/conversations", json={"title": "Alpha secret"})).json()
    workflow = (await a.post("/api/workflows", json={"name": "Alpha flow", "summary": "s", "risk": "low", "match_rules": {}, "steps": [{"title": "t"}]})).json()
    channel = (await a.post("/api/org/channels", json={"kind": "slack", "name": "alpha", "url": "https://hooks.slack.com/services/x"})).json()
    a_me = (await a.get("/api/auth/me")).json()

    # Lists show nothing from the other company.
    for path in ["/api/sources", "/api/panels", "/api/conversations", "/api/org/channels", "/api/org/members", "/api/audit", "/api/llm-calls", "/api/events"]:
        response = await b.get(path)
        assert response.status_code == 200, path
        assert "alpha" not in response.text.lower(), path
    assert "alpha" not in (await b.get("/api/workflows")).text.lower()
    assert "alpha" not in (await b.get("/api/context")).text.lower()

    # Direct requests by ID are refused.
    for method, path, body in [
        ("GET", f"/api/conversations/{conversation['id']}", None),
        ("GET", f"/api/conversations/{conversation['id']}/export", None),
        ("POST", f"/api/conversations/{conversation['id']}/messages", {"prompt": "x", "provider": "ollama", "model": "m", "use_tools": False}),
        ("PATCH", f"/api/conversations/{conversation['id']}", {"status": "resolved"}),
        ("DELETE", f"/api/conversations/{conversation['id']}", None),
        ("PUT", f"/api/sources/{source['id']}", {"kind": "prometheus", "name": "stolen", "poll_seconds": 15, "config": {"scheme": "http", "host": "127.0.0.1", "port": prom_server.server_address[1]}, "secrets": {}}),
        ("DELETE", f"/api/sources/{source['id']}", None),
        ("POST", "/api/metrics/query_range", {"query": "up", "start": 1, "end": 2, "step": 1, "source_id": source["id"]}),
        ("PUT", f"/api/workflows/{workflow['id']}", {"name": "x", "summary": "s", "risk": "low", "match_rules": {}, "steps": [{"title": "t"}]}),
        ("GET", f"/api/workflows/{workflow['id']}/records", None),
        ("PATCH", f"/api/org/members/{a_me['id']}", {"role": "viewer"}),
        ("DELETE", f"/api/org/members/{a_me['id']}", None),
    ]:
        response = await b.request(method, path, json=body) if body is not None else await b.request(method, path)
        assert response.status_code in (403, 404, 409, 422), f"{method} {path} returned {response.status_code}: {response.text[:120]}"

    # Deleting another company's panel changes nothing, even though the request itself succeeds quietly.
    await b.delete(f"/api/panels/{panel['id']}")
    assert any(item["id"] == panel["id"] for item in (await a.get("/api/panels")).json())
    await b.delete(f"/api/org/channels/{channel['id']}")
    assert any(item["id"] == channel["id"] for item in (await a.get("/api/org/channels")).json())

    # The records are untouched.
    assert (await a.get(f"/api/conversations/{conversation['id']}")).status_code == 200
    assert (await a.get("/api/sources")).json()[0]["name"] == "alpha-prom"
    await a.aclose()
    await b.aclose()


async def test_single_company_installation(make_config, prom_server):
    import httpx
    if PG_URL:
        _reset_postgres()
    app = create_app(make_config(multi_company=False, company_name="Acme Payments", admin_email="owner@acme.example"))
    async with app.router.lifespan_context(app):
        outbox = app.state.ctx.mailer.outbox
        public = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost")
        info = (await public.get("/api/public/info")).json()
        assert info["single_company"] is True and info["signup_open"] is False and info["company"] == "Acme Payments"
        signup = await public.post("/api/auth/signup", json={"company": "Other", "name": "Eve", "email": "eve@other.example", "password": "a-Good-Long-Passphrase-42", "accept_terms": True})
        assert signup.status_code == 403
        assert (await public.get("/api/sources")).status_code == 401

        admin = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost")
        await admin.post("/api/auth/login", json={"email": "owner@acme.example", "password": "admin"})
        await admin.post("/api/auth/change-password", json={"current_password": "admin", "new_password": "a-Good-Long-Passphrase-42"})
        me = (await admin.get("/api/auth/me")).json()
        assert me["org_name"] == "Acme Payments" and me["role"] == "owner"
        assert (await admin.get("/api/org/license")).status_code == 404
        assert (await admin.request("DELETE", "/api/org", json={"confirm_name": "Acme Payments"})).status_code == 409

        # A colleague joins by invitation, in the same company, and sees the same data.
        assert (await admin.post("/api/org/members/invite", json={"email": "sam@gmail.com", "name": "Sam Engineer", "role": "engineer"})).status_code < 300
        token = outbox[-1].get_body(preferencelist=("plain",)).get_content().split("token=")[1].split()[0]
        sam = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost")
        assert (await sam.post("/api/auth/accept-invite", json={"token": token, "name": "Sam", "password": "Sams-Long-Passphrase-42"})).status_code == 200
        assert (await sam.get("/api/auth/me")).json()["org_name"] == "Acme Payments"
        assert (await sam.get("/api/org/members")).status_code == 403

        # Private systems can be connected, cloud metadata can't, and alert recipients aren't limited.
        body = {"kind": "prometheus", "name": "p", "poll_seconds": 15, "secrets": {}}
        ok = await admin.post("/api/sources/test", json={**body, "config": {"scheme": "http", "host": "127.0.0.1", "port": prom_server.server_address[1]}})
        assert ok.json()["ok"] is True
        blocked = await admin.post("/api/sources/test", json={**body, "config": {"scheme": "http", "host": "169.254.169.254", "port": 80}})
        assert blocked.json()["ok"] is False
        channel = await admin.post("/api/org/channels", json={"kind": "email", "name": "team", "recipients": ["oncall@gmail.com"]})
        assert channel.status_code < 300
        for client in (public, admin, sam):
            await client.aclose()


def test_production_mode_refuses_unsafe_settings(tmp_path):
    safe = {
        "mode": "production", "data_dir": tmp_path, "public_url": "https://rcc.example.com", "secret_key": "x" * 32,
        "database_url": "postgresql://rcc@db/rcc", "smtp_host": "smtp.example.com", "smtp_from": "no-reply@example.com",
        "admin_password": "a-Good-Long-Passphrase-42", "admin_email": "owner@example.com", "allowed_hosts": ["rcc.example.com"],
    }
    config = Config(**safe)
    config.validate()
    assert config.single_company and config.allow_private_targets
    for name, value in [("admin_password", ""), ("admin_email", ""), ("secret_key", ""), ("database_url", ""), ("public_url", "http://rcc.example.com")]:
        with pytest.raises(SystemExit):
            Config(**{**safe, name: value}).validate()
    Config(**{**safe, "smtp_host": ""}).validate()  # Internal networks may have no email server.


async def test_health_checks_work_by_ip_address_but_other_routes_dont(make_config):
    import httpx
    if PG_URL:
        _reset_postgres()
    app = create_app(make_config(allowed_hosts=["rcc.example.com"]))
    async with app.router.lifespan_context(app):
        probe = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://10.0.0.5:8000")
        assert (await probe.get("/healthz")).status_code == 200
        assert (await probe.get("/readyz")).status_code == 200
        assert (await probe.get("/api/public/info")).status_code == 400
        await probe.aclose()
