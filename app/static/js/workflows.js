// Remediation workflows: reviewed steps for people, shown as a pipeline and followed one at a time. Nothing on this page runs anything.
import { api, can, clear, closeDialog, dateTime, dialogHead, h, openDialog, statusBadge, timeAgo, toast } from './core.js';

const STATE_TEXT = { done: 'Done', skipped: 'Skipped', pending: 'Waiting', current: 'Next', preview: '' };
const OUTCOME_TEXT = { helped: 'It helped', partly: 'It partly helped', did_not_help: 'It didn’t help', abandoned: 'Abandoned' };
const CHECK = 'M3.5 8.4 6.6 11.5 12.5 5';
const SKIP = 'M4 4l4 4-4 4M9 4l4 4-4 4';

function stateIcon(state, number) {
  if (state !== 'done' && state !== 'skipped') return h('span', { class: 'pipe-number' }, number);
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 16 16');
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', state === 'done' ? CHECK : SKIP);
  path.setAttribute('fill', 'none');
  path.setAttribute('stroke', 'currentColor');
  path.setAttribute('stroke-width', '2');
  path.setAttribute('stroke-linecap', 'round');
  path.setAttribute('stroke-linejoin', 'round');
  svg.append(path);
  return svg;
}

export function mount(container, params) {
  let items = [];
  let matchedIds = null;
  let selectedId = params.id ? Number(params.id) : null;
  let runs = [];
  let focusIndex = null;
  let disposed = false;
  const listHost = h('nav', { class: 'workflow-list', 'aria-label': 'Workflows' });
  const detailHost = h('div', { class: 'workflow-detail' });

  clear(container, h('div', { class: 'page' },
    h('div', { class: 'section-head page-head' },
      h('h1', { class: 'page-title' }, 'Workflows'),
      h('button', { class: 'button primary', type: 'button', onClick: () => edit(null) }, 'New workflow')),
    h('p', { class: 'page-lede' }, 'Reviewed remediation steps, followed one at a time and recorded as you go.'),
    h('p', { class: 'read-only-banner' }, h('b', {}, 'This console can’t run these steps. '), 'A person reads each one, decides, and carries it out in your own tools.'),
    h('div', { class: 'workflow-layout' }, listHost, detailHost)));

  const activeRun = () => runs.find((run) => run.status === 'running') || null;

  async function load() {
    items = await api('/api/workflows');
    if (params.alert || params.signal) {
      const query = new URLSearchParams({ alert: params.alert || '', signal: params.signal || '' });
      const matched = await api(`/api/workflows/match?${query}`);
      matchedIds = new Set(matched.map((item) => item.id));
      if (!selectedId && matched.length) selectedId = matched[0].id;
    }
    if (!selectedId && items.length) selectedId = items[0].id;
    await loadRuns();
    if (!disposed) render();
  }

  async function loadRuns() {
    runs = selectedId && items.some((item) => item.id === selectedId) ? await api(`/api/workflows/${selectedId}/runs`) : [];
  }

  function renderList() {
    const shown = matchedIds ? items.filter((item) => matchedIds.has(item.id)) : items;
    clear(listHost,
      matchedIds ? h('p', { class: 'small muted list-note' }, shown.length ? `${shown.length} match this alert. ` : 'None match this alert. ',
        h('button', { class: 'link-button', type: 'button', onClick: () => { matchedIds = null; render(); } }, 'Show all')) : null,
      shown.map((item) => h('a', {
        href: `#/workflows/${item.id}`, 'aria-current': item.id === selectedId ? 'true' : null,
        onClick: async (event) => {
          event.preventDefault();
          selectedId = item.id;
          focusIndex = null;
          history.replaceState(null, '', `#/workflows/${item.id}`);
          await loadRuns();
          render();
        },
      }, item.name, h('small', {}, `${item.status === 'approved' ? 'Reviewed' : 'Draft'}, ${item.risk} risk, ${item.steps.length} steps`))));
  }

  function resultsText(results) {
    const total = Object.values(results || {}).reduce((sum, value) => sum + value, 0);
    if (!total) return 'Not used yet.';
    return `Followed ${total} ${total === 1 ? 'time' : 'times'}: ${results.helped || 0} helped, ${results.partly || 0} partly, ${results.did_not_help || 0} didn’t help.`;
  }

  // The pipeline: step 1 -> step 2 -> step 3, each showing its state.
  function pipeline(steps, run, currentIndex) {
    return h('ol', { class: 'pipeline', 'aria-label': 'Steps in order' }, steps.map((step, index) => {
      const state = !run ? 'preview' : step.state !== 'pending' ? step.state : index === currentIndex ? 'current' : 'pending';
      return h('li', { class: `pipe-step is-${state}${index === focusIndex ? ' is-selected' : ''}` },
        h('button', { type: 'button', 'aria-pressed': String(index === focusIndex), onClick: () => { focusIndex = index; render(); } },
          h('span', { class: 'pipe-marker' }, stateIcon(state, index + 1)),
          h('span', { class: 'pipe-text' },
            h('b', {}, step.title),
            h('small', {}, run ? STATE_TEXT[state] : step.risk && step.risk !== 'none' ? `${step.risk} risk` : 'Check only'))));
    }));
  }

  function stepPanel(step, index, run, currentIndex) {
    const note = h('input', { class: 'input', maxlength: 2000, placeholder: 'Note for your team (needed if you skip)', 'aria-label': 'Note' });
    const risky = step.risk === 'medium' || step.risk === 'high';
    const accept = h('input', { type: 'checkbox' });
    const lastFinished = run ? currentIndex - 1 : -1;
    const isCurrent = run && index === currentIndex;
    const act = async (action) => {
      try {
        await api(`/api/workflow-runs/${run.id}/steps/${index}`, { method: 'POST', body: { action, note: note.value, risk_accepted: accept.checked } });
        focusIndex = null;
        await loadRuns();
        render();
      } catch (error) { toast(error.message); }
    };
    return h('section', { class: 'step-panel', 'aria-label': `Step ${index + 1}` },
      h('h3', {}, `Step ${index + 1}. ${step.title}`),
      step.detail ? h('p', {}, step.detail) : null,
      step.command ? h('div', { class: 'command-row' },
        h('code', { class: 'command' }, step.command),
        h('button', { class: 'button compact', type: 'button', onClick: () => copy(step.command) }, 'Copy')) : null,
      risky ? h('p', { class: 'notice' }, h('b', {}, `This step is ${step.risk} risk. `), step.undo ? `To undo it: ${step.undo}` : 'It has no undo instructions, so take care.') : null,
      !risky && step.undo ? h('p', {}, h('b', {}, 'To undo: '), step.undo) : null,
      step.verify ? h('p', {}, h('b', {}, 'It worked if: '), step.verify) : null,
      run && step.state !== 'pending' ? h('p', { class: 'small muted' }, `${STATE_TEXT[step.state]} by ${step.by}, ${timeAgo(step.at)}.${step.note ? ` Note: ${step.note}` : ''}`) : null,
      isCurrent ? h('div', { class: 'step-actions' },
        note,
        risky ? h('label', { class: 'check' }, accept, 'I’ve read the risk and how to undo this') : null,
        h('div', { class: 'row-buttons' },
          h('button', { class: 'button primary compact', type: 'button', onClick: () => act('done') }, 'I did this'),
          h('button', { class: 'button compact', type: 'button', onClick: () => act('skipped') }, 'Skip'))) : null,
      run && index === lastFinished && step.state !== 'pending' && run.status === 'running'
        ? h('button', { class: 'link-button small', type: 'button', onClick: () => act('reopen') }, 'Reopen this step') : null,
      run && !isCurrent && step.state === 'pending' ? h('p', { class: 'small muted' }, `Finish step ${currentIndex + 1} first.`) : null);
  }

  function finishPanel(run, complete) {
    const outcome = h('select', { class: 'input', 'aria-label': 'Result' }, Object.entries(OUTCOME_TEXT).filter(([value]) => value !== 'abandoned').map(([value, label]) => h('option', { value }, label)));
    const note = h('input', { class: 'input', maxlength: 2000, placeholder: 'What happened?', 'aria-label': 'Notes' });
    const finish = async (value) => {
      try {
        await api(`/api/workflow-runs/${run.id}/finish`, { method: 'POST', body: { outcome: value, note: note.value } });
        toast(value === 'abandoned' ? 'Run ended' : 'Recorded in the audit trail');
        items = await api('/api/workflows');
        await loadRuns();
        render();
      } catch (error) { toast(error.message); }
    };
    return complete
      ? h('section', { class: 'finish-panel' }, h('h3', {}, 'All steps are finished. Did it work?'),
        h('div', { class: 'finish-row' }, outcome, note, h('button', { class: 'button primary compact', type: 'button', onClick: () => finish(outcome.value) }, 'Finish')))
      : h('p', { class: 'small' }, h('button', { class: 'link-button', type: 'button', onClick: () => finish('abandoned') }, 'End this run without finishing'));
  }

  function history_(pastRuns) {
    if (!pastRuns.length) return null;
    return h('details', { class: 'after-form' }, h('summary', {}, `Earlier runs (${pastRuns.length})`),
      h('div', { class: 'table-wrap' }, h('table', { class: 'data-table' },
        h('thead', {}, h('tr', {}, ['Started', 'By', 'Result', 'Notes'].map((label) => h('th', { scope: 'col' }, label)))),
        h('tbody', {}, pastRuns.map((run) => h('tr', {},
          h('td', { class: 'nowrap' }, dateTime(run.started_at)), h('td', {}, run.started_by),
          h('td', {}, OUTCOME_TEXT[run.outcome] || run.outcome), h('td', { class: 'wrap-anywhere' }, run.note)))))));
  }

  function render() {
    renderList();
    const item = items.find((entry) => entry.id === selectedId);
    if (!item) {
      clear(detailHost, h('p', { class: 'empty' }, 'Choose a workflow.'));
      return;
    }
    const run = activeRun();
    const steps = run ? run.steps : item.steps;
    const currentIndex = run ? steps.findIndex((step) => step.state === 'pending') : -1;
    const complete = run && currentIndex === -1;
    const finished = run ? steps.filter((step) => step.state !== 'pending').length : 0;
    if (focusIndex === null || focusIndex >= steps.length) focusIndex = run ? (complete ? steps.length - 1 : currentIndex) : 0;

    clear(detailHost,
      h('div', { class: 'detail-head' },
        h('h2', {}, item.name),
        h('span', { class: 'row-buttons' },
          h('button', { class: 'button compact', type: 'button', onClick: () => edit(item) }, 'Edit'),
          can('admin') && item.status === 'draft' ? h('button', { class: 'button compact primary', type: 'button', onClick: () => setStatus(item, 'approved') }, 'Mark as reviewed') : null,
          can('admin') ? h('button', { class: 'button compact', type: 'button', onClick: () => setStatus(item, 'archived') }, 'Archive') : null)),
      h('p', { class: 'small detail-meta' }, statusBadge(item.status), h('span', {}, `${item.risk} risk`),
        h('span', { class: 'muted' }, item.origin === 'ai' ? 'Drafted by AI' : item.origin === 'built-in' ? 'Built in' : `Written by ${item.created_by}`),
        item.reviewed_by ? h('span', { class: 'muted' }, `Reviewed by ${item.reviewed_by}`) : null),
      h('p', { class: 'body-text' }, item.summary),
      h('p', { class: 'small muted' }, resultsText(item.results)),
      item.status === 'draft' ? h('p', { class: 'notice' }, can('admin') ? 'This draft hasn’t been reviewed. Check every step, then mark it as reviewed so people can follow it.' : 'This draft hasn’t been reviewed, so it can’t be followed yet. Ask an admin to review it.') : null,

      run ? h('div', { class: 'run-bar' },
        h('b', {}, complete ? 'All steps finished' : `Step ${currentIndex + 1} of ${steps.length}`),
        h('span', { class: 'muted small' }, `Started by ${run.started_by}, ${timeAgo(run.started_at)}. Anyone on the team can continue it.`),
        h('span', { class: 'run-meter', role: 'img', 'aria-label': `${finished} of ${steps.length} steps finished` }, h('i', { style: `width:${Math.round(100 * finished / steps.length)}%` })))
        : item.status === 'approved' ? h('div', { class: 'run-bar' },
          h('button', { class: 'button primary', type: 'button', onClick: () => start(item) }, 'Start following these steps'),
          h('span', { class: 'muted small' }, 'This records your progress. It doesn’t run anything.')) : null,

      pipeline(steps, run, currentIndex),
      stepPanel(steps[focusIndex], focusIndex, run, currentIndex),
      run ? finishPanel(run, complete) : null,
      history_(runs.filter((entry) => entry.status !== 'running')));
  }

  async function start(item) {
    try {
      await api(`/api/workflows/${item.id}/runs`, { method: 'POST', body: {} });
      focusIndex = null;
      await loadRuns();
      render();
    } catch (error) { toast(error.message); }
  }

  async function copy(text) {
    try {
      await navigator.clipboard.writeText(text);
      toast('Copied');
    } catch (error) { toast('Select the text to copy it'); }
  }

  async function setStatus(item, status) {
    try {
      await api(`/api/workflows/${item.id}/status`, { method: 'POST', body: { status } });
      if (status === 'archived') selectedId = null;
      toast(status === 'approved' ? 'Marked as reviewed' : 'Archived');
      load();
    } catch (error) { toast(error.message); }
  }

  function edit(item) {
    const name = h('input', { class: 'input', required: true, maxlength: 120, value: item?.name || '' });
    const summary = h('input', { class: 'input', maxlength: 500, value: item?.summary || '' });
    const risk = h('select', { class: 'input' }, ['low', 'medium', 'high'].map((value) => h('option', { value, selected: (item?.risk || 'medium') === value }, value)));
    const alerts = h('input', { class: 'input', value: (item?.match_rules?.alerts || []).join(', '), placeholder: 'highcpu, targetdown' });
    const keywords = h('input', { class: 'input', value: (item?.match_rules?.keywords || []).join(', '), placeholder: 'disk space' });
    const stepsHost = h('div', { class: 'stack' });
    const result = h('div');
    const steps = (item?.steps?.length ? item.steps : [{ title: '' }]).map((step) => ({ ...step }));
    function renderSteps() {
      clear(stepsHost, steps.map((step, index) => {
        const bind = (key, control) => { control.addEventListener('input', () => { step[key] = control.value; }); return control; };
        return h('fieldset', { class: 'stack step-editor' }, h('legend', { class: 'small' }, `Step ${index + 1}`),
          bind('title', h('input', { class: 'input', required: true, placeholder: 'What to do', 'aria-label': 'Step title', value: step.title || '' })),
          bind('detail', h('textarea', { class: 'input', rows: 2, placeholder: 'Details', 'aria-label': 'Details', value: step.detail || '' })),
          bind('command', h('input', { class: 'input mono', placeholder: 'Command for a person to run (optional)', 'aria-label': 'Command', value: step.command || '' })),
          h('label', { class: 'field' }, h('span', {}, 'What this step can change'),
            (() => {
              const select = h('select', { class: 'input' }, [['none', 'Nothing, it only checks'], ['low', 'Low risk'], ['medium', 'Medium risk'], ['high', 'High risk']].map(([value, label]) => h('option', { value, selected: (step.risk || 'none') === value }, label)));
              select.addEventListener('change', () => { step.risk = select.value; });
              return select;
            })()),
          bind('undo', h('input', { class: 'input', placeholder: 'How to undo it', 'aria-label': 'How to undo it', value: step.undo || '' })),
          bind('verify', h('input', { class: 'input', placeholder: 'How to tell it worked', 'aria-label': 'How to tell it worked', value: step.verify || '' })),
          steps.length > 1 ? h('button', { class: 'link-button small', type: 'button', onClick: () => { steps.splice(index, 1); renderSteps(); } }, 'Remove step') : null);
      }));
    }
    renderSteps();
    const list = (text) => text.split(',').map((part) => part.trim().toLowerCase()).filter(Boolean);
    async function save(event) {
      event.preventDefault();
      const body = { name: name.value.trim(), summary: summary.value.trim(), risk: risk.value, match_rules: { alerts: list(alerts.value), keywords: list(keywords.value), signals: item?.match_rules?.signals || [] },
        steps: steps.map((step) => ({ title: step.title || '', detail: step.detail || '', command: step.command || '', risk: step.risk || 'none', undo: step.undo || '', verify: step.verify || '' })) };
      try {
        const saved = item ? await api(`/api/workflows/${item.id}`, { method: 'PUT', body }) : await api('/api/workflows', { method: 'POST', body });
        selectedId = saved.id;
        focusIndex = null;
        closeDialog();
        toast('Workflow saved as a draft for review');
        load();
      } catch (error) { clear(result, h('p', { class: 'form-result bad' }, error.message)); }
    }
    openDialog(h('form', { class: 'stack', onSubmit: save }, dialogHead(item ? 'Edit workflow' : 'New workflow'),
      item ? h('p', { class: 'notice' }, 'Saving returns this workflow to draft, so someone reviews the changes. A run already in progress keeps its steps.') : null,
      h('label', { class: 'field' }, h('span', {}, 'Name'), name), h('label', { class: 'field' }, h('span', {}, 'Summary'), summary), h('label', { class: 'field' }, h('span', {}, 'Risk'), risk),
      h('label', { class: 'field' }, h('span', {}, 'Match alert names containing'), alerts), h('label', { class: 'field' }, h('span', {}, 'Match text containing'), keywords),
      stepsHost, h('button', { class: 'button compact', type: 'button', onClick: () => { steps.push({ title: '' }); renderSteps(); } }, 'Add step'),
      result, h('button', { class: 'button primary', type: 'submit' }, 'Save draft')), { wide: true });
  }

  load().catch((error) => clear(detailHost, h('p', { class: 'error-note' }, error.message)));
  return () => { disposed = true; };
}
