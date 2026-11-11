"""Reads one metric source: targets, alerts, rules and tracked signals, with baselines for what is unusual."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from ..db import utcnow
from ..net_guard import BlockedTarget
from ..secrets_store import scrub
from ..sources.base import SourceError
from .connections import store_change_event
from .signals import Signal
from .state import (ANOMALY_Z, BASELINE_EVERY_S, CHANGES_EVERY_S, MAX_SEEDED_PANELS, MAX_SERIES_PER_SIGNAL, METRICS_EVERY_S, SEED_WINDOW_S,
                    EngineState, SourceState, log)
from .summary import signal_meta, compute_baseline, parse_value, robust_z, series_key


class SourcePolling(EngineState):
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
            await self.record_event("source_recovered", "info", f"source:{source.id}",
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
            await self.record_event("source_unreachable", "critical", f"source:{state.source.id}",
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
                    return {**signal_meta(signal, state.source), "series": [], "total_series": 0, "error": error.message}
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
        return {**signal_meta(signal, state.source), "series": rows[:MAX_SERIES_PER_SIGNAL], "total_series": len(rows), "error": None}

    async def _store_source_changes(self, source: Any) -> None:
        since = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
        for item in await source.change_events(since):
            await store_change_event(self.db, self.org_id, item)

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
