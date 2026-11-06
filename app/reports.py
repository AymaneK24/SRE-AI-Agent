"""Builds a project's PDF report from its saved snapshot. Telemetry text is treated as plain text, never as markup."""
from __future__ import annotations

import io
import re
from datetime import datetime
from typing import Any
from xml.sax.saxutils import escape

from reportlab.graphics.shapes import Drawing, Line, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .analysis import format_minutes, parse_ts, plural

INK = colors.HexColor("#17181c")
BODY = colors.HexColor("#4a4d55")
MUTED = colors.HexColor("#6f727a")
LINE = colors.HexColor("#d9dbe0")
RAISED = colors.HexColor("#f3f4f6")
CRITICAL = colors.HexColor("#c62f2f")
WARNING = colors.HexColor("#e09a00")
OK = colors.HexColor("#0a7d0a")
WIDTH = A4[0] - 36 * mm

BASE = dict(fontName="Helvetica", textColor=BODY, alignment=TA_LEFT)
TITLE = ParagraphStyle("title", **{**BASE, "fontName": "Helvetica-Bold", "textColor": INK}, fontSize=25, leading=29, spaceAfter=4)
SUBTITLE = ParagraphStyle("subtitle", **BASE, fontSize=10.5, leading=15)
HEADING = ParagraphStyle("heading", **{**BASE, "fontName": "Helvetica-Bold", "textColor": INK}, fontSize=14, leading=18, spaceBefore=16, spaceAfter=6, keepWithNext=1)
SUBHEAD = ParagraphStyle("subhead", **{**BASE, "fontName": "Helvetica-Bold", "textColor": INK}, fontSize=10, leading=13, spaceBefore=6, spaceAfter=2, keepWithNext=1)
TEXT = ParagraphStyle("text", **BASE, fontSize=9.5, leading=13.5, spaceAfter=4)
SMALL = ParagraphStyle("small", **{**BASE, "textColor": MUTED}, fontSize=8, leading=11)
CELL = ParagraphStyle("cell", **BASE, fontSize=8.5, leading=11)
CELL_BOLD = ParagraphStyle("cellbold", **{**BASE, "fontName": "Helvetica-Bold", "textColor": INK}, fontSize=8.5, leading=11)
HEAD_CELL = ParagraphStyle("headcell", **{**BASE, "textColor": MUTED}, fontSize=7.5, leading=10)
STAT = ParagraphStyle("stat", **{**BASE, "fontName": "Helvetica-Bold", "textColor": INK}, fontSize=18, leading=21)
BULLET = ParagraphStyle("bullet", **BASE, fontSize=9.5, leading=13.5, leftIndent=11, bulletIndent=1, spaceAfter=3)


def clean(text: Any) -> str:
    """The built-in PDF font covers Western languages. Replace other characters instead of drawing empty boxes."""
    value = str(text if text is not None else "")
    for old, new in (("→", "->"), ("≤", "<="), ("≥", ">="), (" ", " ")):
        value = value.replace(old, new)
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)
    return escape(value.encode("cp1252", "replace").decode("cp1252"))


def _date(iso: str | None, with_time: bool = False) -> str:
    if not iso:
        return "Unknown"
    moment = parse_ts(iso)
    return moment.strftime("%d %b %Y %H:%M UTC") if with_time else moment.strftime("%d %b %Y")


def _table(rows: list[list[Any]], widths: list[float]) -> Table:
    table = Table(rows, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def _status_text(severity: str) -> str:
    return {"critical": "Critical", "warning": "Warning"}.get(severity, severity.capitalize())


def _daily_chart(daily: list[dict[str, Any]]) -> Drawing:
    """A stacked bar for each day. A table of the busiest days follows, so color isn't the only way to read it."""
    height, axis, left = 96, 16, 22
    drawing = Drawing(WIDTH, height + axis + 14)
    plot = WIDTH - left
    peak = max((day["critical"] + day["warning"] for day in daily), default=0)
    top = max(peak, 1)
    for tick in sorted({0, top}):
        y = axis + tick / top * height
        drawing.add(Line(left, y, WIDTH, y, strokeColor=LINE, strokeWidth=0.4))
        drawing.add(String(left - 5, y - 2.5, str(tick), fontName="Helvetica", fontSize=7, fillColor=MUTED, textAnchor="end"))
    slot = plot / max(len(daily), 1)
    bar = max(1.5, min(slot - 1.5, 16))
    for index, day in enumerate(daily):
        x = left + index * slot + (slot - bar) / 2
        y = axis
        for value, color in ((day["critical"], CRITICAL), (day["warning"], WARNING)):
            if value:
                size = value / top * height
                drawing.add(Rect(x, y, bar, max(size - 0.7, 0.7), fillColor=color, strokeColor=None))
                y += size
    step = max(1, round(len(daily) / 6))
    for index in range(0, len(daily), step):
        label = datetime.fromisoformat(daily[index]["date"]).strftime("%d %b")
        drawing.add(String(left + index * slot + slot / 2, 4, label, fontName="Helvetica", fontSize=7, fillColor=MUTED, textAnchor="middle"))
    drawing.add(Rect(left, height + axis + 6, 7, 7, fillColor=CRITICAL, strokeColor=None))
    drawing.add(String(left + 10, height + axis + 7, "Critical", fontName="Helvetica", fontSize=7.5, fillColor=BODY))
    drawing.add(Rect(left + 46, height + axis + 6, 7, 7, fillColor=WARNING, strokeColor=None))
    drawing.add(String(left + 56, height + axis + 7, "Warning", fontName="Helvetica", fontSize=7.5, fillColor=BODY))
    return drawing


def _inline(text: str) -> str:
    """Support only bold and code, after everything else has been escaped."""
    value = clean(text)
    value = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", value)
    return re.sub(r"`([^`]+)`", r'<font face="Courier">\1</font>', value)


def _notes(markdown: str) -> list[Any]:
    flow: list[Any] = []
    for raw in markdown.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            flow.append(Paragraph(_inline(line.lstrip("# ")), SUBHEAD))
        elif re.match(r"^([-*]|\d+\.)\s+", line):
            flow.append(Paragraph(_inline(re.sub(r"^([-*]|\d+\.)\s+", "", line)), BULLET, bulletText="-"))
        else:
            flow.append(Paragraph(_inline(line), TEXT))
    return flow


def _summary_sentence(data: dict[str, Any]) -> str:
    summary = data["summary"]
    if not summary["occurrences"]:
        return "No problems were recorded for these sources in this period."
    top = next((issue for issue in data["issues"] if issue["recurring"]), None)
    text = f"{plural(summary['occurrences'], 'problem occurrence')} across {plural(summary['issues'], 'different issue')}, with {summary['active']} still active."
    reliability = data.get("reliability")
    if reliability:
        text = f"{plural(reliability['incidents'], 'incident')}, made up of " + text
    if top:
        text += f" The most frequent was {top['title']}, at {plural(top['occurrences'], 'occurrence')}."
    return text


def build_pdf(report: dict[str, Any]) -> bytes:
    data = report["data"]
    project = data["project"]
    summary = data["summary"]
    period = data["period"]
    buffer = io.BytesIO()
    title = f"{project['name']} reliability report"

    def footer(canvas: Any, doc: Any) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(18 * mm, 10 * mm, f"{data.get('company') or 'Root Cause Console'}. Confidential.".encode("cp1252", "replace").decode("cp1252"))
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=18 * mm, bottomMargin=18 * mm,
                            title=title, author="Root Cause Console", subject="Reliability report")
    flow: list[Any] = [
        Paragraph(clean(project["name"]), TITLE),
        Paragraph(f"Reliability report, {_date(period['start'])} to {_date(period['end'])}", SUBTITLE),
        Paragraph(clean(f"Generated {_date(report['created_at'], True)} by {report['created_by']}. Times are UTC."), SMALL),
    ]
    if project.get("description"):
        flow += [Spacer(1, 6), Paragraph(clean(project["description"]), TEXT)]

    flow.append(Paragraph("Summary", HEADING))
    flow.append(Paragraph(clean(_summary_sentence(data)), TEXT))
    stats = [
        (str(summary["occurrences"]), "Problem occurrences"), (str(summary["recurring"]), "Recurring issues"), (str(summary["critical"]), "Critical occurrences"),
        (str(summary["active"]), "Active now"), (format_minutes(summary["average_clear_minutes"]) if summary["average_clear_minutes"] is not None else "None", "Average time to clear"),
    ]
    stat_table = Table([[Paragraph(clean(value), STAT) for value, _ in stats], [Paragraph(clean(label), SMALL) for _, label in stats]], colWidths=[WIDTH / 5] * 5)
    stat_table.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    flow += [Spacer(1, 4), stat_table]
    reliability = data.get("reliability")
    if reliability:
        facts = [f"Incidents: {reliability['incidents']}"]
        if reliability["mttr_minutes"] is not None:
            facts.append(f"Average time to clear an incident: {format_minutes(reliability['mttr_minutes'])}")
        if reliability["mtbf_minutes"] is not None:
            facts.append(f"Average time between incidents: {format_minutes(reliability['mtbf_minutes'])}")
        if reliability["clear_percent"] is not None:
            facts.append(f"Time with no incident: {reliability['clear_percent']}%")
        if summary.get("dismissed"):
            facts.append(f"Marked by your team as false alarms or expected: {summary['dismissed']} occurrences ({summary['noise_percent']}%)")
        previous = (data.get("trend") or {}).get("previous")
        if previous is not None:
            facts.append(f"Occurrences in the previous period of the same length: {previous}")
        flow.append(Paragraph(clean(". ".join(facts) + "."), TEXT))
    if period["covered_days"] < period["days"] * 0.9:
        flow.append(Paragraph(clean(f"Recording began {_date(period['data_since'])}, so this covers {'less than 1' if period['covered_days'] < 1 else 'about ' + str(period['covered_days'])} of the {period['days']} days requested."), SMALL))

    flow.append(Paragraph("Source status", HEADING))
    rows = [[Paragraph(item, HEAD_CELL) for item in ("Source", "Type", "Status when generated", "Active alerts", "Targets down")]]
    for source in data["sources"]:
        status = {"ok": "Answering", "unreachable": "Not answering", "paused": "Paused"}.get(source["status"], "Waiting for first reading")
        rows.append([Paragraph(clean(source["name"]), CELL_BOLD), Paragraph(clean(source["kind_label"]), CELL), Paragraph(status, CELL), Paragraph(str(source["alerts"]), CELL), Paragraph(str(source["targets_down"]), CELL)])
    flow.append(_table(rows, [WIDTH * 0.32, WIDTH * 0.2, WIDTH * 0.22, WIDTH * 0.13, WIDTH * 0.13]))

    if summary["occurrences"]:
        flow.append(KeepTogether([Paragraph("Problems per day", HEADING), _daily_chart(data["daily"])]))

    incidents = data.get("incidents") or []
    if incidents:
        flow.append(Paragraph("Incidents", HEADING))
        flow.append(Paragraph("Problems that started close together count as one incident.", ParagraphStyle("incident-intro", parent=SMALL, spaceAfter=3)))
        rows = [[Paragraph(item, HEAD_CELL) for item in ("Started", "Lasted", "Severity", "Problems", "What happened")]]
        for item in incidents[:15]:
            what = ", ".join(item["issues"][:4]) + (f", and {len(item['issues']) - 4} more" if len(item["issues"]) > 4 else "")
            if item.get("after_change"):
                what += f'. Started after: {item["after_change"]}'
            rows.append([Paragraph(_date(item["start"], True), CELL), Paragraph("Still active" if item["active"] else format_minutes(item["minutes"]), CELL), Paragraph(_status_text(item["severity"]), CELL),
                         Paragraph(str(item["problems"]), CELL), Paragraph(clean(what), CELL)])
        flow.append(_table(rows, [WIDTH * 0.2, WIDTH * 0.11, WIDTH * 0.1, WIDTH * 0.09, WIDTH * 0.5]))
        if len(incidents) > 15:
            flow.append(Paragraph(f"Showing the 15 most recent of {len(incidents)}.", SMALL))

    flow.append(Paragraph("Recurring issues", HEADING))
    recurring = [issue for issue in data["issues"] if issue["recurring"]]
    if recurring:
        rows = [[Paragraph(item, HEAD_CELL) for item in ("Issue", "Severity", "Times", "Total time", "Average", "Last started")]]
        for issue in recurring[:25]:
            name = clean(issue["title"]) + (f'<br/><font size="7.5" color="#6f727a">{clean(", ".join(issue["affected"]))}</font>' if issue["affected"] else "")
            verdict = {"false_positive": "Your team marked this a false alarm", "expected": "Your team marked this expected", "real": "Your team confirmed this is real"}.get(issue.get("verdict") or "")
            if verdict:
                name += f'<br/><font size="7.5" color="#6f727a">{clean(verdict)}</font>'
            rows.append([Paragraph(name, CELL), Paragraph(_status_text(issue["severity"]), CELL), Paragraph(str(issue["occurrences"]), CELL_BOLD),
                         Paragraph(format_minutes(issue["total_minutes"]), CELL), Paragraph(format_minutes(issue["average_minutes"]), CELL), Paragraph(_date(issue["last_seen"]), CELL)])
        flow.append(_table(rows, [WIDTH * 0.42, WIDTH * 0.11, WIDTH * 0.07, WIDTH * 0.13, WIDTH * 0.12, WIDTH * 0.15]))
        if len(recurring) > 25:
            flow.append(Paragraph(f"Showing the top 25 of {len(recurring)}.", SMALL))
    else:
        flow.append(Paragraph("No issue happened more than once in this period.", TEXT))

    single = [issue for issue in data["issues"] if not issue["recurring"]]
    if single:
        flow.append(Paragraph(clean(f"Another {plural(len(single), 'issue')} happened once, including: " + "; ".join(issue["title"] for issue in single[:5]) + "."), SMALL))

    flow.append(Paragraph("Patterns", HEADING))
    if data["patterns"]:
        flow.append(Paragraph("These come from timing in recorded events. They point to what's worth checking and don't prove a cause.", ParagraphStyle("intro", parent=SMALL, keepWithNext=1, spaceAfter=3)))
        for pattern in data["patterns"]:
            flow.append(KeepTogether([Paragraph(clean(pattern["title"]), SUBHEAD), Paragraph(clean(pattern["detail"]), TEXT)]))
    else:
        flow.append(Paragraph("No repeating pattern met the thresholds in this period. See Data and method.", TEXT))

    if data.get("root_causes"):
        flow.append(Paragraph("Root causes confirmed by your team", HEADING))
        for item in data["root_causes"]:
            flow.append(KeepTogether([Paragraph(clean(item["title"]), SUBHEAD), Paragraph(clean(f"{item['note']} ({_date(item['updated_at'])})"), TEXT)]))

    if data.get("workflows"):
        flow.append(Paragraph("Related remediation workflows", HEADING))
        flow.append(Paragraph("These are instructions for people. This software doesn't run them.", SMALL))
        flow.append(Spacer(1, 3))
        for item in data["workflows"]:
            state = "Reviewed" if item["status"] == "approved" else "Draft, not reviewed"
            flow.append(Paragraph(clean(f"{item['name']}: {state}, {item['risk']} risk. {item['summary']}"), BULLET, bulletText="-"))

    flow.append(Paragraph("AI notes", HEADING))
    if report.get("ai_notes"):
        cost = report.get("ai_cost_usd")
        origin = f"Written by {report['ai_model']}" + (f" at a cost of ${cost:.4f}" if cost else "") + ". This is AI opinion and can be wrong, so check it against the sections above."
        box: list[Any] = [Paragraph(clean(origin), SMALL), Spacer(1, 3)] + _notes(report["ai_notes"])
        unmatched = (report.get("ai_check") or {}).get("unmatched") or []
        if unmatched:
            box.append(Paragraph(clean("Check these numbers: they don't appear in the report data: " + ", ".join(unmatched)), SUBHEAD))
        wrapper = Table([[box]], colWidths=[WIDTH])
        wrapper.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), RAISED), ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9), ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
        wrapper.splitByRow = 1
        flow += box if len(report["ai_notes"]) > 2800 else [wrapper]
    else:
        flow.append(Paragraph("This report has no AI notes.", TEXT))

    flow.append(Paragraph("Data and method", HEADING))
    for line in (
        "Everything above the AI notes is calculated from state changes recorded by the console while it was running. Problems during downtime can be missing.",
        "An incident groups problems that started within 10 minutes of each other, or during an incident's first hour. Time with no incident covers only the time the console was recording.",
        "An occurrence runs from a problem's start to its end. A recurring issue happened at least twice. Issues with the same name on different instances count together.",
        "Total time adds up all occurrences, so it can be longer than the period when problems overlap.",
        "Patterns need a minimum amount of evidence: 4 starts for timing, 3 shared starts within 10 minutes for problems together, and 2 starts within 30 minutes after a change.",
        "Changes appear only if your tools send them to the console's webhook.",
    ):
        flow.append(Paragraph(clean(line), BULLET, bulletText="-"))
    doc.build(flow, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()
