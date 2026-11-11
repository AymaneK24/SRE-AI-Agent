"""What one organization's engine remembers between polls, and the one way an event is recorded."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Any, Callable, Coroutine

from prometheus_client import Counter

from ..broker import Broker
from ..config import Config
from ..db import Database, utcnow
from ..secrets_store import SecretBox, scrub
from ..topology import Topology
from .summary import headline_for

log = logging.getLogger("rcc.context")

BASELINE_EVERY_S = 300
METRICS_EVERY_S = 300
SNAPSHOT_EVERY_S = 600
INTEGRATION_EVERY_S = 30
INTEGRATIONS_AT_ONCE = 4
CRASH_LOG_PROBLEMS = 3
CRASH_LOG_LINES = 30
CRASH_LOG_CHARS = 1000
NO_SOURCE_POLL_S = 30
STOP_TIMEOUT_S = 10
# A Kubernetes problem must be gone this long before it counts as resolved, so a pod that runs for a few seconds
# between crashes doesn't send "resolved" and "failing" again every minute.
K8S_CLEAR_AFTER_S = 75
TARGET_LIST_LIMIT = 500
CHANGES_EVERY_S = 120
MAX_SERIES_PER_SIGNAL = 10
ANOMALY_Z = 4.0
ANOMALY_SUSTAIN_POLLS = 2
EVENT_MEMORY = 50
# A polling loop that hasn't come round for this long after it was due is stuck, not slow.
# The start value of a "last done" time, so the first check always runs. Zero would not: the monotonic
# clock counts from boot, so on a server started a minute ago, zero looks like "a minute ago".
NEVER = float("-inf")
STALLED_AFTER_S = 300
OBSERVED_EVERY_S = 300
# The console says on the alert channels that it can't complete its checks after this many failed polls in a row.
BLIND_AFTER_POLLS = 3
CONSOLE_FINGERPRINT = "console:polling"
WATCHDOG_EVERY_S = 30
POLL_FAILURES = Counter("rcc_poll_failures_total", "Polls that ended with an unexpected error")
LOOP_RESTARTS = Counter("rcc_polling_restarts_total", "Polling loops restarted after ending unexpectedly")
SEED_WINDOW_S = 300
MAX_SEEDED_PANELS = 6

EventHook = Callable[[int, dict[str, Any], dict[str, Any]], Coroutine[Any, Any, None]]


class SourceState:
    def __init__(self, source: Any, poll_seconds: int) -> None:
        self.source = source
        self.poll_seconds = poll_seconds
        self.metric_names: set[str] = set()
        self.metrics_at = NEVER
        self.baselines: dict[str, dict[str, dict[str, float]]] = {}
        self.baselines_at = NEVER
        self.info: dict[str, Any] = {}
        self.failures = 0
        self.last_ok_iso: str | None = None
        self.last_ok_mono: float | None = None
        self.error: str | None = None
        self.last: dict[str, Any] | None = None
        self.changes_at = NEVER


class EngineState:
    """The shared state. Polling, the cluster watch and problem tracking are separate classes that all build on it."""

    def __init__(self, org_id: int, db: Database, box: SecretBox, config: Config, on_event: EventHook | None = None) -> None:
        self.org_id = org_id
        self.db = db
        self.box = box
        self.config = config
        self.on_event = on_event
        self.broker = Broker()
        self.states: dict[int, SourceState] = {}
        self.source_errors: list[dict[str, Any]] = []
        self.version = 0
        self.context: dict[str, Any] = self._empty_context("not_configured")
        self.recent_events: deque[dict[str, Any]] = deque(maxlen=EVENT_MEMORY)
        # The main connection of each kind, for what shows one of them. All of them are in integration_list.
        self.integration_summary: dict[str, Any] = {}
        self.integration_list: list[dict[str, Any]] = []
        self.integration_kinds: set[str] = set()
        self.main_connections: dict[str, int] = {}
        self.k8s_snapshot: dict[str, Any] | None = None
        self.k8s_snapshots: dict[int, dict[str, Any]] = {}
        self.k8s_problems: list[dict[str, Any]] = []
        # The service dependency map, rebuilt on every poll, and the calls seen in traces or a mesh, read less often.
        self.topology = Topology()
        self.topology_links: dict[str, dict[str, Any]] = {}
        self.topology_current: dict[str, dict[str, Any]] = {}
        self.topology_built_at: str | None = None
        self._observed: dict[str, list[dict[str, Any]]] = {}
        self._observed_at: float | None = None  # None: never read, so the next poll reads them.
        self._crash_logs: dict[str, dict[str, Any]] = {}
        self._integrations_at = NEVER
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._poll_lock = asyncio.Lock()
        self._active: dict[str, dict[str, Any]] = {}
        self._anomaly_streak: dict[str, int] = {}
        self._state_known = False
        self._targets_up: set[str] = set()
        self._targets_unknown: set[str] = set()
        self._snapshot_at = NEVER
        self._material_hash = ""
        self._seeded: set[int] = set()
        self._seed_deadlines: dict[int, float] = {}
        self._last_ok_mono: float | None = None
        self._stopping = False
        self._k8s_missing_since: dict[str, float] = {}
        self._event_tasks: set[asyncio.Task[None]] = set()
        self.poll_failures = 0
        self.failed_polls_in_a_row = 0
        self._blind_announced = False
        self._loop_beat = time.monotonic()
        self._loop_delay = float(NO_SOURCE_POLL_S)

    def _empty_context(self, status: str) -> dict[str, Any]:
        return {
            "version": getattr(self, "version", 0),
            "as_of": None,
            "sources": list(getattr(self, "source_errors", [])),
            "health": {"status": status, "failures": 0, "last_ok": None, "poll_ms": None, "error": None},
            "headline": headline_for(status, {}, {}, []),
            "targets": {"total": 0, "up": 0, "down": 0, "unknown": 0, "down_list": [], "jobs": []},
            "alerts": {"firing": [], "pending": []},
            "rules": {"groups": 0, "alerting": 0, "recording": 0, "failing": []},
            "signals": [],
            "anomalies": [],
            "events": [],
            "changes": [],
            "integrations": {},
            "problems": [],
            "coverage": {"metric_count": 0, "missing": [], "signal_count": 0, "has_metrics": False},
        }

    def _integration_ok(self, kind: str) -> bool:
        """True when every connection of this kind answered."""
        of_kind = [item for item in self.integration_list if item["kind"] == kind]
        return bool(of_kind) and all(item.get("status") == "ok" for item in of_kind)

    async def record_event(self, kind: str, severity: str, fingerprint: str, title: str, detail: dict[str, Any],
                            source_id: int | None = None, notify: bool = True) -> None:
        ts = utcnow()
        # A start that still has a message to send stays unhandled until the notifier is done with it.
        will_notify = bool(notify and self.on_event)
        waiting = will_notify and not kind.endswith(("_cleared", "_recovered"))
        event_id = await self.db.insert(
            "INSERT INTO events(org_id, source_id, ts, kind, severity, fingerprint, title, detail, handled) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.org_id, source_id, ts, kind, severity, fingerprint, title, scrub(json.dumps(detail, default=str)), 0 if waiting else 1),
        )
        event = {"id": event_id, "ts": ts, "kind": kind, "severity": severity, "title": title, "fingerprint": fingerprint}
        self.recent_events.appendleft(event)
        self.broker.publish({"type": "event", "data": event})
        if will_notify and self.on_event:
            # The loop keeps only a weak reference to a task, so hold one until it ends.
            task: asyncio.Task[None] = asyncio.create_task(self.on_event(self.org_id, event, detail))
            self._event_tasks.add(task)
            task.add_done_callback(self._event_tasks.discard)
