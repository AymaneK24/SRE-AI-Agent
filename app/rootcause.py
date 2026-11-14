"""Which of the problems happening now share a cause, and which one is the likely root.

Time alone never links two problems: on a busy system unrelated things break in the same minutes all the time.
A link needs structural evidence, and each one is kept as a plain sentence:

- The same thing:  an alert, a Kubernetes problem and a metric about the same workload or node.
- Runs on:         a workload whose pods run on a node that is failing.
- Depends on:      a workload configured to call another (its environment names the Service in front of it).
- Mentions:        a problem's logs, error or diagnosis name the other's workload or Service (db:5432).

Then two weaker signals adjust the score: which started first (the onset, from when the underlying condition
began, not when an alert fired), and a shared namespace. The likely root of a group is the member nothing else
in the group explains, that explains the most others, ideally the one closest after a recorded change.

The result is a hypothesis for people and for the model to test, never a conclusion: every group says why.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .dependencies import Links, Workload

LINK_MIN = 3
ORDER_WINDOW = timedelta(minutes=15)
ORDER_AGAINST = timedelta(minutes=3)
CHANGE_WINDOW = timedelta(minutes=30)
MAX_PROBLEMS = 60
SEVERITY = {"critical": 2, "warning": 1, "info": 0}
KIND_WEIGHT = {"k8s": 3, "target": 2, "signal": 1, "alert": 1, "rule": 0}
GENERIC = {"default", "kube-system", "monitoring", "app", "api", "web", "main", "prod", "production", "node", "pod", "true", "false", "error", "http", "https"}


def _ts(text: Any) -> datetime | None:
    if not text or not isinstance(text, str):
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _clock(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%H:%M")


def _minutes(delta: timedelta) -> str:
    minutes = round(abs(delta.total_seconds()) / 60)
    return "under a minute" if minutes < 1 else f"{minutes} min"


@dataclass
class Item:
    fingerprint: str
    title: str
    kind: str
    severity: str
    detail: dict[str, Any]
    onset: datetime | None
    workloads: set[Workload] = field(default_factory=set)
    nodes: set[str] = field(default_factory=set)
    namespace: str = ""
    names: set[str] = field(default_factory=set)
    text: str = ""

    @property
    def label(self) -> str:
        if self.workloads:
            namespace, name = sorted(self.workloads)[0]
            return f"{namespace}/{name}" if namespace else name
        if self.nodes:
            return f"node {sorted(self.nodes)[0]}"
        return self.title


def onset(problem: dict[str, Any]) -> datetime | None:
    """When the underlying condition began. Prometheus says when an alert became pending, before its "for" wait;
    a crash loop began when its container first stopped; otherwise it's when the console first saw it."""
    detail = problem.get("detail") or {}
    candidates = [_ts(detail.get("activeAt")), _ts(detail.get("since"))]
    facts = detail.get("facts") or {}
    candidates += [_ts(facts.get("last_finished")), _ts(facts.get("ready_since"))]
    seen = _ts(problem.get("since"))
    found = [moment for moment in candidates if moment and (seen is None or moment <= seen)]
    return min(found) if found else seen


def _text(detail: dict[str, Any]) -> str:
    """Everything a problem says about what it saw: logs, error, diagnosis and summary. Only used to look for names."""
    parts: list[str] = [str(detail.get(key) or "") for key in ("summary", "error", "message")]
    diagnosis = detail.get("diagnosis") or {}
    parts += [str(diagnosis.get("cause") or "")] + [str(item) for item in diagnosis.get("evidence", []) or []]
    facts = detail.get("facts") or {}
    parts.append(str(facts.get("message") or ""))
    logs = detail.get("logs") or {}
    parts += [str(line) for line in (logs.get("lines") or [])[-20:]]
    annotations = detail.get("annotations") or {}
    parts += [str(value) for value in annotations.values()]
    return " ".join(parts).lower()[:20000]


def _subject(problem: dict[str, Any], links: Links) -> Item:
    detail = problem.get("detail") or {}
    labels = detail.get("labels") or {}
    item = Item(problem["fingerprint"], problem["title"], problem.get("kind", ""), problem.get("severity", "warning"), detail, onset(problem))
    namespace = str(detail.get("namespace") or labels.get("namespace") or "")
    item.namespace = namespace
    kind = detail.get("workload_kind")
    if item.kind == "k8s" and kind == "Node":
        item.nodes.add(str(detail.get("workload")))
    elif item.kind == "k8s" and detail.get("workload"):
        item.workloads.add((namespace, str(detail["workload"])))
    else:
        if labels.get("node") and not namespace:
            item.nodes.add(str(labels["node"]))
        for key in ("deployment", "statefulset", "daemonset", "job_name"):
            if namespace and labels.get(key):
                item.workloads.add((namespace, str(labels[key])))
        if namespace and labels.get("pod"):
            owner = links.owner(namespace, str(labels["pod"]))
            if owner:
                item.workloads.add((namespace, owner))
        if namespace and labels.get("service"):
            item.workloads |= links.services.get((namespace, str(labels["service"])), set())
        # A job or app label that is a known workload's name, such as job="payments-api".
        for key in ("job", "app", "container"):
            value = str(labels.get(key) or detail.get(key) or "")
            if value:
                item.workloads |= {workload for workload in links.workloads if workload[1] == value and (not namespace or workload[0] == namespace)}
    for workload in item.workloads:
        item.names |= links.names(workload)
    item.names |= {node.lower() for node in item.nodes}
    item.names = {name for name in item.names if name not in GENERIC and len(name) >= 3}
    item.text = _text(detail)
    return item


def _mentions(text: str, names: set[str]) -> str:
    for name in sorted(names, key=len, reverse=True):
        match = re.search(rf"(?<![a-z0-9-]){re.escape(name)}(?::(\d{{2,5}}))?(?![a-z0-9-])", text)
        if match:
            return match.group(0)
    return ""


@dataclass
class Link:
    cause: str | None  # None: the same thing seen twice, no direction.
    score: int
    reasons: list[str]


def _link(a: Item, b: Item, links: Links) -> Link | None:
    """Evidence that a causes b (or that they are the same thing)."""
    reasons: list[str] = []
    score = 0
    structural = False
    direction: str | None = a.fingerprint
    if a.workloads & b.workloads or (a.nodes and a.nodes == b.nodes and not a.workloads and not b.workloads):
        shared = sorted(a.workloads & b.workloads)
        where = f"{shared[0][0]}/{shared[0][1]}" if shared else f"node {sorted(a.nodes)[0]}"
        return Link(None, 4, [f"both are about {where}"])
    if a.nodes and not a.workloads:
        node = next((n for n in a.nodes if any(n in links.nodes.get(w, set()) for w in b.workloads)), "")
        if node:
            score += 4
            structural = True
            reasons.append(f"{b.label} runs on node {node}, which has a problem")
    for wb in b.workloads:
        for wa in a.workloads:
            host = links.depends.get(wb, {}).get(wa)
            if host:
                score += 4
                structural = True
                reasons.append(f"{b.label} is configured to call {a.label} (host {host})")
                break
    mention = _mentions(b.text, a.names)
    if mention:
        score += 3
        structural = True
        reasons.append(f"{b.label} mentions {mention} in its logs or error")
    if not structural:
        return None
    if a.onset and b.onset:
        gap = b.onset - a.onset
        if timedelta(0) <= gap <= ORDER_WINDOW:
            score += 1
            reasons.append(f"{a.label} started first, {_minutes(gap)} earlier")
        elif gap < -ORDER_AGAINST:
            # b was already failing well before a: whatever links them, a can't be what caused b.
            return None
    if a.namespace and a.namespace == b.namespace:
        score += 1
    return Link(direction, score, reasons) if score >= LINK_MIN else None


def _change_before(item: Item, changes: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not item.onset:
        return None
    for change in changes:
        moment = _ts(change.get("ts"))
        if not moment or not (timedelta(0) <= item.onset - moment <= CHANGE_WINDOW):
            continue
        text = f"{change.get('service') or ''} {change.get('title') or ''}".lower()
        if any(re.search(rf"(?<![a-z0-9-]){re.escape(name)}(?![a-z0-9-])", text) for name in item.names | {w[1].lower() for w in item.workloads}):
            return change
    return None


def analyze(problems: list[dict[str, Any]], snapshots: list[dict[str, Any]], changes: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Groups of problems that share a likely cause. Problems with no structural link to any other are left out."""
    links = Links(snapshots)
    items = [_subject(problem, links) for problem in problems[:MAX_PROBLEMS]]
    by_id = {item.fingerprint: item for item in items}
    edges: dict[tuple[str, str], Link] = {}
    for a in items:
        for b in items:
            if a is b:
                continue
            link = _link(a, b, links)
            # A "same thing" link has no direction, so it's kept once.
            if link and (link.cause is not None or a.fingerprint < b.fingerprint):
                edges[(a.fingerprint, b.fingerprint)] = link
    # Connected groups.
    parent = {item.fingerprint: item.fingerprint for item in items}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for first, second in edges:
        parent[find(first)] = find(second)
    members: dict[str, list[str]] = {}
    for item in items:
        members.setdefault(find(item.fingerprint), []).append(item.fingerprint)
    changes = sorted(changes or [], key=lambda row: str(row.get("ts")), reverse=True)
    groups = []
    for ids in members.values():
        if len(ids) < 2:
            continue
        groups.append(_group([by_id[i] for i in ids], edges, changes))
    groups.sort(key=lambda group: (-SEVERITY.get(group["severity"], 0), -len(group["members"]), group["root_title"]))
    return groups


def _group(items: list[Item], edges: dict[tuple[str, str], Link], changes: list[dict[str, Any]]) -> dict[str, Any]:
    ids = {item.fingerprint for item in items}
    causes: dict[str, set[str]] = {i: set() for i in ids}   # who explains whom, directly
    caused_by: dict[str, set[str]] = {i: set() for i in ids}
    same: dict[str, set[str]] = {i: set() for i in ids}
    for (a, b), link in edges.items():
        if a in ids and b in ids:
            if link.cause is None:
                same[a].add(b)
                same[b].add(a)
            else:
                causes[a].add(b)
                caused_by[b].add(a)

    def explains(start: str) -> set[str]:
        seen, todo = {start}, [start]
        while todo:
            node = todo.pop()
            for nxt in causes[node] | same[node]:
                if nxt not in seen:
                    seen.add(nxt)
                    todo.append(nxt)
        return seen - {start}
    by_id = {item.fingerprint: item for item in items}
    change_of = {item.fingerprint: _change_before(item, changes) for item in items}

    def rank(item: Item) -> tuple[Any, ...]:
        upstream = [c for c in caused_by[item.fingerprint] if c not in same[item.fingerprint]]
        return (len(upstream) == 0, len(explains(item.fingerprint)), change_of[item.fingerprint] is not None,
                -(item.onset.timestamp() if item.onset else 1e12), KIND_WEIGHT.get(item.kind, 0), SEVERITY.get(item.severity, 0))
    root = max(items, key=rank)
    directed = any(causes[i] for i in ids)
    reached = explains(root.fingerprint)
    verdict = "same" if not directed else "likely" if reached >= ids - {root.fingerprint} else "possible"
    rows = []
    for item in sorted(items, key=lambda it: (it.fingerprint != root.fingerprint, it.onset or datetime.max.replace(tzinfo=timezone.utc))):
        reasons: list[str] = []
        if item.fingerprint == root.fingerprint:
            role = "root"
        elif item.fingerprint in same[root.fingerprint] or (not directed):
            role = "same"
        else:
            role = "symptom"
        for (a, b), link in edges.items():
            if b == item.fingerprint and a in ids or (link.cause is None and item.fingerprint in (a, b) and (a in ids and b in ids)):
                for reason in link.reasons:
                    if reason not in reasons:
                        reasons.append(reason)
        rows.append({"fingerprint": item.fingerprint, "title": item.title, "kind": item.kind, "severity": item.severity, "role": role,
                     "onset": item.onset.isoformat().replace("+00:00", "Z") if item.onset else None, "reasons": reasons[:4]})
    change = change_of[root.fingerprint]
    severity = max((item.severity for item in items), key=lambda s: SEVERITY.get(s, 0))
    if verdict == "same":
        summary = f"{len(items)} reports of the same problem on {root.label}."
    else:
        word = "Likely" if verdict == "likely" else "Possibly"
        summary = f"{word} one cause: {root.label}. {len(items) - 1} other {'problem' if len(items) == 2 else 'problems'} may follow from it."
    if change:
        moment = _ts(change.get("ts"))
        summary += f" A change came {_minutes(root.onset - moment) if root.onset and moment else 'shortly'} before it started: {change.get('title')}."
    return {"id": root.fingerprint, "verdict": verdict, "severity": severity, "root": root.fingerprint, "root_title": root.title,
            "root_label": root.label, "summary": summary, "change": {"ts": change.get("ts"), "title": change.get("title")} if change else None,
            "members": rows}


def by_member(groups: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Each grouped problem's group and its own row in it."""
    found: dict[str, dict[str, Any]] = {}
    for group in groups:
        for member in group["members"]:
            found[member["fingerprint"]] = {"group": group, "member": member}
    return found


def describe(groups: list[dict[str, Any]], limit: int = 6) -> list[str]:
    """Lines for the model: hypotheses to test, with their reasons."""
    lines: list[str] = []
    for group in groups[:limit]:
        lines.append(f"- {group['summary']}")
        for member in group["members"]:
            why = f" ({'; '.join(member['reasons'])})" if member["reasons"] else ""
            outside = " [not in the person's selection]" if member.get("not_chosen") else ""
            lines.append(f"    {member['role']}: {member['title']}{outside}{why}")
    return lines
