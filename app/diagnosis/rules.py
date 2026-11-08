"""Known Kubernetes failure signatures, recognised in code.

A diagnosis is computed from the cluster's own fields, events, and the crashed container's last log lines, so a
model can't invent it and a log line can't fake it. Each one says what it saw, so a person can check it, and
gives read-only commands to confirm it. Nothing here runs anything.
"""
from __future__ import annotations

import re
from typing import Any

ERROR_LINE_RE = re.compile(
    r"\b(error|exception|fatal|panic|traceback|refused|denied|forbidden|unauthori[sz]ed|not found|no such file|cannot|can't|could not|"
    r"failed|timeout|timed out|segmentation fault|killed|oom)\b", re.IGNORECASE)
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z?\s+")
EXIT_CODES = {
    1: "a general error in the application", 2: "a shell or argument error", 126: "a command that can't be run (permissions)",
    127: "a command or file that doesn't exist in the image", 128: "an invalid exit argument", 134: "an abort (SIGABRT)",
    137: "a kill (SIGKILL), usually out of memory or a failed liveness probe", 139: "a segmentation fault (SIGSEGV)",
    143: "a stop request (SIGTERM)",
}


def _strip(line: str) -> str:
    return TIMESTAMP_RE.sub("", line).strip()


def first_error(lines: list[str]) -> str | None:
    """The first line that looks like an error: usually the cause, where later lines are its consequences."""
    for line in lines:
        text = _strip(line)
        if text and ERROR_LINE_RE.search(text):
            return text[:300]
    return None


def _events_for(problem: dict[str, Any], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    names = set(problem.get("pods") or []) | {problem.get("workload", "")}
    return [event for event in events if event.get("namespace") == problem.get("namespace") and event.get("name") in names]


def _kubectl(problem: dict[str, Any]) -> list[str]:
    namespace, kind, name = problem.get("namespace"), problem.get("workload_kind", ""), problem.get("workload")
    pod = (problem.get("pods") or [name])[0]
    if kind == "Node":
        return [f"kubectl describe node {name}", f"kubectl get pods -A --field-selector spec.nodeName={name}"]
    target = f"{kind.lower()}/{name}" if kind in {"Deployment", "StatefulSet", "DaemonSet", "Job"} else f"pod/{pod}"
    return [f"kubectl -n {namespace} describe pod {pod}", f"kubectl -n {namespace} logs {target} --previous --tail=100",
            f"kubectl -n {namespace} get events --field-selector involvedObject.name={pod} --sort-by=.lastTimestamp"]


def diagnose(problem: dict[str, Any], log_lines: list[str] | None = None, events: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    facts = problem.get("facts") or {}
    kind = problem.get("problem")
    related = _events_for(problem, events or [])
    lines = log_lines or []
    evidence: list[str] = []
    cause = ""
    confidence = "medium"
    exit_code = facts.get("exit_code")
    if facts.get("restarts"):
        evidence.append(f"{facts['restarts']} restarts")
    if exit_code not in (None, ""):
        meaning = EXIT_CODES.get(int(exit_code), "")
        evidence.append(f"last exit code {exit_code}" + (f": {meaning}" if meaning else ""))

    if kind in {"crashloop", "oomkilled"} and facts.get("last_reason") == "OOMKilled":
        limit = (facts.get("limits") or {}).get("memory")
        cause = f"The container runs out of memory{f' at its {limit} limit' if limit else ''} and is killed (OOMKilled)."
        evidence.append("last state: OOMKilled")
        confidence = "high"
    elif kind == "crashloop":
        liveness = [event for event in related if "liveness probe failed" in event.get("message", "").lower()]
        error = first_error(lines)
        if liveness and int(exit_code or 0) in {137, 143}:
            cause = "Kubernetes kills the container because its liveness probe fails, so it restarts in a loop."
            evidence.append(f"event {liveness[0]['reason']}: {liveness[0]['message'][:160]}")
            confidence = "high"
        elif error:
            cause = f"The application exits on start with an error: {error}"
            evidence.append(f"crashed container's log: {error}")
            confidence = "medium" if exit_code not in (None, "") and int(exit_code) == 0 else "high"
        elif lines:
            cause = "The application exits right after starting, and its last log lines show no error."
            evidence.append(f"last log line: {_strip(lines[-1])[:200]}")
            confidence = "low"
        else:
            cause = "The container exits right after starting. Its logs couldn't be read, so the reason isn't visible here."
            confidence = "low"
    elif kind == "image_pull":
        message = facts.get("message", "")
        lowered = message.lower()
        if "not found" in lowered or "manifest unknown" in lowered:
            cause = f"The image {facts.get('image') or ''} or its tag doesn't exist in the registry.".replace("  ", " ")
        elif any(word in lowered for word in ("unauthorized", "denied", "authentication", "forbidden")):
            cause = "The registry refuses access: the pull secret is missing or wrong."
        else:
            cause = f"The node can't pull the image {facts.get('image') or ''}.".replace("  ", " ")
        evidence.append(f"{facts.get('reason')}: {message[:200]}")
        confidence = "high"
    elif kind == "config_error":
        message = facts.get("message", "")
        match = re.search(r'(secret|configmap)s? "([^"]+)" not found', message, re.IGNORECASE)
        key = re.search(r"couldn't find key (\S+) in (Secret|ConfigMap) ([\w./-]+)", message, re.IGNORECASE)
        if match:
            cause = f"The pod refers to a {match.group(1).capitalize()} named {match.group(2)} that doesn't exist in this namespace."
        elif key:
            cause = f"The pod refers to key {key.group(1)}, which isn't in {key.group(2)} {key.group(3)}."
        else:
            cause = "Kubernetes can't create the container from its configuration."
        evidence.append(f"{facts.get('reason')}: {message[:200]}")
        confidence = "high" if match or key else "medium"
    elif kind == "unschedulable":
        message = facts.get("message", "")
        lowered = message.lower()
        reasons = []
        if "insufficient cpu" in lowered:
            reasons.append("not enough free CPU")
        if "insufficient memory" in lowered:
            reasons.append("not enough free memory")
        if "taint" in lowered:
            reasons.append("node taints the pod doesn't tolerate")
        if "affinity" in lowered or "selector" in lowered:
            reasons.append("a node selector or affinity no node matches")
        if "persistentvolumeclaim" in lowered or "unbound" in lowered:
            reasons.append("an unbound PersistentVolumeClaim")
        cause = f"No node can take the pod: {', '.join(reasons)}." if reasons else "No node can take the pod."
        evidence.append(f"scheduler: {message[:200]}")
        confidence = "high" if reasons else "medium"
    elif kind == "not_ready":
        readiness = [event for event in related if "readiness probe failed" in event.get("message", "").lower()]
        cause = "The container runs but its readiness probe fails, so it gets no traffic."
        if readiness:
            evidence.append(f"event: {readiness[0]['message'][:200]}")
            confidence = "high"
        else:
            confidence = "low"
    elif kind == "rollout_stuck":
        cause = f"The rollout to revision {facts.get('revision')} didn't finish in time: its new pods never became ready."
        if facts.get("images"):
            evidence.append(f"images: {', '.join(facts['images'])}")
        if facts.get("message"):
            evidence.append(facts["message"])
    elif kind in {"unavailable", "degraded"}:
        cause = f"Only {facts.get('ready', 0)} of {facts.get('desired', 0)} replicas are ready. The pods themselves report no error yet."
        confidence = "low"
    elif kind == "node_not_ready":
        cause = "The node stopped reporting Ready, so its pods may be lost or rescheduled."
        if facts.get("message"):
            evidence.append(facts["message"])
    elif kind == "node_pressure":
        cause = f"The node is under {', '.join(facts.get('pressure', []))}, so Kubernetes may evict pods from it."
    else:
        return None
    for event in related[:2]:
        line = f"event {event['reason']}: {event['message'][:160]}"
        if line not in evidence:
            evidence.append(line)
    return {"cause": cause, "confidence": confidence, "evidence": evidence[:6], "commands": _kubectl(problem)}
