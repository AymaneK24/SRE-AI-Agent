"""Read-only alerts to Slack, email, Google Chat, and Microsoft Teams. Nothing here can change a monitored system."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import Config
from prometheus_client import Counter

from .db import Database, utc_after, utcnow
from . import noise
from .mailer import MailError, Mailer, paragraph
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
# A message a channel didn't accept is tried again after each of these waits: about a day in all, then it is given up.
RETRY_AFTER_S = (30, 120, 600, 1800, 3600, 7200, 14400, 28800, 28800)
RETRY_POLL_S = 15
MAX_QUEUED_BYTES = 4_000_000
DELIVERIES = Counter("rcc_deliveries_total", "Alert messages by outcome", ["status"])


class DeliveryError(Exception):
    """A channel didn't take a message. `permanent` means trying again can't help, such as a deleted webhook."""

    def __init__(self, message: str, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


def _age_seconds(iso: str) -> float:
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds()
    except ValueError:
        return float("inf")


def _pack(message: dict[str, Any]) -> str:
    """A message as text for the retry queue. Charts and the PDF are kept unless that makes it too large."""
    def encode(value: Any) -> Any:
        if isinstance(value, (bytes, bytearray)):
            return {"__bytes__": base64.b64encode(bytes(value)).decode()}
        if isinstance(value, dict):
            return {key: encode(item) for key, item in value.items()}
        if isinstance(value, list):
            return [encode(item) for item in value]
        return value

    packed = json.dumps(encode(message), default=str)
    if len(packed) > MAX_QUEUED_BYTES:
        slim = {key: value for key, value in message.items() if key not in {"attachments", "images", "body_html"}}
        packed = json.dumps(encode(slim), default=str)
    return packed


def _unpack(text: str) -> dict[str, Any]:
    def decode(value: Any) -> Any:
        if isinstance(value, dict):
            if set(value) == {"__bytes__"}:
                return base64.b64decode(value["__bytes__"])
            return {key: decode(item) for key, item in value.items()}
        if isinstance(value, list):
            return [decode(item) for item in value]
        return value

    return decode(json.loads(text))


def _delayed(message: dict[str, Any], first_tried_at: str) -> dict[str, Any]:
    """Says plainly that the message is late, so nobody reads an old alert as a new one."""
    note = f"Delayed message. This channel didn't accept it at {first_tried_at[11:16]} UTC on {first_tried_at[:10]}, so it arrives late. Check the console for the current state."
    late = dict(message)
    late["subject"] = f"[DELAYED] {message.get('subject', '')}"[:200]
    late["text"] = f"{note}\n\n{message.get('text', '')}"
    if message.get("email_text"):
        late["email_text"] = f"{note}\n\n{message['email_text']}"
    if message.get("body_html"):
        late["body_html"] = paragraph(note) + message["body_html"]
    return late
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
        self.on_critical: Any = None
        self.engines: Any = None
        self._waiting: set[asyncio.Task[None]] = set()
        self.noise = noise.NoiseControl()

    def close(self) -> None:
        for task in self._waiting:
            task.cancel()

    # Which channel heard about which problem. Kept in the database, so a recovery still goes there after a restart.
    async def _sent_at(self, channel_id: int, fingerprint: str) -> str | None:
        row = await self.db.one("SELECT sent_at FROM alert_sent WHERE channel_id = ? AND fingerprint = ?", (channel_id, fingerprint))
        return row["sent_at"] if row else None

    async def _mark_sent(self, org_id: int, channel_id: int, fingerprints: list[str]) -> None:
        now = utcnow()
        for fingerprint in fingerprints:
            await self.db.execute(
                "INSERT INTO alert_sent(channel_id, fingerprint, org_id, sent_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(channel_id, fingerprint) DO UPDATE SET sent_at = excluded.sent_at", (channel_id, fingerprint, org_id, now))

    async def forget_sent(self, org_id: int, channel_id: int | None = None, fingerprint: str | None = None) -> None:
        """Forgets what was sent: one problem on one channel when it clears, or everything for an organization."""
        if channel_id is None:
            await self.db.execute("DELETE FROM alert_sent WHERE org_id = ?", (org_id,))
        else:
            await self.db.execute("DELETE FROM alert_sent WHERE channel_id = ? AND fingerprint = ?", (channel_id, fingerprint))

    async def handle_event(self, org_id: int, event: dict[str, Any], detail: dict[str, Any]) -> None:
        try:
            if event["kind"].endswith(("_cleared", "_recovered")):
                await self._dispatch(org_id, event, detail)
                return
            settings = await noise.load(self.db, org_id, self.config)
            reason = await self._gate(org_id, event, settings)
            if reason:
                await self._suppressed(org_id, event, reason)
                await self._handled([event])
                return
            if not self.noise.join(org_id, event, detail):
                await self._suppressed(org_id, event, "grouped")
                return  # Another problem opened a group a moment ago; this one goes out with it, and is handled then.
            await noise.sleep(float(settings["group_window_s"]))
            items = self.noise.close(org_id)
        except Exception:  # Notification failures must not affect monitoring.
            log.exception("notification failed")
            return
        if len(items) <= 1:
            await self._send_one(org_id, event, detail, settings)
        else:
            await self._send_group(org_id, items, settings)
        await self._handled([item for item, _ in items] or [event])

    async def _handled(self, events: list[dict[str, Any]]) -> None:
        """Records that these starts were sent or deliberately held back. Until then, a restart sends them again."""
        ids = [int(event["id"]) for event in events if event.get("id")]
        try:
            for event_id in ids:
                await self.db.execute("UPDATE events SET handled = 1 WHERE id = ?", (event_id,))
        except Exception:  # Worst case, the message is sent once more after a restart.
            log.exception("couldn't mark an alert as handled")

    async def _gate(self, org_id: int, event: dict[str, Any], settings: dict[str, Any]) -> str | None:
        """Why this start shouldn't be sent, or None. Waits out the grace period."""
        if settings["respect_verdicts"]:
            title = event["title"].removeprefix("Already active when the console connected. ")
            verdict = await self.db.one("SELECT verdict FROM issue_verdicts WHERE org_id = ? AND issue_key = ? AND verdict IN ('false_positive', 'expected')",
                                        (org_id, title))
            if verdict:
                return "muted"
        if settings["flapping"]:
            state = self.noise.flapping(org_id, event["fingerprint"])
            if state == "quiet":
                return "flapping"
            if state == "first":
                flap = {**event, "severity": "warning", "title": f"{event['title']} keeps starting and stopping",
                        "fingerprint": f"flapping:{event['fingerprint']}"}
                await self._dispatch(org_id, flap, {"summary": f"It started {noise.FLAP_STARTS} times or more in the last hour. Further changes won't be sent until it has been stable for {noise.FLAP_QUIET_S // 60} minutes."}, settings=settings)
                return "flapping"
        # An alert pushed by Alertmanager already waited out its own "for" period there, and the console
        # has no state of its own to check it against, so it isn't made to wait again.
        if event["kind"] == "alertmanager_alert":
            return None
        grace = float(settings["grace_critical_s"] if event["severity"] == "critical" else settings["grace_warning_s"])
        if grace > 0:
            await noise.sleep(grace)
            engine = await self.engines.get(org_id) if self.engines else None
            if engine is not None and not engine.still_active(event["fingerprint"]):
                return "blip"
        return None

    async def _suppressed(self, org_id: int, event: dict[str, Any], reason: str) -> None:
        await self.db.insert(
            "INSERT INTO deliveries(org_id, channel_id, fingerprint, title, status, error, created_at) VALUES (?, NULL, ?, ?, ?, '', ?)",
            (org_id, event["fingerprint"], event["title"][:300], f"suppressed:{reason}", utcnow()))

    async def _send_one(self, org_id: int, event: dict[str, Any], detail: dict[str, Any], settings: dict[str, Any]) -> None:
        prepared = None
        try:
            if self.on_critical and event["severity"] == "critical":
                prepared = await self.on_critical(org_id, event, detail)
            await self._dispatch(org_id, event, detail, prepared[0] if prepared else None, settings=settings)
        except Exception:  # Notification failures must not affect monitoring.
            log.exception("notification failed")
        if prepared:
            try:
                prepared[1]()
            except Exception:
                log.exception("automatic investigation couldn't start")
                await self.release_held(prepared[0], "The automatic investigation couldn't start, so this message has only the alert.", final=True)

    async def _send_group(self, org_id: int, items: list[tuple[dict[str, Any], dict[str, Any]]], settings: dict[str, Any]) -> None:
        """Problems that started together: one message, and one automatic investigation that looks at all of them."""
        items.sort(key=lambda pair: -SEVERITY_RANK.get(pair[0]["severity"], 0))
        worst = items[0][0]
        changes = noise.changes_since(await self.db.all("SELECT ts, title FROM change_events WHERE org_id = ? ORDER BY ts DESC LIMIT 20", (org_id,)))
        hints = noise.common_causes(items, changes)
        prefix = "Already active when the console connected. "
        at_connect = all(item["title"].startswith(prefix) for item, _ in items)
        lines = [f"- {item['title'].removeprefix(prefix)}" for item, _ in items[:12]] + ([f"- and {len(items) - 12} more"] if len(items) > 12 else [])
        if at_connect:
            hints = ["They were already running when the console connected, so their start time is unknown.", *hints]
        summary = "\n".join(lines + ([""] + hints if hints else []))
        together = "problems found when the console connected" if at_connect else "problems started together"
        event = {**worst, "title": f"{len(items)} {together}: {worst['title'].removeprefix(prefix)}" + (" and others" if len(items) > 1 else ""),
                 "fingerprint": f"group:{worst['fingerprint']}", "group": [item["fingerprint"] for item, _ in items]}
        detail = {"summary": summary, "group": [{"title": item["title"], "fingerprint": item["fingerprint"]} for item, _ in items], "hints": hints}
        prepared = None
        try:
            if self.on_critical and event["severity"] == "critical":
                prepared = await self.on_critical(org_id, event, detail)
            await self._dispatch(org_id, event, detail, prepared[0] if prepared else None, settings=settings, members=[item["fingerprint"] for item, _ in items])
        except Exception:
            log.exception("notification failed")
        if prepared:
            try:
                prepared[1]()
            except Exception:
                log.exception("automatic investigation couldn't start")
                await self.release_held(prepared[0], "The automatic investigation couldn't start, so this message has only the alert.", final=True)

    async def _dispatch(self, org_id: int, event: dict[str, Any], detail: dict[str, Any], investigation_id: int | None = None,
                        settings: dict[str, Any] | None = None, members: list[str] | None = None) -> None:
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
                if not await self._sent_at(channel["id"], event["fingerprint"]):
                    continue
                await self.forget_sent(org_id, channel["id"], event["fingerprint"])
                # This email hasn't gone out yet, and when it does it will say the alert cleared.
                if waiting and channel["kind"] == "email":
                    continue
            else:
                if SEVERITY_RANK.get(event["severity"], 0) < SEVERITY_RANK.get(channel["min_severity"], 1):
                    continue
                sent_at = await self._sent_at(channel["id"], event["fingerprint"])
                if sent_at and _age_seconds(sent_at) < COOLDOWN_S:
                    continue
                if settings and not self.noise.allow(channel["id"], int(settings["max_per_hour"])):
                    self._overflow(org_id, channel, event)
                    continue
                # A grouped message stands for each problem in it, so each one's recovery goes to this channel too.
                await self._mark_sent(org_id, channel["id"], [event["fingerprint"], *(members or [])])
                # Email waits so it can carry the findings. Chat channels hear about the alert straight away.
                if investigation_id is not None and channel["kind"] == "email":
                    held_ids.append(channel["id"])
                    continue
            await self.deliver(channel, org_id, event["fingerprint"], message)
        if held_ids and investigation_id is not None:
            await self._hold(org_id, investigation_id, event, detail, message, held_ids)

    def _overflow(self, org_id: int, channel: dict[str, Any], event: dict[str, Any]) -> None:
        """Over the channel's hourly limit: keep the title for one summary when the hour is up."""
        waiting = self.noise.overflow.setdefault(channel["id"], [])
        waiting.append(event["title"])
        asyncio.get_running_loop().create_task(self._suppressed(org_id, event, "rate_limit"))
        if len(waiting) == 1:
            task = asyncio.create_task(self._send_overflow(org_id, channel["id"]))
            self._waiting.add(task)
            task.add_done_callback(self._waiting.discard)

    async def _send_overflow(self, org_id: int, channel_id: int) -> None:
        await asyncio.sleep(self.noise.seconds_until_room(channel_id))
        titles = self.noise.overflow.pop(channel_id, [])
        channel = await self.db.one("SELECT * FROM channels WHERE id = ? AND org_id = ? AND enabled = 1", (channel_id, org_id))
        if not titles or not channel:
            return
        org = await self.db.one("SELECT name FROM organizations WHERE id = ?", (org_id,))
        event = {"severity": "warning", "title": f"{len(titles)} more problems started in the last hour", "ts": utcnow(), "fingerprint": f"digest:{channel_id}"}
        summary = "This channel reached its hourly limit, so these were held back:\n" + "\n".join(f"- {title}" for title in titles[:30])
        await self.deliver(channel, org_id, event["fingerprint"], _message(org["name"], event, summary, [], f"{self.config.public_url}/#/overview"))

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

    async def deliver(self, channel: dict[str, Any], org_id: int, fingerprint: str, message: dict[str, Any], retry: bool = True) -> dict[str, Any]:
        """Sends now, with three quick tries. If the channel still doesn't take it, the message waits in the queue and is tried again later."""
        status, error = "sent", ""
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                await self._send(channel, message)
                last_error = None
                break
            except (httpx.HTTPError, MailError, DeliveryError, ValueError) as caught:
                last_error = caught
                if getattr(caught, "permanent", False):
                    break
                await asyncio.sleep(attempt + 0.2)
        if last_error is not None:
            error = scrub(str(last_error))[:300]
            status = "retrying" if retry and not getattr(last_error, "permanent", False) else "failed"
        delivery_id = await self.db.insert(
            "INSERT INTO deliveries(org_id, channel_id, fingerprint, title, status, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (org_id, channel["id"], fingerprint, str(message["subject"])[:300], status, error, utcnow()),
        )
        if status == "retrying":
            now = utcnow()
            await self.db.insert(
                "INSERT INTO delivery_queue(org_id, channel_id, delivery_id, fingerprint, message, attempts, first_tried_at, next_try_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                (org_id, channel["id"], delivery_id, fingerprint, _pack(message), now, utc_after(seconds=RETRY_AFTER_S[0])))
        DELIVERIES.labels(status).inc()
        return {"status": status, "error": error}

    async def retry_due(self) -> int:
        """Tries again every queued message whose wait is over. Returns how many were delivered."""
        delivered = 0
        rows = await self.db.all("SELECT * FROM delivery_queue WHERE next_try_at <= ? ORDER BY id LIMIT 25", (utcnow(),))
        for row in rows:
            channel = await self.db.one("SELECT * FROM channels WHERE id = ? AND org_id = ? AND enabled = 1", (row["channel_id"], row["org_id"]))
            if not channel:
                await self._retry_ended(row, "failed", "The channel was removed or paused before the message could be delivered.")
                continue
            attempts = int(row["attempts"]) + 1
            try:
                await self._send(channel, _delayed(_unpack(row["message"]), row["first_tried_at"]))
            except (httpx.HTTPError, MailError, DeliveryError, ValueError) as caught:
                error = scrub(str(caught))[:300]
                if getattr(caught, "permanent", False) or attempts >= len(RETRY_AFTER_S):
                    await self._retry_ended(row, "failed", f"Given up after {attempts + 1} tries. {error}"[:300])
                else:
                    await self.db.execute("UPDATE delivery_queue SET attempts = ?, next_try_at = ? WHERE id = ?",
                                          (attempts, utc_after(seconds=RETRY_AFTER_S[attempts]), row["id"]))
                    await self.db.execute("UPDATE deliveries SET error = ? WHERE id = ?", (error, row["delivery_id"]))
                continue
            except Exception as caught:  # A message that can't even be rebuilt is given up, not retried forever.
                log.exception("queued message couldn't be sent")
                await self._retry_ended(row, "failed", scrub(f"The queued message couldn't be sent: {caught}")[:300])
                continue
            await self._retry_ended(row, "sent", f"Delivered late, after {attempts + 1} tries.")
            delivered += 1
        return delivered

    async def _retry_ended(self, row: dict[str, Any], status: str, note: str) -> None:
        await self.db.execute("UPDATE deliveries SET status = ?, error = ? WHERE id = ?", (status, note, row["delivery_id"]))
        await self.db.execute("DELETE FROM delivery_queue WHERE id = ?", (row["id"],))
        DELIVERIES.labels(status).inc()

    async def retry_loop(self) -> None:
        while True:
            try:
                await self.retry_due()
            except Exception:  # The queue is tried again on the next round.
                log.exception("retrying queued messages failed")
            await asyncio.sleep(RETRY_POLL_S)

    async def _send(self, channel: dict[str, Any], message: dict[str, str]) -> None:
        try:
            settings = self.box.decrypt_json(channel["config_enc"])
        except ValueError as error:
            raise DeliveryError(str(error), permanent=True) from error
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
            raise DeliveryError(f"Unknown channel type: {kind}", permanent=True)
        async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False, transport=guarded_transport(self.config.target_policy)) as client:
            response = await client.post(settings["url"], content=json.dumps(payload), headers={"Content-Type": "application/json"})
        if response.status_code >= 300:
            # A refused address or payload stays refused. Busy or failing services are worth another try.
            permanent = 400 <= response.status_code < 500 and response.status_code not in {408, 425, 429}
            raise DeliveryError(f"The service returned HTTP {response.status_code}.", permanent=permanent)

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
            # Charts and the PDF are drawn off the main loop, so polling and live pages don't pause meanwhile.
            message.update(await asyncio.to_thread(build_email, facts, sections, evidence, workflows))
            message["email_text"] = message["text"]
            pdf = await asyncio.to_thread(build_pdf, facts, sections, evidence, workflows)
            message["attachments"] = [{"filename": f"incident-report-{conversation_id}.pdf", "data": pdf, "mime": "application/pdf"}]
        except Exception:
            # The findings still go out as text if charts or the PDF can't be made.
            log.exception("couldn't build the full investigation email")
            message.pop("images", None)
            message.pop("body_html", None)
            message["text"] = message["email_text"] = scrub(f"Confidence after checks: {confidence}\n\n{answer[:6000]}\n\nThis is AI advice, so verify it before acting. {READ_ONLY_NOTE}")
        return message
