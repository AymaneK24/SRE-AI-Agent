"""Read-only tools a model can use during an investigation.

No tool can change a monitored system: metric query APIs can't write data, and nothing here runs commands.
All results are limited in size and scrubbed of obvious credentials.
"""
from __future__ import annotations

import json
import time
from typing import Any

from ..context.engine import ContextEngine, build_integration, pick_main
from ..db import Database
from ..net_guard import BlockedTarget
from ..secrets_store import scrub
from ..sources.base import SourceError
from .logql import repair_logql, simple_logql

MAX_RESULT_CHARS = 6000
MAX_SERIES = 15
MAX_QUERY_CHARS = 4000
_SOURCE_PROP = {"source_id": {"type": "integer", "description": "Which source to use. Leave out when only one is connected."}}
_CONNECTION_PROP = {"connection_id": {"type": "integer", "description": "Which Loki connection or cluster to use, by the connection_id in the context. Leave out to use the main one."}}

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
    "description": "Search a log store for a recent window. With Loki, write LogQL: a stream selector in braces, then filters, "
                   "such as {namespace=\"shop\", app=\"payment\"} |= \"ERROR\". Otherwise use the source's logs language shown in the context.",
    "input_schema": {"type": "object", "properties": {
        "query": {"type": "string", "description": "The log query."},
        "minutes": {"type": "integer", "description": "Window length, 1 to 1440. Defaults to 30."}, **_SOURCE_PROP, **_CONNECTION_PROP}, "required": ["query"]},
}
KUBERNETES_TOOL = {
    "name": "get_kubernetes_state",
    "description": "Read Kubernetes state: the problems the console found (with diagnoses), warning events, unhealthy pods, deployments and their images, "
                   "services and whether their selector reaches ready pods, or nodes.",
    "input_schema": {"type": "object", "properties": {
        "resource": {"type": "string", "enum": ["problems", "events", "pods", "deployments", "services", "nodes"]},
        "namespace": {"type": "string", "description": "Optional namespace."}, **_CONNECTION_PROP}, "required": ["resource"]},
}
POD_LOGS_TOOL = {
    "name": "get_pod_logs",
    "description": "Read the last lines a pod wrote, straight from Kubernetes. Set previous=true to read the container that crashed, "
                   "which is where a crash-looping pod's error is. Give a pod name, or a workload name to pick its worst pod.",
    "input_schema": {"type": "object", "properties": {
        "namespace": {"type": "string"},
        "pod": {"type": "string", "description": "Pod name, or the name of its Deployment, StatefulSet, DaemonSet, or Job."},
        "container": {"type": "string", "description": "Optional container name."},
        "previous": {"type": "boolean", "description": "Read the previous, crashed container. Defaults to false."},
        "lines": {"type": "integer", "description": "How many lines from the end, 1 to 200. Defaults to 80."}, **_CONNECTION_PROP}, "required": ["namespace", "pod"]},
}
DESCRIBE_TOOL = {
    "name": "describe_object",
    "description": "Status, containers, restarts, exit codes, limits, and recent events for one Kubernetes pod or workload, like kubectl describe.",
    "input_schema": {"type": "object", "properties": {
        "namespace": {"type": "string"},
        "name": {"type": "string", "description": "Pod name, or a Deployment, StatefulSet, DaemonSet, or Job name."}, **_CONNECTION_PROP}, "required": ["namespace", "name"]},
}
METRIC_TOOL_NAMES = {"run_instant_query", "run_range_query", "list_metrics"}
TOOL_DEFINITIONS = BASE_TOOLS + [LOGS_TOOL, KUBERNETES_TOOL, POD_LOGS_TOOL, DESCRIBE_TOOL]


class ToolRunner:
    def __init__(self, engine: ContextEngine, db: Database, box: Any, config: Any, owner_id: int | None = None) -> None:
        self.engine, self.db, self.box, self.config = engine, db, box, config
        self.owner_id = owner_id
        # What the console corrected in a query, told to the model with the result instead of failing the query.
        self._notes: list[str] = []

    async def _kinds(self) -> set[str]:
        rows = await self.db.all("SELECT kind FROM integrations WHERE org_id = ? AND enabled = 1", (self.engine.org_id,))
        return {row["kind"] for row in rows}

    async def definitions(self) -> list[dict[str, Any]]:
        """Only the tools that can work here, so a model isn't offered queries against something that isn't connected."""
        kinds = await self._kinds()
        tools = [tool for tool in BASE_TOOLS if self.engine.states or tool["name"] not in METRIC_TOOL_NAMES]
        if "loki" in kinds or any(state.source.logs_language for state in self.engine.states.values()):
            tools.append(LOGS_TOOL)
        if "kubernetes" in kinds:
            tools += [KUBERNETES_TOOL, POD_LOGS_TOOL, DESCRIBE_TOOL]
        return tools

    async def _hint(self, name: str) -> str:
        """One sentence that points a model to the right tool and language, because small models often guess wrong."""
        kinds = await self._kinds()
        parts = []
        if name in METRIC_TOOL_NAMES:
            languages = sorted({state.source.query_language for state in self.engine.states.values()})
            if languages:
                parts.append(f"Metric queries here use {', '.join(languages)}.")
            else:
                parts.append("No metric source is connected, so metric queries can't run.")
        if name == "query_logs" and "loki" in kinds:
            parts.append('Loki uses LogQL: a stream selector in braces, then filters, such as {namespace="shop"} |= "error".')
        if "kubernetes" in kinds and name not in {"get_kubernetes_state", "describe_object", "get_pod_logs"}:
            parts.append("For pods, crashes, and restarts use get_kubernetes_state, describe_object, or get_pod_logs with previous=true.")
        if ("loki" in kinds or any(state.source.logs_language for state in self.engine.states.values())) and name != "query_logs":
            parts.append("For log lines use query_logs.")
        return " ".join(parts)

    async def run(self, name: str, arguments: dict[str, Any]) -> tuple[bool, str]:
        """Return (ok, text). Errors go back to the model so it can correct its query."""
        try:
            handler = {
                "run_instant_query": self._instant, "run_range_query": self._range, "list_metrics": self._metrics,
                "get_recent_changes": self._changes, "search_past_investigations": self._past,
                "query_logs": self._logs, "get_kubernetes_state": self._kubernetes,
                "get_pod_logs": self._pod_logs, "describe_object": self._describe,
            }.get(name)
            if handler is None:
                available = ", ".join(tool["name"] for tool in await self.definitions())
                return False, f"Unknown tool: {name}. The tools you can use are: {available}."
            self._notes = []
            result = await handler(arguments)
            if self._notes and isinstance(result, dict):
                result = {"note": " ".join(self._notes), **result}
            return True, _limit(scrub(json.dumps(result, ensure_ascii=False, default=str)))
        except (SourceError, BlockedTarget) as error:
            return False, scrub(f"Query error: {getattr(error, 'message', error)} {await self._hint(name)}".strip())
        except (TypeError, ValueError, KeyError) as error:
            return False, f"Invalid arguments: {error} {await self._hint(name)}".strip()

    def _source(self, arguments: dict[str, Any]) -> Any:
        raw = arguments.get("source_id")
        try:
            wanted = int(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            wanted = None
        if wanted is not None and wanted not in self.engine.states:
            # Small models often invent an id. Use the main source and say so, instead of failing the query.
            self._notes.append(f"There is no metric source {raw}, so the main source was used.")
            wanted = None
        return self.engine.source(wanted)

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

    async def _integration_row(self, kind: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        """The connection the model named, or the main one of this kind."""
        rows = await self.db.all("SELECT * FROM integrations WHERE org_id = ? AND kind = ? AND enabled = 1 ORDER BY id", (self.engine.org_id, kind))
        if not rows:
            return None
        wanted = arguments.get("connection_id")
        main = pick_main(rows, self.engine.main_connections.get(kind))
        if wanted not in (None, ""):
            for row in rows:
                if str(row["id"]) == str(wanted).strip():
                    return row
            # An unknown id is a model's guess. Use the main connection and say so, instead of failing the query.
            if len(rows) > 1:
                self._notes.append(f"There is no {kind} connection {wanted}, so the main one, {next(row['name'] for row in rows if row['id'] == main)}, was used.")
        return next(row for row in rows if row["id"] == main)

    async def _integration(self, kind: str, arguments: dict[str, Any] | None = None) -> Any:
        row = await self._integration_row(kind, arguments or {})
        return build_integration(row, self.box, self.config) if row else None

    async def _logs(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = self._query_arg(arguments)
        minutes = max(1, min(int(arguments.get("minutes") or 30), 1440))
        loki = await self._integration("loki", arguments) if arguments.get("source_id") is None else None
        if loki:
            try:
                repaired, fixes = repair_logql(query)
                if fixes:
                    self._notes.append(f"The query was corrected to valid LogQL ({'; '.join(fixes)}): {repaired}")
                try:
                    return await loki.query(repaired, minutes)
                except SourceError as error:
                    if "parse error" not in error.message.lower() and error.kind not in {"bad_data", "query"}:
                        raise
                    # Still not valid LogQL: search the same streams for the words the model was after.
                    fallback = simple_logql(query)
                    self._notes.append(f"Loki couldn't read the query ({error.message}), so this simpler search ran instead: {fallback}")
                    return await loki.query(fallback, minutes)
            finally:
                await loki.close()
        return await self._source(arguments).logs(query, minutes)

    async def _kubernetes(self, arguments: dict[str, Any]) -> dict[str, Any]:
        resource = arguments.get("resource")
        namespace = arguments.get("namespace") or None
        row = await self._integration_row("kubernetes", arguments)
        if resource == "problems":
            several = len(self.engine.k8s_snapshots) > 1
            problems = [item for item in self.engine.k8s_problems if (not namespace or item["namespace"] == namespace)
                        and (not several or arguments.get("connection_id") is None or item.get("integration_id") == (row or {}).get("id"))]
            return {"problems": [{key: item.get(key) for key in ("title", "severity", "object", "pods", "facts", "diagnosis")} for item in problems]}
        client = await self._kube_client(arguments)
        try:
            if resource == "events":
                return {"events": await client.warning_events(namespace)}
            if resource == "pods":
                return {"unhealthy_pods": await client.unhealthy_pods(namespace)}
            if resource == "deployments":
                return {"deployments": await client.rollouts(namespace)}
            if resource == "services":
                try:
                    return {"services": await client.services(namespace)}
                except SourceError as error:
                    if error.kind == "forbidden":
                        return {"services": [], "note": "This token may not list services. Add get and list on services to its role."}
                    raise
            if resource == "nodes":
                snapshot = self.engine.k8s_snapshots.get((row or {}).get("id")) or await client.snapshot()
                return {"nodes": snapshot.get("nodes", []), "note": "Node access is forbidden for this token." if "nodes" in snapshot.get("forbidden", []) else ""}
            raise ValueError("resource must be problems, events, pods, deployments, services, or nodes")
        finally:
            await client.close()

    async def _kube_client(self, arguments: dict[str, Any]) -> Any:
        client = await self._integration("kubernetes", arguments)
        if not client:
            raise SourceError("No Kubernetes connection is set up.")
        return client

    def _pods_for(self, namespace: str, name: str, pods: list[dict[str, Any]]) -> list[dict[str, Any]]:
        exact = [pod for pod in pods if pod["namespace"] == namespace and pod["name"] == name]
        if exact:
            return exact
        owned = [pod for pod in pods if pod["namespace"] == namespace and pod["workload"] == name]
        owned.sort(key=lambda pod: (-pod["restarts"], pod["ready"], pod["name"]))
        return owned

    async def _pod_logs(self, arguments: dict[str, Any]) -> dict[str, Any]:
        namespace, name = str(arguments.get("namespace", "")).strip(), str(arguments.get("pod", "")).strip()
        if not namespace or not name:
            raise ValueError("namespace and pod are required")
        lines = max(1, min(int(arguments.get("lines") or 80), 200))
        client = await self._kube_client(arguments)
        try:
            pods = self._pods_for(namespace, name, await client.pods(namespace))
            if not pods:
                raise SourceError(f"No pod or workload named {name} in namespace {namespace}.", "not_found")
            pod = pods[0]
            result = await client.pod_logs(namespace, pod["name"], str(arguments.get("container") or ""), bool(arguments.get("previous")), lines)
            return {"pod": pod["name"], "previous": bool(arguments.get("previous")), "restarts": pod["restarts"], **result}
        finally:
            await client.close()

    async def _describe(self, arguments: dict[str, Any]) -> dict[str, Any]:
        namespace, name = str(arguments.get("namespace", "")).strip(), str(arguments.get("name", "")).strip()
        if not namespace or not name:
            raise ValueError("namespace and name are required")
        client = await self._kube_client(arguments)
        try:
            pods = self._pods_for(namespace, name, await client.pods(namespace))
            deployments = [row for row in await client.rollouts(namespace) if row["name"] == name]
            if not pods and not deployments:
                raise SourceError(f"No pod or workload named {name} in namespace {namespace}.", "not_found")
            events = await client.object_events(namespace, pods[0]["name"] if pods else name)
            problems = [item for item in self.engine.k8s_problems if item["namespace"] == namespace and (item["workload"] == name or name in item["pods"])]
            return {
                "deployment": deployments[0] if deployments else None,
                "pods": [{key: pod[key] for key in ("name", "phase", "ready", "restarts", "node", "containers", "limits", "schedule_message")} for pod in pods[:5]],
                "events": events,
                "problems": [{key: item.get(key) for key in ("title", "severity", "facts", "diagnosis")} for item in problems],
            }
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
