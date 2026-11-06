// Time-series charts built on uPlot, with direct labels, a crosshair, and a value table.
import { clear, formatValue, h } from './core.js';

const MAX_SERIES = 8;
const LABELED_SERIES = 4;

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function seriesKey(metric) {
  return Object.keys(metric).sort().map((key) => `${key}=${metric[key]}`).join(',');
}

function displayNames(metrics, fallback) {
  // Show only labels that differ between series, as constant labels add noise.
  const keys = new Set();
  for (const metric of metrics) Object.keys(metric).forEach((key) => keys.add(key));
  const varying = [...keys].filter((key) => key !== '__name__' && new Set(metrics.map((metric) => metric[key])).size > 1);
  return metrics.map((metric) => {
    const parts = varying.filter((key) => metric[key] !== undefined).map((key) => (varying.length === 1 ? metric[key] : `${key}=${metric[key]}`));
    return parts.join(', ') || metric.__name__ || fallback;
  });
}

export function matrixToSeries(result, start, end, step) {
  const times = [];
  for (let t = start; t <= end + 0.001; t += step) times.push(t);
  const index = (timestamp) => Math.round((timestamp - start) / step);
  const series = result.map((item) => {
    const values = new Array(times.length).fill(null);
    for (const [timestamp, raw] of item.values || []) {
      const position = index(timestamp);
      const value = Number(raw);
      if (position >= 0 && position < times.length && Number.isFinite(value)) values[position] = value;
    }
    const numbers = values.filter((value) => value !== null);
    const last = numbers.length ? numbers[numbers.length - 1] : null;
    return {
      metric: item.metric || {},
      key: seriesKey(item.metric || {}),
      values,
      last,
      min: numbers.length ? Math.min(...numbers) : null,
      max: numbers.length ? Math.max(...numbers) : null,
      mean: numbers.length ? numbers.reduce((sum, value) => sum + value, 0) / numbers.length : null,
    };
  });
  return { times, series };
}

export class TimeSeriesChart {
  constructor(container, { unit = 'short', height = 220, limit = null, title = '', showTable = true, ariaLabel = '', endValue = false } = {}) {
    this.endValue = endValue;
    this.container = container;
    this.unit = unit;
    this.height = height;
    this.limit = limit;
    this.title = title;
    this.showTable = showTable;
    this.colors = new Map();
    this.hidden = new Set();
    this.plot = null;
    this.signature = '';
    this.box = h('div', { class: 'chart-box', role: 'img', 'aria-label': ariaLabel || title });
    this.tableHolder = h('div');
    clear(container, this.box, this.tableHolder);
    this.observer = new ResizeObserver(() => this.resize());
    this.observer.observe(this.box);
  }

  destroy() {
    this.observer.disconnect();
    if (this.plot) this.plot.destroy();
    this.plot = null;
  }

  resize() {
    if (!this.plot) return;
    const width = Math.floor(this.box.clientWidth);
    if (width > 0 && width !== this.plot.width) {
      if (this.wantsLabels(width, this.current?.shown.length || 0) !== this.labelsFit) this.rebuild();
      else this.plot.setSize({ width, height: this.height });
    }
  }

  wantsLabels(width, count) {
    return width >= 420 && count <= LABELED_SERIES && (count > 1 || (this.endValue && count === 1));
  }

  showMessage(text, isError = false) {
    if (this.plot) { this.plot.destroy(); this.plot = null; }
    this.signature = '';
    clear(this.box, h('div', { class: `chart-message${isError ? ' is-error' : ''}` }, text));
    clear(this.tableHolder);
  }

  colorFor(key) {
    if (!this.colors.has(key)) this.colors.set(key, (this.colors.size % MAX_SERIES) + 1);
    return `--series-${this.colors.get(key)}`;
  }

  setData(result, start, end, step, emptyText = 'This query returned no data for the selected time range.') {
    if (!result.length) {
      this.showMessage(emptyText);
      return;
    }
    const { times, series } = matrixToSeries(result, start, end, step);
    const ranked = [...series].sort((a, b) => Math.abs(b.last ?? -Infinity) - Math.abs(a.last ?? -Infinity));
    const shown = ranked.slice(0, MAX_SERIES).sort((a, b) => a.key.localeCompare(b.key));
    const names = displayNames(shown.map((item) => item.metric), this.title || 'Value');
    shown.forEach((item, position) => { item.name = names[position]; });
    this.current = { times, shown, total: series.length };
    const signature = shown.map((item) => item.key).join('|');
    const data = [times, ...shown.map((item) => item.values)];
    if (this.plot && signature === this.signature) {
      this.plot.setData(data);
    } else {
      this.signature = signature;
      this.rebuild();
    }
    this.renderTable();
  }

  rebuild() {
    if (!this.current) return;
    if (this.plot) this.plot.destroy();
    clear(this.box);
    const { times, shown } = this.current;
    const width = Math.max(Math.floor(this.box.clientWidth), 240);
    const labels = this.wantsLabels(width, shown.length);
    const rightPad = shown.length === 1 ? 72 : 148;
    this.labelsFit = labels;
    const unit = this.unit;
    const muted = cssVar('--muted');
    const grid = cssVar('--grid');
    const line = cssVar('--line');
    const chart = this;
    const tooltip = h('div', { class: 'chart-tooltip', hidden: true });
    const allNonNegative = shown.every((item) => item.min === null || item.min >= 0);

    const options = {
      width,
      height: this.height,
      padding: [10, labels ? rightPad : 26, 0, 2],
      legend: { show: false },
      cursor: { drag: { x: false, y: false }, points: { size: 8, width: 2 }, y: false },
      scales: {
        x: { time: true },
        y: {
          range: (_, min, max) => {
            let high = max ?? 1;
            if (chart.limit !== null && chart.limit !== undefined) high = Math.max(high, chart.limit);
            let low = allNonNegative ? 0 : (min ?? 0);
            if (unit === 'percent' && high <= 100 && high > 60) high = 100;
            if (high === low) high = low + 1;
            const pad = (high - low) * 0.08;
            return [allNonNegative ? low : low - pad, high + (unit === 'percent' && high === 100 ? 0 : pad)];
          },
        },
      },
      axes: [
        { stroke: muted, font: '12px "Schibsted Grotesk", sans-serif', grid: { show: false }, ticks: { stroke: line, size: 4, width: 1 }, border: { show: true, stroke: line, width: 1 } },
        {
          stroke: muted, font: '12px "Schibsted Grotesk", sans-serif', size: 62, gap: 6,
          grid: { stroke: grid, width: 1 }, ticks: { show: false },
          values: (_, splits) => splits.map((value) => formatValue(value, unit)),
        },
      ],
      series: [
        {},
        ...shown.map((item) => ({
          label: item.name,
          stroke: cssVar(this.colorFor(item.key)),
          width: 2,
          show: !this.hidden.has(item.key),
          points: { show: false },
          spanGaps: false,
        })),
      ],
      hooks: {
        draw: [(plot) => {
          chart.drawLimit(plot);
          // Read the latest data. Refreshes update the plot without rebuilding these hooks.
          if (labels) chart.drawLabels(plot, chart.current.shown);
        }],
        setCursor: [(plot) => chart.updateTooltip(plot, tooltip, chart.current.shown)],
      },
    };
    this.plot = new window.uPlot(options, [times, ...shown.map((item) => item.values)], this.box);
    this.plot.over.addEventListener('mouseleave', () => { tooltip.hidden = true; });
    this.box.append(tooltip);
  }

  drawLimit(plot) {
    if (this.limit === null || this.limit === undefined) return;
    const { ctx, bbox } = plot;
    const y = Math.round(plot.valToPos(this.limit, 'y', true));
    const ratio = window.devicePixelRatio || 1;
    ctx.save();
    ctx.strokeStyle = cssVar('--critical');
    ctx.lineWidth = ratio;
    ctx.setLineDash([5 * ratio, 4 * ratio]);
    ctx.beginPath();
    ctx.moveTo(bbox.left, y);
    ctx.lineTo(bbox.left + bbox.width, y);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = cssVar('--muted');
    ctx.font = `${11.5 * ratio}px "Schibsted Grotesk", sans-serif`;
    ctx.textAlign = 'left';
    ctx.textBaseline = 'bottom';
    ctx.fillText(`Limit ${formatValue(this.limit, this.unit)}`, bbox.left + 6 * ratio, y - 2 * ratio);
    ctx.restore();
  }

  drawLabels(plot, shown) {
    const { ctx, bbox } = plot;
    const ratio = window.devicePixelRatio || 1;
    const items = [];
    shown.forEach((item, position) => {
      if (this.hidden.has(item.key)) return;
      let lastIndex = item.values.length - 1;
      while (lastIndex >= 0 && item.values[lastIndex] === null) lastIndex -= 1;
      if (lastIndex < 0) return;
      items.push({
        item,
        color: plot.series[position + 1].stroke(),
        x: plot.valToPos(plot.data[0][lastIndex], 'x', true),
        y: plot.valToPos(item.values[lastIndex], 'y', true),
        value: item.values[lastIndex],
      });
    });
    items.sort((a, b) => a.y - b.y);
    const gap = 17 * ratio;
    for (let index = 1; index < items.length; index += 1) {
      items[index].labelY = Math.max(items[index].y, (items[index - 1].labelY ?? items[index - 1].y) + gap);
    }
    ctx.save();
    ctx.font = `500 ${12.5 * ratio}px "Schibsted Grotesk", sans-serif`;
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    const ink = cssVar('--ink');
    const surface = cssVar('--page');
    const maxWidth = 128 * ratio;
    for (const label of items) {
      const labelY = label.labelY ?? label.y;
      ctx.beginPath();
      ctx.arc(label.x, label.y, 4.5 * ratio, 0, Math.PI * 2);
      ctx.fillStyle = surface;
      ctx.fill();
      ctx.beginPath();
      ctx.arc(label.x, label.y, 3.2 * ratio, 0, Math.PI * 2);
      ctx.fillStyle = label.color;
      ctx.fill();
      let text = shown.length === 1 ? formatValue(label.value, this.unit) : label.item.name;
      if (ctx.measureText(text).width > maxWidth) {
        while (text.length > 4 && ctx.measureText(`${text}...`).width > maxWidth) text = text.slice(0, -1);
        text += '...';
      }
      ctx.fillStyle = ink;
      ctx.fillText(text, bbox.left + bbox.width + 12 * ratio, labelY);
    }
    ctx.restore();
  }

  updateTooltip(plot, tooltip, shown) {
    const { idx, left, top } = plot.cursor;
    if (idx === null || idx === undefined || left === null || left < 0) {
      tooltip.hidden = true;
      return;
    }
    const rows = shown
      .filter((item) => !this.hidden.has(item.key) && item.values[idx] !== null)
      .sort((a, b) => b.values[idx] - a.values[idx]);
    const time = new Date(plot.data[0][idx] * 1000).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'medium' });
    clear(tooltip, h('div', { class: 'tip-time' }, time),
      rows.length ? rows.map((item) => h('div', { class: 'tip-row' },
        h('span', { class: 'swatch', style: `background:var(${this.colorFor(item.key)})` }),
        h('span', {}, item.name),
        h('b', {}, formatValue(item.values[idx], this.unit)))) : h('div', {}, 'No data at this time'));
    tooltip.hidden = false;
    const overRect = plot.over.getBoundingClientRect();
    const boxRect = this.box.getBoundingClientRect();
    const offsetX = overRect.left - boxRect.left;
    const offsetY = overRect.top - boxRect.top;
    const width = tooltip.offsetWidth;
    let x = offsetX + left + 14;
    if (x + width > boxRect.width) x = Math.max(0, offsetX + left - width - 14);
    tooltip.style.left = `${x}px`;
    tooltip.style.top = `${Math.max(0, offsetY + Math.min(top ?? 0, plot.over.clientHeight - tooltip.offsetHeight))}px`;
  }

  renderTable() {
    if (!this.showTable) return;
    const { shown, total } = this.current;
    const rows = shown.map((item, position) => h('tr', {},
      h('td', {}, h('button', {
        class: 'legend-name', type: 'button', title: item.name,
        'aria-pressed': String(!this.hidden.has(item.key)),
        onClick: () => this.toggle(item.key, position + 1),
      }, h('span', { class: 'swatch', style: `background:var(${this.colorFor(item.key)})` }), h('span', {}, item.name))),
      h('td', {}, h('b', {}, formatValue(item.last, this.unit))),
      h('td', {}, formatValue(item.min, this.unit)),
      h('td', {}, formatValue(item.max, this.unit)),
      h('td', {}, formatValue(item.mean, this.unit))));
    clear(this.tableHolder,
      h('div', { class: 'legend-table-wrap' },
        h('table', { class: 'legend-table' },
          h('thead', {}, h('tr', {}, ['Series', 'Last', 'Min', 'Max', 'Mean'].map((name) => h('th', { scope: 'col' }, name)))),
          h('tbody', {}, rows))),
      total > shown.length ? h('p', { class: 'legend-note' }, `Showing the ${shown.length} highest of ${total} series. Add topk() or a label filter to narrow the query.`) : null);
  }

  toggle(key, seriesIndex) {
    if (this.hidden.has(key)) this.hidden.delete(key);
    else this.hidden.add(key);
    this.plot?.setSeries(seriesIndex, { show: !this.hidden.has(key) });
    this.renderTable();
  }
}

export function stepFor(rangeSeconds) {
  return Math.max(5, Math.round(rangeSeconds / 240));
}
