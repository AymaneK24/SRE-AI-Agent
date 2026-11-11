"""One live engine per organization, with a watchdog over their polling loops."""
from __future__ import annotations

import asyncio

from ..config import Config
from ..db import Database
from ..secrets_store import SecretBox
from .engine import ContextEngine
from .state import WATCHDOG_EVERY_S, EventHook, log


class EngineManager:
    """One live engine per organization, started when first needed."""

    def __init__(self, db: Database, box: SecretBox, config: Config, on_event: EventHook | None = None) -> None:
        self.db, self.box, self.config, self.on_event = db, box, config, on_event
        self.engines: dict[int, ContextEngine] = {}
        self._lock = asyncio.Lock()

    async def get(self, org_id: int) -> ContextEngine:
        async with self._lock:
            engine = self.engines.get(org_id)
            if engine is None:
                engine = ContextEngine(org_id, self.db, self.box, self.config, self.on_event)
                self.engines[org_id] = engine
                await engine.start()
            return engine

    async def start_active(self) -> None:
        rows = await self.db.all(
            "SELECT DISTINCT s.org_id FROM sources s JOIN organizations o ON o.id = s.org_id WHERE s.enabled = 1 AND o.status = 'active' "
            "UNION SELECT DISTINCT i.org_id FROM integrations i JOIN organizations o ON o.id = i.org_id WHERE i.enabled = 1 AND o.status = 'active'")
        for row in rows:
            await self.get(row["org_id"])

    async def remove(self, org_id: int) -> None:
        async with self._lock:
            engine = self.engines.pop(org_id, None)
        if engine:
            await engine.stop()

    async def stop_all(self) -> None:
        for engine in list(self.engines.values()):
            await engine.stop()
        self.engines = {}

    def stalled(self) -> list[int]:
        """Organizations whose polling loop is stuck. A restart of the console is the only cure."""
        return [org_id for org_id, engine in self.engines.items() if engine.stalled]

    def revive(self) -> int:
        """Restarts polling loops that ended unexpectedly. Returns how many."""
        return sum(1 for engine in list(self.engines.values()) if engine.revive())

    async def watchdog(self) -> None:
        while True:
            await asyncio.sleep(WATCHDOG_EVERY_S)
            try:
                self.revive()
            except Exception:  # The watchdog itself must keep running.
                log.exception("the polling watchdog failed")
