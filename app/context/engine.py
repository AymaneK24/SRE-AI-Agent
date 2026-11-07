"""Keeps a current picture of one organization's systems, across all of its connections.

Metric sources, log stores, and Kubernetes clusters are each optional: any one of them is enough to build a context.
Each poll rebuilds the context from targets, alerts, rules, tracked signals, log summaries, and cluster problems. The engine compares it
with the previous poll, records what changed, and pushes it to subscribers. Investigations always read
`engine.context`, so a model never works from an old copy.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import statistics
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from ..broker import Broker
from ..config import Config
from ..db import Database, sha256_text, utcnow
from ..diagnosis.problems import SEVERITY_RANK, find_problems
from ..diagnosis.rules import diagnose
from ..integrations.kubernetes import KubernetesClient, summary_from_snapshot
from ..integrations.loki import LokiClient
from ..net_guard import BlockedTarget
from ..secrets_store import SecretBox, scrub
from ..sources.base import SourceError
from ..sources.registry import build_source
from .signals import COVERAGE_CHECKS, GOLDEN, Signal

log = logging.getLogger("rcc.context")

BASELINE_EVERY_S = 300
METRICS_EVERY_S = 300
SNAPSHOT_EVERY_S = 600
INTEGRATION_EVERY_S = 30
CRASH_LOG_PROBLEMS = 3
CRASH_LOG_LINES = 30
CRASH_LOG_CHARS = 1000
NO_SOURCE_POLL_S = 30
STOP_TIMEOUT_S = 10
# A Kubernetes problem must be gone this long before it counts as resolved, so a pod that runs for a few seconds
# between crashes doesn't send "resolved" and "failing" again every minute.
K8S_CLEAR_AFTER_S = 75
CHANGES_EVERY_S = 120
MAX_SERIES_PER_SIGNAL = 10
ANOMALY_Z = 4.0
ANOMALY_SUSTAIN_POLLS = 2
EVENT_MEMORY = 50
SEED_WINDOW_S = 300
MAX_SEEDED_PANELS = 6

EventHook = Callable[[int, dict[str, Any], dict[str, Any]], Awaitable[None]]


def series_key(labels: dict[str, str]) -> str:
    return ",".join(f"{key}={labels[key]}" for key in sorted(labels))


def label_text(labels: dict[str, str]) -> str:
    return ", ".join(f"{key}={value}" for key, value in sorted(labels.items())) or "all"


def parse_value(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value == value and abs(value) != float("inf") else None


def compute_baseline(values: list[float]) -> dict[str, float] | None:
    """Median and median absolute deviation resist outliers better than mean and stddev."""
    if len(values) < 20:
        return None
    median = statistics.median(values)
    mad = statistics.median(abs(value - median) for value in values)
    return {"median": median, "mad": mad, "samples": float(len(values))}


def robust_z(value: float, baseline: dict[str, float], min_sigma: float) -> float:
    sigma = max(1.4826 * baseline["mad"], abs(baseline["median"]) * 0.05, min_sigma, 1e-9)
    return (value - baseline["median"]) / sigma


class SourceState:
    def __init__(self, source: Any, poll_seconds: int) -> None:
        self.source = source
        self.poll_seconds = poll_seconds
        self.metric_names: set[str] = set()
        self.metrics_at = 0.0
        self.baselines: dict[str, dict[str, dict[str, float]]] = {}
        self.baselines_at = 0.0
        self.info: dict[str, Any] = {}
        self.failures = 0
        self.last_ok_iso: str | None = None
        self.last_ok_mono: float | None = None
        self.error: str | None = None
        self.last: dict[str, Any] | None = None
        self.changes_at = 0.0


class ContextEngine:
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
        self.integration_summary: dict[str, Any] = {}
        self.integration_kinds: set[str] = set()
        self.k8s_snapshot: dict[str, Any] | None = None
        self.k8s_problems: list[dict[str, Any]] = []
        self._crash_logs: dict[str, dict[str, Any]] = {}
        self._integrations_at = 0.0
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._poll_lock = asyncio.Lock()
        self._active: dict[str, dict[str, Any]] = {}
        self._anomaly_streak: dict[str, int] = {}
        self._state_known = False
        self._targets_up: set[str] = set()
        self._targets_unknown: set[str] = set()
        self._snapshot_at = 0.0
        self._material_hash = ""
        self._seeded: set[int] = set()
        self._seed_deadlines: dict[int, float] = {}
        self._last_ok_mono: float | None = None
        self._stopping = False
        self._k8s_missing_since: dict[str, float] = {}

    # Lifecycle --------------------------------------------------------------
    async def start(self) -> None:
        await self.reload_sources()
        self._task = asyncio.create_task(self._loop(), name=f"context-engine-{self.org_id}")

    async def stop(self) -> None:
        # The flag and the wake-up end the loop even if a cancellation is lost. That happens when it arrives at the
        # same moment as the loop's own wait timeout, or inside a client library that manages cancellation itself.
        self._stopping = True
        self._wake.set()
        if self._task:
            self._task.cancel()
            try:
                await asyncio.wait_for(self._task, timeout=STOP_TIMEOUT_S)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception:  # A failed last poll must not stop the shutdown.
                log.exception("context engine ended with an error")
        await self._close_sources()

    async def _close_sources(self) -> None:
        for state in self.states.values():
            try:
                await state.source.close()
            except Exception:  # Closing must not block a reload.
                log.exception("closing source failed")
        self.states = {}

    async def reload_sources(self) -> None:
        async with self._poll_lock:
            await self._close_sources()
            self._reset_state()
            rows = await self.db.all("SELECT * FROM sources WHERE org_id = ? AND enabled = 1 ORDER BY id", (self.org_id,))
            self.source_errors = []
            for row in rows:
                try:
                    source = build_source(row, self.box, self.config)
                    self.states[row["id"]] = SourceState(source, row["poll_seconds"])
                except (ValueError, SourceError, KeyError) as error:
                    message = getattr(error, "message", str(error))
                    log.error("cannot load source %s: %s", row["id"], message)
                    self.source_errors.append({"id": row["id"], "name": row["name"], "kind": row["kind"], "status": "unreachable", "error": message})
            await self._load_integration_kinds()
            if not rows and not self.integration_kinds:
                self.context = self._empty_context("not_configured")
        self._wake.set()

    async def _load_integration_kinds(self) -> None:
        rows = await self.db.all("SELECT kind FROM integrations WHERE org_id = ? AND enabled = 1", (self.org_id,))
        self.integration_kinds = {row["kind"] for row in rows}

    async def integrations_changed(self) -> None:
        """A log store or cluster was added or removed: read it on the next poll, without waiting."""
        await self._load_integration_kinds()
        self._integrations_at = 0.0
        if "kubernetes" not in self.integration_kinds:
            self.k8s_snapshot, self.k8s_problems = None, []
        if not self.states and not self.integration_kinds:
            self.integration_summary = {}
            self.context = self._empty_context("not_configured")
        self._wake.set()

    @property
    def has_connections(self) -> bool:
        return bool(self.states) or bool(self.integration_kinds)

    def _reset_state(self) -> None:
        self._active = {}
        self._anomaly_streak = {}
        self._state_known = False
        self._targets_up = set()
        self._targets_unknown = set()
        self._snapshot_at = 0.0
        self._material_hash = ""
        self._last_ok_mono = None
        self.integration_summary = {}
        self.k8s_snapshot, self.k8s_problems = None, []
        self._integrations_at = 0.0
        self.recent_events.clear()

    async def _loop(self) -> None:
        while not self._stopping:
            delay = float(NO_SOURCE_POLL_S)
            if self.has_connections:
                await self.poll()
                if self.states:
                    delay = float(min(min(state.poll_seconds for state in self.states.values()), NO_SOURCE_POLL_S if self.integration_kinds else 3600))
            else:
                if self.source_errors:
                    self.context = self._empty_context("unreachable")
                self.broker.publish({"type": "context", "data": self.public_context()})
            if self._stopping:
                return
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                task = asyncio.current_task()
                if task and task.cancelling():
                    raise asyncio.CancelledError from None
            self._wake.clear()

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

    def public_context(self) -> dict[str, Any]:
        context = dict(self.context)
        health = dict(context.get("health", {}))
        if self._last_ok_mono is not None:
            health["age_seconds"] = round(time.monotonic() - self._last_ok_mono, 1)
        context["health"] = health
        return context

    def source(self, source_id: int | None) -> Any:
        if source_id is not None and source_id in self.states:
            return self.states[source_id].source
        if source_id is None and self.states:
            return next(iter(self.states.values())).source
        if not self.states:
            raise SourceError("No metric source is connected, so metric queries can't run. Use the log and Kubernetes tools instead.", "bad_data")
        raise SourceError("That metric source isn't connected.", "bad_data")

    # Polling ----------------------------------------------------------------
    async def poll(self) -> None:
        async with self._poll_lock:
            if not self.has_connections:
                return
            started = time.monotonic()
            results = list(await asyncio.gather(*(self._poll_source(state) for state in self.states.values())))
            await self._refresh_integrations()
            await self._merge(results, started)

    async def _poll_source(self, state: SourceState) -> dict[str, Any] | None:
        source = state.source
        try:
            await source.guard()
            now = time.monotonic()
            if not state.info:
                state.info = await source.info()
            if now - state.metrics_at >= METRICS_EVERY_S or not state.metric_names:
                state.metric_names = set(await source.metric_names())
                state.metrics_at = now
            targets, alerts, groups = await asyncio.gather(source.targets(), source.alerts(), source.rules())
            definitions = await self._signal_definitions(state)
            signals = await self._evaluate_signals(state, definitions)
            if now - state.changes_at >= CHANGES_EVERY_S:
                state.changes_at = now
                await self._store_source_changes(source)
        except (SourceError, BlockedTarget) as error:
            message = getattr(error, "message", str(error))
            await self._source_failed(state, message)
            return None
        except Exception as error:  # A bad payload must not stop the polling loop.
            log.exception("context poll failed for source %s", source.id)
            await self._source_failed(state, f"Poll failed: {error}")
            return None
        if state.failures:
            await self._record_event("source_recovered", "info", f"source:{source.id}",
                                     f"{source.name} is answering again", {}, source.id)
        state.failures = 0
        state.error = None
        state.last_ok_iso = utcnow()
        state.last_ok_mono = time.monotonic()
        state.last = {"targets": targets, "alerts": alerts, "groups": groups, "signals": signals}
        return state.last

    async def _source_failed(self, state: SourceState, message: str) -> None:
        state.failures += 1
        state.error = scrub(message)
        if state.failures == 1:
            await self._record_event("source_unreachable", "critical", f"source:{state.source.id}",
                                     f"{state.source.name} stopped answering", {"error": state.error}, state.source.id)

    async def _signal_definitions(self, state: SourceState) -> list[Signal]:
        source = state.source
        signals = list(await source.signals(state.metric_names))
        known = {signal.query for signal in signals}
        panels = await self.db.all("SELECT * FROM panels WHERE org_id = ? AND source_id = ? ORDER BY position, id", (self.org_id, source.id))
        for panel in panels:
            if panel["query"] not in known:
                signals.append(Signal(f"panel_{panel['id']}", panel["title"], "Your panels", panel["unit"] or "short", (), panel["query"], direction="both"))
                known.add(panel["query"])
        return signals

    async def _evaluate_signals(self, state: SourceState, definitions: list[Signal]) -> list[dict[str, Any]]:
        limiter = asyncio.Semaphore(8)
        refresh = time.monotonic() - state.baselines_at >= BASELINE_EVERY_S

        async def evaluate(signal: Signal) -> dict[str, Any]:
            async with limiter:
                try:
                    data = await state.source.query(signal.query)
                    if refresh:
                        await self._refresh_baseline(state, signal)
                except SourceError as error:
                    if error.kind in {"unreachable", "timeout", "auth", "tls"}:
                        raise
                    return {**_signal_meta(signal, state.source), "series": [], "total_series": 0, "error": error.message}
            return self._signal_result(state, signal, data)

        results = await asyncio.gather(*(evaluate(signal) for signal in definitions))
        if refresh:
            state.baselines_at = time.monotonic()
        return list(results)

    async def _refresh_baseline(self, state: SourceState, signal: Signal) -> None:
        end = time.time() - 300
        data = await state.source.query_range(signal.query, end - 3600, end, 60)
        table: dict[str, dict[str, float]] = {}
        for series in data.get("result", []):
            labels = dict(series.get("metric", {}))
            labels.pop("__name__", None)
            values = [parse_value(point[1]) for point in series.get("values", [])]
            baseline = compute_baseline([value for value in values if value is not None])
            if baseline:
                table[series_key(labels)] = baseline
        state.baselines[signal.id] = table

    def _signal_result(self, state: SourceState, signal: Signal, data: dict[str, Any]) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        baselines = state.baselines.get(signal.id, {})
        result = data.get("result", []) if data.get("resultType") == "vector" else []
        for item in result:
            labels = dict(item.get("metric", {}))
            labels.pop("__name__", None)
            value = parse_value(item.get("value", [0, None])[1])
            if value is None:
                continue
            row: dict[str, Any] = {"labels": labels, "value": value, "state": "normal"}
            baseline = baselines.get(series_key(labels))
            if baseline:
                z = robust_z(value, baseline, signal.min_sigma)
                row["baseline"] = baseline["median"]
                row["z"] = round(z, 1)
                if z >= ANOMALY_Z or (signal.direction == "both" and z <= -ANOMALY_Z):
                    row["state"] = "unusual"
            if signal.limit is not None and value >= signal.limit:
                row["state"] = "over_limit"
            rows.append(row)
        rows.sort(key=lambda row: (row["state"] == "normal", -abs(row.get("z", 0)), -abs(row["value"])))
        return {**_signal_meta(signal, state.source), "series": rows[:MAX_SERIES_PER_SIGNAL], "total_series": len(rows), "error": None}

    async def _store_source_changes(self, source: Any) -> None:
        since = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
        for item in await source.change_events(since):
            await store_change_event(self.db, self.org_id, item)

    async def _refresh_integrations(self) -> None:
        if time.monotonic() - self._integrations_at < INTEGRATION_EVERY_S:
            return
        self._integrations_at = time.monotonic()
        rows = await self.db.all("SELECT * FROM integrations WHERE org_id = ? AND enabled = 1", (self.org_id,))
        self.integration_kinds = {row["kind"] for row in rows}
        summary: dict[str, Any] = {}
        for row in rows:
            client = None
            try:
                client = build_integration(row, self.box, self.config)
                if row["kind"] == "kubernetes":
                    snapshot = await client.snapshot()
                    self.k8s_snapshot = snapshot
                    self.k8s_problems = find_problems(snapshot)
                    await self._enrich_k8s_problems(client)
                    summary["kubernetes"] = {"name": row["name"], "status": "ok", **summary_from_snapshot(snapshot), "problems": self.k8s_problems}
                else:
                    summary[row["kind"]] = {"name": row["name"], "status": "ok", **(await client.summary())}
            except (SourceError, BlockedTarget, ValueError) as error:
                summary[row["kind"]] = {"name": row["name"], "status": "error", "error": scrub(getattr(error, "message", str(error)))}
            except Exception as error:  # A bad answer from one connection must not stop the others.
                log.exception("integration refresh failed for %s", row["kind"])
                summary[row["kind"]] = {"name": row["name"], "status": "error", "error": scrub(f"Refresh failed: {error}")}
            finally:
                if client:
                    await client.close()
        if "kubernetes" not in summary:
            self.k8s_snapshot, self.k8s_problems = None, []
        self.integration_summary = summary

    async def _enrich_k8s_problems(self, client: Any) -> None:
        """Add the crashed container's last lines and a rule-based diagnosis to the worst problems.

        Logs are read again only when a pod restarts, so a crash loop costs one log call per restart, not per poll.
        A failure here never hides the problems themselves.
        """
        events = (self.k8s_snapshot or {}).get("events", [])
        wanted: dict[str, dict[str, Any]] = {}
        for problem in self.k8s_problems:
            logs = None
            if problem["problem"] in {"crashloop", "oomkilled", "not_ready"} and problem["pods"] and len(wanted) < CRASH_LOG_PROBLEMS:
                pod = problem["pods"][0]
                container = problem["facts"].get("container", "")
                previous = problem["problem"] != "not_ready"
                key = f"{problem['namespace']}/{pod}/{container}/{problem['facts'].get('restarts')}/{previous}"
                logs = self._crash_logs.get(key)
                if logs is None:
                    try:
                        result = await client.pod_logs(problem["namespace"], pod, container, previous, CRASH_LOG_LINES)
                        lines = [scrub(line) for line in result.get("lines", [])]
                        while lines and sum(len(line) for line in lines) > CRASH_LOG_CHARS:
                            lines.pop(0)
                        logs = {"pod": pod, "which": "the crashed container" if previous else "the running container",
                                "lines": lines, "note": result.get("note", "")}
                    except SourceError as error:
                        logs = {"pod": pod, "lines": [], "note": f"Couldn't read the logs: {error.message}"}
                wanted[key] = logs
                problem["logs"] = logs
            try:
                problem["diagnosis"] = diagnose(problem, (logs or {}).get("lines"), events)
            except Exception:  # A rule that trips on odd data must not stop the poll.
                log.exception("diagnosis failed for %s", problem["fingerprint"])
        self._crash_logs = wanted

    def _integration_ok(self, kind: str) -> bool:
        return self.integration_summary.get(kind, {}).get("status") == "ok"

    # Combining sources ------------------------------------------------------
    async def _merge(self, results: list[dict[str, Any] | None], started: float) -> None:
        now_iso = utcnow()
        sources_info, targets_raw, alerts_raw, groups_raw, signals = [], [], [], [], []
        any_ok = any(result is not None for result in results) or any(item.get("status") == "ok" for item in self.integration_summary.values())
        for state in self.states.values():
            source = state.source
            sources_info.append({
                "id": source.id, "name": source.name, "kind": source.kind, "url": source.describe(),
                "version": state.info.get("version"), "poll_seconds": state.poll_seconds,
                "query_language": source.query_language, "logs_language": source.logs_language,
                "status": "ok" if state.failures == 0 else "unreachable", "error": state.error,
                "last_ok": state.last_ok_iso, "stale": state.failures > 0 and state.last is not None,
            })
            if state.last:
                multi = len(self.states) > 1
                for target in state.last["targets"]:
                    targets_raw.append((source, target, multi))
                for alert in state.last["alerts"]:
                    alerts_raw.append((source, alert, multi))
                groups_raw.extend(state.last["groups"])
                signals.extend(state.last["signals"])
        sources_info.extend(self.source_errors)
        failed = [item for item in sources_info if item["status"] != "ok"]
        failed_connections = failed + [{"name": item.get("name", kind), "error": item.get("error")} for kind, item in self.integration_summary.items() if item.get("status") != "ok"]
        if not sources_info and not self.integration_summary:
            status = "not_configured" if not self.integration_kinds else "unreachable"
        else:
            status = "ok" if not failed_connections else ("partial" if any_ok else "unreachable")
        target_state = self._summarize_targets(targets_raw, now_iso)
        alert_state = summarize_alerts(alerts_raw)
        rule_state = summarize_rules(groups_raw)
        anomalies = self._find_anomalies(signals)
        current = self._current_problems(target_state, alert_state, rule_state, anomalies)
        if any_ok:
            await self._diff_and_record(current)
            self._last_ok_mono = time.monotonic()
        self.version += 1
        has_data = any(state.last for state in self.states.values())
        missing = []
        names: set[str] = set()
        for state in self.states.values():
            if state.source.kind == "prometheus":
                names |= state.metric_names
        if any(state.source.kind == "prometheus" for state in self.states.values()):
            missing = [label for metric, label in COVERAGE_CHECKS if metric not in names]
        changes = await self.db.all(
            "SELECT ts, origin, service, title FROM change_events WHERE org_id = ? AND ts >= ? ORDER BY ts DESC LIMIT 25",
            (self.org_id, (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat().replace("+00:00", "Z")),
        )
        previous_as_of = self.context.get("as_of")
        context = {
            "version": self.version,
            "as_of": now_iso if any_ok else previous_as_of,
            "sources": sources_info,
            "health": {
                "status": status, "failures": len(failed),
                "last_ok": max((item["last_ok"] for item in sources_info if item.get("last_ok")), default=None),
                "poll_ms": round((time.monotonic() - started) * 1000),
                "error": "; ".join(f"{item['name']}: {item['error']}" for item in failed_connections) or None,
                "age_seconds": 0,
            },
            "stale": bool(failed) and has_data,
            "headline": headline_for(status, target_state, alert_state, anomalies, [item["name"] for item in failed_connections], self.k8s_problems),
            "targets": target_state,
            "alerts": alert_state,
            "rules": rule_state,
            "signals": signals,
            "anomalies": anomalies,
            "events": list(self.recent_events),
            "changes": changes,
            "integrations": self.integration_summary,
            "problems": ranked_problems(current),
            "coverage": {
                "has_metrics": bool(self.states),
                "metric_count": sum(len(state.metric_names) for state in self.states.values()),
                "signal_count": sum(1 for signal in signals if signal["series"]),
                "missing": missing,
                "has_logs": "loki" in self.integration_kinds or "kubernetes" in self.integration_kinds or any(state.source.logs_language for state in self.states.values()),
                "has_kubernetes": "kubernetes" in self.integration_kinds,
                "has_changes": bool(changes),
            },
        }
        self.context = context
        if any_ok:
            await self._seed_panels(signals)
        material = material_hash(context)
        if material != self._material_hash:
            reason = "state_changed" if self._material_hash else "first_poll"
            self._material_hash = material
            await self.save_snapshot(context, reason)
        elif time.monotonic() - self._snapshot_at >= SNAPSHOT_EVERY_S:
            await self.save_snapshot(context, "periodic")
        self.broker.publish({"type": "context", "data": self.public_context()})

    def _summarize_targets(self, targets: list[tuple[Any, dict[str, Any], bool]], now_iso: str) -> dict[str, Any]:
        up = unknown = 0
        down_list = []
        jobs: dict[str, dict[str, int]] = {}
        self._targets_up = set()
        self._targets_unknown = set()
        for source, target, multi in targets:
            labels = target.get("labels", {})
            job = labels.get("job") or target.get("scrapePool", "unknown")
            if multi:
                job = f"{source.name}/{job}"
            bucket = jobs.setdefault(job, {"up": 0, "down": 0, "unknown": 0})
            health = target.get("health", "unknown")
            instance = labels.get("instance") or target.get("scrapeUrl", "")
            fingerprint = f"target:{source.id}:{job}:{instance}"
            if health == "up":
                up += 1
                bucket["up"] += 1
                self._targets_up.add(fingerprint)
            elif health == "down":
                bucket["down"] += 1
                since = self._active.get(fingerprint, {}).get("since", now_iso)
                down_list.append({
                    "job": job, "instance": instance, "error": scrub(target.get("lastError", "")),
                    "last_scrape": target.get("lastScrape"), "since": since, "fingerprint": fingerprint, "source_id": source.id,
                })
            else:
                unknown += 1
                bucket["unknown"] += 1
                self._targets_unknown.add(fingerprint)
        return {
            "total": len(targets), "up": up, "down": len(down_list), "unknown": unknown, "down_list": down_list,
            "jobs": [{"job": job, **counts} for job, counts in sorted(jobs.items())],
        }

    def _find_anomalies(self, signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
        found = []
        seen = set()
        for signal in signals:
            for row in signal["series"]:
                if row["state"] == "normal":
                    continue
                fingerprint = f"signal:{signal['source_id']}:{signal['id']}:{series_key(row['labels'])}:{row['state']}"
                seen.add(fingerprint)
                streak = self._anomaly_streak.get(fingerprint, 0) + 1
                self._anomaly_streak[fingerprint] = streak
                if row["state"] == "unusual" and streak < ANOMALY_SUSTAIN_POLLS:
                    continue
                found.append({
                    "fingerprint": fingerprint, "signal_id": signal["id"], "source_id": signal["source_id"],
                    "title": signal["title"], "unit": signal["unit"], "query": signal["query"], "labels": row["labels"],
                    "value": row["value"], "baseline": row.get("baseline"), "z": row.get("z"), "limit": signal.get("limit"), "kind": row["state"],
                })
        for fingerprint in list(self._anomaly_streak):
            if fingerprint not in seen:
                del self._anomaly_streak[fingerprint]
        found.sort(key=lambda item: (item["kind"] != "over_limit", -abs(item.get("z") or 0)))
        return found

    def _current_problems(self, targets: dict[str, Any], alerts: dict[str, Any], rules: dict[str, Any], anomalies: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Everything wrong right now, from every connection, keyed by a fingerprint that stays the same while it lasts."""
        current: dict[str, dict[str, Any]] = {}
        for alert in alerts["firing"]:
            current[alert["fingerprint"]] = {
                "kind": "alert", "severity": _severity(alert), "title": f"Alert firing: {alert['name']}",
                "clear": f"Alert resolved: {alert['name']}", "detail": alert,
            }
        for target in targets["down_list"]:
            current[target["fingerprint"]] = {
                "kind": "target", "severity": "critical", "title": f"Target down: {target['instance']} ({target['job']})",
                "clear": f"Target back up: {target['instance']} ({target['job']})", "detail": target,
            }
        for problem in self.k8s_problems:
            detail = {key: value for key, value in problem.items() if key not in {"title", "clear"}}
            if problem.get("diagnosis"):
                detail["summary"] = problem["diagnosis"]["cause"]
            current[problem["fingerprint"]] = {
                "kind": "k8s", "severity": problem["severity"], "title": problem["title"], "clear": problem["clear"], "detail": detail,
            }
        for anomaly in anomalies:
            wording = "over its limit" if anomaly["kind"] == "over_limit" else "unusual for this server"
            where = label_text(anomaly["labels"])
            current[anomaly["fingerprint"]] = {
                "kind": "signal", "severity": "warning", "title": f"{anomaly['title']} is {wording}: {where}",
                "clear": f"{anomaly['title']} is back to normal: {where}", "detail": anomaly,
            }
        for rule in rules["failing"]:
            fingerprint = f"rule:{rule['group']}:{rule['name']}"
            current[fingerprint] = {
                "kind": "rule", "severity": "warning", "title": f"Rule failing: {rule['name']}",
                "clear": f"Rule evaluating again: {rule['name']}", "detail": rule,
            }
        return current

    async def _diff_and_record(self, current: dict[str, dict[str, Any]]) -> None:
        k8s_unknown = "kubernetes" in self.integration_kinds and not self._integration_ok("kubernetes")
        # Problems seen on the first poll started at an unknown earlier time, so don't date them as new.
        first_poll = not self._state_known
        self._state_known = True
        if first_poll:
            await self._close_problems_that_ended_offline(current)
        for fingerprint, item in current.items():
            self._k8s_missing_since.pop(fingerprint, None)
            if fingerprint not in self._active:
                self._active[fingerprint] = {"since": utcnow(), "clear": item["clear"], "kind": item["kind"], "source_id": item["detail"].get("source_id")}
                title = f"Already active when the console connected. {item['title']}" if first_poll else item["title"]
                # Log lines stay in the live context only. Events keep the diagnosis, which is short and computed.
                detail = {key: value for key, value in item["detail"].items() if key != "logs"}
                await self._record_event(f"{item['kind']}_started", item["severity"], fingerprint, title, detail, item["detail"].get("source_id"), notify=not first_poll)
        for fingerprint in [key for key in self._active if key not in current]:
            # Prometheus reports targets as unknown until it scrapes them, such as after a restart.
            # That isn't evidence of recovery, so the target stays marked as a known problem.
            if fingerprint in self._targets_unknown:
                continue
            # A cluster that didn't answer gives no evidence that its problems ended.
            if k8s_unknown and fingerprint.startswith("k8s:"):
                continue
            if fingerprint.startswith("k8s:"):
                missing_since = self._k8s_missing_since.setdefault(fingerprint, time.monotonic())
                if time.monotonic() - missing_since < K8S_CLEAR_AFTER_S:
                    continue
                self._k8s_missing_since.pop(fingerprint, None)
            item = self._active.pop(fingerprint)
            title = item["clear"]
            if item.get("kind") == "target" and fingerprint not in self._targets_up:
                title = title.replace("Target back up:", "Target no longer reported:")
            await self._record_event(f"{item['kind']}_cleared", "info", fingerprint, title, {}, item.get("source_id"))

    async def _close_problems_that_ended_offline(self, current: dict[str, Any]) -> None:
        """A problem left open at the last shutdown has ended if it isn't active now, so reports don't count it as running."""
        rows = await self.db.all(
            "SELECT fingerprint, kind, title, source_id FROM events WHERE id IN (SELECT MAX(id) FROM events WHERE org_id = ? GROUP BY fingerprint)", (self.org_id,))
        for row in rows:
            if not row["kind"].endswith("_started") or row["fingerprint"] in current:
                continue
            if row["fingerprint"].startswith("k8s:"):
                if not self._integration_ok("kubernetes"):
                    continue
            else:
                state = self.states.get(row["source_id"])
                if not state or not state.last:
                    continue  # Without an answer from its source, there's no evidence that it ended.
            name = row["title"].removeprefix("Already active when the console connected. ")
            await self._record_event(row["kind"].replace("_started", "_cleared"), "info", row["fingerprint"], f"Ended while the console was offline: {name}", {}, row["source_id"], notify=False)

    async def _record_event(self, kind: str, severity: str, fingerprint: str, title: str, detail: dict[str, Any],
                            source_id: int | None = None, notify: bool = True) -> None:
        ts = utcnow()
        event_id = await self.db.insert(
            "INSERT INTO events(org_id, source_id, ts, kind, severity, fingerprint, title, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (self.org_id, source_id, ts, kind, severity, fingerprint, title, scrub(json.dumps(detail, default=str))),
        )
        event = {"id": event_id, "ts": ts, "kind": kind, "severity": severity, "title": title, "fingerprint": fingerprint}
        self.recent_events.appendleft(event)
        self.broker.publish({"type": "event", "data": event})
        if notify and self.on_event:
            asyncio.create_task(self.on_event(self.org_id, event, detail))

    async def _seed_panels(self, signals: list[dict[str, Any]]) -> None:
        """Build a starting dashboard for each new source, then leave it to the user.

        Rate queries return nothing until a server has two samples, so seeding keeps adding
        panels for a few minutes after connecting. Any manual panel change ends it early.
        """
        for state in self.states.values():
            source_id = state.source.id
            if not source_id or source_id in self._seeded:
                continue
            flag = f"panels_seeded:{source_id}"
            if await self.db.get_setting(self.org_id, flag, False):
                self._seeded.add(source_id)
                continue
            deadline = self._seed_deadlines.setdefault(source_id, time.monotonic() + SEED_WINDOW_S)
            existing = await self.db.all("SELECT title FROM panels WHERE org_id = ? AND source_id = ?", (self.org_id, source_id))
            titles = {row["title"] for row in existing}
            position = len(existing)
            candidates = sorted(
                (signal for signal in signals if signal["series"] and signal["group"] != "Your panels" and signal["source_id"] == source_id),
                key=lambda signal: signal["panel_priority"],
            )
            added = False
            for signal in candidates:
                if signal["title"] in titles or position >= MAX_SEEDED_PANELS:
                    continue
                titles.add(signal["title"])
                await self.db.insert(
                    "INSERT INTO panels(org_id, source_id, title, query, unit, position, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (self.org_id, source_id, signal["title"], signal["query"], signal["unit"], position, utcnow()),
                )
                position += 1
                added = True
            if added:
                self.broker.publish({"type": "panels_changed", "data": {"source_id": source_id}})
            if position >= MAX_SEEDED_PANELS or time.monotonic() >= deadline:
                await self.db.set_setting(self.org_id, flag, True)
                self._seeded.add(source_id)

    # Snapshots --------------------------------------------------------------
    async def save_snapshot(self, context: dict[str, Any], reason: str) -> int:
        self._snapshot_at = time.monotonic()
        return await self.db.insert(
            "INSERT INTO context_snapshots(org_id, version, ts, material_hash, reason, payload) VALUES (?, ?, ?, ?, ?, ?)",
            (self.org_id, context.get("version", 0), utcnow(), material_hash(context), reason, scrub(json.dumps(context, default=str))),
        )

    async def refresh_now(self) -> dict[str, Any]:
        await self.poll()
        return self.public_context()

    def is_fresh(self) -> bool:
        if self._last_ok_mono is None or not self.has_connections:
            return False
        interval = min([state.poll_seconds for state in self.states.values()] + ([INTEGRATION_EVERY_S] if self.integration_kinds else []))
        return time.monotonic() - self._last_ok_mono <= max(interval * 2, 30)


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


def _signal_meta(signal: Signal, source: Any) -> dict[str, Any]:
    return {
        "id": signal.id, "source_id": source.id, "source_name": source.name, "title": signal.title, "group": signal.group,
        "unit": signal.unit, "query": signal.query, "limit": signal.limit, "panel_priority": signal.panel_priority, "golden": GOLDEN.get(signal.id),
    }


def _severity(alert: dict[str, Any]) -> str:
    return "critical" if str(alert.get("severity", "")).lower() in {"critical", "page", "error"} else "warning"


def summarize_alerts(alerts: list[tuple[Any, dict[str, Any], bool]]) -> dict[str, Any]:
    firing, pending = [], []
    for source, alert, multi in alerts:
        labels = alert.get("labels", {})
        annotations = alert.get("annotations", {})
        shown = {key: scrub(str(value)) for key, value in labels.items() if key != "alertname"}
        if multi:
            shown["source"] = source.name
        item = {
            "name": labels.get("alertname", "Unnamed alert"),
            "severity": labels.get("severity", "unset"),
            "labels": shown,
            "summary": scrub(annotations.get("summary") or annotations.get("description") or ""),
            "description": scrub(annotations.get("description") or ""),
            "active_at": alert.get("activeAt"),
            "value": alert.get("value"),
            "source_id": source.id,
            "fingerprint": f"alert:{source.id}:" + series_key(labels),
        }
        (firing if alert.get("state") == "firing" else pending).append(item)
    rank = {"critical": 0, "page": 0, "error": 0, "warning": 1}
    firing.sort(key=lambda item: (rank.get(str(item["severity"]).lower(), 2), item["name"]))
    return {"firing": firing, "pending": pending}


def summarize_rules(groups: list[dict[str, Any]]) -> dict[str, Any]:
    alerting = recording = 0
    failing = []
    for group in groups:
        for rule in group.get("rules", []):
            if rule.get("type") == "alerting":
                alerting += 1
            else:
                recording += 1
            if rule.get("health") not in (None, "ok", "unknown"):
                failing.append({"group": group.get("name", ""), "name": rule.get("name", ""), "error": scrub(rule.get("lastError", ""))})
    return {"groups": len(groups), "alerting": alerting, "recording": recording, "failing": failing}


def material_hash(context: dict[str, Any]) -> str:
    """Hash the state that defines the incident picture, ignoring values that move every poll."""
    state = {
        "health": context.get("health", {}).get("status"),
        "alerts": sorted(alert["fingerprint"] for alert in context.get("alerts", {}).get("firing", [])),
        "down": sorted(target["fingerprint"] for target in context.get("targets", {}).get("down_list", [])),
        "anomalies": sorted(item["fingerprint"] for item in context.get("anomalies", [])),
        "rules": sorted(f"{rule['group']}:{rule['name']}" for rule in context.get("rules", {}).get("failing", [])),
        "problems": sorted(item["fingerprint"] for item in context.get("problems", [])),
    }
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def headline_for(status: str, targets: dict[str, Any], alerts: dict[str, Any], anomalies: list[dict[str, Any]], failed: list[str] | None = None,
                 k8s_problems: list[dict[str, Any]] | None = None) -> dict[str, str]:
    if status == "not_configured":
        return {"tone": "idle", "text": "Connect a metric source, a log store, or a Kubernetes cluster to start."}
    if status == "unreachable":
        return {"tone": "critical", "text": "Your connections aren't answering. These numbers are out of date."}
    firing = len(alerts.get("firing", []))
    down = targets.get("down", 0)
    k8s = k8s_problems or []
    k8s_critical = [item for item in k8s if item["severity"] == "critical"]
    parts = []
    if firing:
        parts.append(f"{_plural(firing, 'alert is', 'alerts are')} firing.")
    if down:
        parts.append(f"{_plural(down, 'target is', 'targets are')} down.")
    if k8s_critical:
        parts.append(f"{_plural(len(k8s_critical), 'Kubernetes workload is', 'Kubernetes workloads are')} failing.")
    if status == "partial" and failed:
        parts.append(f"{_plural(len(failed), 'connection isn’t', 'connections aren’t')} answering.")
    if parts:
        return {"tone": "critical", "text": " ".join(parts)}
    if anomalies or k8s:
        count = len(anomalies) + len(k8s)
        return {"tone": "warning", "text": f"Nothing is firing, but {_plural(count, 'thing looks', 'things look')} wrong."}
    if targets.get("total", 0) == 0 and status == "ok" and not alerts.get("firing"):
        return {"tone": "ok", "text": "Connected. Nothing is firing."}
    return {"tone": "ok", "text": "Everything is up. Nothing is firing."}


KIND_ORDER = {"alert": 0, "target": 1, "k8s": 2, "log": 3, "rule": 4, "signal": 5}


def ranked_problems(current: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """One list of what is wrong, worst first, for the page and for the model."""
    rows = [{"fingerprint": fingerprint, "kind": item["kind"], "severity": item["severity"], "title": item["title"],
             "detail": item["detail"]} for fingerprint, item in current.items()]
    rows.sort(key=lambda row: (-SEVERITY_RANK.get(row["severity"], 0), KIND_ORDER.get(row["kind"], 9), row["title"]))
    return rows


def age_seconds(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - when).total_seconds()


__all__ = ["ContextEngine", "EngineManager", "sha256_text"]
