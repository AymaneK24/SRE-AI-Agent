"""Turns the live context into the text a model reads, within a fixed size budget."""
from __future__ import annotations

from typing import Any

from .summary import age_seconds, label_text


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


def _problem_lines(index: int, problem: dict[str, Any]) -> list[str]:
    detail = problem.get("detail") or {}
    lines = [f"{index}. [{problem['severity']}] {problem['title']}"]
    # From the service dependency map: a lead to check, not a conclusion.
    cause = detail.get("caused_by") or {}
    if cause.get("title"):
        lines.append(f"   Dependency map: likely a symptom of \"{cause['title']}\" ({' -> '.join(cause.get('path') or [])}). Check that problem first.")
    elif detail.get("affects"):
        lines.append(f"   Dependency map: also failing and depend on it: {', '.join(detail['affects'][:8])}. This is a likely root cause.")
    if problem["kind"] == "k8s":
        facts = detail.get("facts") or {}
        bits = []
        if detail.get("pods"):
            bits.append(f"pods: {', '.join(detail['pods'][:5])}")
        if facts.get("container"):
            bits.append(f"container: {facts['container']}")
        if facts.get("image"):
            bits.append(f"image: {facts['image']}")
        if facts.get("limits"):
            bits.append("limits: " + ", ".join(f"{key}={value}" for key, value in facts["limits"].items()))
        if facts.get("message"):
            bits.append(f"message: {facts['message']}")
        if bits:
            lines.append("   " + "; ".join(bits))
        diagnosis = detail.get("diagnosis")
        if diagnosis:
            lines.append(f"   Diagnosis (computed from cluster state, not by a model): {diagnosis['cause']}")
            for item in diagnosis.get("evidence", [])[:4]:
                lines.append(f"     - {item}")
        logs = detail.get("logs")
        if logs and logs.get("lines"):
            lines.append(f"   Last {len(logs['lines'])} log lines of {logs.get('which', 'the container')} ({logs.get('pod', '')}):")
            lines.append("   <untrusted_logs>")
            lines += [f"     {line}" for line in logs["lines"]]
            lines.append("   </untrusted_logs>")
        elif logs and logs.get("note"):
            lines.append(f"   Logs: {logs['note']}")
    elif problem["kind"] == "alert" and detail.get("summary"):
        lines.append(f"   summary: {detail['summary']}")
    elif problem["kind"] == "target" and detail.get("error"):
        lines.append(f"   error: {detail['error']}")
    elif problem["kind"] == "signal":
        if detail.get("value") is not None:
            usual = f", usual {format_value(detail.get('baseline'), detail.get('unit', ''))}" if detail.get("baseline") is not None else ""
            lines.append(f"   value {format_value(detail['value'], detail.get('unit', ''))}{usual}. PromQL: {detail.get('query', '')}")
    return lines


def focus_context(context: dict[str, Any], fingerprints: list[str]) -> dict[str, Any]:
    """The live context narrowed to the problems a person chose: those problems, their alerts, targets and signals,
    and only the cluster and log detail of their namespaces. Everything else is left out, so the model looks where
    the person is looking, and fewer tokens are sent."""
    wanted = set(fingerprints)
    problems = [item for item in context.get("problems", []) if item.get("fingerprint") in wanted]
    if not problems:
        return context
    focused = dict(context)
    focused["focus"] = [item["title"] for item in problems]
    focused["problems"] = problems
    alerts = context.get("alerts") or {}
    focused["alerts"] = {**alerts, "firing": [item for item in alerts.get("firing", []) if item.get("fingerprint") in wanted], "pending": []}
    targets = context.get("targets") or {}
    focused["targets"] = {**targets, "down_list": [item for item in targets.get("down_list", []) if item.get("fingerprint") in wanted]}
    focused["anomalies"] = [item for item in context.get("anomalies", []) if item.get("fingerprint") in wanted]
    focused["rules"] = {**(context.get("rules") or {}), "failing": []}
    # Signals the chosen anomalies come from, so their current values stay visible. Other signals are left out.
    chosen_signals = {(item.get("source_id"), item.get("signal_id")) for item in focused["anomalies"]}
    focused["signals"] = [signal for signal in context.get("signals", []) if (signal.get("source_id"), signal.get("id")) in chosen_signals]
    namespaces = {(item.get("detail") or {}).get("namespace") for item in problems if item.get("kind") == "k8s"} - {None, ""}
    workloads = {(item.get("detail") or {}).get("workload") for item in problems if item.get("kind") == "k8s"} - {None, ""}
    jobs = ({(item.get("detail") or {}).get("job") for item in problems}
            | {((item.get("detail") or {}).get("labels") or {}).get("job") for item in problems}) - {None, ""}
    connections = []
    for item in _connections(context):
        item = dict(item)
        if item["kind"] == "kubernetes":
            item["unhealthy_pods"] = [row for row in item.get("unhealthy_pods", []) if row.get("namespace") in namespaces]
            item["deployments_below_desired"] = [row for row in item.get("deployments_below_desired", []) if row.get("namespace") in namespaces]
            item["warning_events"] = [row for row in item.get("warning_events", []) if any(f" {ns}/" in f" {row.get('object', '')}" or row.get("namespace") == ns for ns in namespaces)]
        elif item["kind"] == "loki":
            def related(job: str) -> bool:
                return job in jobs or any(job.startswith(f"{ns}/") for ns in namespaces) or any(workload and workload in job for workload in workloads)
            item["error_lines"] = [row for row in item.get("error_lines", []) if related(str(row.get("job", "")))]
        connections.append(item)
    focused["connections"] = connections
    focused["events"] = [event for event in context.get("events", []) if event.get("fingerprint") in wanted][:10]
    return focused


def _connections(context: dict[str, Any]) -> list[dict[str, Any]]:
    """Every log store and cluster. Older contexts only hold the main one of each kind."""
    if context.get("connections") is not None:
        return list(context["connections"])
    return [{"kind": kind, **item} for kind, item in (context.get("integrations") or {}).items() if kind in {"loki", "kubernetes"}]


def render_context(context: dict[str, Any], max_chars: int = 16000) -> str:
    """Render what is broken first, then the evidence around it, then the background, and say when something was left out."""
    health = context.get("health", {})
    status = health.get("status", "unknown")
    coverage = context.get("coverage", {})
    has_metrics = coverage.get("has_metrics", bool(context.get("sources")))
    sections: list[list[str]] = []
    header = [
        f"Context version {context.get('version')} as of {context.get('as_of') or 'never'} ({_age(context.get('as_of'))}).",
        f"Overall connection health: {status}.",
    ]
    for source in context.get("sources", []):
        line = f"Metric source {source.get('id')} \"{source.get('name')}\": {source.get('kind')} {source.get('version') or ''} at {source.get('url') or 'unknown'}, query language {source.get('query_language') or 'unknown'}, status {source.get('status')}."
        if source.get("logs_language"):
            line += f" Logs language: {source['logs_language']}."
        header.append(line)
        if source.get("stale"):
            header.append(f"WARNING: source \"{source.get('name')}\" is not answering ({source.get('error')}). Its data below is from the last good poll at {source.get('last_ok')}.")
    connections = _connections(context)
    labels = {"loki": "Log store: Loki, query language LogQL", "kubernetes": "Kubernetes cluster"}
    for item in connections:
        state = "ok" if item.get("status") == "ok" else f"not answering: {item.get('error')}"
        several = sum(1 for other in connections if other["kind"] == item["kind"]) > 1
        which = f" (connection_id {item['id']}{', main' if item.get('main') else ''})" if several and item.get("id") is not None else ""
        header.append(f"{labels.get(item['kind'], item['kind'])} \"{item.get('name', item['kind'])}\"{which}, status {state}.")
    if context.get("stale") and not context.get("sources"):
        header.append(f"WARNING: the source is not answering. Everything below is from the last good poll at {health.get('last_ok')}.")
    sections.append(header)

    if status == "not_configured":
        sections.append(["Nothing is connected: no metric source, log store, or Kubernetes cluster, so there is no live data."])
        return "\n".join(sections[0] + sections[1])

    problems = context.get("problems", [])
    if context.get("focus"):
        sections.append(["FOCUS: the person chose to investigate only these problems. Explain them, and leave other problems aside:"]
                        + [f"- {title}" for title in context["focus"]])
    problem_lines = [f"PROBLEMS RIGHT NOW ({len(problems)}), worst first:"]
    for index, problem in enumerate(problems, start=1):
        problem_lines += _problem_lines(index, problem)
    if not problems:
        problem_lines.append("- none detected by the console. That doesn't prove the system is healthy: check what the user describes.")
    sections.append(problem_lines)

    changes = context.get("changes", [])
    if changes:
        sections.append(
            [f"RECORDED CHANGES IN THE LAST 6 HOURS, newest first ({len(changes)}):"]
            + [f"- {change['ts']} [{change['origin']}] {change.get('service') or ''} {change['title']}" for change in changes]
        )

    for loki in [item for item in connections if item["kind"] == "loki"]:
        name = f" \"{loki.get('name')}\"" if len([item for item in connections if item["kind"] == "loki"]) > 1 else ""
        if loki.get("status") == "ok":
            rows = loki.get("error_lines", [])
            sections.append([f"LOGS, error-like lines in the last 5 minutes from Loki{name} (selector {loki.get('selector')}):"]
                            + ([f"- job={row['job']}: {row['errors_5m']:.0f} lines" for row in rows] or ["- none"]))
        else:
            sections.append([f"LOGS: Loki{name} query failed: {loki.get('error')}"])

    clusters = [item for item in connections if item["kind"] == "kubernetes"]
    for kubernetes in clusters:
        name = f" \"{kubernetes.get('name')}\"" if len(clusters) > 1 else ""
        if kubernetes.get("status") == "ok":
            lines = [f"KUBERNETES DETAIL{name}:"]
            lines += [f"- Pod {row['namespace']}/{row['pod']}: {row['phase']}, {row['restarts']} restarts, {', '.join(row['reasons']) or 'no reason given'}" for row in kubernetes.get("unhealthy_pods", [])]
            lines += [f"- Deployment {row['namespace']}/{row['deployment']}: {row['ready']} of {row['desired']} ready, revision {row['revision']}, images {', '.join(row['images'])}" for row in kubernetes.get("deployments_below_desired", [])]
            lines += [f"- Warning {row['last_seen']} {row['object']}: {row['reason']}: {row['message']}" for row in kubernetes.get("warning_events", [])]
            if kubernetes.get("forbidden"):
                lines.append(f"- This token can't list: {', '.join(kubernetes['forbidden'])}. Problems in those kinds can't be seen.")
            if len(lines) == 1:
                lines.append("- no unhealthy pods, warning events, or under-replicated deployments")
            sections.append(lines)
        else:
            sections.append([f"KUBERNETES: the cluster{name} didn't answer: {kubernetes.get('error')}"])

    if has_metrics:
        alerts = context.get("alerts", {})
        firing = alerts.get("firing", [])
        pending = alerts.get("pending", [])
        targets = context.get("targets", {})
        rules = context.get("rules", {})
        anomalies = context.get("anomalies", [])
        quiet = []
        if firing:
            sections.append([f"FIRING ALERTS ({len(firing)}):"] + _alert_lines(firing))
        else:
            quiet.append("FIRING ALERTS: none.")
        if pending:
            sections.append([f"PENDING ALERTS ({len(pending)}):"] + _alert_lines(pending))
        target_line = f"TARGETS: {targets.get('up', 0)} up, {targets.get('down', 0)} down, {targets.get('unknown', 0)} unknown, of {targets.get('total', 0)}."
        if targets.get("down_list"):
            sections.append([target_line] + [
                f"- DOWN {target['instance']} job={target['job']} since {target.get('since')} error: {target.get('error') or 'none reported'}"
                for target in targets["down_list"]])
        else:
            quiet.append(target_line)
        failing = rules.get("failing", [])
        if failing:
            sections.append([f"RULES: {len(failing)} failing."] + [f"- FAILING {rule['name']} in {rule['group']}: {rule['error']}" for rule in failing])
        if anomalies:
            anomaly_lines = [f"SIGNALS OUT OF NORMAL RANGE ({len(anomalies)}):"]
            for item in anomalies:
                reason = "over its limit" if item["kind"] == "over_limit" else "unusual compared with its last hour"
                baseline = f", usual {format_value(item.get('baseline'), item['unit'])}" if item.get("baseline") is not None else ""
                score = f", robust z-score {item['z']}" if item.get("z") is not None else ""
                anomaly_lines.append(
                    f"- {item['title']} [{label_text(item['labels'])}] = {format_value(item['value'], item['unit'])}{baseline}{score}; {reason}. PromQL: {item['query']}"
                )
            sections.append(anomaly_lines)
        else:
            quiet.append("SIGNALS OUT OF RANGE: none.")
        if quiet:
            sections.append([" ".join(quiet)])

    events = context.get("events", [])
    if events:
        sections.append(
            [f"RECENT EVENTS, newest first ({len(events)}):"]
            + [f"- {event['ts']} [{event['severity']}] {event['title']}" for event in events]
        )

    missing = coverage.get("missing", [])
    available = [name for name, present in (("metrics", has_metrics), ("logs", coverage.get("has_logs")), ("Kubernetes", coverage.get("has_kubernetes"))) if present]
    absent = [name for name, present in (("metrics", has_metrics), ("logs", coverage.get("has_logs")), ("the Kubernetes API", coverage.get("has_kubernetes"))) if not present]
    absent.append("traces")
    if not coverage.get("has_changes"):
        absent.append("recorded deployments or changes")
    coverage_lines = [f"DATA COVERAGE: available: {', '.join(available) or 'nothing'}. Not available here: {', '.join(absent)}."]
    if has_metrics:
        coverage_lines.append(f"{coverage.get('metric_count', 0)} metric names exist.")
    if missing:
        coverage_lines.append("Not found on this server: " + ", ".join(missing) + ".")
    sections.append(coverage_lines)

    if has_metrics:
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
