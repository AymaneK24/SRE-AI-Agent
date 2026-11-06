"""Remediation workflows: reviewed instructions for people. This system never runs them."""
from __future__ import annotations

import json
import re
from typing import Any

from .db import Database, utcnow


def _step(title: str, detail: str, command: str = "", risk: str = "none", undo: str = "", verify: str = "") -> dict[str, str]:
    return {"title": title, "detail": detail, "command": command, "risk": risk, "undo": undo, "verify": verify}


BUILTIN_WORKFLOWS: list[dict[str, Any]] = [
    {
        "name": "Target can't be scraped", "summary": "Find out whether a service is down or only unreachable by Prometheus.", "risk": "low",
        "match": {"alerts": ["targetdown", "instancedown"], "keywords": ["target down", "can't be scraped"]},
        "steps": [
            _step("Read the scrape error", "Open Targets in Prometheus and read the last error for this target."),
            _step("Test the endpoint from the Prometheus host", "A timeout suggests a network or firewall problem. A refused connection suggests the process is down.", "curl -sS -m 5 http://TARGET:PORT/metrics | head"),
            _step("Check the service", "Confirm whether the process is running and read its recent logs.", "systemctl status SERVICE && journalctl -u SERVICE -n 100"),
            _step("Restart only if it's stopped", "Check for active work first. A restart drops connections.", "systemctl restart SERVICE", "medium", "A restart can't be undone. Roll back the last change if it fails again.", "The target shows as up within two scrape intervals."),
        ],
    },
    {
        "name": "High CPU use", "summary": "Find what's using CPU, then decide whether to scale or fix it.", "risk": "medium",
        "match": {"alerts": ["highcpu", "cpu"], "signals": ["node_cpu", "container_cpu", "process_cpu"], "keywords": ["cpu use", "load per cpu"]},
        "steps": [
            _step("Find the top processes", "Note the process and whether its use started after a change.", "ps -eo pid,comm,%cpu,%mem --sort=-%cpu | head -15"),
            _step("Compare with traffic", "If requests rose at the same time, this may be load rather than a fault.", "sum(rate(http_requests_total[5m]))"),
            _step("Check recent changes", "Look for a deployment or configuration change just before the rise."),
            _step("Add capacity or roll back", "Scale out for real load. Roll back if a release caused it.", "kubectl scale deployment/APP --replicas=N -n NAMESPACE", "medium", "Scale back to the previous replica count.", "CPU falls below its limit and response times recover."),
        ],
    },
    {
        "name": "Memory running out", "summary": "Separate a memory leak from normal growth before anything gets killed.", "risk": "medium",
        "match": {"alerts": ["memory", "oom"], "signals": ["node_memory", "process_memory", "kube_oom"], "keywords": ["memory use", "killed for memory"]},
        "steps": [
            _step("Find the largest processes", "Note which process is growing.", "ps -eo pid,comm,%mem,rss --sort=-rss | head -15"),
            _step("Check for out-of-memory kills", "This confirms whether the kernel is already killing processes.", "journalctl -k -g 'Out of memory' -n 20"),
            _step("Look at the growth over time", "Steady growth without more traffic suggests a leak.", "process_resident_memory_bytes"),
            _step("Restart or raise the limit", "A restart buys time but doesn't fix a leak, so open a ticket too.", "kubectl rollout restart deployment/APP -n NAMESPACE", "medium", "Roll back with kubectl rollout undo if the new pods fail.", "Memory drops and stays stable for an hour."),
        ],
    },
    {
        "name": "Disk filling up", "summary": "Free space safely without deleting something you need.", "risk": "high",
        "match": {"alerts": ["disk", "filesystem"], "signals": ["node_disk", "node_io"], "keywords": ["disk space"]},
        "steps": [
            _step("Find what's using the space", "Start at the affected mount.", "du -xh --max-depth=2 /MOUNT | sort -h | tail -20"),
            _step("Look for deleted files still held open", "These use space until the process restarts.", "lsof +L1 | head -20"),
            _step("Clear only what's known to be safe", "Review the list first. Deleted data can't be recovered.", "journalctl --vacuum-size=500M", "high", "Deleted files need restoring from backup.", "Free space rises and stays above your limit."),
            _step("Plan for growth", "Add retention rules or more disk so this doesn't return."),
        ],
    },
    {
        "name": "Error rate is high", "summary": "Find the failing dependency or release behind the errors.", "risk": "medium",
        "match": {"alerts": ["higherrorrate", "error"], "signals": ["http_errors_code", "http_errors_status"], "keywords": ["failed requests"]},
        "steps": [
            _step("Split errors by route and code", "This shows whether one endpoint or everything is failing.", "sum by (route, code) (rate(http_requests_total{code=~\"5..\"}[5m]))"),
            _step("Read the logs for the first failures", "Search around the time errors started.", "kubectl logs deployment/APP -n NAMESPACE --since=15m | grep -i error"),
            _step("Check dependencies", "Look at databases, caches, and upstream services for saturation or timeouts."),
            _step("Roll back if a release caused it", "Compare the start of errors with the last deployment.", "kubectl rollout undo deployment/APP -n NAMESPACE", "medium", "Deploy the newer version again after fixing it.", "The error rate returns below its limit."),
        ],
    },
    {
        "name": "Responses are slow", "summary": "Find where the time is going before changing capacity.", "risk": "medium",
        "match": {"alerts": ["slowresponses", "latency"], "signals": ["http_p95"], "keywords": ["response time"]},
        "steps": [
            _step("Confirm the scope", "Check whether all routes are slow or only some.", "histogram_quantile(0.95, sum by (route, le) (rate(http_request_duration_seconds_bucket[5m])))"),
            _step("Check for saturation", "Look at CPU, memory, connection pools, and queues."),
            _step("Check dependencies", "Compare database and upstream timings with the rise in latency."),
            _step("Reduce load or add capacity", "Scale out if the service is saturated. Don't add capacity if a dependency is the cause.", "kubectl scale deployment/APP --replicas=N -n NAMESPACE", "medium", "Scale back to the previous replica count.", "p95 latency returns below its target."),
        ],
    },
    {
        "name": "Containers keep restarting", "summary": "Find the exit reason before restarting anything again.", "risk": "low",
        "match": {"alerts": ["crashloop", "restart"], "signals": ["kube_restarts", "kube_not_ready"], "keywords": ["container restarts", "pods not ready"]},
        "steps": [
            _step("Read the pod's events and exit reason", "Look for exit codes, failed probes, and missing configuration.", "kubectl describe pod POD -n NAMESPACE"),
            _step("Read the previous container's logs", "These show why the last run ended.", "kubectl logs POD -n NAMESPACE --previous --tail=100"),
            _step("Compare with recent changes", "Check for a new image, configuration, or secret."),
            _step("Roll back if a release caused it", "Only roll back if the timing matches.", "kubectl rollout undo deployment/APP -n NAMESPACE", "medium", "Deploy the newer version again after fixing it.", "Restarts stop and pods stay ready for 15 minutes."),
        ],
    },
]


async def seed_workflows(db: Database, org_id: int) -> None:
    now = utcnow()
    for item in BUILTIN_WORKFLOWS:
        await db.insert(
            "INSERT INTO workflows(org_id, name, summary, match_rules, risk, steps, status, origin, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'approved', 'built-in', 'Root Cause Console', ?, ?)",
            (org_id, item["name"], item["summary"], json.dumps(item["match"]), item["risk"], json.dumps(item["steps"]), now, now),
        )


def public_workflow(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "match_rules": json.loads(row["match_rules"]), "steps": json.loads(row["steps"])}


def new_run_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A copy of the steps at the moment someone starts. Later edits to the workflow don't change a run in progress."""
    return [{**step, "state": "pending", "note": "", "by": "", "at": None} for step in steps]


def public_run(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "steps": json.loads(row["steps"])}


def _matches(rules: dict[str, Any], names: set[str], signals: set[str], text: str) -> bool:
    if any(any(rule.lower() in name for name in names) for rule in rules.get("alerts", [])):
        return True
    if signals & {item for item in rules.get("signals", [])}:
        return True
    return any(keyword.lower() in text for keyword in rules.get("keywords", []))


async def workflows_for(db: Database, org_id: int, *, alert_names: list[str] | None = None, signal_ids: list[str] | None = None, text: str = "") -> list[dict[str, Any]]:
    rows = await db.all("SELECT * FROM workflows WHERE org_id = ? AND status <> 'archived' ORDER BY id", (org_id,))
    names = {name.lower() for name in alert_names or []}
    signals = {re.sub(r"^s\d+_", "", item) for item in signal_ids or []}
    lowered = text.lower()
    return [public_workflow(row) for row in rows if _matches(json.loads(row["match_rules"]), names, signals, lowered)]


async def matching_workflows(db: Database, org_id: int, prompt: str, context: dict[str, Any], limit: int = 4) -> list[dict[str, Any]]:
    names = [alert["name"] for alert in context.get("alerts", {}).get("firing", [])]
    if context.get("targets", {}).get("down"):
        names.append("targetdown")
    signals = [item["signal_id"] for item in context.get("anomalies", [])]
    found = await workflows_for(db, org_id, alert_names=names, signal_ids=signals, text=prompt)
    # Reviewed workflows come first, and drafts never hide them.
    found.sort(key=lambda item: item["status"] != "approved")
    return found[:limit]


DRAFT_PROMPT = """Turn this investigation into a remediation workflow that a person will review and carry out by hand.
Return only JSON with this shape:
{"name": "short name", "summary": "one sentence", "risk": "low|medium|high",
 "match": {"alerts": ["lowercase alert name"], "keywords": ["lowercase phrase"]},
 "steps": [{"title": "", "detail": "", "command": "", "risk": "none|low|medium|high", "undo": "", "verify": ""}]}
Use placeholders such as APP or NAMESPACE where a value isn't known. Start with checks that change nothing. Say how to undo every step that changes something."""


# Names of the model's own read-only tools. They mean nothing to a person at a terminal.
_MODEL_TOOL_NAMES = re.compile(r"\b(run_instant_query|run_range_query|list_metrics|query_logs|get_kubernetes_state|get_recent_changes|search_past_investigations)\b")


def _person_command(text: str) -> str:
    return "" if _MODEL_TOOL_NAMES.search(text) else text


def parse_draft(text: str) -> dict[str, Any]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("The model didn't return a workflow.")
    data = json.loads(text[start:end + 1])
    steps = []
    for raw in data.get("steps", [])[:20]:
        steps.append(_step(str(raw.get("title", ""))[:200], str(raw.get("detail", ""))[:1000], _person_command(str(raw.get("command", ""))[:1000]),
                           str(raw.get("risk", "none"))[:10], str(raw.get("undo", ""))[:500], str(raw.get("verify", ""))[:500]))
    if not steps:
        raise ValueError("The model's workflow has no steps.")
    risk = data.get("risk") if data.get("risk") in {"low", "medium", "high"} else "medium"
    match = data.get("match") if isinstance(data.get("match"), dict) else {}
    return {"name": str(data.get("name", "Draft workflow"))[:120], "summary": str(data.get("summary", ""))[:500], "risk": risk,
            "match": {"alerts": [str(x)[:80] for x in match.get("alerts", [])][:10], "keywords": [str(x)[:80] for x in match.get("keywords", [])][:10]}, "steps": steps}
