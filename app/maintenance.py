"""Applies each organization's retention period. Audit entries stay, because they must be append-only."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from .db import Database, utcnow

log = logging.getLogger("rcc.maintenance")
DEFAULT_RETENTION_DAYS = 365


async def purge_org(db: Database, org_id: int) -> dict[str, int]:
    days = int(await db.get_setting(org_id, "retention_days", DEFAULT_RETENTION_DAYS))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace("+00:00", "Z")
    old = await db.all("SELECT id FROM conversations WHERE org_id = ? AND updated_at < ?", (org_id, cutoff))
    for row in old:
        await db.unindex(row["id"])
    if old:
        await db.execute("DELETE FROM conversations WHERE org_id = ? AND updated_at < ?", (org_id, cutoff))
    events = await db.get("SELECT count(*) AS n FROM events WHERE org_id = ? AND ts < ?", (org_id, cutoff))
    snapshots = await db.get("SELECT count(*) AS n FROM context_snapshots WHERE org_id = ? AND ts < ?", (org_id, cutoff))
    if events["n"]:
        await db.execute("DELETE FROM events WHERE org_id = ? AND ts < ?", (org_id, cutoff))
    if snapshots["n"]:
        await db.execute("DELETE FROM context_snapshots WHERE org_id = ? AND ts < ?", (org_id, cutoff))
    result = {"conversations": len(old), "events": events["n"], "snapshots": snapshots["n"]}
    if any(result.values()):
        await db.audit(org_id, "retention_purge", "database", {"retention_days": days, **result})
    return result


async def maintenance_loop(db: Database) -> None:
    while True:
        try:
            for row in await db.all("SELECT id FROM organizations"):
                await purge_org(db, row["id"])
            now = utcnow()
            await db.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
            await db.execute("DELETE FROM sso_states WHERE expires_at < ?", (now,))
        except Exception:
            log.exception("retention purge failed")
        await asyncio.sleep(3600)
