"""What each role may see of the live state. The server applies this, so hiding something on a page is never the only protection.

Viewer: plain status. No addresses, queries, labels, host names, versions, or error text.
Engineer: everything needed to investigate, except where the connections are.
Admin and owner: everything.
"""
from __future__ import annotations

import re
from typing import Any

from .auth import Principal

_ADDRESS = re.compile(r"https?://\S+|\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b|\b[a-zA-Z][\w.-]*:\d{2,5}\b")


def mask_addresses(text: Any) -> Any:
    """Status text can name a machine, such as "node-1:9100 is down". A viewer needs the fact, not the address."""
    return _ADDRESS.sub("a monitored host", text) if isinstance(text, str) else text


def _viewer_golden(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The worst current value for each golden signal, with no labels or query."""
    output = []
    for signal in signals:
        values = [item["value"] for item in signal.get("series", []) if isinstance(item.get("value"), (int, float))]
        if signal.get("golden") and values:
            output.append({"id": signal["id"], "title": signal["title"], "golden": signal["golden"], "unit": signal["unit"], "limit": signal.get("limit"), "value": max(values)})
    return output


def viewer_context(context: dict[str, Any]) -> dict[str, Any]:
    health = context.get("health", {})
    targets = context.get("targets", {})
    return {
        "as_of": context.get("as_of"),
        "headline": {**(context.get("headline") or {}), "text": mask_addresses((context.get("headline") or {}).get("text"))},
        "health": {key: health.get(key) for key in ("status", "last_ok", "age_seconds")},
        "targets": {key: targets.get(key, 0) for key in ("total", "up", "down")},
        "alerts": {"firing": [{"name": alert.get("name"), "severity": alert.get("severity"), "summary": mask_addresses(alert.get("summary")), "active_at": alert.get("active_at")} for alert in context.get("alerts", {}).get("firing", [])]},
        "anomalies": [{key: item.get(key) for key in ("title", "value", "unit", "kind", "limit", "baseline")} for item in context.get("anomalies", [])],
        "signals": _viewer_golden(context.get("signals", [])),
        "sources": [{"id": source.get("id"), "name": source.get("name"), "status": source.get("status")} for source in context.get("sources", [])],
        "events": [viewer_event(event) for event in context.get("events", [])],
        # Viewers see that a workload is failing, never its logs, pods, or diagnosis evidence.
        "problems": [{"kind": item.get("kind"), "severity": item.get("severity"), "title": mask_addresses(item.get("title"))}
                     for item in context.get("problems", []) if item.get("kind") == "k8s"],
        "coverage": {key: (context.get("coverage") or {}).get(key) for key in ("has_metrics", "has_logs", "has_kubernetes")},
        # Counts only: how many pods and nodes are healthy, never their names.
        "cluster_counts": ((context.get("integrations") or {}).get("kubernetes") or {}).get("counts"),
    }


def viewer_event(event: dict[str, Any]) -> dict[str, Any]:
    return {"id": event.get("id"), "ts": event.get("ts"), "severity": event.get("severity"), "title": mask_addresses(event.get("title"))}


def engineer_context(context: dict[str, Any]) -> dict[str, Any]:
    output = dict(context)
    output["sources"] = [{key: value for key, value in source.items() if key not in {"url", "version", "describe", "address"}} for source in context.get("sources", [])]
    return output


def context_for(user: Principal, context: dict[str, Any]) -> dict[str, Any]:
    if user.can("admin"):
        return context
    return engineer_context(context) if user.can("engineer") else viewer_context(context)


def live_message_for(user: Principal, message: dict[str, Any]) -> dict[str, Any] | None:
    """Filter one live update for this person. None means they don't receive it."""
    kind = message.get("type")
    if kind == "context":
        return {"type": "context", "data": context_for(user, message["data"])}
    if kind == "event":
        return message if user.can("engineer") else {"type": "event", "data": viewer_event(message["data"])}
    return message if user.can("engineer") or kind == "panels_changed" else None
