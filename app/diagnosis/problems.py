"""Turn a Kubernetes snapshot into problems with stable fingerprints.

A problem is keyed on the workload that owns the pods (a Deployment, StatefulSet, DaemonSet, or Job), not on the
pod, because a pod's name changes on every restart. One crash-looping Deployment with three replicas is one problem.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..integrations.kubernetes import parse_time

PENDING_GRACE_S = 120
NOT_READY_GRACE_S = 120
OOM_RECENT_S = 900
IMAGE_REASONS = {"ImagePullBackOff", "ErrImagePull", "InvalidImageName", "ErrImageNeverPull"}
CONFIG_REASONS = {"CreateContainerConfigError", "CreateContainerError", "RunContainerError"}
SEVERITY_RANK = {"critical": 2, "warning": 1, "info": 0}
LABELS = {
    "crashloop": "crash-looping", "oomkilled": "killed for running out of memory", "image_pull": "can't pull its image",
    "config_error": "can't start its container", "unschedulable": "can't be scheduled", "not_ready": "running but not ready",
    "unavailable": "has no ready replicas", "degraded": "is below its desired replicas", "rollout_stuck": "rollout is stuck",
    "node_not_ready": "is NotReady", "node_pressure": "is under pressure",
}


def _age_s(iso: str, now: datetime) -> float:
    moment = parse_time(iso)
    return (now - moment).total_seconds() if moment else 0.0


def _workload_key(pod: dict[str, Any]) -> tuple[str, str, str]:
    return pod["namespace"], pod["workload_kind"], pod["workload"] or pod["name"]


def _pod_problem(pod: dict[str, Any], now: datetime) -> tuple[str, str, dict[str, Any]] | None:
    """The single most important problem of one pod: (problem, severity, facts)."""
    for container in pod["containers"]:
        facts = {"container": container["name"], "reason": container["reason"], "message": container["message"],
                 "last_reason": container["last_reason"], "exit_code": container["last_exit_code"], "restarts": container["restarts"],
                 "image": container["image"], "limits": pod["limits"].get(container["name"], {})}
        if container["state"] == "waiting" and container["reason"] in IMAGE_REASONS:
            return "image_pull", "critical", facts
        if container["state"] == "waiting" and container["reason"] in CONFIG_REASONS:
            return "config_error", "critical", facts
        if container["state"] == "waiting" and container["reason"] == "CrashLoopBackOff":
            return "crashloop", "critical", facts
    for container in pod["containers"]:
        if container["last_reason"] == "OOMKilled" and _age_s(container["last_finished"], now) <= OOM_RECENT_S:
            return "oomkilled", "critical", {"container": container["name"], "last_reason": "OOMKilled", "exit_code": container["last_exit_code"],
                                             "restarts": container["restarts"], "limits": pod["limits"].get(container["name"], {})}
    if pod["phase"] == "Pending" and pod["unschedulable"] and _age_s(pod["created"], now) >= PENDING_GRACE_S:
        return "unschedulable", "warning", {"message": pod["schedule_message"]}
    if pod["phase"] == "Running" and not pod["ready"] and pod["containers"] and _age_s(pod["ready_since"] or pod["created"], now) >= NOT_READY_GRACE_S:
        waiting = [c for c in pod["containers"] if not c["ready"]]
        return "not_ready", "warning", {"container": waiting[0]["name"] if waiting else "", "restarts": pod["restarts"]}
    return None


def _describe(problem: str, facts: dict[str, Any], pods: list[str]) -> str:
    bits = []
    if facts.get("restarts"):
        bits.append(f"{facts['restarts']} restarts")
    if facts.get("last_reason") and facts["last_reason"] not in {"Error", ""} and problem != "oomkilled":
        bits.append(f"last exit {facts['last_reason']}")
    if facts.get("exit_code") not in (None, ""):
        bits.append(f"exit code {facts['exit_code']}")
    if problem in {"image_pull", "config_error", "unschedulable"} and facts.get("message"):
        bits.append(facts["message"][:120])
    if len(pods) > 1:
        bits.append(f"{len(pods)} pods")
    return f" ({', '.join(bits)})" if bits else ""


def find_problems(snapshot: dict[str, Any], now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for pod in snapshot.get("pods", []):
        found = _pod_problem(pod, now)
        if not found:
            continue
        problem, severity, facts = found
        key = _workload_key(pod)
        current = grouped.get(key)
        if current and SEVERITY_RANK[current["severity"]] >= SEVERITY_RANK[severity]:
            current["pods"].append(pod["name"])
            continue
        grouped[key] = {"problem": problem, "severity": severity, "facts": facts, "pods": ([*current["pods"], pod["name"]] if current else [pod["name"]]),
                        "namespace": pod["namespace"], "workload_kind": key[1], "workload": key[2], "node": pod["node"]}
    problems = []
    for (namespace, kind, name), item in grouped.items():
        object_name = f"{kind} {namespace}/{name}"
        problems.append({
            "fingerprint": f"k8s:{namespace}/{kind.lower()}/{name}:{item['problem']}", "kind": "k8s", "problem": item["problem"],
            "severity": item["severity"], "object": object_name, "namespace": namespace, "workload_kind": kind, "workload": name,
            "pods": sorted(item["pods"])[:10], "facts": item["facts"],
            "title": f"{object_name} {LABELS[item['problem']]}{_describe(item['problem'], item['facts'], item['pods'])}",
            "clear": f"Resolved: {object_name} {LABELS[item['problem']]}",
        })
    workloads_with_pod_problems = {(p["namespace"], p["workload_kind"], p["workload"]) for p in problems}
    for deployment in snapshot.get("deployments", []):
        key = (deployment["namespace"], "Deployment", deployment["name"])
        object_name = f"Deployment {deployment['namespace']}/{deployment['name']}"
        base = {"kind": "k8s", "object": object_name, "namespace": deployment["namespace"], "workload_kind": "Deployment",
                "workload": deployment["name"], "pods": [], "facts": {"desired": deployment["desired"], "ready": deployment["ready"],
                                                                      "revision": deployment["revision"], "images": deployment["images"]}}
        if deployment["stuck"]:
            problems.append({**base, "problem": "rollout_stuck", "severity": "critical",
                             "fingerprint": f"k8s:{deployment['namespace']}/deployment/{deployment['name']}:rollout_stuck",
                             "title": f"{object_name} rollout is stuck at revision {deployment['revision']} ({deployment['ready']} of {deployment['desired']} ready)",
                             "clear": f"{object_name} rollout finished", "facts": {**base["facts"], "message": deployment["stuck_message"]}})
            continue
        if key in workloads_with_pod_problems or deployment["desired"] == 0 or deployment["ready"] >= deployment["desired"]:
            continue
        problem = "unavailable" if deployment["ready"] == 0 else "degraded"
        problems.append({**base, "problem": problem, "severity": "critical" if problem == "unavailable" else "warning",
                         "fingerprint": f"k8s:{deployment['namespace']}/deployment/{deployment['name']}:{problem}",
                         "title": f"{object_name} {LABELS[problem]} ({deployment['ready']} of {deployment['desired']} ready)",
                         "clear": f"{object_name} is fully available again"})
    for node in snapshot.get("nodes", []):
        base = {"kind": "k8s", "object": f"Node {node['name']}", "namespace": "", "workload_kind": "Node", "workload": node["name"], "pods": []}
        if not node["ready"]:
            problems.append({**base, "problem": "node_not_ready", "severity": "critical", "facts": {"message": node["ready_message"]},
                             "fingerprint": f"k8s:node/{node['name']}:not_ready", "title": f"Node {node['name']} is NotReady",
                             "clear": f"Node {node['name']} is Ready again"})
        elif node["pressure"]:
            problems.append({**base, "problem": "node_pressure", "severity": "warning", "facts": {"pressure": node["pressure"]},
                             "fingerprint": f"k8s:node/{node['name']}:pressure", "title": f"Node {node['name']} is under {', '.join(node['pressure'])}",
                             "clear": f"Node {node['name']} is no longer under pressure"})
    problems.sort(key=lambda item: (-SEVERITY_RANK[item["severity"]], item["object"]))
    return problems
