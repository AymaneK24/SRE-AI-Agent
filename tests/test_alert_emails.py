"""Alert emails wait for the automatic investigation, carry its findings, and are never lost to it."""
import asyncio
import io
import json
import time
from pathlib import Path

import pytest
from conftest import FakeProm
from pypdf import PdfReader
from test_investigations import _company_with_source, _set_ollama, hook_server  # noqa: F401

from app.evidence_charts import draw_chart
from app.investigation_report import markdown_html, split_sections
from app.mailer import Mailer


def _body(message, kind):
    return message.get_body(preferencelist=(kind,)).get_content()


def _alert_emails(console, to="o@test.example"):
    return [message for message in console.outbox if message["To"] == to and "HighErrorRate" in message["Subject"]]


async def _setup(console, prom_server, fake_models, tools=True, wait=30.0):
    console.ctx.config.__dict__["alert_email_wait_s"] = wait
    client, org_id, engine = await _company_with_source(console, prom_server)
    await _set_ollama(client, fake_models, tools=tools)
    assert (await client.post("/api/org/channels", json={"kind": "email", "name": "mail", "recipients": ["o@test.example"], "min_severity": "critical"})).status_code == 201
    assert (await client.put("/api/org/auto-investigate", json={"enabled": True, "per_hour": 5})).status_code == 200
    return client, org_id, engine


async def _until(check, seconds=15.0):
    for _ in range(int(seconds * 10)):
        if check():
            return True
        await asyncio.sleep(0.1)
    return False


async def test_email_waits_and_carries_findings_charts_and_a_pdf(console, prom_server, fake_models, hook_server):  # noqa: F811
    client, org_id, engine = await _setup(console, prom_server, fake_models)
    await client.post("/api/org/channels", json={"kind": "slack", "name": "ops", "url": hook_server, "min_severity": "warning"})
    now = time.time()
    FakeProm.state.range_result = [{"metric": {"job": "api"}, "values": [[now - 600 + index * 60, str(index)] for index in range(10)]}]
    FakeProm.state.firing = True
    await engine.refresh_now()
    assert await _until(lambda: _alert_emails(console))
    emails = _alert_emails(console)
    assert len(emails) == 1, "one email, with the findings, not one before and one after"
    message = emails[0]
    assert message["Subject"].startswith("[CRITICAL]") and "investigation attached" in message["Subject"]
    html = _body(message, "html")
    assert "Likely root cause" in html and "Open the full investigation" in html and "Evidence" in html
    assert "cid:chart0" in html, "charts of the signals behind the alert"
    assert "never ask for your password" not in html, "an alert isn't about passwords"
    assert "Confidentiality" in html
    plain = _body(message, "plain")
    assert "Likely root cause" in plain and "never ask" not in plain
    # Inline images and the PDF are really attached.
    kinds = [part.get_content_type() for part in message.walk()]
    assert kinds.count("image/png") >= 2 and "application/pdf" in kinds
    pdf = next(part for part in message.walk() if part.get_content_type() == "application/pdf")
    assert pdf.get_filename().startswith("incident-report-")
    text = " ".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf.get_content())).pages)
    assert "Likely root cause" in text and "changed nothing" in text and "Confidential" in text
    # Chat channels don't wait.
    assert any("HighErrorRate" in item["text"] and "Read-only" in item["text"] for item in hook_server_messages())
    assert not (await console.ctx.db.all("SELECT * FROM held_alerts"))


def hook_server_messages():
    from test_investigations import Hook
    return Hook.received


async def test_a_failed_investigation_still_sends_the_alert(console, prom_server, fake_models):
    client, org_id, engine = await _setup(console, prom_server, "http://127.0.0.1:9")
    FakeProm.state.firing = True
    await engine.refresh_now()
    assert await _until(lambda: _alert_emails(console), 30)
    message = _alert_emails(console)[0]
    assert "only the alert" in _body(message, "plain") and "Error rate is high" in _body(message, "plain")
    assert not (await console.ctx.db.all("SELECT * FROM held_alerts"))


async def test_a_slow_investigation_doesnt_hold_the_alert(console, prom_server, fake_models, monkeypatch):
    client, org_id, engine = await _setup(console, prom_server, fake_models, wait=0.05)
    started = asyncio.Event()
    original = console.ctx.investigations.start

    def delayed(*args, **kwargs):
        async def later():
            await started.wait()
            original(*args, **kwargs)
        asyncio.create_task(later())
    monkeypatch.setattr(console.ctx.investigations, "start", delayed)
    FakeProm.state.firing = True
    await engine.refresh_now()
    assert await _until(lambda: _alert_emails(console))
    first = _alert_emails(console)[0]
    assert "will follow" in _body(first, "plain") and "application/pdf" not in [part.get_content_type() for part in first.walk()]
    # The findings then follow in their own email.
    started.set()
    assert await _until(lambda: len(_alert_emails(console)) == 2)
    second = _alert_emails(console)[1]
    assert second["Subject"].startswith("[INVESTIGATION]") and "application/pdf" in [part.get_content_type() for part in second.walk()]


async def test_waiting_alerts_go_out_after_a_restart(console, prom_server, fake_models):
    client, org_id, engine = await _setup(console, prom_server, fake_models)
    channel = (await client.get("/api/org/channels")).json()[0]
    notifier = console.ctx.notifier
    conversation = await console.ctx.db.insert("INSERT INTO conversations(org_id, created_by, trigger, title, visibility, created_at, updated_at) VALUES (?, 'automatic', 'auto', 't', 'team', 'now', 'now')", (org_id,))
    message = {"subject": "[CRITICAL] HighErrorRate", "text": "Alert details", "email_text": "Alert details"}
    await console.ctx.db.execute(
        "INSERT INTO held_alerts(conversation_id, org_id, fingerprint, message, event, detail, channel_ids, created_at) VALUES (?, ?, 'f', ?, '{}', '{}', ?, 'now')",
        (conversation, org_id, json.dumps(message), json.dumps([channel["id"]])))
    await notifier.release_after_restart()
    sent = _alert_emails(console)
    assert len(sent) == 1 and "restarted" in _body(sent[0], "plain")
    assert not (await console.ctx.db.all("SELECT * FROM held_alerts"))


async def test_alert_that_clears_while_waiting_says_so_once(console, prom_server, fake_models, monkeypatch):
    client, org_id, engine = await _setup(console, prom_server, fake_models)
    release = asyncio.Event()
    original = console.ctx.investigations.start

    def delayed(*args, **kwargs):
        async def later():
            await release.wait()
            original(*args, **kwargs)
        asyncio.create_task(later())
    monkeypatch.setattr(console.ctx.investigations, "start", delayed)
    FakeProm.state.firing = True
    await engine.refresh_now()
    await asyncio.sleep(0.2)
    FakeProm.state.firing = False
    await engine.refresh_now()
    await asyncio.sleep(0.2)
    assert not _alert_emails(console), "no recovery email for an alert nobody has heard about yet"
    release.set()
    assert await _until(lambda: _alert_emails(console))
    await asyncio.sleep(0.3)
    emails = _alert_emails(console)
    assert len(emails) == 1 and "cleared at" in _body(emails[0], "plain")


async def test_invitations_have_the_security_notice_and_the_new_layout(console):
    admin = await console.admin()
    assert (await admin.post("/api/org/members/invite", json={"email": "s@x.example", "name": "Sam Lee", "role": "viewer"})).status_code == 201
    message = next(item for item in console.outbox if item["To"] == "s@x.example")
    html = _body(message, "html")
    assert "never ask for your password" in html and "Hello Sam Lee" in html and "cid:rcc-logo" in html and "#17181c" in html and "An invitation from" in html


async def test_model_text_cant_inject_markup_into_an_email():
    html = markdown_html('## Heading\n- <img src=x onerror=alert(1)>\n[click](https://evil.example)\n**bold** `<code>`')
    assert "<img" not in html and "&lt;img" in html and "<a " not in html and "<b " in html and "&lt;code&gt;" in html


def test_sections_are_split_by_heading():
    parts = split_sections("## Summary\nA\n\n## Likely root cause\nB\nConfidence: Low\n")
    assert parts == {"summary": "A", "likely root cause": "B\nConfidence: Low"}
    assert split_sections("plain answer") == {"summary": "plain answer"}


def test_charts_are_png_and_handle_missing_data():
    points = [(1000.0 + index * 60, float(index % 5)) for index in range(30)]
    png = draw_chart([{"label": "job=api", "points": points}], "percent", limit=3.0, marker=1500.0)
    assert png and png.startswith(b"\x89PNG")
    assert draw_chart([{"label": "x", "points": [(1.0, 1.0)]}]) is None
    assert draw_chart([]) is None


async def test_attachments_keep_the_message_readable(make_config):
    mailer = Mailer(make_config())
    await mailer.send("a@x.example", "[CRITICAL] Disk full", "Body", attachments=[{"filename": "r.pdf", "data": b"%PDF-", "mime": "application/pdf"}], images={"chart0": b"\x89PNG"}, body_html="<p>Rich</p>")
    message = mailer.outbox[0]
    assert "Rich" in _body(message, "html") and _body(message, "plain").startswith("Body")
    assert ">Disk full<" in _body(message, "html"), "the header shows the title without the [CRITICAL] label"


def test_dockerfile_follows_the_hardening_rules():
    text = Path("Dockerfile").read_text()
    assert text.count("FROM ") == 2 and "AS build" in text
    assert "USER 10001:10001" in text and 'ENTRYPOINT ["python", "-m", "app"]' in text and "\nCMD " not in text
    assert 'CMD ["python", "-c"' in text, "the health check works with no shell"
    assert "--only-binary=:all:" in text and "pip uninstall" in text and "harden-image.py" in text


@pytest.mark.parametrize("name", ["subprocess", "os.system"])
def test_app_needs_no_shell(name):
    for path in Path("app").rglob("*.py"):
        assert name not in path.read_text(), f"{path} needs a shell, which the image doesn't have"
