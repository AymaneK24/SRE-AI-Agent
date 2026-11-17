// Logs: investigations, every prompt sent to a model, state changes, and the audit trail.
import { api, background, can, clear, dateTime, dialogHead, h, openDialog, statusBadge, usd } from './core.js';

const TABS = [['investigations', 'Investigations'], ['prompts', 'Prompts sent'], ['events', 'State changes'], ['alerts', 'Alerts sent'], ['audit', 'Audit trail']];

export function mount(container, params) {
  const visible = TABS.filter(([id]) => can('admin') || !['alerts', 'audit'].includes(id));
  let tab = visible.some(([id]) => id === params.id) ? params.id : 'investigations';
  const tabs = h('div', { class: 'tabs', role: 'tablist', 'aria-label': 'Log type' });
  const body = h('div');
  clear(container, h('div', { class: 'page' },
    h('h1', { class: 'page-title' }, 'Every prompt, on the record.'),
    h('p', { class: 'page-lede' }, can('admin')
      ? 'Everything your team investigated stays searchable. Each prompt is stored exactly as it was sent to the model.'
      : 'Your own investigations, and those shared with the team. Each prompt is stored exactly as it was sent to the model.'),
    tabs, body));

  function renderTabs() {
    clear(tabs, visible.map(([id, label]) => h('button', {
      type: 'button', role: 'tab', 'aria-selected': String(id === tab),
      onClick: () => { tab = id; renderTabs(); background(load()); },
    }, label)));
  }

  function table(headers, rows) {
    return h('div', { class: 'table-wrap' }, h('table', { class: 'data-table' },
      h('thead', {}, h('tr', {}, headers.map(([label, numeric]) => h('th', { scope: 'col', class: numeric ? 'num' : null }, label)))),
      h('tbody', {}, rows)));
  }

  async function load() {
    clear(body, h('p', { class: 'muted' }, 'Loading'));
    try {
      if (tab === 'investigations') await loadInvestigations();
      if (tab === 'prompts') await loadPrompts();
      if (tab === 'events') await loadEvents();
      if (tab === 'audit') await loadAudit();
      if (tab === 'alerts') await loadAlerts();
    } catch (error) {
      clear(body, h('p', { class: 'error-note' }, error.message));
    }
  }

  async function loadInvestigations() {
    const search = h('input', { class: 'input', type: 'search', placeholder: 'Search prompts, answers, and root causes', 'aria-label': 'Search investigations', style: 'flex:1 1 300px;max-width:520px' });
    const status = h('select', { class: 'input', 'aria-label': 'Filter by status', style: 'width:auto' },
      [['', 'All statuses'], ['open', 'Open'], ['resolved', 'Resolved'], ['false_alarm', 'False alarm']].map(([value, label]) => h('option', { value }, label)));
    const results = h('div');
    let debounce;

    async function run() {
      const query = new URLSearchParams({ q: search.value, status: status.value, limit: '100' });
      try {
        const data = await api(`/api/conversations?${query}`);
        if (!data.items.length) {
          clear(results, h('p', { class: 'empty' }, search.value || status.value ? 'No investigations match this search.' : 'No investigations yet. Start one from the Investigate page.'));
          return;
        }
        clear(results, table(
          [['Investigation'], ['Status'], ['Started by'], ['Confirmed root cause'], ['Model'], ['Queries', true], ['Tokens', true], ['Cost', true], ['Last updated']],
          data.items.map((item) => h('tr', {},
            h('td', { class: 'cell-title' }, h('a', { href: `#/investigate/${item.id}` }, item.title)),
            h('td', {}, statusBadge(item.status)),
            h('td', {}, item.created_by || h('span', { class: 'muted' }, 'Unknown')),
            h('td', { style: 'min-width:200px;max-width:340px' }, item.root_cause_note || h('span', { class: 'muted' }, 'Not recorded')),
            h('td', {}, item.model || h('span', { class: 'muted' }, 'None')),
            h('td', { class: 'num' }, item.queries_run),
            h('td', { class: 'num' }, Number(item.tokens).toLocaleString()),
            h('td', { class: 'num' }, usd(item.cost_usd)),
            h('td', { style: 'white-space:nowrap' }, dateTime(item.updated_at))))));
      } catch (error) {
        clear(results, h('p', { class: 'error-note' }, error.message));
      }
    }
    search.addEventListener('input', () => { clearTimeout(debounce); debounce = setTimeout(run, 250); });
    status.addEventListener('change', run);
    clear(body, h('div', { class: 'filter-row' }, search, status), results);
    await run();
  }

  async function showCall(id) {
    const call = await api(`/api/llm-calls/${id}`);
    const request = call.request || {};
    const system = request.system ?? (request.messages || []).find((message) => message.role === 'system')?.content ?? '';
    const messages = (request.messages || []).filter((message) => message.role !== 'system');
    openDialog(h('div', {},
      dialogHead(`Prompt ${call.id}, round ${call.round}`),
      h('dl', { class: 'facts' },
        fact('Model', call.model), fact('Provider', call.provider), call.endpoint ? fact('Sent to', call.endpoint) : null, fact('Status', statusBadge(call.status)),
        fact('Context version', call.context_version ?? 'None'), fact('Started', dateTime(call.started_at)),
        fact('First output', call.first_token_ms !== null ? `${(call.first_token_ms / 1000).toFixed(1)} s` : 'None'),
        fact('Total time', call.duration_ms !== null ? `${(call.duration_ms / 1000).toFixed(1)} s` : 'None'),
        fact('Tokens in, out', `${call.input_tokens ?? '?'}, ${call.output_tokens ?? '?'}`), fact('Cost', usd(call.cost_usd)), fact('Stop reason', call.stop_reason || 'None')),
      call.error ? h('p', { class: 'error-note' }, call.error) : null,
      h('h3', {}, 'System prompt and live context'), h('pre', { class: 'prompt-text', tabindex: '0' }, system),
      h('h3', { style: 'margin-top:16px' }, 'Messages'), h('pre', { class: 'prompt-text', tabindex: '0' }, JSON.stringify(messages, null, 1)),
      h('h3', { style: 'margin-top:16px' }, 'Response'), h('pre', { class: 'prompt-text', tabindex: '0' }, call.response_text || JSON.stringify(call.response_tool_calls, null, 1)),
      h('p', { class: 'small muted', style: 'margin-top:12px' }, `SHA-256 of the request: ${call.request_sha256}`)), { wide: true });
  }

  function fact(label, value) {
    return h('div', {}, h('dt', {}, label), h('dd', {}, value));
  }

  // One answer can take several rounds with the model. They appear as one line, so the list doesn't look full of repeats.
  function groupCalls(rows) {
    const groups = new Map();
    for (const row of [...rows].reverse()) {
      const key = `${row.conversation_id}:${row.message_id ?? row.id}`;
      if (!groups.has(key)) groups.set(key, { ...row, calls: [], input_tokens: 0, output_tokens: 0, cost_usd: 0, duration_ms: 0 });
      const group = groups.get(key);
      group.calls.push(row);
      group.input_tokens += row.input_tokens || 0;
      group.output_tokens += row.output_tokens || 0;
      group.cost_usd = row.cost_usd === null || group.cost_usd === null ? null : group.cost_usd + row.cost_usd;
      group.duration_ms += row.duration_ms || 0;
      group.status = row.status;
    }
    return [...groups.values()].reverse();
  }

  function showRounds(group) {
    if (group.calls.length === 1) {
      showCall(group.calls[0].id).catch((error) => clear(body, h('p', { class: 'error-note' }, error.message)));
      return;
    }
    openDialog(h('div', {},
      dialogHead(`${group.calls.length} rounds: ${group.title}`),
      table([['Round', true], ['Sent'], ['Tokens in', true], ['Tokens out', true], ['Status'], ['']], group.calls.map((row) => h('tr', {},
        h('td', { class: 'num' }, row.round), h('td', { class: 'nowrap' }, dateTime(row.started_at)),
        h('td', { class: 'num' }, row.input_tokens ?? ''), h('td', { class: 'num' }, row.output_tokens ?? ''), h('td', {}, statusBadge(row.status)),
        h('td', {}, h('button', { class: 'button compact', type: 'button', onClick: () => showCall(row.id) }, 'View')))))), { wide: true });
  }

  async function loadPrompts() {
    const rows = await api('/api/llm-calls?limit=500');
    if (!rows.length) {
      clear(body, h('p', { class: 'empty' }, 'No prompts have been sent yet.'));
      return;
    }
    clear(body, table(
      [['Sent'], ['Investigation'], ['Model'], ['Rounds', true], ['Tokens in', true], ['Tokens out', true], ['Cost', true], ['Time', true], ['Status'], ['']],
      groupCalls(rows).map((group) => h('tr', {},
        h('td', { class: 'nowrap' }, dateTime(group.started_at)),
        h('td', { class: 'cell-title' }, h('a', { href: `#/investigate/${group.conversation_id}` }, group.title)),
        h('td', {}, group.model),
        h('td', { class: 'num' }, group.calls.length),
        h('td', { class: 'num' }, group.input_tokens.toLocaleString()),
        h('td', { class: 'num' }, group.output_tokens.toLocaleString()),
        h('td', { class: 'num' }, usd(group.cost_usd)),
        h('td', { class: 'num' }, `${(group.duration_ms / 1000).toFixed(1)} s`),
        h('td', {}, statusBadge(group.status)),
        h('td', {}, h('button', { class: 'button compact', type: 'button', onClick: () => showRounds(group) }, 'View'))))));
  }

  async function loadEvents() {
    const rows = await api('/api/events?limit=300');
    if (!rows.length) {
      clear(body, h('p', { class: 'empty' }, 'No state changes recorded yet.'));
      return;
    }
    clear(body, table([['Time'], ['Severity'], ['What changed']],
      rows.map((row) => h('tr', {},
        h('td', { style: 'white-space:nowrap' }, dateTime(row.ts)),
        h('td', {}, statusBadge(row.severity)),
        h('td', { style: 'overflow-wrap:anywhere' }, row.title)))));
  }

  async function loadAlerts() {
    const rows = await api('/api/org/deliveries');
    if (!rows.length) {
      clear(body, h('p', { class: 'empty' }, 'No alerts sent yet. Add a channel in Connections.'));
      return;
    }
    clear(body, table([['Sent'], ['Alert'], ['Status'], ['Problem']], rows.map((row) => h('tr', {},
      h('td', { style: 'white-space:nowrap' }, dateTime(row.created_at)), h('td', { style: 'overflow-wrap:anywhere' }, row.title),
      h('td', {}, statusBadge(row.status)), h('td', { style: 'overflow-wrap:anywhere' }, row.error)))));
  }

  async function loadAudit() {
    const [rows, verify] = await Promise.all([api('/api/audit?limit=300'), api('/api/audit/verify')]);
    clear(body,
      h('div', { class: 'filter-row' },
        h('a', { class: 'button compact', href: '/api/audit/export?format=csv', download: 'audit-log.csv' }, 'Export CSV'),
        h('a', { class: 'button compact', href: '/api/audit/export?format=json', download: 'audit-log.json' }, 'Export JSON')),
      h('p', { class: `form-result ${verify.intact ? 'ok' : 'bad'}`, style: 'margin:0 0 14px' },
        verify.intact
          ? `Hash chain intact across ${verify.entries} entries. No entry has been changed or removed since it was written.`
          : `Hash chain broken at entry ${verify.broken_at_id}. This log has been altered.`),
      table([['Time'], ['Person'], ['Action'], ['Target'], ['Details']],
        rows.map((row) => h('tr', {},
          h('td', { style: 'white-space:nowrap' }, dateTime(row.ts)),
          h('td', { style: 'white-space:nowrap' }, row.actor),
          h('td', { style: 'white-space:nowrap' }, row.action),
          h('td', { style: 'white-space:nowrap' }, row.target),
          h('td', { class: 'mono', style: 'overflow-wrap:anywhere;min-width:280px' }, row.detail)))));
  }

  renderTabs();
  background(load());
  return () => {};
}
