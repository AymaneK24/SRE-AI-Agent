"""Pure functions that turn raw answers from sources into the picture the console shows. Nothing here keeps state."""
from __future__ import annotations

import hashlib
import json
import statistics
from datetime import datetime, timezone
from typing import Any

from ..diagnosis.problems import SEVERITY_RANK
from ..secrets_store import scrub
from .signals import GOLDEN, Signal


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


def signal_meta(signal: Signal, source: Any) -> dict[str, Any]:
    return {
        "id": signal.id, "source_id": source.id, "source_name": source.name, "title": signal.title, "group": signal.group,
        "unit": signal.unit, "query": signal.query, "limit": signal.limit, "panel_priority": signal.panel_priority, "golden": GOLDEN.get(signal.id),
    }


def alert_severity(alert: dict[str, Any]) -> str:
    return "critical" if str(alert.get("severity", "")).lower() in {"critical", "page", "error"} else "warning"


def summarize_alerts(alerts: list[tuple[Any, dict[str, Any], bool]]) -> dict[str, Any]:
    firing: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
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
    # Alerts about a workload already counted below aren't counted twice.
    firing = len([alert for alert in alerts.get("firing", []) if not alert.get("merged_into")])
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


def alert_workload(alert: dict[str, Any], pod_owners: dict[tuple[str, str], str]) -> tuple[str, str] | None:
    """Which workload or node a Prometheus alert is about, from the labels kube-state-metrics rules use."""
    labels = alert.get("labels") or {}
    namespace = labels.get("namespace", "")
    for key in ("deployment", "statefulset", "daemonset", "job_name", "replicaset"):
        if namespace and labels.get(key):
            return (namespace, labels[key])
    if namespace and labels.get("pod"):
        owner = pod_owners.get((namespace, labels["pod"]))
        if owner:
            return (namespace, owner)
    if labels.get("node") and not namespace:
        return ("node", labels["node"])
    return None


def ranked_problems(current: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """One list of what is wrong, worst first, for the page and for the model. An alert merged into another problem shows there."""
    rows = [{"fingerprint": fingerprint, "kind": item["kind"], "severity": item["severity"], "title": item["title"],
             "detail": item["detail"]} for fingerprint, item in current.items() if not item.get("merged_into")]
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
