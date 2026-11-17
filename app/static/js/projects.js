// Projects: metric sources and Kubernetes namespaces behind one system, with status, recurring issues, patterns, and PDF reports.
import { api, background, can, clear, closeDialog, confirmDialog, dateTime, dialogHead, h, icon, live, openDialog, timeAgo, toast, usd } from './core.js';

const HEALTH = {
  healthy: ['ok', 'Healthy'], attention: ['warning', 'Needs attention'], problems: ['critical', 'Problems now'],
  waiting: ['neutral', 'Waiting for data'], no_sources: ['neutral', 'No sources'],
};
const VERDICTS = { real: 'Real problem', false_positive: 'False alarm', expected: 'Expected' };
const SOURCE_STATUS = { ok: ['ok', 'Answering'], unreachable: ['critical', 'Not answering'], paused: ['neutral', 'Paused'], waiting: ['neutral', 'Waiting'] };

function badge([tone, text]) { return h('span', { class: `badge ${tone}` }, text); }

function coverText(project) {
  const parts = [];
  if (project.sources?.length) parts.push(`${project.sources.length} ${project.sources.length === 1 ? 'source' : 'sources'}`);
  const namespaces = project.namespaces || [];
  if (namespaces.includes('*')) parts.push('all namespaces');
  else if (namespaces.length) parts.push(namespaces.length <= 2 ? namespaces.join(', ') : `${namespaces.length} namespaces`);
  return parts.join(' and ') || 'Nothing yet';
}

function duration(minutes) {
  if (minutes === null || minutes === undefined) return 'Not finished';
  if (minutes < 1) return 'Under 1 min';
  if (minutes < 60) return `${Math.round(minutes)} min`;
  if (minutes < 2880) return `${(minutes / 60).toFixed(1)} h`;
  return `${(minutes / 1440).toFixed(1)} days`;
}

function stat(value, label) { return h('div', { class: 'stat' }, h('b', {}, String(value)), h('span', {}, label)); }

function tableOf(headers, rows) {
  return h('div', { class: 'table-wrap' }, h('table', { class: 'data-table' },
    h('thead', {}, h('tr', {}, headers.map(([label, num]) => h('th', { scope: 'col', class: num ? 'num' : null }, label)))),
    h('tbody', {}, rows)));
}

// A bar per day. Values also have a tooltip and appear in the table below it.
function dailyChart(daily) {
  const svgNS = 'http://www.w3.org/2000/svg';
  const make = (tag, attrs, text) => {
    const node = document.createElementNS(svgNS, tag);
    Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const width = 900; const height = 150; const left = 30; const bottom = 20; const top = 6;
  const peak = Math.max(1, ...daily.map((day) => day.critical + day.warning));
  const plot = height - bottom - top;
  const total = daily.reduce((sum, day) => sum + day.critical + day.warning, 0);
  const svg = make('svg', { viewBox: `0 0 ${width} ${height}`, role: 'img', class: 'daily-chart', 'aria-label': `Problems per day, ${total} in total, with at most ${peak} in one day` });
  [0, peak].forEach((tick) => {
    const y = top + plot - (tick / peak) * plot;
    svg.append(make('line', { x1: left, x2: width, y1: y, y2: y, class: 'chart-grid' }));
    svg.append(make('text', { x: left - 6, y: y + 4, 'text-anchor': 'end', class: 'chart-label' }, tick));
  });
  const slot = (width - left) / daily.length;
  const bar = Math.max(2, Math.min(slot - 2, 22));
  daily.forEach((day, index) => {
    const x = left + index * slot + (slot - bar) / 2;
    let y = top + plot;
    [['critical', day.critical], ['warning', day.warning]].forEach(([name, value]) => {
      if (!value) return;
      const size = (value / peak) * plot;
      y -= size;
      const rect = make('rect', { x, y: y + 1, width: bar, height: Math.max(size - 2, 1), rx: 1.5, class: `bar-${name}` });
      rect.append(make('title', {}, `${day.date}: ${value} ${name}`));
      svg.append(rect);
    });
  });
  const step = Math.max(1, Math.round(daily.length / 7));
  for (let index = 0; index < daily.length; index += step) {
    svg.append(make('text', { x: left + index * slot + slot / 2, y: height - 5, 'text-anchor': 'middle', class: 'chart-label' }, daily[index].date.slice(5)));
  }
  return h('div', {}, h('div', { class: 'chart-legend' },
    h('span', {}, h('i', { class: 'swatch bar-critical' }), 'Critical'), h('span', {}, h('i', { class: 'swatch bar-warning' }), 'Warning')), svg);
}

export function mount(container, params) {
  let disposed = false;
  let notesTimer = null;
  let days = 30;
  const body = h('div');
  clear(container, h('div', { class: 'page' }, body));

  async function sourcesPicker(selected) {
    const sources = await api('/api/sources');
    const boxes = sources.map((source) => ({ id: source.id, input: h('input', { type: 'checkbox', checked: selected.includes(source.id) }), name: source.name, kind: source.kind }));
    return {
      node: sources.length
        ? h('fieldset', { class: 'stack source-picker' }, h('legend', {}, 'Metric sources in this project'),
          boxes.map((box) => h('label', { class: 'check' }, box.input, box.name, h('span', { class: 'small muted' }, ` ${box.kind}`))))
        : null,
      values: () => boxes.filter((box) => box.input.checked).map((box) => box.id),
    };
  }

  // A project can cover a cluster's namespaces, so a team with only Kubernetes still gets status, incidents, and reports.
  async function namespacesPicker(selected) {
    const data = await api('/api/k8s/namespaces').catch(() => ({ connected: false, namespaces: [] }));
    if (!data.connected) return { node: null, values: () => selected };
    const known = [...new Set([...data.namespaces, ...selected.filter((name) => name !== '*')])].sort();
    const all = h('input', { type: 'checkbox', checked: selected.includes('*') });
    const boxes = known.map((name) => ({ name, input: h('input', { type: 'checkbox', checked: selected.includes(name) }) }));
    const sync = () => boxes.forEach((box) => { box.input.disabled = all.checked; });
    all.addEventListener('change', sync);
    sync();
    return {
      node: h('fieldset', { class: 'stack source-picker' }, h('legend', {}, 'Kubernetes namespaces in this project'),
        h('label', { class: 'check' }, all, 'All namespaces'),
        boxes.map((box) => h('label', { class: 'check' }, box.input, box.name))),
      values: () => (all.checked ? ['*'] : boxes.filter((box) => box.input.checked).map((box) => box.name)),
    };
  }

  async function editProject(project) {
    const picker = await sourcesPicker(project?.source_ids || []);
    const nsPicker = await namespacesPicker(project?.namespaces || []);
    const name = h('input', { class: 'input', required: true, maxlength: 120, value: project?.name || '', placeholder: 'Payments platform' });
    const description = h('textarea', { class: 'input', rows: 2, maxlength: 1000, placeholder: 'What this project covers' });
    description.value = project?.description || '';
    const result = h('div');
    openDialog(h('form', { class: 'stack', onSubmit: async (event) => {
      event.preventDefault();
      const payload = { name: name.value.trim(), description: description.value.trim(), source_ids: picker.values(), namespaces: nsPicker.values() };
      if (!payload.source_ids.length && !payload.namespaces.length) {
        clear(result, h('p', { class: 'form-result bad', role: 'alert' }, picker.node || nsPicker.node
          ? 'Choose at least one metric source or namespace, so the project has something to watch.'
          : 'Connect a metric source or a Kubernetes cluster in Connections first.'));
        return;
      }
      try {
        const saved = project ? await api(`/api/projects/${project.id}`, { method: 'PUT', body: payload }) : await api('/api/projects', { method: 'POST', body: payload });
        closeDialog();
        toast('Project saved');
        if (project) background(showProject(saved.id));
        else location.hash = `#/projects/${saved.id}`;
      } catch (error) { clear(result, h('p', { class: 'form-result bad', role: 'alert' }, error.message)); }
    } }, dialogHead(project ? 'Edit project' : 'New project'),
    h('label', { class: 'field' }, h('span', {}, 'Name'), name), h('label', { class: 'field' }, h('span', {}, 'Description'), description),
    picker.node, nsPicker.node,
    !picker.node && !nsPicker.node ? h('p', { class: 'notice' }, 'Connect a metric source or a Kubernetes cluster first. You can add it to this project later.') : null,
    result, h('button', { class: 'button primary', type: 'submit' }, 'Save project')));
  }

  async function showList() {
    const projects = await api('/api/projects');
    if (disposed) return;
    clear(body,
      h('div', { class: 'section-head page-head' },
        h('h1', { class: 'page-title' }, 'Projects'),
        can('engineer') ? h('button', { class: 'button primary', type: 'button', onClick: () => editProject(null) }, 'New project') : null),
      h('p', { class: 'page-lede' }, 'Group the metric sources and Kubernetes namespaces behind a system, then see its status and recurring problems, and download a report.'),
      projects.length
        ? tableOf([['Project'], ['Status'], ['Covers'], ['Problems, 30 days', true], ['Recurring issues', true], ['Active now', true]],
          projects.map((project) => h('tr', {},
            h('td', { class: 'cell-title' }, h('a', { href: `#/projects/${project.id}` }, project.name), project.description ? h('div', { class: 'small muted', style: 'font-weight:400' }, project.description) : null),
            h('td', {}, badge(HEALTH[project.health])),
            h('td', { class: 'small' }, coverText(project)), h('td', { class: 'num' }, project.summary.occurrences),
            h('td', { class: 'num' }, project.summary.recurring), h('td', { class: 'num' }, project.summary.active))))
        : h('p', { class: 'empty' }, can('engineer') ? 'No projects yet. Create one to group your metric sources or namespaces.' : 'No projects yet.'));
  }

  async function showProject(projectId) {
    const view = await api(`/api/projects/${projectId}?days=${days}`);
    const reports = await api(`/api/projects/${projectId}/reports`);
    if (disposed) return;
    const { project, summary, period, reliability } = view;
    const previous = view.trend?.previous;
    const trendLine = previous === null || previous === undefined ? '' : `The previous ${period.days} days had ${previous} ${previous === 1 ? 'occurrence' : 'occurrences'}.`;

    async function setVerdict(issue, verdict, extra = {}) {
      try {
        await api('/api/issues/verdict', { method: 'PUT', body: { title: issue.title, verdict: verdict || null, ...extra } });
        toast(verdict ? 'Verdict saved' : 'Verdict cleared');
        background(showProject(projectId));
      } catch (error) { toast(error.message); }
    }

    // A false alarm or expected label mutes the alert, so the person chooses for how long, in hours or days.
    const LENGTHS = [[1, '1 hour'], [6, '6 hours'], [24, '1 day'], [72, '3 days'], [168, '7 days'], [720, '30 days'], [0, 'Until something changes'], ['custom', 'Custom']];
    function labelDialog(issue, verdict) {
      let saved = false;
      let chosen = 720;
      const amount = h('input', { class: 'input', type: 'number', min: 1, max: 365, value: 12, 'aria-label': 'Length' });
      const unit = h('select', { class: 'input', 'aria-label': 'Unit' }, h('option', { value: 'hours' }, 'hours'), h('option', { value: 'days', selected: true }, 'days'));
      const custom = h('div', { class: 'label-custom', hidden: true }, amount, unit);
      const note = h('textarea', { class: 'input', rows: 2, maxlength: 1000, placeholder: 'Why? For example: fires during the nightly backup.' });
      const error = h('p', { class: 'error-note', hidden: true });
      const choices = h('div', { class: 'label-lengths', role: 'radiogroup', 'aria-label': 'How long this label lasts' },
        LENGTHS.map(([value, text]) => h('label', { class: 'label-length' },
          h('input', { type: 'radio', name: 'label-length', value: String(value), checked: value === chosen, onChange: () => { chosen = value; custom.hidden = value !== 'custom'; } }),
          h('span', {}, text))));
      const hours = () => {
        if (chosen !== 'custom') return chosen;
        const number = Math.round(Number(amount.value));
        const total = unit.value === 'days' ? number * 24 : number;
        return number >= 1 && total <= 8760 ? total : null;
      };
      openDialog(h('form', { class: 'label-dialog', onSubmit: (event) => {
        event.preventDefault();
        const value = hours();
        if (value === null) { error.textContent = 'Choose between 1 hour and 365 days.'; error.hidden = false; return; }
        saved = true;
        closeDialog();
        background(setVerdict(issue, verdict, { expires_hours: value, note: note.value.trim() }));
      } },
        dialogHead(`Mark as ${VERDICTS[verdict].toLowerCase()}`),
        h('p', { class: 'body-text' }, h('b', {}, issue.title)),
        h('p', { class: 'small muted' }, 'Its alerts stay quiet while the label lasts. It also ends early after a change to the same service, if the problem gets worse, or if its rule changes, and the alert then comes back saying why.'),
        h('p', { class: 'field-label' }, 'Keep this label for'), choices, custom,
        h('p', { class: 'field-label' }, 'Note (optional)'), note, error,
        h('div', { class: 'form-actions' },
          h('button', { class: 'button primary', type: 'submit' }, 'Save'),
          h('button', { class: 'button', type: 'button', onClick: closeDialog }, 'Cancel'))),
      { onClose: () => { if (!saved) background(showProject(projectId)); } });
    }

    async function renewVerdict(issue) {
      try {
        await api('/api/issues/verdict/renew', { method: 'POST', body: { title: issue.title } });
        toast('Label renewed');
        background(showProject(projectId));
      } catch (error) { toast(error.message); }
    }

    // When a false alarm or expected label ends, or why it already ended.
    function labelState(issue) {
      if (issue.verdict_ended) return h('div', { class: 'verdict-ended', title: issue.verdict_ended }, 'Label ended: ', issue.verdict_ended.replace(/^.*that label ended: /, ''));
      if (!issue.verdict_expires_at || !['false_positive', 'expected'].includes(issue.verdict)) return null;
      const hoursLeft = Math.max(0, (new Date(issue.verdict_expires_at) - Date.now()) / 3600000);
      const left = hoursLeft < 1 ? 'Expires within the hour' : hoursLeft < 48 ? `Expires in ${Math.ceil(hoursLeft)} ${Math.ceil(hoursLeft) === 1 ? 'hour' : 'hours'}` : `Expires in ${Math.ceil(hoursLeft / 24)} days`;
      return h('div', { class: 'verdict-expiry' }, left,
        can('engineer') ? h('button', { class: 'linkish', type: 'button', onClick: () => renewVerdict(issue) }, 'Renew') : null);
    }

    function verdictCell(issue) {
      if (!can('engineer')) return h('div', {}, issue.verdict ? VERDICTS[issue.verdict] : h('span', { class: 'muted' }, 'Not reviewed'), labelState(issue));
      const select = h('select', { class: 'input verdict-select', 'aria-label': `Verdict for ${issue.title}`,
        onChange: () => (['false_positive', 'expected'].includes(select.value) ? labelDialog(issue, select.value) : setVerdict(issue, select.value)) },
        h('option', { value: '', selected: !issue.verdict }, 'Not reviewed'),
        Object.entries(VERDICTS).map(([value, label]) => h('option', { value, selected: issue.verdict === value }, label)));
      return h('div', {}, select, labelState(issue));
    }

    async function shareReport(report, shared) {
      try {
        await api(`/api/projects/${projectId}/reports/${report.id}/share`, { method: 'POST', body: { shared } });
        toast(shared ? 'Viewers can now download this report' : 'Report is for engineers only again');
        background(showProject(projectId));
      } catch (error) { toast(error.message); }
    }
    const recurring = view.issues.filter((issue) => issue.recurring);
    const single = view.issues.filter((issue) => !issue.recurring);
    const includeAi = h('input', { type: 'checkbox', checked: Boolean(live.ai) });
    const reportStatus = h('div');
    const covered = period.covered_days < period.days * 0.9;

    // With AI notes, the report is saved at once and its PDF becomes available only when the notes are in.
    async function generate() {
      clear(reportStatus, h('p', { class: 'form-result muted', role: 'status' }, 'Saving the report'));
      try {
        const report = await api(`/api/projects/${projectId}/reports`, { method: 'POST', body: { days, ai_notes: includeAi.checked } });
        if (report.notes_status === 'failed') toast(`Report saved without AI notes. ${report.notes_error}`);
        background(showProject(projectId));
      } catch (error) { clear(reportStatus, h('p', { class: 'form-result bad', role: 'alert' }, error.message)); }
    }

    async function addNotes(report) {
      try {
        await api(`/api/projects/${projectId}/reports/${report.id}/notes`, { method: 'POST', body: {} });
        background(showProject(projectId));
      } catch (error) { toast(error.message); }
    }

    clearTimeout(notesTimer);
    if (reports.some((report) => report.notes_status === 'pending')) {
      notesTimer = setTimeout(() => { if (!disposed) background(showProject(projectId)); }, 4000);
    }
    const notesCell = (report) => {
      if (report.notes_status === 'pending') return h('span', { class: 'badge neutral' }, 'Writing AI notes');
      if (report.notes_status === 'failed') return h('span', { class: 'small', title: report.notes_error }, h('span', { class: 'badge warning' }, 'Notes failed'), ' ', h('span', { class: 'muted' }, report.notes_error));
      return report.ai_model ? `${report.ai_model}${report.ai_cost_usd ? `, ${usd(report.ai_cost_usd)}` : ''}` : h('span', { class: 'muted' }, 'None');
    };

    async function removeReport(report) {
      await api(`/api/projects/${projectId}/reports/${report.id}`, { method: 'DELETE' });
      toast('Report deleted');
      background(showProject(projectId));
    }

    async function removeProject() {
      const ok = await confirmDialog({ title: `Delete ${project.name}?`, body: 'This deletes the project and its saved reports. Metric sources and recorded events stay.', action: 'Delete project' });
      if (!ok) return;
      await api(`/api/projects/${projectId}`, { method: 'DELETE' });
      location.hash = '#/projects';
    }

    clear(body,
      h('p', { class: 'small', style: 'margin:24px 0 6px' }, h('a', { href: '#/projects' }, 'All projects')),
      h('div', { class: 'section-head page-head', style: 'padding-top:12px' },
        h('h1', { class: 'page-title' }, project.name),
        can('engineer') ? h('span', { class: 'row-buttons' },
          h('button', { class: 'button compact', type: 'button', onClick: () => editProject(project) }, 'Edit'),
          can('admin') ? h('button', { class: 'button compact', type: 'button', onClick: removeProject }, 'Delete') : null) : null),
      project.description ? h('p', { class: 'page-lede' }, project.description) : null,
      h('p', { style: 'margin-top:8px' }, badge(HEALTH[view.health])),

      h('div', { class: 'filter-row', style: 'margin-top:18px' }, h('span', { class: 'muted small' }, 'Period'),
        h('div', { class: 'segments', role: 'group', 'aria-label': 'Period' }, [7, 30, 90].map((value) => h('button', { type: 'button', 'aria-pressed': String(days === value), onClick: () => { days = value; background(showProject(projectId)); } }, `${value} days`)))),
      h('div', { class: 'stat-row' }, stat(reliability.incidents, 'Incidents'), stat(summary.occurrences, 'Problem occurrences'), stat(summary.recurring, 'Recurring issues'),
        stat(summary.active, 'Active now'),
        stat(reliability.mttr_minutes === null ? 'None' : duration(reliability.mttr_minutes), 'Average time to clear'),
        stat(reliability.mtbf_minutes === null ? 'None' : duration(reliability.mtbf_minutes), 'Average time between incidents'),
        stat(reliability.clear_percent === null ? 'None' : `${reliability.clear_percent}%`, 'Time with no incident')),
      trendLine ? h('p', { class: 'small muted' }, trendLine) : null,
      summary.dismissed ? h('p', { class: 'small muted' }, `Your team marked ${summary.dismissed} of ${summary.occurrences} occurrences as false alarms or expected (${summary.noise_percent}%).`) : null,
      covered ? h('p', { class: 'small muted' }, period.data_since ? `Recording began ${timeAgo(period.data_since)}, so this covers ${period.covered_days < 1 ? 'less than 1' : `about ${period.covered_days}`} of the ${period.days} days. Recurring issues and patterns need time to appear.` : 'Nothing recorded yet. Recurring issues and patterns need time to appear.') : null,

      h('section', { class: 'section' }, h('h2', { style: 'margin-bottom:10px' }, 'Reports'),
        h('p', { class: 'body-text', style: 'margin-bottom:10px' }, can('engineer') ? `Saves the numbers for the last ${days} days as they are now, in a PDF. Share a report to let viewers download it.` : 'Reports your team has shared with you.'),
        can('engineer') ? h('div', { class: 'form-actions', style: 'margin-top:0' },
          h('button', { class: 'button primary', type: 'button', onClick: generate }, 'Generate report'),
          h('label', { class: 'check' }, includeAi, 'Include AI notes'),
          live.ai ? h('span', { class: 'small muted' }, `Uses ${live.ai[live.ai.provider]?.model || 'no model yet'}.`) : null) : null,
        reportStatus,
        reports.length ? h('div', { style: 'margin-top:12px' }, tableOf([['Generated'], ['Period', true], ['Problems', true], can('engineer') ? ['AI notes'] : null, can('engineer') ? ['Viewers'] : null, ['']].filter(Boolean),
          reports.map((report) => h('tr', {},
            h('td', { style: 'white-space:nowrap' }, dateTime(report.created_at), report.created_by ? h('div', { class: 'small muted' }, report.created_by) : null),
            h('td', { class: 'num' }, `${report.days} days`), h('td', { class: 'num' }, report.summary.occurrences),
            can('engineer') ? h('td', {}, notesCell(report)) : null,
            can('engineer') ? h('td', {}, report.shared ? 'Shared' : h('span', { class: 'muted' }, 'Not shared')) : null,
            h('td', {}, h('span', { class: 'row-buttons' },
              report.notes_status === 'pending'
                ? h('span', { class: 'small muted', role: 'status' }, 'The PDF is ready when the notes are')
                : h('a', { class: 'button compact primary', href: `/api/projects/${projectId}/reports/${report.id}/pdf`, download: '' }, 'Download PDF'),
              can('engineer') ? h('button', { class: 'button compact', type: 'button', onClick: () => shareReport(report, !report.shared) }, report.shared ? 'Stop sharing' : 'Share') : null,
              can('engineer') && !report.ai_model && report.notes_status !== 'pending' ? h('button', { class: 'button compact', type: 'button', onClick: () => addNotes(report) }, report.notes_status === 'failed' ? 'Try AI notes again' : 'Add AI notes') : null,
              can('engineer') ? h('button', { class: 'button compact', type: 'button', onClick: () => removeReport(report) }, 'Delete') : null))))))
          : can('engineer') ? null : h('p', { class: 'empty' }, 'No reports have been shared yet.')),

      view.kubernetes ? h('section', { class: 'section' }, h('h2', { style: 'margin-bottom:10px' }, 'Kubernetes'),
        h('p', { class: 'small muted', style: 'margin-bottom:8px' }, view.kubernetes.namespaces?.includes('*') || !view.kubernetes.namespaces ? 'All namespaces.' : `Namespaces: ${view.kubernetes.namespaces.join(', ')}.`),
        view.kubernetes.status === 'not_connected' ? h('p', { class: 'empty' }, 'No cluster is connected.')
          : view.kubernetes.problems.length ? h('ul', { class: 'rows' }, view.kubernetes.problems.map((item) => h('li', { class: 'row' }, icon(item.severity === 'critical' ? 'critical' : 'warning'),
            h('span', { class: 'row-title' }, item.title), h('span', { class: 'row-side' }), item.cause ? h('span', { class: 'row-detail' }, item.cause) : null)))
            : h('p', { class: 'empty' }, 'No workload problems in these namespaces.')) : null,

      view.sources.length || !view.kubernetes ? h('section', { class: 'section' }, h('h2', { style: 'margin-bottom:10px' }, 'Source status'),
        view.sources.length ? tableOf([['Source'], can('engineer') ? ['Type'] : null, ['Status'], ['Active alerts', true], ['Targets down', true], ['Signals out of range', true]].filter(Boolean),
          view.sources.map((source) => h('tr', {},
            h('td', { class: 'cell-title' }, source.name, source.error ? h('div', { class: 'small', style: 'font-weight:400;overflow-wrap:anywhere' }, source.error) : null),
            can('engineer') ? h('td', {}, source.kind_label) : null, h('td', {}, badge(SOURCE_STATUS[source.status] || SOURCE_STATUS.waiting)),
            h('td', { class: 'num' }, source.alerts), h('td', { class: 'num' }, source.targets_down), h('td', { class: 'num' }, source.signals_out_of_range))))
          : h('p', { class: 'empty' }, can('engineer') ? 'No sources in this project. Select Edit to add some.' : 'No sources in this project.')) : null,

      summary.occurrences ? h('section', { class: 'section' }, h('h2', { style: 'margin-bottom:10px' }, 'Problems per day'), dailyChart(view.daily)) : null,

      view.incidents.length ? h('section', { class: 'section' }, h('h2', { style: 'margin-bottom:6px' }, 'Incidents'),
        h('p', { class: 'small muted', style: 'margin-bottom:8px' }, 'Problems that started close together count as one incident.'),
        tableOf([['Started'], ['Lasted', true], ['Severity'], ['Problems', true], ['What happened']],
          view.incidents.slice(0, 15).map((item) => h('tr', {},
            h('td', { class: 'nowrap' }, dateTime(item.start)),
            h('td', { class: 'num' }, item.active ? 'Still active' : duration(item.minutes)),
            h('td', {}, item.severity === 'critical' ? 'Critical' : 'Warning'),
            h('td', { class: 'num' }, item.problems),
            h('td', { class: 'wrap-anywhere' }, item.issues.join(', '), item.after_change ? h('div', { class: 'small muted' }, `Started after: ${item.after_change}`) : null))))) : null,

      h('section', { class: 'section' }, h('h2', { style: 'margin-bottom:10px' }, 'Recurring issues'),
        recurring.length ? tableOf([['Issue'], ['Severity'], ['Times', true], ['Total time', true], ['Average', true], ['Last started'], ['Verdict']],
          recurring.map((issue) => h('tr', {},
            h('td', { style: 'min-width:240px;overflow-wrap:anywhere' }, issue.title, issue.active ? h('span', { class: 'badge critical', style: 'margin-left:6px' }, 'Active') : null,
              (issue.affected || []).length ? h('div', { class: 'small muted' }, issue.affected.join(', ')) : null,
              issue.noise_hint ? h('div', { class: 'small noise-hint' }, `Possible false alarm. ${issue.noise_hint}`) : null),
            h('td', {}, issue.severity === 'critical' ? 'Critical' : 'Warning'), h('td', { class: 'num' }, issue.occurrences),
            h('td', { class: 'num' }, duration(issue.total_minutes)), h('td', { class: 'num' }, duration(issue.average_minutes)),
            h('td', { style: 'white-space:nowrap' }, timeAgo(issue.last_seen)),
            h('td', {}, verdictCell(issue)))))
          : h('p', { class: 'empty' }, 'No issue has happened more than once in this period.'),
        single.length ? h('details', { class: 'after-form' }, h('summary', {}, `${single.length} ${single.length === 1 ? 'issue' : 'issues'} that happened once`),
          h('ul', { class: 'plain-list' }, single.map((issue) => h('li', { style: 'overflow-wrap:anywhere' }, issue.title, h('span', { class: 'small muted' }, ` ${timeAgo(issue.last_seen)}`))))) : null),

      h('section', { class: 'section' }, h('h2', { style: 'margin-bottom:6px' }, 'Patterns'),
        h('p', { class: 'small muted', style: 'margin-bottom:8px' }, 'Calculated from event timing. These point to what to check and don’t prove a cause.'),
        view.patterns.length ? h('ul', { class: 'plain-list' }, view.patterns.map((pattern) => h('li', { style: 'overflow-wrap:anywhere' }, h('b', {}, `${pattern.title}. `), pattern.detail)))
          : h('p', { class: 'empty' }, 'No repeating pattern has enough evidence yet.')),

      view.root_causes.length ? h('section', { class: 'section' }, h('h2', { style: 'margin-bottom:6px' }, 'Root causes your team confirmed'),
        h('ul', { class: 'plain-list' }, view.root_causes.map((item) => h('li', {}, h('b', {}, `${item.title}. `), item.note)))) : null,
      view.workflows.length ? h('section', { class: 'section' }, h('h2', { style: 'margin-bottom:6px' }, 'Related workflows'),
        h('ul', { class: 'plain-list' }, view.workflows.map((item) => h('li', {}, h('a', { href: `#/workflows/${item.id}` }, item.name), h('span', { class: 'small muted' }, ` ${item.status === 'approved' ? 'Reviewed' : 'Draft'}, ${item.risk} risk`))))) : null);
  }

  (params.id ? showProject(Number(params.id)) : showList()).catch((error) => clear(body, h('p', { class: 'error-note' }, error.message)));
  return () => { disposed = true; clearTimeout(notesTimer); };
}
