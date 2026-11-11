"""Turns a finished automatic investigation into an email with evidence charts and a PDF on how to resolve the problem.

Everything here reads. The commands are for a person to review and run in their own tools.
"""
from __future__ import annotations

import html
import io
import json
import logging
import re
import time
from typing import Any

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import Image as PdfImage
from reportlab.platypus import KeepTogether, Paragraph, Preformatted, SimpleDocTemplate, Spacer

from . import reports
from .analysis import parse_ts
from .evidence_charts import draw_chart, format_number, points_of, series_label
from .mailer import BODY, FONT, INK, MUTED
from .secrets_store import scrub
from .workflows import workflows_for

log = logging.getLogger("rcc.report")

MAX_CHARTS = 4
MAX_SERIES = 6
MINIMUM_WINDOW_S = 3600
READ_ONLY_NOTE = "This console changed nothing. It's AI advice based on the data shown, so check it before acting. People run any commands, in their own tools."
SECTION_ORDER = ("summary", "likely root cause", "evidence", "competing explanation", "next checks", "suggested remediation")


def split_sections(answer: str) -> dict[str, str]:
    """The answer's parts by heading. An answer that doesn't follow the format counts as one summary."""
    parts = re.split(r"^##\s+(.+?)\s*$", answer, flags=re.MULTILINE)
    found = {parts[index].strip().lower(): parts[index + 1].strip() for index in range(1, len(parts) - 1, 2)}
    if not found:
        return {"summary": answer.strip()}
    return found


def _inline_html(text: str) -> str:
    value = html.escape(text)
    value = re.sub(r"\*\*(.+?)\*\*", rf'<b style="color:{INK}">\1</b>', value)
    return re.sub(r"`([^`]+)`", r'<code style="font-family:Consolas,Menlo,monospace;font-size:13px;background:#f1f2f4;padding:1px 4px;word-break:break-word">\1</code>', value)


def markdown_html(markdown: str) -> str:
    """Only paragraphs, lists, bold, and code. The text comes from a model reading monitored systems, so all of it is escaped and links stay plain text."""
    output: list[str] = []
    items: list[str] = []
    ordered = False

    def flush() -> None:
        nonlocal items
        if items:
            tag = "ol" if ordered else "ul"
            lis = "".join(f'<li style="margin:0 0 6px">{item}</li>' for item in items)
            output.append(f'<{tag} style="margin:0 0 16px;padding-left:22px;color:{BODY};font-size:15px;line-height:1.55;{FONT}">{lis}</{tag}>')
            items = []

    for line in markdown.splitlines():
        stripped = line.strip()
        bullet = re.match(r"^(?:[-*•]\s+|(\d+)[.)]\s+)(.*)", stripped)
        if bullet:
            is_number = bool(bullet.group(1))
            if items and is_number != ordered:
                flush()
            ordered = is_number
            items.append(_inline_html(bullet.group(2)))
            continue
        flush()
        if stripped.startswith("#"):
            stripped = stripped.lstrip("#").strip()
        if stripped and not stripped.startswith("```"):
            output.append(f'<p style="margin:0 0 12px;color:{BODY};font-size:15px;line-height:1.6;{FONT}">{_inline_html(stripped)}</p>')
    flush()
    return "".join(output)


async def _query_chart(engine: Any, query: str, source_id: int | None, title: str, unit: str, limit: float | None, began: float, end: float) -> dict[str, Any] | None:
    window = max(end - min(began - 1800, end - MINIMUM_WINDOW_S), MINIMUM_WINDOW_S)
    start = end - window
    step = max(15, round(window / 120))
    try:
        data = await engine.source(source_id).query_range(query, start, end, step)
    except Exception as error:  # Evidence is a bonus. It must never stop the email.
        log.info("evidence chart skipped: %s", error)
        return None
    series: list[dict[str, Any]] = [{"label": series_label(item.get("metric") or {}), "points": points_of(item)} for item in data.get("result", []) if isinstance(item, dict)]
    series = [item for item in series if item["points"]]
    series.sort(key=lambda item: max(value for _, value in item["points"]), reverse=True)
    png = draw_chart(series[:MAX_SERIES], unit, limit, began)
    if not png:
        return None
    peak = max(value for item in series for _, value in item["points"])
    latest = max(item["points"][-1][1] for item in series[:MAX_SERIES])
    return {"title": title, "query": query, "png": png, "peak": format_number(peak, unit), "latest": format_number(latest, unit), "series_total": len(series)}


async def collect_evidence(db: Any, engine: Any, conversation_id: int, event: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    """Charts of what the AI queried. A model that can't run queries still gets charts of the signals behind the alert."""
    calls = await db.all("SELECT name, arguments FROM tool_calls WHERE conversation_id = ? AND ok = 1 ORDER BY id", (conversation_id,))
    wanted: list[dict[str, Any]] = []
    seen: set[str] = set()
    ran: list[str] = []
    for call in calls:
        if call["name"] not in {"run_range_query", "run_instant_query"}:
            continue
        arguments = json.loads(call["arguments"])
        query = str(arguments.get("query", "")).strip()
        if query and query not in seen:
            seen.add(query)
            ran.append(query)
            raw = arguments.get("source_id")
            wanted.append({"query": query, "source_id": int(raw) if raw not in (None, "") else None, "title": query[:90], "unit": "", "limit": None})
    context = engine.public_context()
    signals = context.get("signals", [])
    chosen = [signal for signal in signals if signal.get("id") == detail.get("signal_id")]
    chosen += [signal for signal in signals if signal.get("golden") and signal not in chosen]
    for signal in chosen:
        if signal.get("query") and signal["query"] not in seen:
            seen.add(signal["query"])
            wanted.append({"query": signal["query"], "source_id": signal.get("source_id"), "title": signal.get("title") or signal["query"][:90],
                           "unit": signal.get("unit") or "", "limit": signal.get("limit")})
    began = parse_ts(event["ts"]).timestamp()
    end = time.time()
    charts: list[dict[str, Any]] = []
    for item in wanted:
        if len(charts) >= MAX_CHARTS:
            break
        chart = await _query_chart(engine, item["query"], item["source_id"], item["title"], item["unit"], item["limit"], began, end)
        if chart:
            charts.append(chart)
    return {"charts": charts, "queries_ran": ran}


async def reviewed_workflows(db: Any, org_id: int, event: dict[str, Any], detail: dict[str, Any]) -> list[dict[str, Any]]:
    names = [str(detail.get("name"))] if detail.get("name") else (["targetdown"] if event["kind"].startswith("target") else [])
    signals = [str(detail["signal_id"])] if detail.get("signal_id") else []
    rows = await workflows_for(db, org_id, alert_names=names, signal_ids=signals, text=event["title"])
    return [row for row in rows if row["status"] == "approved"][:2]


def _when(iso: str) -> str:
    return (parse_ts(iso)).strftime("%d %b %Y %H:%M UTC")


def _heading(text: str) -> str:
    return f'<h2 style="margin:24px 0 8px;color:{INK};font-size:18px;line-height:1.3;font-weight:600;{FONT}">{html.escape(text)}</h2>'


def _facts(rows: list[tuple[str, str]]) -> str:
    cells = "".join(
        f'<tr><td style="padding:6px 16px 6px 0;color:{MUTED};font-size:14px;white-space:nowrap;vertical-align:top;border-bottom:1px solid #e6e7ea;{FONT}">{html.escape(label)}</td>'
        f'<td style="padding:6px 0;color:{INK};font-size:14px;font-weight:600;border-bottom:1px solid #e6e7ea;{FONT}">{html.escape(value)}</td></tr>' for label, value in rows)
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 8px">{cells}</table>'


def _commands_html(workflows: list[dict[str, Any]]) -> str:
    output = ""
    for workflow in workflows:
        output += f'<p style="margin:12px 0 6px;color:{INK};font-size:15px;font-weight:600;{FONT}">{html.escape(workflow["name"])}, {html.escape(workflow["risk"])} risk</p>'
        for index, step in enumerate(workflow["steps"], start=1):
            output += f'<p style="margin:0 0 4px;color:{BODY};font-size:14px;line-height:1.5;{FONT}">{index}. {html.escape(step["title"])}</p>'
            if step.get("command"):
                output += f'<pre style="margin:0 0 8px;padding:8px 10px;background:#f1f2f4;color:{INK};font-family:Consolas,Menlo,monospace;font-size:12.5px;white-space:pre-wrap;word-break:break-word">{html.escape(step["command"])}</pre>'
    return output


def build_email(facts: dict[str, Any], sections: dict[str, str], evidence: dict[str, Any], workflows: list[dict[str, Any]]) -> dict[str, Any]:
    fact_rows = [("Status", facts["status"]), ("Started", _when(facts["ts"])), ("Company", facts["company"]), ("AI confidence after checks", facts["confidence"])]
    images: dict[str, bytes] = {}
    body = _facts(fact_rows)
    if facts.get("status_note"):
        body += f'<p style="margin:14px 0 0;color:{BODY};font-size:14px;line-height:1.5;{FONT}">{html.escape(facts["status_note"])}</p>'
    if sections.get("summary"):
        body += _heading("What the AI found") + markdown_html(sections["summary"])
    if sections.get("likely root cause"):
        body += _heading("Likely root cause") + markdown_html(sections["likely root cause"])
    if evidence["charts"]:
        body += _heading("Evidence")
        for index, chart in enumerate(evidence["charts"]):
            name = f"chart{index}"
            images[name] = chart["png"]
            body += (f'<p style="margin:12px 0 4px;color:{INK};font-size:14px;font-weight:600;word-break:break-word;{FONT}">{html.escape(chart["title"])}</p>'
                     f'<img src="cid:{name}" width="576" alt="Chart of {html.escape(chart["title"], quote=True)}. Peak {html.escape(chart["peak"], quote=True)}, latest {html.escape(chart["latest"], quote=True)}." style="display:block;width:100%;max-width:576px;height:auto;border:1px solid #e6e7ea">'
                     f'<p style="margin:4px 0 14px;color:{MUTED};font-size:13px;{FONT}">Peak {html.escape(chart["peak"])}, latest {html.escape(chart["latest"])}. Times are UTC.</p>')
    if sections.get("evidence"):
        body += ("" if evidence["charts"] else _heading("Evidence")) + markdown_html(sections["evidence"])
    if sections.get("next checks"):
        body += _heading("Next checks") + markdown_html(sections["next checks"])
    if sections.get("suggested remediation"):
        body += _heading("Suggested remediation") + markdown_html(sections["suggested remediation"])
    if workflows:
        body += _heading("Commands from your reviewed workflows") + _commands_html(workflows)
    body += f'<p style="margin:20px 0 20px;padding:12px 14px;background:#fff6e5;color:{BODY};font-size:14px;line-height:1.5;{FONT}">{html.escape(READ_ONLY_NOTE)} The attached PDF has the full report.</p>'
    plain = [f"Alert: {facts['title']}", f"Status: {facts['status']}", f"Started: {_when(facts['ts'])}", f"AI confidence after checks: {facts['confidence']}"]
    if facts.get("status_note"):
        plain.append(facts["status_note"])
    for name in SECTION_ORDER:
        if sections.get(name):
            plain.append(f"{name.capitalize()}\n{sections[name]}")
    plain.append(READ_ONLY_NOTE + " The attached PDF has the full report and the charts.")
    return {"text": scrub("\n\n".join(plain)), "body_html": scrub(body), "images": images}


def build_pdf(facts: dict[str, Any], sections: dict[str, str], evidence: dict[str, Any], workflows: list[dict[str, Any]]) -> bytes:
    """A report someone can keep, hand over, and work from: what happened, why, the evidence, and the steps."""
    buffer = io.BytesIO()
    TEXT, HEADING, SUBHEAD, SMALL, BULLET, clean = reports.TEXT, reports.HEADING, reports.SUBHEAD, reports.SMALL, reports.BULLET, reports.clean
    code_style = reports.ParagraphStyle("code", parent=SMALL, fontName="Courier", textColor=reports.INK, backColor=reports.RAISED, borderPadding=4, leading=11, spaceBefore=3, spaceAfter=8)

    def footer(canvas: Any, doc: Any) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(reports.MUTED)
        canvas.drawString(18 * mm, 10 * mm, f"{facts['company']}. Confidential.".encode("cp1252", "replace").decode("cp1252"))
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()

    def body_of(markdown: str) -> list[Any]:
        output: list[Any] = []
        for line in markdown.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("```"):
                continue
            bullet = re.match(r"^(?:[-*•]\s+|\d+[.)]\s+)(.*)", stripped)
            if bullet:
                output.append(Paragraph(reports._inline(bullet.group(1)), BULLET, bulletText="-"))
            else:
                output.append(Paragraph(reports._inline(stripped.lstrip("#").strip()), TEXT))
        return output

    flow: list[Any] = [Paragraph(clean(facts["title"]), reports.TITLE), Paragraph(facts.get("subtitle") or f"Incident report from an automatic investigation. Alert started {_when(facts['ts'])}.", reports.SUBTITLE), Spacer(1, 8)]
    rows = [[Paragraph(item, reports.HEAD_CELL) for item in ("Status", "AI confidence after checks", "Company")],
            [Paragraph(clean(facts["status"]), reports.CELL_BOLD), Paragraph(clean(facts["confidence"]), reports.CELL_BOLD), Paragraph(clean(facts["company"]), reports.CELL_BOLD)]]
    flow.append(reports._table(rows, [reports.WIDTH / 3] * 3))
    flow.append(Spacer(1, 6))
    flow.append(Paragraph(clean(READ_ONLY_NOTE), SMALL))
    if facts.get("status_note"):
        flow.append(Paragraph(clean(facts["status_note"]), TEXT))
    for name, heading in (("summary", "What the AI found"), ("likely root cause", "Likely root cause")):
        if sections.get(name):
            flow.append(Paragraph(heading, HEADING))
            flow += body_of(sections[name])
    if evidence["charts"]:
        flow.append(Paragraph("Evidence", HEADING))
        for chart in evidence["charts"]:
            width = reports.WIDTH
            flow.append(KeepTogether([
                Paragraph(clean(chart["title"]), SUBHEAD),
                PdfImage(io.BytesIO(chart["png"]), width=width, height=width * 460 / 1152),
                Paragraph(f"Peak {clean(chart['peak'])}, latest {clean(chart['latest'])}. Times are UTC. Query:", SMALL),
                Preformatted(clean(chart["query"])[:300], code_style, maxLineLength=100),
            ]))
    if sections.get("evidence"):
        if not evidence["charts"]:
            flow.append(Paragraph("Evidence", HEADING))
        flow += body_of(sections["evidence"])
    for name, heading in (("competing explanation", "Competing explanation"), ("next checks", "Next checks"), ("suggested remediation", "How to resolve it")):
        if sections.get(name):
            flow.append(Paragraph(heading, HEADING))
            flow += body_of(sections[name])
    if workflows:
        flow.append(Paragraph("Steps and commands from your reviewed workflows", HEADING))
        flow.append(Paragraph("Your team reviewed these. Read each one before running it. Nothing here runs by itself.", SMALL))
        for workflow in workflows:
            flow.append(Paragraph(clean(f"{workflow['name']}, {workflow['risk']} risk"), SUBHEAD))
            for index, step in enumerate(workflow["steps"], start=1):
                block: list[Any] = [Paragraph(clean(f"{index}. {step['title']}"), reports.CELL_BOLD)]
                if step.get("detail"):
                    block.append(Paragraph(clean(step["detail"]), TEXT))
                if step.get("command"):
                    block.append(Preformatted(clean(step["command"])[:800], code_style, maxLineLength=100))
                if step.get("undo"):
                    block.append(Paragraph(clean(f"To undo: {step['undo']}"), TEXT))
                if step.get("verify"):
                    block.append(Paragraph(clean(f"It worked if: {step['verify']}"), TEXT))
                block.append(Spacer(1, 4))
                flow.append(KeepTogether(block))
    if evidence["queries_ran"]:
        flow.append(Paragraph("Queries the AI ran", HEADING))
        for query in evidence["queries_ran"][:20]:
            flow.append(Preformatted(clean(query)[:300], code_style, maxLineLength=100))
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=18 * mm, bottomMargin=18 * mm,
                            title=f"Incident report: {facts['title']}"[:120], author="Root Cause Console")
    doc.build(flow, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()


