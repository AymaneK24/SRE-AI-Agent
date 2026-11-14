"""When a team's "false alarm" or "expected" label still applies.

A label is a judgement about the system as it was when someone made it, so it doesn't last for ever:

- Time: it ends after the organization's label period (30 days by default; 0 keeps it until something changes).
- Change: a recorded deploy or change to the same service or namespace, made after the label, ends it.
- Behaviour: the problem gets worse than anything seen when it was labelled: a value 20% above the highest
  one, or lasting much longer than the longest occurrence.
- Rule: the query or the limit behind the alert changed.

An ended label is kept, with its reason, so the team sees why the alert came back. Labelling the issue again,
or renewing the label, starts a new period.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .analysis import FIRST_SEEN_PREFIX, parse_ts

MUTING = ("false_positive", "expected")
DEFAULT_DAYS = 30
VALUE_MARGIN = 1.2
DURATION_FACTOR = 1.5
DURATION_SLACK_MINUTES = 10
PEAK_WINDOW_DAYS = 30
# Names too common to say that a change was about this problem.
GENERIC_NAMES = {"default", "prod", "production", "staging", "dev", "kube-system", "monitoring", "app", "api", "web", "main", "master", "node", "true", "false"}
TARGET_KEYS = ("namespace", "job", "service", "app", "deployment", "statefulset", "daemonset", "workload", "pod", "container", "object", "name", "alertname")
VERDICT_WORDS = {"false_positive": "a false alarm", "expected": "expected"}


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def targets(detail: dict[str, Any]) -> list[str]:
    """The names a problem is about: its namespace, service, workload, pod and job, from its detail and labels."""
    found: set[str] = set()
    sources = [detail, detail.get("labels") or {}]
    for source in sources:
        for key in TARGET_KEYS:
            value = source.get(key)
            if isinstance(value, str) and value:
                for part in re.split(r"[/\s]+", value.strip()):
                    if len(part) >= 3:
                        found.add(part.lower())
    # A pod's name without its generated suffix is the workload: payments-api-7d9f6c5b8d-x2k4q -> payments-api.
    for name in list(found):
        base = re.sub(r"(-[a-z0-9]{8,10})?-[a-z0-9]{5}$", "", name)
        if base != name and len(base) >= 3:
            found.add(base)
    return sorted(found - GENERIC_NAMES)[:20]


def rule_of(detail: dict[str, Any]) -> str:
    """The query and limit behind a problem, so a changed rule can be noticed. Empty when unknown."""
    query = detail.get("query") or detail.get("expr")
    limit = detail.get("limit")
    if not query and limit is None:
        return ""
    return json.dumps({"query": str(query or ""), "limit": limit}, sort_keys=True)


def change_matches(change: dict[str, Any], names: list[str]) -> bool:
    text = f"{change.get('service') or ''} {change.get('title') or ''}".lower()
    return any(re.search(rf"(?<![a-z0-9-]){re.escape(name)}(?![a-z0-9-])", text) for name in names)


async def peak(db: Any, org_id: int, title: str, now: datetime) -> dict[str, Any]:
    """What the issue looked like when it was labelled: its highest value, longest occurrence, the names it's about and its rule."""
    since = _iso(now - timedelta(days=PEAK_WINDOW_DAYS))
    rows = await db.all(
        "SELECT id, ts, kind, fingerprint, title, detail FROM events WHERE org_id = ? AND ts >= ? AND (title = ? OR title = ?) ORDER BY id",
        (org_id, since, title, FIRST_SEEN_PREFIX + title))
    value: float | None = None
    longest = 0.0
    names: set[str] = set()
    rule = ""
    started: dict[str, datetime] = {}
    fingerprints = {row["fingerprint"] for row in rows}
    for row in rows:
        try:
            detail = json.loads(row["detail"] or "{}")
        except ValueError:
            detail = {}
        if row["kind"].endswith(("_started", "_unreachable")):
            number = _number(detail.get("value"))
            if number is not None:
                value = number if value is None else max(value, number)
            names.update(targets(detail))
            rule = rule_of(detail) or rule
            started.setdefault(row["fingerprint"], parse_ts(row["ts"]))
    if fingerprints:
        ends = await db.all(
            "SELECT fingerprint, ts, kind FROM events WHERE org_id = ? AND ts >= ? AND (kind LIKE '%_cleared' OR kind LIKE '%_recovered') ORDER BY id",
            (org_id, since))
        open_since = dict(started)
        for row in rows:
            if row["kind"].endswith(("_started", "_unreachable")) and row["fingerprint"] not in open_since:
                open_since[row["fingerprint"]] = parse_ts(row["ts"])
        for end in ends:
            if end["fingerprint"] in open_since:
                longest = max(longest, (parse_ts(end["ts"]) - open_since.pop(end["fingerprint"])).total_seconds() / 60)
        for begun in open_since.values():
            longest = max(longest, (now - begun).total_seconds() / 60)
    return {"value": value, "minutes": round(longest, 1), "targets": sorted(names)[:20], "rule": rule}


def expires_at(row: dict[str, Any], days: int) -> datetime | None:
    if not days:
        return None
    return parse_ts(row.get("renewed_at") or row["decided_at"]) + timedelta(days=days)


def ended_text(row: dict[str, Any]) -> str:
    """One sentence for a message or a page: what the label was, and why it no longer applies."""
    if not row.get("ended_at"):
        return ""
    word = VERDICT_WORDS.get(row["verdict"], row["verdict"])
    return f"Marked {word} by {row['decided_by']} on {row['decided_at'][:10]}; that label ended: {row.get('ended_note') or row.get('ended_reason')}."


class Verdicts:
    """Reads labels and ends the ones that no longer apply. Every ending is saved and written to the audit log."""

    def __init__(self, db: Any) -> None:
        self.db = db

    async def _end(self, org_id: int, row: dict[str, Any], reason: str, note: str, now: datetime) -> dict[str, Any]:
        stamp = _iso(now)
        await self.db.execute("UPDATE issue_verdicts SET ended_at = ?, ended_reason = ?, ended_note = ? WHERE org_id = ? AND issue_key = ? AND ended_at IS NULL",
                              (stamp, reason, note[:300], org_id, row["issue_key"]))
        await self.db.audit(org_id, "verdict_ended", "issue", {"title": row["issue_key"], "verdict": row["verdict"], "reason": reason, "note": note[:300]}, actor="console")
        return {**row, "ended_at": stamp, "ended_reason": reason, "ended_note": note[:300]}

    async def check(self, org_id: int, title: str, detail: dict[str, Any] | None, days: int, now: datetime | None = None) -> dict[str, Any] | None:
        """The issue's label, after ending it if it no longer applies. Returns None when there is no label."""
        row = await self.db.one("SELECT * FROM issue_verdicts WHERE org_id = ? AND issue_key = ?", (org_id, title))
        if not row:
            return None
        row = dict(row)
        if row["verdict"] not in MUTING or row.get("ended_at"):
            return row
        now = now or datetime.now(timezone.utc)
        until = expires_at(row, days)
        if until is not None and now >= until:
            return await self._end(org_id, row, "expired", f"it was set for {days} days and wasn't renewed", now)
        try:
            known = json.loads(row.get("peak") or "{}")
        except ValueError:
            known = {}
        since = row.get("renewed_at") or row["decided_at"]
        names = list(known.get("targets") or []) + (targets(detail) if detail else [])
        names = sorted(set(names) - GENERIC_NAMES)
        if names:
            changes = await self.db.all("SELECT ts, service, title FROM change_events WHERE org_id = ? AND ts > ? ORDER BY ts DESC LIMIT 50", (org_id, since))
            for change in changes:
                if change_matches(change, names):
                    return await self._end(org_id, row, "change", f"{change['title']} ({change['ts'][:16].replace('T', ' ')} UTC) came after it", now)
        if detail:
            rule = rule_of(detail)
            if known.get("rule") and rule and rule != known["rule"]:
                return await self._end(org_id, row, "rule_changed", "the alert's query or limit changed since then", now)
            value, highest = _number(detail.get("value")), _number(known.get("value"))
            if value is not None and highest is not None and highest > 0 and value > highest * VALUE_MARGIN:
                return await self._end(org_id, row, "behaviour", f"its value reached {value:g}, above the highest seen when it was labelled ({highest:g})", now)
        return row

    async def check_duration(self, org_id: int, row: dict[str, Any], active_minutes: float, now: datetime | None = None) -> dict[str, Any] | None:
        """Ends a label whose problem has lasted much longer than any occurrence seen when it was labelled."""
        if row["verdict"] not in MUTING or row.get("ended_at"):
            return None
        try:
            longest = float(json.loads(row.get("peak") or "{}").get("minutes") or 0)
        except (ValueError, TypeError):
            return None
        if longest <= 0 or active_minutes <= max(longest * DURATION_FACTOR, longest + DURATION_SLACK_MINUTES):
            return None
        return await self._end(org_id, row, "behaviour", f"it has lasted {active_minutes:.0f} minutes, longer than any time before it was labelled ({longest:.0f} minutes)",
                               now or datetime.now(timezone.utc))
