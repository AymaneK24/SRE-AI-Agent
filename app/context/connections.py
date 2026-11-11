"""Log stores and Kubernetes clusters: which one is the main one, how a client is built, and recorded changes."""
from __future__ import annotations

import json
from typing import Any

from ..config import Config
from ..db import Database, utcnow
from ..integrations.kubernetes import KubernetesClient
from ..integrations.loki import LokiClient
from ..secrets_store import SecretBox, scrub
from ..sources.base import SourceError

MAIN_KINDS = ("metrics", "loki", "kubernetes")


async def main_connections(db: Database, org_id: int) -> dict[str, int]:
    """The connection an admin marked as main for each kind. Without a choice, the oldest one is main."""
    saved = await db.get_setting(org_id, "main_connections", {}) or {}
    return {kind: int(saved[kind]) for kind in MAIN_KINDS if isinstance(saved.get(kind), int)}


def pick_main(items: list[dict[str, Any]], preferred: int | None) -> int | None:
    ids = [item["id"] for item in items]
    if preferred in ids:
        return preferred
    return ids[0] if ids else None


def build_integration(row: dict[str, Any], box: SecretBox, config: Config) -> Any:
    settings = json.loads(row["config"]) if isinstance(row["config"], str) else row["config"]
    secrets = box.decrypt_json(row.get("secret_enc", ""))
    if row["kind"] == "loki":
        return LokiClient(settings, secrets, config.target_policy)
    if row["kind"] == "kubernetes":
        return KubernetesClient(settings, secrets, config.target_policy)
    raise SourceError(f"Unknown connection type: {row['kind']}")


async def store_change_event(db: Database, org_id: int, item: dict[str, Any]) -> bool:
    """Store a change once, ignoring repeats of the same event."""
    ts = item.get("ts") or utcnow()
    title = scrub(str(item.get("title", "Change"))[:300])
    origin = str(item.get("origin", "webhook"))[:80]
    existing = await db.one("SELECT id FROM change_events WHERE org_id = ? AND ts = ? AND origin = ? AND title = ?", (org_id, ts, origin, title))
    if existing:
        return False
    await db.insert(
        "INSERT INTO change_events(org_id, ts, origin, service, title, detail) VALUES (?, ?, ?, ?, ?, ?)",
        (org_id, ts, origin, str(item.get("service", ""))[:200], title, scrub(json.dumps(item.get("detail", {}), default=str))),
    )
    return True
