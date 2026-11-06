// Shared helpers: DOM building, API calls, formatting, live state, and dialogs.

export function h(tag, attrs, ...children) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key.startsWith('on') && typeof value === 'function') element.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === 'class') element.className = value;
    // Set through the DOM, which the Content-Security-Policy allows. A style attribute would be blocked.
    else if (key === 'style') element.style.cssText = value;
    else if (key === 'dataset') Object.assign(element.dataset, value);
    else if (key === 'value') element.value = value;
    else if (key === 'checked' || key === 'disabled' || key === 'selected' || key === 'open' || key === 'hidden') element[key] = Boolean(value);
    else element.setAttribute(key, value === true ? '' : value);
  }
  append(element, children);
  return element;
}

function append(parent, children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    if (Array.isArray(child)) append(parent, child);
    else parent.append(child.nodeType ? child : String(child));
  }
}

export function clear(element, ...children) {
  element.replaceChildren();
  append(element, children);
  return element;
}

const ICONS = {
  critical: 'M8 1.5 15 14H1L8 1.5Zm0 4.2v4m0 2.2v.1',
  warning: 'M8 1.5a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13Zm0 3.2v4.2m0 2.2v.1',
  ok: 'M8 1.5a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13ZM5 8.2l2.1 2.1L11 6',
  info: 'M8 1.5a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13Zm0 5.5v4m0-6.2v.1',
};

export function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 16 16');
  svg.setAttribute('class', `row-icon icon-${name}`);
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', ICONS[name] || ICONS.info);
  path.setAttribute('fill', 'none');
  path.setAttribute('stroke', 'currentColor');
  path.setAttribute('stroke-width', '1.5');
  path.setAttribute('stroke-linecap', 'round');
  path.setAttribute('stroke-linejoin', 'round');
  svg.append(path);
  return svg;
}

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

let onAuthRequired = () => {};
export function setAuthHandler(handler) { onAuthRequired = handler; }

export async function api(path, options = {}) {
  const init = { method: options.method || 'GET', headers: {}, credentials: 'same-origin' };
  if (options.body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(options.body);
  }
  if (options.signal) init.signal = options.signal;
  let response;
  try {
    response = await fetch(path, init);
  } catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new ApiError('The console server is not responding. Check that it is still running.', 0);
  }
  if (response.status === 401 && !path.startsWith('/api/auth/')) {
    onAuthRequired();
    throw new ApiError('Sign in to continue.', 401);
  }
  if (response.status === 204) return null;
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (error) { data = null; }
  if (response.status === 403 && data && data.detail === 'password_change_required') {
    location.hash = '#/change-password';
    throw new ApiError('Change your password to continue.', 403);
  }
  if (!response.ok) throw new ApiError(errorMessage(data, response.status), response.status);
  return data;
}

function errorMessage(data, status) {
  const detail = data && data.detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail) && detail.length) {
    return String(detail[0].msg || 'Check what you entered and try again.');
  }
  return `The request failed with status ${status}.`;
}

// Formatting ---------------------------------------------------------------
export function formatValue(value, unit) {
  if (value === null || value === undefined || Number.isNaN(value)) return 'No data';
  const abs = Math.abs(value);
  switch (unit) {
    case 'percent': return `${abs >= 100 || Number.isInteger(value) ? value.toFixed(0) : value.toFixed(1)}%`;
    case 'seconds':
      if (abs === 0) return '0 s';
      if (abs < 0.001) return `${(value * 1e6).toFixed(0)} µs`;
      if (abs < 1) return `${(value * 1000).toFixed(0)} ms`;
      return `${value.toFixed(2)} s`;
    case 'bytes':
      if (abs >= 2 ** 40) return `${(value / 2 ** 40).toFixed(1)} TiB`;
      if (abs >= 2 ** 30) return `${(value / 2 ** 30).toFixed(1)} GiB`;
      if (abs >= 2 ** 20) return `${(value / 2 ** 20).toFixed(0)} MiB`;
      if (abs >= 2 ** 10) return `${(value / 2 ** 10).toFixed(0)} KiB`;
      return `${value.toFixed(0)} B`;
    case 'req_per_second': return `${compact(value)} req/s`;
    case 'per_second': return `${compact(value)}/s`;
    default: return compact(value);
  }
}

function compact(value) {
  const abs = Math.abs(value);
  if (abs >= 1e9) return `${(value / 1e9).toFixed(1)}B`;
  if (abs >= 1e6) return `${(value / 1e6).toFixed(1)}M`;
  if (abs >= 1e4) return `${(value / 1e3).toFixed(1)}k`;
  if (abs >= 100) return value.toFixed(0);
  if (abs >= 1) return value.toFixed(2).replace(/\.?0+$/, '');
  if (abs === 0) return '0';
  return value.toPrecision(2);
}

export function timeAgo(iso) {
  if (!iso) return 'never';
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (Number.isNaN(seconds)) return 'unknown';
  if (seconds < 5) return 'just now';
  if (seconds < 90) return `${Math.round(seconds)} s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 129600) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} days ago`;
}

export function dateTime(iso) {
  if (!iso) return '';
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'medium' });
}

export function labelText(labels) {
  const entries = Object.entries(labels || {});
  return entries.length ? entries.map(([key, value]) => `${key}=${value}`).join(', ') : 'all';
}

// Live state ---------------------------------------------------------------
export const live = {
  context: null,
  connected: false,
  ai: null,
  me: null,
  info: null,
  listeners: new Set(),
  subscribe(listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  },
  emit(type, data) {
    for (const listener of [...this.listeners]) listener(type, data);
  },
};

let source = null;
export function connectLive() {
  if (source) source.close();
  source = new EventSource('/api/stream');
  source.onopen = () => {
    live.connected = true;
    live.emit('connection', true);
  };
  source.onerror = () => {
    live.connected = false;
    live.emit('connection', false);
  };
  source.onmessage = (message) => {
    const parsed = JSON.parse(message.data);
    if (parsed.type === 'context') {
      live.context = parsed.data;
      live.emit('context', parsed.data);
    } else {
      live.emit(parsed.type, parsed.data);
    }
  };
}

export async function refreshAi() {
  if (!can('engineer')) return null;
  live.ai = await api('/api/ai/settings');
  live.emit('ai', live.ai);
  return live.ai;
}

// Interface helpers --------------------------------------------------------
let toastTimer;
export function toast(message) {
  const element = document.getElementById('toast');
  element.textContent = message;
  element.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove('show'), 3200);
}

export function openDialog(content, { wide = false, onClose } = {}) {
  const dialog = document.getElementById('dialog');
  dialog.className = wide ? 'wide' : '';
  clear(dialog, content);
  dialog.onclose = () => {
    clear(dialog);
    if (onClose) onClose();
  };
  if (!dialog.open) dialog.showModal();
  return dialog;
}

export function closeDialog() {
  const dialog = document.getElementById('dialog');
  if (dialog.open) dialog.close();
}

export function dialogHead(title) {
  return h('div', { class: 'dialog-head' },
    h('h2', {}, title),
    h('button', { class: 'button compact', type: 'button', onClick: closeDialog }, 'Close'));
}

export function confirmDialog({ title, body, action, danger = true }) {
  return new Promise((resolve) => {
    let result = false;
    openDialog(h('div', {},
      dialogHead(title),
      h('p', { class: 'body-text' }, body),
      h('div', { class: 'form-actions' },
        h('button', { class: `button ${danger ? 'danger' : 'primary'}`, type: 'button', onClick: () => { result = true; closeDialog(); } }, action),
        h('button', { class: 'button', type: 'button', onClick: closeDialog }, 'Cancel'))),
    { onClose: () => resolve(result) });
  });
}

export function statusBadge(status) {
  const map = {
    open: ['warning', 'Open'], resolved: ['ok', 'Resolved'], false_alarm: ['neutral', 'False alarm'],
    complete: ['ok', 'Complete'], failed: ['critical', 'Failed'], stopped: ['neutral', 'Stopped'],
    running: ['warning', 'Running'], tools_unsupported: ['neutral', 'No query support'],
    active: ['ok', 'Active'], suspended: ['critical', 'Suspended'],
    approved: ['ok', 'Reviewed'], draft: ['warning', 'Draft, needs review'], sent: ['ok', 'Sent'],
    critical: ['critical', 'Critical'], warning: ['warning', 'Warning'], info: ['neutral', 'Info'],
  };
  const [tone, label] = map[status] || ['neutral', status];
  return h('span', { class: `badge ${tone}` }, label);
}

export function renderMarkdown(element, text) {
  const html = window.marked.parse(text || '', { breaks: false, gfm: true });
  element.innerHTML = window.DOMPurify.sanitize(html, { USE_PROFILES: { html: true }, FORBID_TAGS: ['img', 'style', 'form'], FORBID_ATTR: ['style'] });
  for (const link of element.querySelectorAll('a')) {
    link.setAttribute('rel', 'noopener noreferrer');
    link.setAttribute('target', '_blank');
  }
}

export function can(role) {
  const rank = { viewer: 1, engineer: 2, admin: 3, owner: 4 };
  return (rank[live.me?.role] || 0) >= rank[role];
}

export function disconnectLive() {
  if (source) source.close();
  source = null;
  live.connected = false;
  live.context = null;
  live.ai = null;
}

export function usd(value) {
  if (value === null || value === undefined) return 'Unknown';
  if (value === 0) return '$0';
  return value < 0.01 ? `$${value.toFixed(4)}` : `$${value.toFixed(2)}`;
}

export function modelName(settings) {
  if (!settings) return '';
  return settings[settings.provider]?.model || 'No model chosen';
}
