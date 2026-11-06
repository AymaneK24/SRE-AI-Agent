"""Read-only tools a model can use during an investigation.

No tool can change a monitored system: metric query APIs can't write data, and nothing here runs commands.
All results are limited in size and scrubbed of obvious credentials.
"""
from __future__ import annotations

import json
import time
from typing import Any

from ..context.engine import ContextEngine, build_integration
from ..db import Database
from ..net_guard import BlockedTarget
from ..secrets_store import scrub
from ..sources.base import SourceError

MAX_RESULT_CHARS = 6000
MAX_SERIES = 15
MAX_QUERY_CHARS = 4000
_SOURCE_PROP = {"source_id": {"type": "integer", "description": "Which source to use. Leave out when only one is connected."}}

BASE_TOOLS: list[dict[str, Any]] = [
    {
        "name": "run_instant_query",
        "description": "Run a query at the current moment and get each series' current value, using the source's query language shown in the context.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string", "description": "The query."}, **_SOURCE_PROP}, "required": ["query"]},
    },
    {
        "name": "run_range_query",
        "description": "Run a query over a recent window. Returns first, last, min, max, mean, and the time of the largest change per series. Use it to find when something started.",
        "input_schema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "The query."},
            "minutes": {"type": "integer", "description": "Window length, 1 to 1440. Defaults to 60."}, **_SOURCE_PROP}, "required": ["query"]},
    },
    {
        "name": "list_metrics",
        "description": "List metric names that contain some text, to find what data exists before writing a query.",
        "input_schema": {"type": "object", "properties": {"contains": {"type": "string", "description": "Text the metric name must contain."}, **_SOURCE_PROP}, "required": ["contains"]},
    },
    {
        "name": "get_recent_changes",
        "description": "List recorded deployments and configuration changes from the last 24 hours, newest first.",
        "input_schema": {"type": "object", "properties": {"contains": {"type": "string", "description": "Optional text to filter by, such as a service name."}}},
    },
    {
        "name": "search_past_investigations",
        "description": "Search earlier investigations and their confirmed root causes. Past results may not apply now, so verify them against current data.",
        "input_schema": {"type": "object", "properties": {"text": {"type": "string", "description": "Words to search for, such as an alert or job name."}}, "required": ["text"]},
    },
]
LOGS_TOOL = {
    "name": "query_logs",
    "description": "Search logs for a recent window. Use LogQL when Loki is connected, otherwise the source's logs language shown in the context.",
    "input_schema": {"type": "object", "properties": {
        "query": {"type": "string", "description": "The log query."},
        "minutes": {"type": "integer", "description": "Window length, 1 to 1440. Defaults to 30."}, **_SOURCE_PROP}, "required": ["query"]},
}
KUBERNETES_TOOL = {
    "name": "get_kubernetes_state",
    "description": "Read Kubernetes state: warning events, unhealthy pods, or deployments and their images.",
    "input_schema": {"type": "object", "properties": {
        "resource": {"type": "string", "enum": ["events", "pods", "deployments"]},
        "namespace": {"type": "string", "description": "Optional namespace."}}, "required": ["resource"]},
}
TOOL_DEFINITIONS = BASE_TOOLS + [LOGS_TOOL, KUBERNETES_TOOL]


class ToolRunner:
    def __init__(self, engine: ContextEngine, db: Database, box: Any, config: Any, owner_id: int | None = None) -> None:
        self.engine, self.db, self.box, self.config = engine, db, box, config
        self.owner_id = owner_id

    async def definitions(self) -> list[dict[str, Any]]:
        tools = list(BASE_TOOLS)
        rows = await self.db.all("SELECT kind FROM integrations WHERE org_id = ? AND enabled = 1", (self.engine.org_id,))
        kinds = {row["kind"] for row in rows}
        if "loki" in kinds or any(state.source.logs_language for state in self.engine.states.values()):
            tools.append(LOGS_TOOL)
        if "kubernetes" in kinds:
            tools.append(KUBERNETES_TOOL)
        return tools

    async def run(self, name: str, arguments: dict[str, Any]) -> tuple[bool, str]:
        """Return (ok, text). Errors go back to the model so it can correct its query."""
        try:
            handler = {
                "run_instant_query": self._instant, "run_range_query": self._range, "list_metrics": self._metrics,
                "get_recent_changes": self._changes, "search_past_investigations": self._past,
                "query_logs": self._logs, "get_kubernetes_state": self._kubernetes,
            }.get(name)
            if handler is None:
                return False, f"Unknown tool: {name}"
            result = await handler(arguments)
            return True, _limit(scrub(json.dumps(result, ensure_ascii=False, default=str)))
        except (SourceError, BlockedTarget) as error:
            return False, scrub(f"Query error: {getattr(error, 'message', error)}")
        except (TypeError, ValueError, KeyError) as error:
            return False, f"Invalid arguments: {error}"

    def _source(self, arguments: dict[str, Any]) -> Any:
        raw = arguments.get("source_id")
        return self.engine.source(int(raw) if raw not in (None, "") else None)

    @staticmethod
    def _query_arg(arguments: dict[str, Any]) -> str:
        query = str(arguments.get("query", "")).strip()
        if not query:
            raise ValueError("query is required")
        if len(query) > MAX_QUERY_CHARS:
            raise ValueError(f"query is longer than {MAX_QUERY_CHARS} characters")
        return query

    async def _instant(self, arguments: dict[str, Any]) -> dict[str, Any]:
        data = await self._source(arguments).query(self._query_arg(arguments))
        kind = data.get("resultType")
        result = data.get("result", [])
        if kind != "vector":
            return {"type": kind, "result": result}
        series = [{"labels": item.get("metric", {}), "value": item.get("value", [None, None])[1]} for item in result]
        series.sort(key=lambda item: _sort_number(item["value"]), reverse=True)
        return {"series_total": len(series), "series_shown": min(len(series), MAX_SERIES), "series": series[:MAX_SERIES]}

    async def _range(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = self._query_arg(arguments)
        minutes = max(1, min(int(arguments.get("minutes") or 60), 1440))
        end = time.time()
        step = max(15, round(minutes * 60 / 120))
        data = await self._source(arguments).query_range(query, end - minutes * 60, end, step)
        rows = [_summarize_range(item) for item in data.get("result", [])]
        rows.sort(key=lambda row: abs(row.get("max", 0) or 0), reverse=True)
        return {"window_minutes": minutes, "step_seconds": step, "series_total": len(rows), "series_shown": min(len(rows), MAX_SERIES), "series": rows[:MAX_SERIES]}

    async def _metrics(self, arguments: dict[str, Any]) -> dict[str, Any]:
        text = str(arguments.get("contains", "")).lower()
        names = await self._source(arguments).metric_names()
        matches = [name for name in names if text in name.lower()]
        return {"total": len(matches), "metrics": matches[:100]}

    async def _changes(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from ..db import utc_after

        rows = await self.db.all("SELECT ts, origin, service, title FROM change_events WHERE org_id = ? AND ts >= ? ORDER BY ts DESC LIMIT 100",
                                 (self.engine.org_id, utc_after(hours=-24)))
        text = str(arguments.get("contains") or "").lower()
        if text:
            rows = [row for row in rows if text in f"{row['service']} {row['title']}".lower()]
        return {"total": len(rows), "changes": rows[:40]}

    async def _past(self, arguments: dict[str, Any]) -> dict[str, Any]:
        terms = str(arguments.get("text", "")).split()
        return {"note": "Past records may not apply now.", "results": await related_investigations(self.db, self.engine.org_id, terms, owner_id=self.owner_id)}

    async def _integration(self, kind: str) -> Any:
        row = await self.db.one("SELECT * FROM integrations WHERE org_id = ? AND kind = ? AND enabled = 1 LIMIT 1", (self.engine.org_id, kind))
        return build_integration(row, self.box, self.config) if row else None

    async def _logs(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = self._query_arg(arguments)
        minutes = max(1, min(int(arguments.get("minutes") or 30), 1440))
        loki = await self._integration("loki")
        if loki:
            try:
                return await loki.query(query, minutes)
            finally:
                await loki.close()
        return await self._source(arguments).logs(query, minutes)

    async def _kubernetes(self, arguments: dict[str, Any]) -> dict[str, Any]:
        client = await self._integration("kubernetes")
        if not client:
            raise SourceError("No Kubernetes connection is set up.")
        namespace = arguments.get("namespace") or None
        try:
            resource = arguments.get("resource")
            if resource == "events":
                return {"events": await client.warning_events(namespace)}
            if resource == "pods":
                return {"unhealthy_pods": await client.unhealthy_pods(namespace)}
            if resource == "deployments":
                return {"deployments": await client.rollouts(namespace)}
            raise ValueError("resource must be events, pods, or deployments")
        finally:
            await client.close()


async def related_investigations(db: Database, org_id: int, terms: list[str], exclude_id: int | None = None, limit: int = 3, owner_id: int | None = None) -> list[dict[str, Any]]:
    """Past work the person asking may read: their own, and what was shared with the team. Someone's private investigation never reaches another person's model."""
    ids = await db.search_conversation_ids(org_id, terms, limit=limit + 20)
    output = []
    for conversation_id in ids:
        if conversation_id == exclude_id:
            continue
        row = await db.one("SELECT * FROM conversations WHERE id = ? AND org_id = ?", (conversation_id, org_id))
        if not row or not (row["visibility"] == "team" or (owner_id is not None and row["owner_id"] == owner_id)):
            continue
        answer = await db.one(
            "SELECT content FROM messages WHERE conversation_id = ? AND role = 'assistant' AND status = 'complete' ORDER BY id DESC LIMIT 1", (conversation_id,))
        output.append({
            "id": row["id"], "date": row["created_at"][:10], "title": row["title"], "status": row["status"],
            "confirmed_root_cause": row["root_cause_note"], "last_answer_excerpt": (answer["content"][:300] if answer else ""),
        })
        if len(output) >= limit:
            break
    return output


def _sort_number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float("-inf")
    return number if number == number else float("-inf")


def _summarize_range(item: dict[str, Any]) -> dict[str, Any]:
    points = []
    for timestamp, raw in item.get("values", []):
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value == value and abs(value) != float("inf"):
            points.append((timestamp, value))
    labels = item.get("metric", {})
    if not points:
        return {"labels": labels, "points": 0}
    values = [value for _, value in points]
    largest_at, largest_change = None, 0.0
    for (_, before), (when, after) in zip(points, points[1:], strict=False):
        if abs(after - before) > abs(largest_change):
            largest_at, largest_change = when, after - before
    return {
        "labels": labels, "points": len(points), "first": round(values[0], 4), "last": round(values[-1], 4),
        "min": round(min(values), 4), "max": round(max(values), 4), "mean": round(sum(values) / len(values), 4),
        "largest_step_change": round(largest_change, 4),
        "largest_step_change_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(largest_at)) if largest_at else None,
    }


def _limit(text: str) -> str:
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return text[:MAX_RESULT_CHARS] + f"... [cut at {MAX_RESULT_CHARS} characters; narrow the query]"
