"""The documentation space: its own page, content only for people who are signed in, and links that resolve."""
import re

import pytest

from app.api.docs_routes import SECTIONS, _pages
from app.security import SECURITY_HEADERS

from test_security import _invite


@pytest.fixture
async def viewer(console):
    admin = await console.admin()
    client = await _invite(console, admin, "reader@docs.example", "viewer")
    yield client
    await client.aclose()
    await admin.aclose()


async def test_docs_page_is_its_own_space_with_the_security_headers(console):
    client = console.client()
    for path in ("/docs", "/docs/loki", "/docs/does-not-exist"):
        response = await client.get(path)
        assert response.status_code == 200
        assert 'href="/"' in response.text and "/js/docs.js" in response.text, "the logo leads back to the platform"
        assert "script-src 'self'" in response.headers["content-security-policy"]
    assert SECURITY_HEADERS


async def test_docs_content_needs_a_sign_in(console):
    client = console.client()
    for path in ("/api/docs", "/api/docs/introduction", "/api/docs/search?q=loki"):
        assert (await client.get(path)).status_code == 401


async def test_viewers_can_read_and_search_the_docs(viewer):
    listed = (await viewer.get("/api/docs")).json()
    slugs = [page["slug"] for section in listed["sections"] for page in section["pages"]]
    assert slugs[0] == "introduction" and {"loki", "kubernetes", "deploy-aks", "single-sign-on", "accuracy-benchmark"} <= set(slugs)

    page = (await viewer.get("/api/docs/loki")).json()
    assert page["title"] == "Loki" and page["section"] == "Connections"
    assert 'namespace=~".+", namespace!="kube-system"' in page["markdown"]
    assert page["prev"]["slug"] == "prometheus" and page["next"]["slug"] == "kubernetes"

    found = (await viewer.get("/api/docs/search?q=empty-compatible")).json()["results"]
    assert found and found[0]["slug"] in {"loki", "troubleshooting"}
    assert (await viewer.get("/api/docs/search?q=x")).json() == {"results": []}


async def test_unknown_and_unsafe_page_names_are_refused(viewer):
    for slug in ("nope", "..%2F..%2Fconfig", "LOKI", "a" * 61):
        assert (await viewer.get(f"/api/docs/{slug}")).status_code == 404


def test_every_page_exists_and_every_link_resolves():
    pages = _pages()
    assert len(pages) == sum(len(slugs) for _, slugs in SECTIONS)
    anchors = {}
    for slug, page in pages.items():
        used = set()
        for match in re.finditer(r"^(?:##|###) (.+)$", re.sub(r"```.*?```", "", page["markdown"], flags=re.S), flags=re.M):
            text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", re.sub(r"[`*_]", "", match.group(1)))
            anchor = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
            while anchor in used:
                anchor += "-"
            used.add(anchor)
        anchors[slug] = used
    for slug, page in pages.items():
        for link in re.findall(r"\]\((/docs/[^)\s]+|#[^)\s]+)\)", page["markdown"]):
            target, _, anchor = (slug + link).partition("#") if link.startswith("#") else link[6:].partition("#")
            assert target in pages, f"{slug} links to a missing page: {link}"
            assert not anchor or anchor in anchors[target], f"{slug} links to a missing heading: {link}"


def test_docs_never_contain_real_secrets():
    text = "\n".join(page["markdown"] for page in _pages().values())
    for pattern in (r"AIza[0-9A-Za-z_-]{30,}", r"sk-ant-[0-9A-Za-z_-]{20,}", r"AKIA[0-9A-Z]{16}", r"xox[bp]-[0-9A-Za-z-]{10,}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"):
        assert not re.search(pattern, text), pattern


async def test_loki_tenant_is_checked_before_it_becomes_a_header(console):
    client = await console.admin()
    body = {"kind": "loki", "name": "Loki", "config": {"url": "http://loki.example:3100", "tenant": "team-a\r\nX-Evil: 1"}, "secrets": {}}
    response = await client.post("/api/integrations", json=body)
    assert response.status_code == 422 and "tenant" in response.json()["detail"]
    body["config"]["tenant"] = "team-a"
    saved = await client.post("/api/integrations", json=body)
    assert saved.status_code in (200, 201), saved.text
    assert saved.json()["config"]["tenant"] == "team-a"


def test_deployment_files_default_to_a_private_server():
    import pathlib
    import subprocess

    import yaml

    root = pathlib.Path(__file__).resolve().parent.parent
    compose = yaml.safe_load((root / "docker-compose.yml").read_text())
    caddy_volumes = compose["services"]["caddy"]["volumes"]
    assert any("RCC_CADDYFILE:-./deploy/Caddyfile.selfsigned}" in volume for volume in caddy_volumes), \
        "a missing RCC_CADDYFILE must not fall back to Let's Encrypt over the internet"
    assert "ports" not in compose["services"]["console"] and "ports" not in compose["services"]["postgres"]

    service = [doc for doc in yaml.safe_load_all((root / "deploy/k8s/caddy.yaml").read_text()) if doc and doc["kind"] == "Service"][0]
    assert service["metadata"]["annotations"]["service.beta.kubernetes.io/azure-load-balancer-internal"] == "true"

    assert (root / "deploy/Caddyfile.azure-dns").read_text().count("dns azure") == 1
    assert subprocess.run(["sh", "-n", str(root / "deploy/setup-env.sh")]).returncode == 0


def test_ip_address_access_has_a_default_certificate():
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    # Clients send no server name to https://IP-ADDRESS, so Caddy must be told which certificate to use.
    for name in ("Caddyfile.selfsigned", "Caddyfile.internal"):
        assert "default_sni {$RCC_DEFAULT_SNI:localhost}" in (root / "deploy" / name).read_text()
    assert 'RCC_DEFAULT_SNI: "${RCC_DEFAULT_SNI:-localhost}"' in (root / "docker-compose.yml").read_text()
    assert "RCC_DEFAULT_SNI=$domain" in (root / "deploy/setup-env.sh").read_text()


async def test_removing_someone_can_delete_their_private_investigations(console):
    from test_security import _invite

    admin = await console.admin()
    engineer = await _invite(console, admin, "leaver@docs.example", "engineer")
    me = (await engineer.get("/api/auth/me")).json()
    db = console.ctx.db
    insert = "INSERT INTO conversations(org_id, created_by, owner_id, visibility, title, created_at, updated_at) VALUES (?, 'Leaver', ?, ?, ?, 'now', 'now')"
    private = await db.insert(insert, (me["org_id"], me["id"], "private", "mine"))
    shared = await db.insert(insert, (me["org_id"], me["id"], "team", "ours"))

    response = await admin.delete(f"/api/org/members/{me['id']}?delete_investigations=true")
    assert response.status_code == 204, response.text
    assert await db.one("SELECT id FROM conversations WHERE id = ?", (private,)) is None
    assert await db.one("SELECT id FROM conversations WHERE id = ?", (shared,)) is not None, "team work stays"
    entry = await db.one("SELECT detail FROM audit_log WHERE action = 'member_removed' ORDER BY id DESC LIMIT 1")
    assert '"private_investigations_deleted": 1' in entry["detail"]
    await engineer.aclose()
    await admin.aclose()


def test_alert_emails_say_why_they_arrived_and_how_to_stop_them():
    from app.config import Config
    from app.mailer import Mailer
    from app.notifications import ALERT_EMAIL_REASON

    reason = ALERT_EMAIL_REASON.format(company="Shop")
    notices = Mailer(Config())._notices("Shop", False, reason)
    assert notices[0] == reason and "To stop these emails" in reason


def test_the_software_is_licensed_and_credits_its_creator():
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    license_text = (root / "LICENSE").read_text()
    assert "All rights reserved" in license_text and "Aymane KENBOUCH" in license_text
    about = _pages()["about"]["markdown"]
    assert "aymanekenbouch@gmail.com" in about and "github.com/AymaneK24" in about


def test_an_external_database_replaces_the_bundled_one():
    import pathlib

    import yaml

    class ComposeLoader(yaml.SafeLoader):
        """Reads Compose's own tags, such as !reset, as plain values."""

    ComposeLoader.add_multi_constructor("!", lambda loader, tag, node: None)
    override = yaml.load((pathlib.Path(__file__).resolve().parent.parent / "deploy/compose.external-db.yml").read_text(), Loader=ComposeLoader)  # noqa: S506
    assert override["services"]["postgres"]["profiles"], "the bundled database must not start"
    assert "RCC_DATABASE_URL:?" in override["services"]["console"]["environment"]["RCC_DATABASE_URL"]
