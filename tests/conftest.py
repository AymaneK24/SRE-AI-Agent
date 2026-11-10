import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).parent))

from app.config import Config  # noqa: E402
from app.main import create_app  # noqa: E402

PG_URL = os.getenv("RCC_TEST_DATABASE_URL", "")


def _reset_postgres() -> None:
    import psycopg

    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


@pytest.fixture
def make_config(tmp_path):
    def build(**overrides):
        # Most tests create several companies, which also proves that their data stays apart if this mode is ever used.
        # Fake servers in these tests listen on this machine, which a real installation refuses as a target.
        values = {"data_dir": tmp_path, "public_url": "http://localhost", "database_url": PG_URL, "multi_company": True, "allow_loopback_targets": True}
        values.update(overrides)
        return Config(**values)
    return build


class Console:
    """A running app with helpers for signing in as different people."""

    def __init__(self, app):
        self.app = app
        self.ctx = app.state.ctx

    def client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://localhost", timeout=30)

    @property
    def outbox(self):
        return self.ctx.mailer.outbox

    def last_link_token(self, to):
        for message in reversed(self.outbox):
            if message["To"] == to:
                body = message.get_body(preferencelist=("plain",)).get_content()
                return body.split("token=")[1].split()[0]
        raise AssertionError(f"no email to {to}")

    async def admin(self):
        client = self.client()
        response = await client.post("/api/auth/login", json={"email": "admin@rootcause.local", "password": "admin"})
        assert response.status_code == 200, response.text
        response = await client.post("/api/auth/change-password", json={"current_password": "admin", "new_password": "a-Strong-Passphrase-2026"})
        assert response.status_code == 200, response.text
        return client

    async def company(self, name, email, password="another-Good-Passphrase-77"):
        client = self.client()
        response = await client.post("/api/auth/signup", json={"company": name, "name": "Owner Person", "email": email, "password": password, "accept_terms": True})
        assert response.status_code == 202, response.text
        response = await client.post("/api/auth/verify", json={"token": self.last_link_token(email)})
        assert response.status_code == 200, response.text
        response = await client.post("/api/auth/login", json={"email": email, "password": password})
        assert response.status_code == 200, response.text
        return client


@pytest_asyncio.fixture
async def console(make_config):
    if PG_URL:
        _reset_postgres()
    app = create_app(make_config())
    async with app.router.lifespan_context(app):
        yield Console(app)


class PromState:
    def __init__(self):
        self.down = False
        self.firing = False
        self.range_result = []
        self.unknown = False
        self.no_targets = False
        self.extra_alerts = []


class FakeProm(BaseHTTPRequestHandler):
    state = PromState()

    def log_message(self, *args):
        pass

    def _send(self, data):
        import json

        body = json.dumps({"status": "success", "data": data}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if "buildinfo" in self.path:
            self._send({"version": "2.53.4"})
        elif "/targets" in self.path:
            if self.state.no_targets:
                self._send({"activeTargets": []})
                return
            health = "unknown" if self.state.unknown else "down" if self.state.down else "up"
            self._send({"activeTargets": [{"labels": {"job": "api", "instance": "a:1"}, "health": health,
                                           "lastError": "connect refused password=hunter2" if self.state.down else "", "scrapeUrl": "http://a:1/metrics"}]})
        elif "/alerts" in self.path:
            alerts = [{"labels": {"alertname": "HighErrorRate", "severity": "critical"}, "annotations": {"summary": "Errors are high"},
                       "state": "firing", "activeAt": "2026-01-01T00:00:00Z", "value": "9"}] if self.state.firing else []
            self._send({"alerts": alerts + list(self.state.extra_alerts)})
        elif "/rules" in self.path:
            self._send({"groups": []})
        elif "__name__" in self.path:
            self._send(["up", "node_load5"])
        else:
            self._send({})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        if "query_range" in self.path:
            self._send({"resultType": "matrix", "result": self.state.range_result})
        else:
            self._send({"resultType": "vector", "result": [{"metric": {"job": "api"}, "value": [1, "42.5"]}]})


@pytest.fixture
def prom_server():
    FakeProm.state = PromState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeProm)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


@pytest.fixture(scope="session")
def fake_models():
    from fake_models import serve

    server = serve(0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
