"""Read-only alerts to Slack, email, Google Chat, and Microsoft Teams. Nothing here can change a monitored system."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import Config
from .db import Database, utcnow
from .mailer import MailError, Mailer
from .net_guard import check_url, guarded_transport
from .investigation_report import build_email, build_pdf, collect_evidence, reviewed_workflows, split_sections
from .secrets_store import SecretBox, scrub
from .workflows import workflows_for

log = logging.getLogger("rcc.notify")

# Every alert email says why it arrived and how to stop it.
ALERT_EMAIL_REASON = ("You receive this because an administrator of {company} added your address to an alert email channel "
                      "in Root Cause Console. To stop these emails, ask them to remove it in Connections, Alerts.")

SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
COOLDOWN_S = 900
WEBHOOK_HOSTS = {
    "slack": ("hooks.slack.com",),
    "google_chat": ("chat.googleapis.com",),
    "teams": (".webhook.office.com", ".logic.azure.com", ".api.powerplatform.com", ".environment.api.powerplatform.com"),
}
READ_ONLY_NOTE = "Read-only notification. Root Cause Console didn't change anything."


async def validate_channel(db: Database, config: Config, org_id: int, kind: str, settings: dict[str, Any]) -> str | None:
    if kind == "email":
        recipients = settings.get("recipients") or []
        if not recipients or len(recipients) > 20:
            return "Enter between 1 and 20 email addresses."
        rows = await db.all("SELECT email FROM users WHERE org_id = ? AND email IS NOT NULL AND email_verified_at IS NOT NULL", (org_id,))
        # A shared mail provider's domain doesn't identify a company, so only exact addresses count there.
        domains = {row["email"].split("@")[-1] for row in rows} - FREE_MAIL_DOMAINS
        members = {row["email"] for row in rows}
        if config.is_hosted:
            # This stops the service being used to send email to strangers.
            for address in recipients:
                if address.lower() not in members and address.lower().split("@")[-1] not in domains:
                    return f"{address} isn't a verified member of your organization. Invite them first, or use your company's email domain."
        return None
    url = settings.get("url", "")
    parsed = urlparse(url)
    allowed = WEBHOOK_HOSTS.get(kind)
    if not allowed:
        return "Unknown channel type."
    host = parsed.hostname or ""
    official = parsed.scheme == "https" and any(host == item or (item.startswith(".") and host.endswith(item)) for item in allowed)
    if official:
        return None
    if config.is_hosted:
        return f"Enter the official webhook address for this service ({allowed[0].lstrip('.')})."
    try:
        await check_url(url, config.target_policy)
    except ValueError as error:
        return str(error)
    return None


FREE_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "yahoo.com", "yahoo.fr",
    "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com", "gmx.com", "mail.com", "zoho.com",
}


def _message(org_name: str, event: dict[str, Any], summary: str, workflows: list[dict[str, Any]], link: str) -> dict[str, str]:
    icon = {"critical": "CRITICAL", "warning": "WARNING", "info": "RESOLVED"}.get(event["severity"], "NOTICE")
    title = f"[{icon}] {event['title']}"
    lines = [title, f"Organization: {org_name}", f"Time: {event['ts']}"]
    if summary:
        lines.append(summary)
    for workflow in workflows[:2]:
        lines.append(f"\nSuggested workflow for a person to review: {workflow['name']} (risk: {workflow['risk']})")
        for index, step in enumerate(workflow["steps"][:6], start=1):
            lines.append(f"{index}. {step['title']}")
    lines += [f"\nOpen in the console: {link}", READ_ONLY_NOTE]
    status = {"critical": "Critical", "warning": "Warning", "info": "Resolved"}.get(event["severity"], "Notice")
    return {"subject": title, "text": scrub("\n".join(lines)), "email_text": scrub(_alert_email(org_name, event, summary, workflows)), "link": link,
            "tone": {"critical": "critical", "warning": "warning", "info": "resolved"}.get(event["severity"], "default"), "subtitle": f"{status} at {org_name}"}


def _alert_email(org_name: str, event: dict[str, Any], summary: str, workflows: list[dict[str, Any]]) -> str:
    """The same facts as the channel message, laid out for the email template, which adds the button."""
    status = {"critical": "Critical", "warning": "Warning", "info": "Resolved"}.get(event["severity"], "Notice")
    parts = [f"Status: {status}\nCompany: {org_name}\nTime: {str(event['ts'])[:16].replace('T', ' ')} UTC"]
    if summary:
        parts.append(summary)
    for workflow in workflows[:2]:
        steps = "\n".join(f"{index}. {step['title']}" for index, step in enumerate(workflow["steps"][:6], start=1))
        parts.append(f"Suggested workflow for a person to review: {workflow['name']} (risk: {workflow['risk']})\n{steps}")
    parts.append(READ_ONLY_NOTE)
    return "\n\n".join(parts)


def _escape_slack(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Notifier:
    def __init__(self, db: Database, box: SecretBox, config: Config, mailer: Mailer) -> None:
        self.db, self.box, self.config, self.mailer = db, box, config, mailer
        self._sent: dict[tuple[int, str], float] = {}
        self.on_critical: Any = None
        self.engines: Any = None
        self._waiting: set[asyncio.Task[None]] = set()

    def close(self) -> None:
        for task in self._waiting:
            task.cancel()

    async def handle_event(self, org_id: int, event: dict[str, Any], detail: dict[str, Any]) -> None:
        prepared = None
        try:
            if self.on_critical and event["severity"] == "critical":
                prepared = await self.on_critical(org_id, event, detail)
            await self._dispatch(org_id, event, detail, prepared[0] if prepared else None)
        except Exception:  # Notification failures must not affect monitoring.
            log.exception("notification failed")
        if prepared:
            try:
                prepared[1]()
            except Exception:
                log.exception("automatic investigation couldn't start")
                await self.release_held(prepared[0], "The automatic investigation couldn't start, so this message has only the alert.", final=True)

    async def _dispatch(self, org_id: int, event: dict[str, Any], detail: dict[str, Any], investigation_id: int | None = None) -> None:
        channels = await self.db.all("SELECT * FROM channels WHERE org_id = ? AND enabled = 1", (org_id,))
        if not channels:
            return
        org = await self.db.one("SELECT name FROM organizations WHERE id = ?", (org_id,))
        cleared = event["kind"].endswith(("_cleared", "_recovered"))
        names = [str(detail.get("name"))] if detail.get("name") else (["targetdown"] if event["kind"].startswith("target") else [])
        signals = [str(detail["signal_id"])] if detail.get("signal_id") else []
        workflows = [] if cleared else await workflows_for(self.db, org_id, alert_names=names, signal_ids=signals, text=event["title"])
        summary = str(detail.get("summary") or detail.get("error") or "")[:500]
        message = _message(org["name"], event, summary, workflows, f"{self.config.public_url}/#/overview")
        held_ids: list[int] = []
        waiting = None
        if cleared:
            waiting = await self.db.one("SELECT conversation_id FROM held_alerts WHERE org_id = ? AND fingerprint = ? AND released = 0", (org_id, event["fingerprint"]))
            if waiting:
                await self.db.execute("UPDATE held_alerts SET cleared_at = ? WHERE conversation_id = ?", (event["ts"], waiting["conversation_id"]))
        for channel in channels:
            if cleared:
                # Only report recovery on channels that heard about the problem.
                if (channel["id"], event["fingerprint"]) not in self._sent:
                    continue
                self._sent.pop((channel["id"], event["fingerprint"]), None)
                # This email hasn't gone out yet, and when it does it will say the alert cleared.
                if waiting and channel["kind"] == "email":
                    continue
            else:
                if SEVERITY_RANK.get(event["severity"], 0) < SEVERITY_RANK.get(channel["min_severity"], 1):
                    continue
                key = (channel["id"], event["fingerprint"])
                if time.monotonic() - self._sent.get(key, -COOLDOWN_S) < COOLDOWN_S:
                    continue
                self._sent[key] = time.monotonic()
                # Email waits so it can carry the findings. Chat channels hear about the alert straight away.
                if investigation_id is not None and channel["kind"] == "email":
                    held_ids.append(channel["id"])
                    continue
            await self.deliver(channel, org_id, event["fingerprint"], message)
        if held_ids and investigation_id is not None:
            await self._hold(org_id, investigation_id, event, detail, message, held_ids)

    async def _hold(self, org_id: int, conversation_id: int, event: dict[str, Any], detail: dict[str, Any], message: dict[str, Any], channel_ids: list[int]) -> None:
        await self.db.execute(
            "INSERT INTO held_alerts(conversation_id, org_id, fingerprint, message, event, detail, channel_ids, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (conversation_id, org_id, event["fingerprint"], json.dumps(message), json.dumps(event, default=str), json.dumps(detail, default=str), json.dumps(channel_ids), utcnow()))
        task = asyncio.create_task(self._release_after_wait(conversation_id))
        self._waiting.add(task)
        task.add_done_callback(self._waiting.discard)

    async def _release_after_wait(self, conversation_id: int) -> None:
        await asyncio.sleep(self.config.alert_email_wait_s)
        minutes = max(1, round(self.config.alert_email_wait_s / 60))
        await self.release_held(conversation_id, f"An automatic investigation has been running for {minutes} minutes. Its findings will follow in another email when it finishes.")

    async def release_held(self, conversation_id: int, note: str, final: bool = False) -> None:
        """Sends the alert without findings, because it must never be lost to a slow or failed investigation."""
        row = await self.db.one("SELECT * FROM held_alerts WHERE conversation_id = ?", (conversation_id,))
        if not row or row["released"]:
            if row and final:
                await self.db.execute("DELETE FROM held_alerts WHERE conversation_id = ?", (conversation_id,))
            return
        # With `final`, no findings can follow. Otherwise the row stays, so the findings can still go out with the alert's details.
        if final:
            await self.db.execute("DELETE FROM held_alerts WHERE conversation_id = ?", (conversation_id,))
        else:
            await self.db.execute("UPDATE held_alerts SET released = 1 WHERE conversation_id = ?", (conversation_id,))
        message = json.loads(row["message"])
        cleared = f"This alert cleared at {str(row['cleared_at'])[:16].replace('T', ' ')} UTC. " if row["cleared_at"] else ""
        message["email_text"] = f"{cleared}{note}\n\n{message.get('email_text', message['text'])}"
        for channel_id in json.loads(row["channel_ids"]):
            channel = await self.db.one("SELECT * FROM channels WHERE id = ? AND org_id = ? AND enabled = 1", (channel_id, row["org_id"]))
            if channel:
                await self.deliver(channel, row["org_id"], row["fingerprint"], message)

    async def release_after_restart(self) -> None:
        """A restart ends running investigations, so any alert still waiting goes out now."""
        for row in await self.db.all("SELECT conversation_id FROM held_alerts"):
            await self.release_held(row["conversation_id"], "The console restarted before the automatic investigation finished, so this message has only the alert.", final=True)

    async def deliver(self, channel: dict[str, Any], org_id: int, fingerprint: str, message: dict[str, str]) -> dict[str, Any]:
        status, error = "sent", ""
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                await self._send(channel, message)
                last_error = None
                break
            except (httpx.HTTPError, MailError, ValueError) as caught:
                last_error = caught
                await asyncio.sleep(attempt + 0.2)
        if last_error is not None:
            status, error = "failed", scrub(str(last_error))[:300]
        await self.db.insert(
            "INSERT INTO deliveries(org_id, channel_id, fingerprint, title, status, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (org_id, channel["id"], fingerprint, message["subject"][:300], status, error, utcnow()),
        )
        return {"status": status, "error": error}

    async def _send(self, channel: dict[str, Any], message: dict[str, str]) -> None:
        settings = self.box.decrypt_json(channel["config_enc"])
        kind = channel["kind"]
        if kind == "email":
            for address in settings.get("recipients", []):
                await self.mailer.send(address, message["subject"], message.get("email_text", message["text"]), message.get("link"), message.get("link_label", "Open in the console"),
                                       tone=message.get("tone", "default"), subtitle=message.get("subtitle", ""), title=message.get("title", ""),
                                       body_html=message.get("body_html", ""), images=message.get("images"), attachments=message.get("attachments"),
                                       reason=ALERT_EMAIL_REASON.format(company=self.mailer.config.company_name or "your organization"))
            return
        if kind == "slack":
            payload: dict[str, Any] = {"text": _escape_slack(message["text"])}
        elif kind == "google_chat":
            payload = {"text": message["text"]}
        elif kind == "teams":
            payload = {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard", "version": "1.4",
                "body": [{"type": "TextBlock", "text": message["text"], "wrap": True}]}}]}
        else:
            raise ValueError(f"Unknown channel type: {kind}")
        async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False, transport=guarded_transport(self.config.target_policy)) as client:
            response = await client.post(settings["url"], content=json.dumps(payload), headers={"Content-Type": "application/json"})
        if response.status_code >= 300:
            raise ValueError(f"The service returned HTTP {response.status_code}.")

    async def notify_investigation(self, org_id: int, conversation_id: int, title: str, answer: str, verification: dict[str, Any]) -> None:
        """Chat channels get a short summary. Email gets the findings, charts, commands, and a PDF, clearly labeled as advice to verify."""
        channels = await self.db.all("SELECT * FROM channels WHERE org_id = ? AND enabled = 1", (org_id,))
        held = await self.db.one("SELECT * FROM held_alerts WHERE conversation_id = ?", (conversation_id,))
        await self.db.execute("DELETE FROM held_alerts WHERE conversation_id = ?", (conversation_id,))
        if not channels:
            return
        lower = answer.lower()
        start = lower.find("## summary")
        excerpt = answer[start:start + 700] if start >= 0 else answer[:700]
        confidence = verification.get("confidence_final") or "Not stated"
        link = f"{self.config.public_url}/#/investigate/{conversation_id}"
        text = (f"AI investigation finished: {title}\nConfidence after checks: {confidence}\n\n{excerpt}\n\n"
                f"Open the full answer: {link}\n"
                f"This is AI advice, so verify it before acting. {READ_ONLY_NOTE}")
        message: dict[str, Any] = {"subject": f"[INVESTIGATION] {title}"[:200], "text": scrub(text)}
        emails = [channel for channel in channels if channel["kind"] == "email"]
        for channel in channels:
            if channel["kind"] != "email":
                await self.deliver(channel, org_id, f"investigation:{conversation_id}", message)
        if not emails:
            return
        rich = await self._investigation_email(org_id, conversation_id, title, answer, confidence, link, held)
        for channel in emails:
            # A waiting alert goes only where it would have gone. Otherwise, this is a follow-up for every email channel.
            if not held or held["released"] or channel["id"] in json.loads(held["channel_ids"]):
                await self.deliver(channel, org_id, f"investigation:{conversation_id}", rich)

    async def _investigation_email(self, org_id: int, conversation_id: int, title: str, answer: str, confidence: str, link: str, held: dict[str, Any] | None) -> dict[str, Any]:
        org = await self.db.one("SELECT name FROM organizations WHERE id = ?", (org_id,))
        conversation = await self.db.one("SELECT created_at FROM conversations WHERE id = ?", (conversation_id,))
        event = json.loads(held["event"]) if held else {"title": title, "ts": conversation["created_at"], "kind": "alert", "severity": "critical", "fingerprint": ""}
        detail = json.loads(held["detail"]) if held else {}
        cleared_at = held["cleared_at"] if held else None
        waiting_alert = bool(held and not held["released"])
        facts = {
            "title": event["title"], "ts": event["ts"], "company": self.config.company_name or org["name"], "confidence": str(confidence).capitalize(),
            "status": "Cleared" if cleared_at else "Critical, still firing when this was sent" if held else "Critical",
            "status_note": f"This alert cleared at {str(cleared_at)[:16].replace('T', ' ')} UTC, while the investigation was running." if cleared_at else "",
        }
        subject = f"[CRITICAL] {event['title']}: investigation attached" if waiting_alert else f"[INVESTIGATION] {event['title']}"
        message: dict[str, Any] = {"subject": subject[:200], "text": "", "link": link, "link_label": "Open the full investigation",
                                   "tone": "resolved" if cleared_at else "critical", "subtitle": f"Automatic investigation for {facts['company']}", "title": event["title"]}
        sections = split_sections(answer)
        try:
            engine = await self.engines.get(org_id)
            evidence = await collect_evidence(self.db, engine, conversation_id, event, detail)
            workflows = await reviewed_workflows(self.db, org_id, event, detail)
            message.update(build_email(facts, sections, evidence, workflows))
            message["email_text"] = message["text"]
            pdf = build_pdf(facts, sections, evidence, workflows)
            message["attachments"] = [{"filename": f"incident-report-{conversation_id}.pdf", "data": pdf, "mime": "application/pdf"}]
        except Exception:
            # The findings still go out as text if charts or the PDF can't be made.
            log.exception("couldn't build the full investigation email")
            message.pop("images", None)
            message.pop("body_html", None)
            message["text"] = message["email_text"] = scrub(f"Confidence after checks: {confidence}\n\n{answer[:6000]}\n\nThis is AI advice, so verify it before acting. {READ_ONLY_NOTE}")
        return message
