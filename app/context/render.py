"""Turns the live context into the text a model reads, within a fixed size budget."""
from __future__ import annotations

from typing import Any

from .engine import age_seconds, label_text


def format_value(value: float | None, unit: str) -> str:
    if value is None:
        return "n/a"
    if unit == "percent":
        return f"{value:.1f}%"
    if unit == "seconds":
        return f"{value * 1000:.0f} ms" if abs(value) < 1 else f"{value:.2f} s"
    if unit == "bytes":
        for suffix, size in (("GiB", 2**30), ("MiB", 2**20), ("KiB", 2**10)):
            if abs(value) >= size:
                return f"{value / size:.1f} {suffix}"
        return f"{value:.0f} B"
    if unit == "req_per_second":
        return f"{value:.2f} req/s"
    if unit == "per_second":
        return f"{value:.3f}/s"
    return f"{value:.3g}" if abs(value) < 1000 else f"{value:,.0f}"


def _age(iso: str | None) -> str:
    seconds = age_seconds(iso)
    if seconds is None:
        return "unknown time"
    if seconds < 90:
        return f"{seconds:.0f} s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min ago"
    return f"{seconds / 3600:.1f} h ago"


def _alert_lines(alerts: list[dict[str, Any]]) -> list[str]:
    lines = []
    for alert in alerts:
        line = f"- {alert['name']} severity={alert['severity']} active since {alert.get('active_at')} ({_age(alert.get('active_at'))}) value={alert.get('value')}"
        labels = label_text(alert.get("labels", {}))
        lines.append(f"{line}\n  labels: {labels}")
        if alert.get("summary"):
            lines.append(f"  summary: {alert['summary']}")
    return lines


def render_context(context: dict[str, Any], max_chars: int = 16000) -> str:
    """Render most important facts first and say explicitly when something was left out."""
    health = context.get("health", {})
    status = health.get("status", "unknown")
    sections: list[list[str]] = []
    header = [
        f"Context version {context.get('version')} as of {context.get('as_of') or 'never'} ({_age(context.get('as_of'))}).",
        f"Overall source health: {status}.",
    ]
    for source in context.get("sources", []):
        line = f"Source {source.get('id')} \"{source.get('name')}\": {source.get('kind')} {source.get('version') or ''} at {source.get('url') or 'unknown'}, query language {source.get('query_language') or 'unknown'}, status {source.get('status')}."
        if source.get("logs_language"):
            line += f" Logs language: {source['logs_language']}."
        header.append(line)
        if source.get("stale"):
            header.append(f"WARNING: source \"{source.get('name')}\" is not answering ({source.get('error')}). Its data below is from the last good poll at {source.get('last_ok')}.")
    if context.get("stale") and not context.get("sources"):
        header.append(f"WARNING: the source is not answering. Everything below is from the last good poll at {health.get('last_ok')}.")
    if health.get("error"):
        header.append(f"Source error: {health['error']}")
    sections.append(header)

    if status == "not_configured":
        sections.append(["No metric source is connected, so no metrics are available."])
        return "\n".join(sections[0] + sections[1])

    alerts = context.get("alerts", {})
    firing = alerts.get("firing", [])
    sections.append([f"FIRING ALERTS ({len(firing)}):"] + (_alert_lines(firing) or ["- none"]))
    pending = alerts.get("pending", [])
    if pending:
        sections.append([f"PENDING ALERTS ({len(pending)}):"] + _alert_lines(pending))

    targets = context.get("targets", {})
    target_lines = [
        f"TARGETS: {targets.get('up', 0)} up, {targets.get('down', 0)} down, {targets.get('unknown', 0)} unknown, of {targets.get('total', 0)}."
    ]
    for target in targets.get("down_list", []):
        target_lines.append(
            f"- DOWN {target['instance']} job={target['job']} since {target.get('since')} error: {target.get('error') or 'none reported'}"
        )
    sections.append(target_lines)

    rules = context.get("rules", {})
    rule_lines = [
        f"RULES: {rules.get('alerting', 0)} alerting and {rules.get('recording', 0)} recording, {len(rules.get('failing', []))} failing."
    ]
    rule_lines += [f"- FAILING {rule['name']} in {rule['group']}: {rule['error']}" for rule in rules.get("failing", [])]
    sections.append(rule_lines)

    anomalies = context.get("anomalies", [])
    anomaly_lines = [f"SIGNALS OUT OF NORMAL RANGE ({len(anomalies)}):"]
    for item in anomalies:
        reason = "over its limit" if item["kind"] == "over_limit" else "unusual compared with its last hour"
        baseline = f", usual {format_value(item.get('baseline'), item['unit'])}" if item.get("baseline") is not None else ""
        score = f", robust z-score {item['z']}" if item.get("z") is not None else ""
        anomaly_lines.append(
            f"- {item['title']} [{label_text(item['labels'])}] = {format_value(item['value'], item['unit'])}{baseline}{score}; {reason}. PromQL: {item['query']}"
        )
    if not anomalies:
        anomaly_lines.append("- none")
    sections.append(anomaly_lines)

    changes = context.get("changes", [])
    if changes:
        sections.append(
            [f"RECORDED CHANGES IN THE LAST 6 HOURS, newest first ({len(changes)}):"]
            + [f"- {change['ts']} [{change['origin']}] {change.get('service') or ''} {change['title']}" for change in changes]
        )

    integrations = context.get("integrations", {})
    loki = integrations.get("loki")
    if loki:
        if loki.get("status") == "ok":
            rows = loki.get("error_lines", [])
            sections.append([f"LOGS, error-like lines in the last 5 minutes from Loki (selector {loki.get('selector')}):"]
                            + ([f"- job={row['job']}: {row['errors_5m']:.0f} lines" for row in rows] or ["- none"]))
        else:
            sections.append([f"LOGS: Loki query failed: {loki.get('error')}"])
    kubernetes = integrations.get("kubernetes")
    if kubernetes:
        if kubernetes.get("status") == "ok":
            lines = ["KUBERNETES:"]
            lines += [f"- Pod {row['namespace']}/{row['pod']}: {row['phase']}, {row['restarts']} restarts, {', '.join(row['reasons']) or 'no reason given'}" for row in kubernetes.get("unhealthy_pods", [])]
            lines += [f"- Deployment {row['namespace']}/{row['deployment']}: {row['ready']} of {row['desired']} ready, revision {row['revision']}, images {', '.join(row['images'])}" for row in kubernetes.get("deployments_below_desired", [])]
            lines += [f"- Warning {row['last_seen']} {row['object']}: {row['reason']}: {row['message']}" for row in kubernetes.get("warning_events", [])]
            if len(lines) == 1:
                lines.append("- no unhealthy pods, warning events, or under-replicated deployments")
            sections.append(lines)
        else:
            sections.append([f"KUBERNETES: query failed: {kubernetes.get('error')}"])

    events = context.get("events", [])
    if events:
        sections.append(
            [f"RECENT CHANGES, newest first ({len(events)}):"]
            + [f"- {event['ts']} [{event['severity']}] {event['title']}" for event in events]
        )

    coverage = context.get("coverage", {})
    missing = coverage.get("missing", [])
    absent = ["traces"]
    if not coverage.get("has_logs"):
        absent.append("logs")
    if not coverage.get("has_changes"):
        absent.append("recorded deployments or changes")
    if not coverage.get("has_kubernetes"):
        absent.append("the Kubernetes API")
    coverage_lines = [
        f"DATA COVERAGE: {coverage.get('metric_count', 0)} metric names exist. Not available here: {', '.join(absent)}."
    ]
    if missing:
        coverage_lines.append("Not found on this server: " + ", ".join(missing) + ".")
    sections.append(coverage_lines)

    signal_lines = ["CURRENT SIGNALS:"]
    for signal in context.get("signals", []):
        if signal.get("error"):
            signal_lines.append(f"- {signal['title']}: query failed: {signal['error']}")
            continue
        if not signal.get("series"):
            continue
        extra = signal.get("total_series", 0) - len(signal["series"])
        values = "; ".join(
            f"{label_text(row['labels'])} = {format_value(row['value'], signal['unit'])}"
            + (f" (usual {format_value(row['baseline'], signal['unit'])})" if row.get("baseline") is not None else "")
            for row in signal["series"]
        )
        suffix = f"; plus {extra} more series" if extra > 0 else ""
        signal_lines.append(f"- {signal['title']}: {values}{suffix}. PromQL: {signal['query']}")
    sections.append(signal_lines)

    return _fit(sections, max_chars)


def _fit(sections: list[list[str]], max_chars: int) -> str:
    output: list[str] = []
    used = 0
    omitted = 0
    for section in sections:
        for line in section:
            if used + len(line) + 1 > max_chars - 120:
                omitted += 1
                continue
            output.append(line)
            used += len(line) + 1
        output.append("")
    text = "\n".join(output).strip()
    if omitted:
        text += f"\n\n[{omitted} lower-priority lines were left out to fit this model's limit. Query for details if tools are available.]"
    return text
