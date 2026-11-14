"""Turns stored state changes into recurring issues and patterns.

Everything here is calculated from recorded events, with no AI, so every number in a report can be traced.
Patterns describe timing only. They point at things worth checking and don't prove a cause.
"""
from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
SEVERITY_RANK = {"critical": 2, "warning": 1, "info": 0}
FIRST_SEEN_PREFIX = "Already active when the console connected. "

RECURRING_MIN = 2
FLAPPING_MIN = 3
FLAPPING_MEDIAN_MINUTES = 10
TIMING_MIN = 4
TIMING_SHARE = 0.6
TOGETHER_WINDOW_MINUTES = 10
TOGETHER_MIN = 3
AFTER_CHANGE_MINUTES = 30
AFTER_CHANGE_MIN = 2
TREND_MIN = 4
CHRONIC_SHARE = 0.5
INCIDENT_START_GAP_MINUTES = 10
INCIDENT_OVERLAP_MINUTES = 60
INCIDENT_LIST_MAX = 50


def parse_ts(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def plural(count: int, word: str, many: str | None = None) -> str:
    return f"{count} {word if count == 1 else (many or word + 's')}"


def _title(event: dict[str, Any]) -> str:
    return event["title"].removeprefix(FIRST_SEEN_PREFIX)


def _affected(detail_text: str) -> str:
    try:
        detail = json.loads(detail_text or "{}")
    except ValueError:
        return ""
    labels = detail.get("labels") or {}
    return str(detail.get("instance") or labels.get("instance") or labels.get("job") or detail.get("job") or "")


def _occurrences(events: list[dict[str, Any]], start: datetime, now: datetime) -> list[dict[str, Any]]:
    """Pair each problem's start and end. A restart reports a known problem again, which continues the same occurrence."""
    by_fingerprint: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in sorted(events, key=lambda item: (item["ts"], item["id"])):
        by_fingerprint[event["fingerprint"]].append(event)
    found = []
    for fingerprint, items in by_fingerprint.items():
        current = None
        for event in items:
            moment = parse_ts(event["ts"])
            if event["kind"].endswith(("_started", "_unreachable")):
                if current is not None:
                    continue
                current = {
                    "fingerprint": fingerprint, "title": _title(event), "kind": event["kind"].rsplit("_", 1)[0],
                    "severity": event["severity"], "start": moment, "end": None, "source_id": event.get("source_id"),
                    "affected": _affected(event.get("detail", "")), "start_known": not event["title"].startswith(FIRST_SEEN_PREFIX),
                }
            elif event["kind"].endswith(("_cleared", "_recovered")) and current is not None:
                current["end"] = moment
                found.append(current)
                current = None
        if current is not None:
            found.append(current)
    return [item for item in found if (item["end"] or now) >= start]


def _minutes(item: dict[str, Any], start: datetime, now: datetime) -> float:
    return max(0.0, ((item["end"] or now) - max(item["start"], start)).total_seconds() / 60)


def _issues(occurrences: list[dict[str, Any]], start: datetime, now: datetime) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in occurrences:
        groups[item["title"]].append(item)
    issues = []
    for title, items in groups.items():
        durations = [_minutes(item, start, now) for item in items]
        ended = [(item["end"] - item["start"]).total_seconds() / 60 for item in items if item["end"]]
        issues.append({
            "title": title, "kind": items[0]["kind"], "severity": max((item["severity"] for item in items), key=lambda s: SEVERITY_RANK.get(s, 0)),
            "occurrences": len(items), "recurring": len(items) >= RECURRING_MIN, "active": any(item["end"] is None for item in items),
            "first_seen": _iso(min(item["start"] for item in items)), "last_seen": _iso(max(item["start"] for item in items)),
            "total_minutes": round(sum(durations), 1), "average_minutes": round(statistics.mean(ended), 1) if ended else None,
            "longest_minutes": round(max(durations), 1), "affected": sorted({item["affected"] for item in items if item["affected"]})[:8],
            "source_ids": sorted({item["source_id"] for item in items if item["source_id"]}),
        })
    issues.sort(key=lambda item: (-item["occurrences"], -SEVERITY_RANK.get(item["severity"], 0), -item["total_minutes"], item["title"]))
    return issues


def _pattern(kind: str, title: str, detail: str, issues: list[str], evidence: dict[str, Any]) -> dict[str, Any]:
    return {"type": kind, "title": title, "detail": detail, "issues": issues, "evidence": evidence}


def _timing_patterns(groups: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    patterns = []
    for title, items in groups.items():
        starts = [item["start"] for item in items if item["start_known"]]
        if len(starts) < TIMING_MIN:
            continue
        blocks = Counter(moment.hour // 3 for moment in starts)
        block, count = blocks.most_common(1)[0]
        if count / len(starts) >= TIMING_SHARE:
            first = block * 3
            patterns.append(_pattern("time_of_day", f"Often starts between {first:02d}:00 and {first + 3:02d}:00 UTC",
                                     f"{title}: {count} of {len(starts)} starts fell in this 3-hour window. Check scheduled jobs, backups, and traffic peaks.",
                                     [title], {"in_window": count, "total": len(starts), "from_hour_utc": first}))
        days = Counter(moment.weekday() for moment in starts)
        day, count = days.most_common(1)[0]
        if count / len(starts) >= TIMING_SHARE and len({moment.date() for moment in starts}) >= 2:
            patterns.append(_pattern("day_of_week", f"Often starts on {WEEKDAYS[day]}", f"{title}: {count} of {len(starts)} starts fell on this day. Check weekly jobs and release schedules.",
                                     [title], {"on_day": count, "total": len(starts), "day": WEEKDAYS[day]}))
    return patterns


def _flapping(groups: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    patterns = []
    for title, items in groups.items():
        ended = [(item["end"] - item["start"]).total_seconds() / 60 for item in items if item["end"]]
        if len(ended) >= FLAPPING_MIN and statistics.median(ended) < FLAPPING_MEDIAN_MINUTES:
            median = round(statistics.median(ended), 1)
            patterns.append(_pattern("flapping", "Keeps starting and clearing quickly", f"{title}: {len(ended)} short occurrences, with a median of {median} minutes. Check for a limit set too close to normal values, or an unstable dependency.",
                                     [title], {"occurrences": len(ended), "median_minutes": median}))
    return patterns


def _together(groups: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    patterns = []
    window = timedelta(minutes=TOGETHER_WINDOW_MINUTES)
    titles = sorted(title for title, items in groups.items() if len(items) >= TOGETHER_MIN)
    for index, first in enumerate(titles):
        for second in titles[index + 1:]:
            gaps = []
            for item in groups[first]:
                nearest = min(((other["start"] - item["start"]) for other in groups[second]), key=abs)
                if abs(nearest) <= window:
                    gaps.append(nearest.total_seconds() / 60)
            smaller = min(len(groups[first]), len(groups[second]))
            if len(gaps) < TOGETHER_MIN or len(gaps) / smaller < 0.5:
                continue
            median = statistics.median(gaps)
            if abs(median) < 0.5:
                order = "They usually start at about the same time."
            else:
                leader, follower = (first, second) if median > 0 else (second, first)
                order = f"{leader} usually starts first, about {abs(round(median, 1))} minutes before {follower}."
            patterns.append(_pattern("together", "These problems tend to happen together", f"{first} and {second} started within {TOGETHER_WINDOW_MINUTES} minutes of each other {plural(len(gaps), 'time')}. {order}",
                                     [first, second], {"times": len(gaps), "median_gap_minutes": round(median, 1)}))
    return patterns


def _after_changes(groups: dict[str, list[dict[str, Any]]], changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    patterns = []
    moments = [(parse_ts(change["ts"]), change["title"]) for change in changes]
    window = timedelta(minutes=AFTER_CHANGE_MINUTES)
    for title, items in groups.items():
        matched = []
        for item in items:
            if not item["start_known"]:
                continue
            before = [(moment, name) for moment, name in moments if timedelta(0) <= item["start"] - moment <= window]
            if before:
                matched.append(max(before)[1])
        if len(matched) >= AFTER_CHANGE_MIN:
            patterns.append(_pattern("after_change", "Often starts soon after a change", f"{title}: {len(matched)} of {len(items)} occurrences started within {AFTER_CHANGE_MINUTES} minutes after a recorded change, such as \"{matched[-1]}\". Check what those changes had in common.",
                                     [title], {"after_change": len(matched), "total": len(items), "example": matched[-1]}))
    return patterns


def _trends(groups: dict[str, list[dict[str, Any]]], start: datetime, now: datetime) -> list[dict[str, Any]]:
    patterns = []
    middle = start + (now - start) / 2
    for title, items in groups.items():
        known = [item for item in items if item["start"] >= start]
        early = sum(1 for item in known if item["start"] < middle)
        late = len(known) - early
        if late >= TREND_MIN and late >= 2 * max(early, 1):
            patterns.append(_pattern("worsening", "Happening more often", f"{title}: {early} in the first half of this period, then {late} in the second half.", [title], {"first_half": early, "second_half": late}))
        elif early >= TREND_MIN and early >= 2 * max(late, 1):
            patterns.append(_pattern("improving", "Happening less often", f"{title}: {early} in the first half of this period, then {late} in the second half.", [title], {"first_half": early, "second_half": late}))
    return patterns


def _chronic(issues: list[dict[str, Any]], start: datetime, now: datetime) -> list[dict[str, Any]]:
    period = (now - start).total_seconds() / 60
    return [_pattern("chronic", "Unresolved for most of the period", f"{issue['title']}: active for {format_minutes(issue['total_minutes'])}, more than half of the time covered.", [issue["title"]], {"total_minutes": issue["total_minutes"]})
            for issue in issues if period > 0 and issue["total_minutes"] >= CHRONIC_SHARE * period]


def _incidents(occurrences: list[dict[str, Any]], changes: list[dict[str, Any]], start: datetime, now: datetime) -> list[dict[str, Any]]:
    """Group alerts that belong to one event, so one outage reads as one incident and not as twenty separate problems.

    Problems join an incident when they start close together, or while it's in its first hour. A problem that runs for
    weeks therefore doesn't swallow everything that happens during those weeks.
    """
    gap = timedelta(minutes=INCIDENT_START_GAP_MINUTES)
    overlap = timedelta(minutes=INCIDENT_OVERLAP_MINUTES)
    moments = sorted((parse_ts(change["ts"]), change["title"]) for change in changes)
    groups: list[list[dict[str, Any]]] = []
    for item in sorted(occurrences, key=lambda entry: entry["start"]):
        if groups:
            current = groups[-1]
            first_start = current[0]["start"]
            last_start = current[-1]["start"]
            ends = [entry["end"] or now for entry in current]
            if item["start"] - last_start <= gap or (item["start"] <= max(ends) and item["start"] - first_start <= overlap):
                current.append(item)
                continue
        groups.append([item])
    incidents = []
    for group in groups:
        began = group[0]["start"]
        active = any(entry["end"] is None for entry in group)
        ended = None if active else max(entry["end"] for entry in group)
        before = [name for moment, name in moments if timedelta(0) <= began - moment <= timedelta(minutes=AFTER_CHANGE_MINUTES)]
        incidents.append({
            "start": _iso(began), "end": _iso(ended) if ended else None, "active": active, "start_known": group[0]["start_known"],
            "minutes": round(((ended or now) - max(began, start)).total_seconds() / 60, 1),
            "severity": max((entry["severity"] for entry in group), key=lambda value: SEVERITY_RANK.get(value, 0)),
            "problems": len(group), "issues": sorted({entry["title"] for entry in group})[:12],
            "first_issue": group[0]["title"], "after_change": before[-1] if before else None,
        })
    return incidents


def _reliability(incidents: list[dict[str, Any]], start: datetime, now: datetime, covered_from: datetime) -> dict[str, Any]:
    """The team's headline numbers, worked out from incidents. None means there's too little data to say."""
    finished = [item["minutes"] for item in incidents if item["end"] and item["start_known"]]
    starts = sorted(parse_ts(item["start"]) for item in incidents if item["start_known"])
    gaps = [(later - earlier).total_seconds() / 60 for earlier, later in zip(starts, starts[1:], strict=False)]
    covered = max((now - max(start, covered_from)).total_seconds() / 60, 0.0)
    # Time in an incident, with overlapping incidents counted once.
    spans = sorted((max(parse_ts(item["start"]), covered_from), parse_ts(item["end"]) if item["end"] else now) for item in incidents)
    busy = 0.0
    edge: datetime | None = None
    for begin, end in spans:
        begin = max(begin, edge) if edge else begin
        if end > begin:
            busy += (end - begin).total_seconds() / 60
            edge = end
    return {
        "incidents": len(incidents),
        "mttr_minutes": round(statistics.mean(finished), 1) if finished else None,
        "mtbf_minutes": round(statistics.mean(gaps), 1) if gaps else None,
        "clear_percent": round(100 * (1 - min(busy, covered) / covered), 2) if covered >= 60 else None,
    }


def likely_noise(issue: dict[str, Any], flapping_titles: set[str]) -> str | None:
    """A reason to suspect a false alarm. This only suggests: a person records the verdict."""
    if issue["title"] in flapping_titles and issue["severity"] != "critical":
        return "It clears by itself within minutes, again and again, so its limit may be too close to normal values."
    return None


def apply_verdicts(analysis: dict[str, Any], verdicts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Add the team's verdicts. False alarms and expected problems stop counting towards current health and the noise-free totals."""
    flapping = {title for pattern in analysis["patterns"] if pattern["type"] == "flapping" for title in pattern["issues"]}
    dismissed = 0
    active_real = 0
    real = 0
    for issue in analysis["issues"]:
        found = verdicts.get(issue["title"])
        # A label that ended (app/verdicts.py) no longer counts: the issue is back to "not reviewed", with the reason shown.
        ended = bool(found and found.get("ended_at"))
        verdict = None if ended else found
        issue["verdict"] = verdict["verdict"] if verdict else None
        issue["verdict_note"] = verdict["note"] if verdict else ""
        issue["verdict_by"] = verdict["decided_by"] if verdict else ""
        issue["verdict_expires_at"] = verdict.get("expires_at") if verdict else None
        issue["verdict_ended"] = (found.get("ended_text") or "Its earlier label ended.") if ended and found else ""
        issue["noise_hint"] = likely_noise(issue, flapping) if not verdict else None
        if issue["verdict"] in {"false_positive", "expected"}:
            dismissed += issue["occurrences"]
        else:
            real += issue["occurrences"]
            active_real += 1 if issue["active"] else 0
    analysis["summary"]["dismissed"] = dismissed
    analysis["summary"]["real_occurrences"] = real
    analysis["summary"]["active_real_issues"] = active_real
    analysis["summary"]["noise_percent"] = round(100 * dismissed / (dismissed + real), 1) if dismissed + real else 0.0
    return analysis


def statements(data: dict[str, Any]) -> list[str]:
    """Sentences the AI notes can repeat as they are, because they're calculated and can't be wrong."""
    summary = data["summary"]
    reliability = data["reliability"]
    output = [f"In this period there were {plural(reliability['incidents'], 'incident')}, made up of {plural(summary['occurrences'], 'problem occurrence')} across {plural(summary['issues'], 'different issue')}."]
    if reliability["mttr_minutes"] is not None:
        output.append(f"Incidents took {format_minutes(reliability['mttr_minutes'])} on average to clear.")
    if summary.get("dismissed"):
        output.append(f"The team marked {plural(summary['dismissed'], 'occurrence')} as false alarms or expected, which is {summary['noise_percent']}% of the total.")
    if data.get("trend") and data["trend"]["previous"] is not None:
        output.append(f"The previous period of the same length had {plural(data['trend']['previous'], 'occurrence')}.")
    for issue in [item for item in data["issues"] if item["recurring"]][:3]:
        verdict = {"false_positive": " The team marked it a false alarm.", "expected": " The team marked it expected."}.get(issue.get("verdict") or "", "")
        output.append(f"{issue['title']} happened {plural(issue['occurrences'], 'time')}, for {format_minutes(issue['total_minutes'])} in total.{verdict}")
    return output


def format_minutes(minutes: float | None) -> str:
    if minutes is None:
        return "Not finished"
    if minutes < 1:
        return "Under 1 min"
    if minutes < 60:
        return f"{round(minutes)} min"
    if minutes < 48 * 60:
        return f"{minutes / 60:.1f} h"
    return f"{minutes / 1440:.1f} days"


def analyze(events: list[dict[str, Any]], changes: list[dict[str, Any]], start: datetime, now: datetime, data_since: str | None = None) -> dict[str, Any]:
    """Summarize one period. Events must include earlier ones, so problems already running at the start are counted."""
    occurrences = _occurrences(events, start, now)
    issues = _issues(occurrences, start, now)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in occurrences:
        groups[item["title"]].append(item)
    patterns = (_after_changes(groups, changes) + _together(groups) + _flapping(groups) + _timing_patterns(groups)
                + _trends(groups, start, now) + _chronic(issues, start, now))

    # The chart starts when recording began, so empty days before it don't look like quiet days.
    first_day = max(start, parse_ts(data_since)).date() if data_since else now.date()
    days = (now.date() - first_day).days + 1
    daily = {(first_day + timedelta(days=offset)).isoformat(): {"critical": 0, "warning": 0} for offset in range(days)}
    for item in occurrences:
        key = item["start"].date().isoformat()
        if key in daily:
            daily[key]["critical" if item["severity"] == "critical" else "warning"] += 1
    ended = [(item["end"] - item["start"]).total_seconds() / 60 for item in occurrences if item["end"] and item["start_known"]]
    covered_from = max(start, parse_ts(data_since)) if data_since else now
    incidents = _incidents(occurrences, changes, start, now)
    previous_start = start - (now - start)
    previous = None
    if data_since and parse_ts(data_since) <= previous_start:
        previous = sum(1 for item in _occurrences(events, previous_start, start) if previous_start <= item["start"] < start)
    return {
        "period": {"start": _iso(start), "end": _iso(now), "days": max(1, round((now - start).total_seconds() / 86400)),
                   "data_since": data_since, "covered_days": round(max(0.0, (now - covered_from).total_seconds()) / 86400, 1)},
        "summary": {
            "occurrences": len(occurrences), "issues": len(issues), "recurring": sum(1 for issue in issues if issue["recurring"]),
            "critical": sum(1 for item in occurrences if item["severity"] == "critical"), "active": sum(1 for item in occurrences if item["end"] is None),
            "problem_minutes": round(sum(_minutes(item, start, now) for item in occurrences), 1),
            "average_clear_minutes": round(statistics.mean(ended), 1) if ended else None,
        },
        "reliability": _reliability(incidents, start, now, covered_from),
        "trend": {"previous": previous},
        "incidents": incidents[-INCIDENT_LIST_MAX:][::-1],
        "issues": issues,
        "patterns": patterns,
        "daily": [{"date": key, **value} for key, value in daily.items()],
    }
