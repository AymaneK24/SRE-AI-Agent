"""Draws evidence charts as PNG images, for emails and PDF files.

Email programs can't run a page's charts, so these are real images. Pillow draws them with its own built-in
font, so they look the same on any server, including minimal container images.
"""
from __future__ import annotations

import io
import math
from datetime import datetime, timezone
from typing import Any

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 1152, 460  # Twice the displayed size, so they stay sharp.
INK, MUTED, GRID = (23, 24, 28), (111, 114, 122), (226, 227, 232)
# Red from the logo first, then colors that stay apart for most kinds of color vision.
COLORS = [(229, 72, 77), (37, 99, 168), (13, 138, 106), (185, 122, 8), (122, 79, 181), (23, 24, 28)]
PLOT = (104, 28, WIDTH - 28, HEIGHT - 110)


def format_number(value: float, unit: str = "") -> str:
    if unit == "percent":
        return f"{value:.1f}%"
    if unit == "seconds":
        return f"{value * 1000:.0f} ms" if abs(value) < 1 else f"{value:.2f} s"
    if unit == "bytes":
        for size, name in ((1 << 40, "TB"), (1 << 30, "GB"), (1 << 20, "MB"), (1 << 10, "KB")):
            if abs(value) >= size:
                return f"{value / size:.1f} {name}"
        return f"{value:.0f} B"
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if abs(value) >= 10_000:
        return f"{value / 1000:.1f}k"
    return f"{value:.0f}" if value == int(value) else f"{value:.3g}"


def _nice_ticks(low: float, high: float, count: int = 4) -> list[float]:
    span = high - low or abs(high) or 1.0
    rough = span / count
    power = 10 ** math.floor(math.log10(rough))
    step = next((item * power for item in (1, 2, 2.5, 5, 10) if item * power >= rough), 10 * power)
    start = math.floor(low / step) * step
    ticks = []
    value = start
    while value <= high + step * 0.999 and len(ticks) < 12:
        ticks.append(round(value, 10))
        value += step
    return ticks


def series_label(metric: dict[str, Any]) -> str:
    parts = [f"{key}={value}" for key, value in sorted(metric.items()) if key != "__name__"]
    return (", ".join(parts) or str(metric.get("__name__") or "value"))[:60]


def points_of(item: dict[str, Any]) -> list[tuple[float, float]]:
    points = []
    for moment, raw in item.get("values", []):
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            points.append((float(moment), value))
    return points


def draw_chart(series: list[dict[str, Any]], unit: str = "", limit: float | None = None, marker: float | None = None) -> bytes | None:
    """`series` holds {"label", "points"}. `marker` is the moment the alert started. Returns None if there's nothing to draw."""
    lines = [item for item in series if len(item["points"]) >= 2][:len(COLORS)]
    if not lines:
        return None
    image = Image.new("RGB", (WIDTH, HEIGHT), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=22)
    small = ImageFont.load_default(size=20)
    left, top, right, bottom = PLOT
    times = [moment for item in lines for moment, _ in item["points"]]
    values = [value for item in lines for _, value in item["points"]] + ([limit] if limit is not None else [])
    start, end = min(times), max(times)
    ticks = _nice_ticks(min(0.0, min(values)) if min(values) >= 0 else min(values), max(values))
    low, high = ticks[0], ticks[-1] if ticks[-1] > ticks[0] else ticks[0] + 1

    def x_of(moment: float) -> float:
        return left + (moment - start) / ((end - start) or 1) * (right - left)

    def y_of(value: float) -> float:
        return bottom - (value - low) / ((high - low) or 1) * (bottom - top)

    for tick in ticks:
        y = y_of(tick)
        draw.line((left, y, right, y), fill=GRID, width=2)
        draw.text((left - 12, y), format_number(tick, unit), fill=MUTED, font=small, anchor="rm")
    for index in range(5):
        moment = start + (end - start) * index / 4
        draw.text((x_of(moment), bottom + 10), datetime.fromtimestamp(moment, timezone.utc).strftime("%H:%M"),
                  fill=MUTED, font=small, anchor="ma" if 0 < index < 4 else ("la" if index == 0 else "ra"))
    if marker is not None and start <= marker <= end:
        x = x_of(marker)
        for y in range(top, bottom, 14):
            draw.line((x, y, x, y + 7), fill=INK, width=2)
        draw.text((x + 8, top), "Alert started", fill=INK, font=small, anchor="la")
    if limit is not None:
        y = y_of(limit)
        for x in range(left, right, 18):
            draw.line((x, y, x + 10, y), fill=(198, 47, 47), width=2)
    for index, item in enumerate(lines):
        coordinates = [(x_of(moment), y_of(value)) for moment, value in item["points"]]
        draw.line(coordinates, fill=COLORS[index], width=4, joint="curve")
        end_x, end_y = coordinates[-1]
        draw.ellipse((end_x - 6, end_y - 6, end_x + 6, end_y + 6), fill=COLORS[index])
    # Two rows of labels under the chart, cut short rather than overflowing.
    x, y = left, bottom + 44
    for index, item in enumerate(lines):
        label = item["label"]
        while draw.textlength(label, font=font) > 500 and len(label) > 8:
            label = label[:-4] + "..."
        width = draw.textlength(label, font=font) + 42
        if x + width > right and x > left:
            x, y = left, y + 30
        if y > HEIGHT - 26:
            break
        draw.rectangle((x, y + 4, x + 22, y + 18), fill=COLORS[index])
        draw.text((x + 30, y), label, fill=INK, font=font)
        x += width + 22
    buffer = io.BytesIO()
    image.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()
