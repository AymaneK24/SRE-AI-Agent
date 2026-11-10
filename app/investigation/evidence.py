"""Evidence the console gathers by itself before the model starts.

For each problem being investigated, the obvious first queries are known: the error lines of the crashing
workload, its Kubernetes description, the chart of the signal that went out of range, or whether a down target
was up before. Running them here means a model of any size starts with evidence instead of having to write
correct LogQL or PromQL first, and an API model needs fewer rounds, which costs less.
"""
from __future__ import annotations

from typing import Any

MAX_PROBLEMS = 2
MAX_QUERIES = 4
RESULT_CHARS = 2500
ERROR_WORDS = "(?i)(error|exception|fatal|panic|fail|refused|timeout)"


def _log_selector(context: dict[str, Any], namespace: str, workload: str) -> str:
    """Prefer a stream the log store actually has (Promtail names streams namespace/app), else the namespace."""
    jobs = [str(row.get("job", "")) for item in context.get("connections", []) if item.get("kind") == "loki" for row in item.get("streams", [])]
    exact = f"{namespace}/{workload}"
    if exact in jobs:
        return f'{{job="{exact}"}}'
    near = next((job for job in jobs if job.startswith(f"{namespace}/") and workload and workload in job), None)
    if near:
        return f'{{job="{near}"}}'
    return f'{{namespace="{namespace}"}}'


def plan(context: dict[str, Any], problems: list[dict[str, Any]], has_loki: bool, has_kubernetes: bool, has_metrics: bool) -> list[dict[str, Any]]:
    """The queries to run, each with the tool, its arguments, and why."""
    planned: list[dict[str, Any]] = []
    for problem in problems[:MAX_PROBLEMS]:
        detail = problem.get("detail") or {}
        kind = problem.get("kind")
        if kind == "k8s" and detail.get("namespace"):
            namespace, workload = detail["namespace"], detail.get("workload") or ""
            if has_loki:
                selector = _log_selector(context, namespace, workload)
                planned.append({"name": "query_logs", "arguments": {"query": f'{selector} |~ "{ERROR_WORDS}"', "minutes": 30},
                                "why": f"Error lines from {workload or namespace} in the last 30 minutes"})
            if has_kubernetes and workload:
                arguments = {"namespace": namespace, "name": workload}
                if detail.get("integration_id"):
                    arguments["connection_id"] = detail["integration_id"]
                planned.append({"name": "describe_object", "arguments": arguments, "why": f"Status, limits and events of {workload}"})
        elif kind == "signal" and has_metrics and detail.get("query"):
            planned.append({"name": "run_range_query", "arguments": {"query": detail["query"], "minutes": 60, "source_id": detail.get("source_id")},
                            "why": f"{detail.get('title', 'The signal')} over the last hour"})
        elif kind == "target" and has_metrics and detail.get("instance"):
            planned.append({"name": "run_range_query", "arguments": {"query": f'up{{instance="{detail["instance"]}"}}', "minutes": 60, "source_id": detail.get("source_id")},
                            "why": f"Whether {detail['instance']} answered over the last hour"})
        elif kind == "alert":
            labels = detail.get("labels") or {}
            if has_loki and labels.get("namespace"):
                selector = _log_selector(context, labels["namespace"], labels.get("deployment") or labels.get("app") or labels.get("container") or "")
                planned.append({"name": "query_logs", "arguments": {"query": f'{selector} |~ "{ERROR_WORDS}"', "minutes": 30},
                                "why": f"Error lines around the {detail.get('name', 'alert')} alert"})
            if has_kubernetes and labels.get("namespace") and (labels.get("deployment") or labels.get("pod")):
                planned.append({"name": "describe_object", "arguments": {"namespace": labels["namespace"], "name": labels.get("deployment") or labels["pod"]},
                                "why": f"Kubernetes state behind the {detail.get('name', 'alert')} alert"})
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in planned:
        key = f"{item['name']}:{sorted(item['arguments'].items())}"
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique[:MAX_QUERIES]


def section(results: list[dict[str, Any]]) -> str:
    """The gathered evidence as one block of the model's context."""
    if not results:
        return ""
    lines = ["EVIDENCE THE CONSOLE GATHERED BEFORE YOU STARTED (read-only queries it ran itself; use it before running your own):"]
    for item in results:
        state = "result" if item["ok"] else "failed"
        text = item["result"] if len(item["result"]) <= RESULT_CHARS else item["result"][:RESULT_CHARS] + " ... (cut)"
        lines.append(f"- {item['why']}. {item['name']} {item['arguments']}, {state}:\n  <tool_result>{text}</tool_result>")
    return "\n".join(lines)
