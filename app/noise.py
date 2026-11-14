"""Noise control: fewer, better alert messages.

Every problem the console finds goes through these steps before anyone is messaged:

1. Muted: the team marked this problem a false alarm or expected. It still shows in the console, silently.
   The label ends after a set time, a change to the same service, worse behaviour or a changed rule
   (app/verdicts.py), and the alert then goes out saying why.
2. Flapping: it started 3 times or more in an hour. One message says so; further starts stay quiet
   until it has been stable for 30 minutes.
3. Grace period: it must still be there after a short wait (critical: 1 minute, warning: 5 minutes by
   default). A blip that clears by itself is never sent, and never starts an automatic investigation.
4. Grouping: problems that start within the same window (90 seconds by default) become one message,
   with what they have in common: the same node, namespace or cluster, a dependency they all mention,
   or a change recorded just before. One automatic investigation then looks at all of them together.
5. Rate limit: each channel gets at most a set number of messages an hour. The rest arrive as one
   summary at the end of the hour.

Recovery messages go only where the problem itself was sent.
"""
from __future__ import annotations

import asyncio
import re
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

FLAP_STARTS = 3
FLAP_WINDOW_S = 3600
FLAP_QUIET_S = 1800
DEPENDENCY_RE = re.compile(r"\b([a-z][a-z0-9-]{1,62}(?:\.[a-z0-9-]+)*):(\d{2,5})\b")


def defaults(config: Any) -> dict[str, Any]:
    return {
        "grace_critical_s": int(config.alert_grace_s),
        "grace_warning_s": int(config.alert_warning_grace_s),
        "group_window_s": int(config.alert_group_window_s),
        "max_per_hour": 12,
        "flapping": True,
        "respect_verdicts": True,
        "label_days": 30,
    }


async def load(db: Any, org_id: int, config: Any) -> dict[str, Any]:
    merged = defaults(config)
    stored = await db.get_setting(org_id, "noise", {}) or {}
    merged.update({key: value for key, value in stored.items() if key in merged})
    return merged


@dataclass
class Batch:
    items: list[tuple[dict[str, Any], dict[str, Any]]] = field(default_factory=list)


class NoiseControl:
    def __init__(self) -> None:
        self._starts: dict[tuple[int, str], deque[float]] = {}
        self._flapping: dict[tuple[int, str], float] = {}
        self._batches: dict[int, Batch] = {}
        self._channel_sent: dict[int, deque[float]] = {}
        self.overflow: dict[int, list[str]] = {}
        self._calls = 0

    def flapping(self, org_id: int, fingerprint: str) -> str | None:
        """None if this start can go out; "first" the first time it flaps; "quiet" while it keeps flapping."""
        now = time.monotonic()
        key = (org_id, fingerprint)
        self._forget_old(now)
        starts = self._starts.setdefault(key, deque())
        starts.append(now)
        while starts and now - starts[0] > FLAP_WINDOW_S:
            starts.popleft()
        if key in self._flapping:
            if len(starts) >= 2 and now - starts[-2] < FLAP_QUIET_S:
                self._flapping[key] = now
                return "quiet"
            del self._flapping[key]
            return None
        if len(starts) >= FLAP_STARTS:
            self._flapping[key] = now
            return "first"
        return None

    def _forget_old(self, now: float) -> None:
        """Problems that haven't started for an hour are forgotten, so this doesn't grow with every problem ever seen."""
        self._calls += 1
        if self._calls % 500:
            return
        for key in [key for key, starts in self._starts.items() if not starts or now - starts[-1] > FLAP_WINDOW_S]:
            del self._starts[key]
            self._flapping.pop(key, None)
        for channel_id in [channel_id for channel_id, sent in self._channel_sent.items() if not sent or now - sent[-1] > 3600]:
            del self._channel_sent[channel_id]

    def join(self, org_id: int, event: dict[str, Any], detail: dict[str, Any]) -> bool:
        """Adds a problem to its organization's open group. True when it opened the group, so the caller sends it."""
        batch = self._batches.get(org_id)
        if batch is not None:
            batch.items.append((event, detail))
            return False
        self._batches[org_id] = Batch([(event, detail)])
        return True

    def close(self, org_id: int) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        batch = self._batches.pop(org_id, None)
        return batch.items if batch else []

    def allow(self, channel_id: int, max_per_hour: int) -> bool:
        now = time.monotonic()
        sent = self._channel_sent.setdefault(channel_id, deque())
        while sent and now - sent[0] > 3600:
            sent.popleft()
        if len(sent) >= max_per_hour:
            return False
        sent.append(now)
        return True

    def seconds_until_room(self, channel_id: int) -> float:
        sent = self._channel_sent.get(channel_id)
        return max(1.0, 3600 - (time.monotonic() - sent[0])) if sent else 1.0


def common_causes(items: list[tuple[dict[str, Any], dict[str, Any]]], changes: list[dict[str, Any]]) -> list[str]:
    """What problems that started together have in common. Each line is a fact, not a guess."""
    hints: list[str] = []
    details = [detail for _, detail in items]
    nodes = [detail.get("object", "") for detail in details if str(detail.get("fingerprint", "")).find("node/") >= 0 or detail.get("problem", "").startswith("node")]
    if nodes:
        hints.append(f"{nodes[0]} is in trouble at the same time, so the other problems may follow from it.")
    namespaces = Counter(detail.get("namespace") or (detail.get("labels") or {}).get("namespace") for detail in details)
    namespaces.pop(None, None)
    namespaces.pop("", None)
    for name, count in namespaces.most_common(1):
        if count >= 2:
            hints.append(f"{count} of them are in namespace {name}.")
    clusters = Counter(detail.get("cluster") for detail in details if detail.get("cluster"))
    if len(clusters) == 1 and len(items) >= 2 and sum(clusters.values()) == len(items):
        hints.append(f"All of them are in cluster {next(iter(clusters))}.")
    mentioned: Counter[str] = Counter()
    for detail in details:
        text = " ".join(str(part) for part in (detail.get("summary"), (detail.get("diagnosis") or {}).get("cause"), detail.get("error"),
                                               " ".join((detail.get("logs") or {}).get("lines", [])[-5:])) if part)
        mentioned.update({match.group(0) for match in DEPENDENCY_RE.finditer(text) if not match.group(1).replace(".", "").isdigit()})
    for dependency, count in mentioned.most_common(2):
        if count >= 2:
            hints.append(f"{count} of them mention {dependency}: check it first.")
    if changes:
        latest = changes[0]
        hints.append(f"Recorded change shortly before: {latest['title']} ({latest['ts'][11:16]} UTC).")
    return hints


def changes_since(rows: list[dict[str, Any]], minutes: int = 20) -> list[dict[str, Any]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
    return [row for row in rows if str(row.get("ts", "")) >= cutoff]


async def sleep(seconds: float) -> None:
    if seconds > 0:
        await asyncio.sleep(seconds)
