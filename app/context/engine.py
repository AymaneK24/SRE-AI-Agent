"""Keeps a current picture of one organization's systems, across all of its connections.

Metric sources, log stores, and Kubernetes clusters are each optional: any one of them is enough to build a context.
Each poll rebuilds the context from targets, alerts, rules, tracked signals, log summaries, and cluster problems. The engine compares it
with the previous poll, records what changed, and pushes it to subscribers. Investigations always read
`engine.context`, so a model never works from an old copy.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import rootcause
from ..db import utcnow
from ..secrets_store import scrub
from ..sources.base import SourceError
from ..sources.registry import build_source
from .connections import main_connections, pick_main
from .integrations import IntegrationWatch
from .polling import SourcePolling
from .problems import ProblemTracking
from .signals import COVERAGE_CHECKS
from .state import (NEVER, BLIND_AFTER_POLLS, CONSOLE_FINGERPRINT, INTEGRATION_EVERY_S, LOOP_RESTARTS, NO_SOURCE_POLL_S, POLL_FAILURES, SNAPSHOT_EVERY_S, STALLED_AFTER_S, STOP_TIMEOUT_S,
                    SourceState, log)
from .summary import headline_for, material_hash, ranked_problems, summarize_alerts, summarize_rules


class ContextEngine(SourcePolling, IntegrationWatch, ProblemTracking):
    """One organization's live picture: the polling loop, and how each poll's answers become the context."""

    async def start(self) -> None:
        await self.reload_sources()
        self._start_loop()

    def _start_loop(self) -> None:
        self._loop_beat = time.monotonic()
        self._task = asyncio.create_task(self._loop(), name=f"context-engine-{self.org_id}")

    @property
    def loop_ended(self) -> bool:
        """The polling loop ended although nobody stopped it."""
        return bool(self._task and self._task.done() and not self._stopping)

    @property
    def stalled(self) -> bool:
        """The polling loop is stuck: it hasn't come round long after it was due."""
        if self._stopping or self._task is None or self._task.done():
            return False
        return time.monotonic() - self._loop_beat > self._loop_delay + STALLED_AFTER_S

    def revive(self) -> bool:
        """Starts the polling loop again if it ended unexpectedly. True when it did."""
        if not self.loop_ended:
            return False
        error = None if self._task is None or self._task.cancelled() else self._task.exception()
        log.error("the polling loop for organization %s ended unexpectedly (%s); starting it again", self.org_id, error or "cancelled")
        LOOP_RESTARTS.inc()
        self._start_loop()
        return True

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

    async def integrations_changed(self) -> None:
        """A log store or cluster was added or removed: read it on the next poll, without waiting."""
        await self._load_integration_kinds()
        self._integrations_at = NEVER
        if "kubernetes" not in self.integration_kinds:
            self.k8s_snapshot, self.k8s_snapshots, self.k8s_problems = None, {}, []
        if not self.states and not self.integration_kinds:
            self.integration_summary, self.integration_list = {}, []
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
        self._snapshot_at = NEVER
        self._material_hash = ""
        self._last_ok_mono = None
        self.integration_summary, self.integration_list = {}, []
        self.k8s_snapshot, self.k8s_snapshots, self.k8s_problems = None, {}, []
        self._integrations_at = NEVER
        self.recent_events.clear()

    async def _loop(self) -> None:
        while not self._stopping:
            self._loop_beat = time.monotonic()
            delay = float(NO_SOURCE_POLL_S)
            if self.has_connections:
                try:
                    await self.poll()
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # One failed poll, such as a database restart, must not end monitoring.
                    log.exception("context poll failed for organization %s", self.org_id)
                    self._poll_failed(error)
                    await self._say_if_blind(error)
                else:
                    await self._say_if_seeing_again()
                if self.states:
                    delay = float(min(min(state.poll_seconds for state in self.states.values()), NO_SOURCE_POLL_S if self.integration_kinds else 3600))
            else:
                if self.source_errors:
                    self.context = self._empty_context("unreachable")
                self.broker.publish({"type": "context", "data": self.public_context()})
            if self._stopping:
                return
            self._loop_beat, self._loop_delay = time.monotonic(), delay
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                task = asyncio.current_task()
                if task and task.cancelling():
                    raise asyncio.CancelledError from None
            self._wake.clear()

    def _poll_failed(self, error: Exception) -> None:
        """Keeps the last picture, marked as out of date, and tells open pages. The next poll tries again."""
        self.poll_failures += 1
        self.failed_polls_in_a_row += 1
        POLL_FAILURES.inc()
        health = {**self.context.get("health", {}), "error": scrub(f"The console couldn't finish its last check ({type(error).__name__}). It will try again.")}
        self.context = {**self.context, "health": health, "stale": True}
        self.broker.publish({"type": "context", "data": self.public_context()})

    async def _say_if_blind(self, error: Exception) -> None:
        """After several failed polls in a row, the alert channels hear that the console can't complete its checks.

        This often can't be delivered, because what stops the polls, usually the database, also stops the message.
        The heartbeat covers that case from outside.
        """
        if self.failed_polls_in_a_row != BLIND_AFTER_POLLS or self._blind_announced:
            return
        try:
            await self.record_event("polling_started", "critical", CONSOLE_FINGERPRINT, "The console can't complete its checks",
                                    {"summary": f"{BLIND_AFTER_POLLS} checks in a row ended with an error ({type(error).__name__}), so problems in your systems "
                                                "may go unnoticed until this clears. Look at the console's log and its database."})
            self._blind_announced = True
        except Exception:
            log.exception("couldn't record that polling is failing")

    async def _say_if_seeing_again(self) -> None:
        self.failed_polls_in_a_row = 0
        if not self._blind_announced:
            return
        self._blind_announced = False
        try:
            await self.record_event("polling_cleared", "info", CONSOLE_FINGERPRINT, "The console completes its checks again", {})
        except Exception:
            log.exception("couldn't record that polling works again")

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
            main = self.main_connections.get("metrics")
            return (self.states[main] if main in self.states else next(iter(self.states.values()))).source
        if not self.states:
            raise SourceError("No metric source is connected, so metric queries can't run. Use the log and Kubernetes tools instead.", "bad_data")
        raise SourceError("That metric source isn't connected.", "bad_data")

    async def poll(self) -> None:
        async with self._poll_lock:
            if not self.has_connections:
                return
            started = time.monotonic()
            results = list(await asyncio.gather(*(self._poll_source(state) for state in self.states.values())))
            await self._refresh_integrations()
            await self._merge(results, started)

    async def _merge(self, results: list[dict[str, Any] | None], started: float) -> None:
        now_iso = utcnow()
        sources_info, targets_raw, alerts_raw, groups_raw, signals = [], [], [], [], []
        any_ok = any(result is not None for result in results) or any(item.get("status") == "ok" for item in self.integration_list)
        self.main_connections = await main_connections(self.db, self.org_id)
        main_source = pick_main([{"id": state.source.id} for state in self.states.values()], self.main_connections.get("metrics"))
        for state in self.states.values():
            source = state.source
            sources_info.append({
                "id": source.id, "name": source.name, "kind": source.kind, "url": source.describe(),
                "version": state.info.get("version"), "poll_seconds": state.poll_seconds,
                "query_language": source.query_language, "logs_language": source.logs_language,
                "status": "ok" if state.failures == 0 else "unreachable", "error": state.error,
                "last_ok": state.last_ok_iso, "stale": state.failures > 0 and state.last is not None,
                "main": source.id == main_source,
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
        failed_connections = failed + [{"name": item.get("name", item["kind"]), "error": item.get("error")} for item in self.integration_list if item.get("status") != "ok"]
        if not sources_info and not self.integration_list:
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
        problems = ranked_problems(current)
        for problem in problems:
            problem["since"] = self._active.get(problem["fingerprint"], {}).get("since")
        try:
            groups = rootcause.analyze(problems, list(self.k8s_snapshots.values()), changes)
        except Exception:  # A hypothesis must never stop monitoring.
            log.exception("root cause ranking failed")
            groups = []
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
            "connections": self.integration_list,
            "main": self.main_connections,
            "problems": problems,
            # Problems that look connected, with the likely root of each group and why. A hypothesis to test.
            "groups": groups,
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
