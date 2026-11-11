"""The console watches itself.

A monitoring tool that stops is silent, and silence looks like "all is well". Two things cover that:

1. A heartbeat. While the console is watching, it calls an address of the customer's choice every few
   minutes: a dead man's switch such as Healthchecks.io, Better Stack, Cronitor, or their own endpoint.
   When the calls stop, that service raises the alarm. The console can't do this part itself, because
   whatever stopped it, such as a lost database or a dead server, also stops its messages.
2. An alert on the normal channels when the console is running but can't complete its checks. That
   one is in the context engine (`console:polling`).

The heartbeat is sent only while this organization's polling loop is alive and its last check finished.
Sources that don't answer don't stop it: the console is working then, and says so on its own channels.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import Config
from .context.manager import EngineManager
from .db import Database, utcnow
from .net_guard import BlockedTarget, check_url, guarded_transport
from .secrets_store import SecretBox, scrub

log = logging.getLogger("rcc.heartbeat")
SETTING, STATE = "heartbeat", "heartbeat_state"
CHECK_EVERY_S = 20
DEFAULTS: dict[str, Any] = {"enabled": False, "url_enc": "", "every_minutes": 5}


async def load(db: Database, org_id: int) -> dict[str, Any]:
    return {**DEFAULTS, **(await db.get_setting(org_id, SETTING, {}) or {})}


class Heartbeat:
    def __init__(self, db: Database, box: SecretBox, config: Config, engines: EngineManager) -> None:
        self.db, self.box, self.config, self.engines = db, box, config, engines
        self._last: dict[int, float] = {}

    async def public(self, org_id: int) -> dict[str, Any]:
        """What the page shows. The address is a secret, so only its host is returned."""
        settings = await load(self.db, org_id)
        url = self.box.decrypt(settings["url_enc"]) if settings["url_enc"] else ""
        state = await self.db.get_setting(org_id, STATE, {}) or {}
        return {"enabled": bool(settings["enabled"]), "every_minutes": int(settings["every_minutes"]), "url_set": bool(url),
                "url_host": urlparse(url).hostname or "", "last_status": state.get("status", "never"), "last_at": state.get("at"),
                "last_ok_at": state.get("ok_at"), "last_error": state.get("error", "")}

    async def save(self, org_id: int, enabled: bool, url: str | None, every_minutes: int) -> None:
        settings = await load(self.db, org_id)
        if url:
            await check_url(url, self.config.target_policy)
            settings["url_enc"] = self.box.encrypt(url)
        if enabled and not settings["url_enc"]:
            raise ValueError("Enter the address to call.")
        settings.update(enabled=enabled, every_minutes=every_minutes)
        await self.db.set_setting(org_id, SETTING, settings)
        self._last.pop(org_id, None)  # The next round sends one straight away.

    async def watching(self, org_id: int) -> str:
        """Empty when this organization is being watched. Otherwise, why the heartbeat is held back."""
        engine = await self.engines.get(org_id)
        if engine.loop_ended:
            return "The polling loop ended."
        if engine.stalled:
            return "The polling loop is stuck."
        if engine.failed_polls_in_a_row:
            return "The last check couldn't be completed."
        return ""

    async def beat(self, org_id: int, test: bool = False) -> dict[str, Any]:
        """Sends one heartbeat if the console is watching. A test is sent either way, so the address can be checked."""
        settings = await load(self.db, org_id)
        previous = await self.db.get_setting(org_id, STATE, {}) or {}
        state: dict[str, Any] = {"at": utcnow(), "ok_at": previous.get("ok_at"), "error": ""}
        reason = "" if test else await self.watching(org_id)
        if reason:
            state.update(status="held", error=reason)
        else:
            try:
                url = self.box.decrypt(settings["url_enc"])
                if not url:
                    raise ValueError("No address is set.")
                async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False, transport=guarded_transport(self.config.target_policy)) as client:
                    response = await client.get(url, headers={"User-Agent": "root-cause-console-heartbeat"})
                if response.status_code >= 300:
                    raise ValueError(f"The address answered HTTP {response.status_code}.")
                state.update(status="ok", ok_at=state["at"])
            except (httpx.HTTPError, BlockedTarget, ValueError) as error:
                state.update(status="failed", error=scrub(str(error) or type(error).__name__)[:300])
        if not test or state["status"] == "ok":
            await self.db.set_setting(org_id, STATE, state)
        if state["status"] != previous.get("status") and not test:
            log.warning("heartbeat for organization %s is now %s %s", org_id, state["status"], state["error"])
        return state

    async def run_due(self) -> int:
        """Sends the heartbeat of every organization whose turn it is. Returns how many were tried."""
        tried = 0
        rows = await self.db.all("SELECT org_id, value FROM settings WHERE key = ?", (SETTING,))
        for row in rows:
            settings = {**DEFAULTS, **json.loads(row["value"])}
            org_id = int(row["org_id"])
            if not settings["enabled"] or not settings["url_enc"]:
                continue
            if time.monotonic() - self._last.get(org_id, -1e9) < int(settings["every_minutes"]) * 60:
                continue
            self._last[org_id] = time.monotonic()
            try:
                await self.beat(org_id)
            except Exception:  # One organization's heartbeat must not stop the others.
                log.exception("heartbeat failed for organization %s", org_id)
            tried += 1
        return tried

    async def loop(self) -> None:
        while True:
            try:
                await self.run_due()
            except Exception:
                log.exception("the heartbeat round failed")
            await asyncio.sleep(CHECK_EVERY_S)
