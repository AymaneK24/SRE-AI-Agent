// Investigate: send prompts with the live context, watch the model work, and record the outcome.
import { TimeSeriesChart } from './charts.js';
import { api, can, clear, confirmDialog, dialogHead, formatValue, h, labelText, live, openDialog, refreshAi, renderMarkdown, timeAgo, toast, usd } from './core.js';

let remembered = null;

function queryFromArgs(args) {
  if (!args) return '';
  if (args.pod || args.name) return `${args.namespace ? `${args.namespace}/` : ''}${args.pod || args.name}${args.previous ? ', crashed container' : ''}`;
  return args.query || args.contains || args.text || args.resource || '';
}

function toolLabel(name) {
  return {
    run_instant_query: 'Current value',
    run_range_query: 'Over time',
    list_metrics: 'Searched metric names',
    query_logs: 'Searched logs',
    get_kubernetes_state: 'Read Kubernetes state',
    get_pod_logs: 'Read pod logs',
    describe_object: 'Described',
    get_recent_changes: 'Read recent changes',
    search_past_investigations: 'Searched past investigations',
  }[name] || name;
}

function prettyResult(text) {
  try { return JSON.stringify(JSON.parse(text), null, 1); } catch (error) { return text; }
}

export function mount(container, params) {
  let conversationId = params.id ? Number(params.id) : null;
  let conversation = null;
  let activeRunId = null;
  let streamAbort = null;
  let tickTimer = null;
  let modelOptions = { ollama: [], claude: [], gemini: [] };
  let disposed = false;

  const titleHost = h('div');
  const messagesHost = h('div', { class: 'messages' });
  const textarea = h('textarea', { placeholder: 'Describe the problem, or ask what changed', 'aria-label': 'Your prompt', rows: 2, maxlength: 20000 });
  const modelSelect = h('select', { 'aria-label': 'AI model', onChange: onModelChange });
  const toolsCheck = h('input', { type: 'checkbox' });
  const sendButton = h('button', { class: 'button primary', type: 'submit' }, 'Investigate');
  const stopButton = h('button', { class: 'button', type: 'button', hidden: true, onClick: stopRun }, 'Stop');
  const composerNote = h('p', { class: 'privacy-note', hidden: true });
  const composerError = h('div');
  const rail = h('aside', { class: 'rail', 'aria-label': 'Live context' });

  const composer = h('form', { class: 'composer', onSubmit: submit },
    textarea,
    h('div', { class: 'composer-row' },
      modelSelect,
      h('label', { class: 'check' }, toolsCheck, 'Let the model run queries'),
      h('div', { class: 'composer-send' }, stopButton, sendButton)),
    composerNote, composerError);
  textarea.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') composer.requestSubmit();
  });

  // Stays hidden until the investigation loads, so it never appears and then disappears.
  composer.hidden = Boolean(conversationId);

  clear(container, h('div', { class: 'page' }, h('div', { class: 'investigate' },
    h('div', {}, titleHost, messagesHost, composer), rail)));

  // Model selection ---------------------------------------------------------
  function selectedParts() {
    const [provider, ...rest] = modelSelect.value.split('::');
    return { provider, model: rest.join('::') };
  }

  function onModelChange() {
    const { provider } = selectedParts();
    const section = live.ai?.[provider];
    if (section) toolsCheck.checked = Boolean(section.use_tools);
    remembered = modelSelect.value;
    renderComposerNote();
  }

  function renderComposerNote() {
    const { provider } = selectedParts();
    const company = { claude: 'Anthropic', gemini: 'Google' }[provider];
    composerNote.hidden = !company;
    composerNote.textContent = company ? `Your prompt and the live context, including host and service names, go to ${company}.` : '';
  }

  async function loadModels() {
    const ai = live.ai || await refreshAi();
    const [ollama, claude, gemini] = await Promise.all([
      api('/api/ai/providers/ollama/models').catch(() => ({ models: [] })),
      api('/api/ai/providers/claude/models').catch(() => ({ models: [] })),
      api('/api/ai/providers/gemini/models').catch(() => ({ models: [] })),
    ]);
    if (disposed) return;
    modelOptions = { ollama: ollama.models || [], claude: claude.models || [], gemini: gemini.models || [] };
    const claudeReady = ai.claude.key_configured && ai.claude.consent;
    const geminiReady = Boolean(ai.gemini?.key_configured && ai.gemini?.consent);
    const options = [];
    const ollamaIds = modelOptions.ollama.map((item) => item.id);
    if (ai.ollama.model && !ollamaIds.includes(ai.ollama.model)) modelOptions.ollama.unshift({ id: ai.ollama.model, label: ai.ollama.model });
    options.push(h('optgroup', { label: 'Ollama, on your machine' },
      modelOptions.ollama.length
        ? modelOptions.ollama.map((item) => h('option', { value: `ollama::${item.id}` }, item.label))
        : h('option', { value: 'ollama::', disabled: true }, 'No models found')));
    const claudeIds = modelOptions.claude.map((item) => item.id);
    if (ai.claude.model && !claudeIds.includes(ai.claude.model)) modelOptions.claude.unshift({ id: ai.claude.model, label: ai.claude.model });
    options.push(h('optgroup', { label: claudeReady ? 'Claude, over the internet' : can('admin') ? 'Claude: set up in Connections' : 'Claude: not set up' },
      modelOptions.claude.map((item) => h('option', { value: `claude::${item.id}`, disabled: !claudeReady }, item.label))));
    if (ai.gemini?.model && !modelOptions.gemini.some((item) => item.id === ai.gemini.model)) modelOptions.gemini.unshift({ id: ai.gemini.model, label: ai.gemini.model });
    if (modelOptions.gemini.length || geminiReady) {
      options.push(h('optgroup', { label: geminiReady ? 'Gemini, over the internet' : can('admin') ? 'Gemini: set up in Connections' : 'Gemini: not set up' },
        modelOptions.gemini.map((item) => h('option', { value: `gemini::${item.id}`, disabled: !geminiReady }, item.label))));
    }
    clear(modelSelect, options);
    const preferred = remembered || `${ai.provider}::${ai[ai.provider].model}`;
    modelSelect.value = preferred;
    if (modelSelect.value !== preferred || !modelSelect.value || modelSelect.selectedOptions[0]?.disabled) {
      const firstUsable = [...modelSelect.options].find((option) => !option.disabled);
      if (firstUsable) modelSelect.value = firstUsable.value;
    }
    onModelChange();
  }

  // Conversation ------------------------------------------------------------
  async function loadConversation() {
    if (!conversationId) {
      conversation = null;
      composer.hidden = false;
      renderConversation();
      return;
    }
    try {
      conversation = await api(`/api/conversations/${conversationId}`);
      composer.hidden = !conversation.can_change;
    } catch (error) {
      clear(titleHost, h('div', { class: 'stale-banner' }, error.message, ' ', h('a', { href: '#/investigate' }, 'Start a new investigation.')));
      clear(messagesHost);
      return;
    }
    renderConversation();
    if (conversation.active_run_id) attachRun(conversation.active_run_id, true);
  }

  function renderHeader() {
    if (!conversation) {
      clear(titleHost);
      return;
    }
    const status = h('select', { class: 'input', 'aria-label': 'Investigation status', onChange: () => patch({ status: status.value }, 'Status saved') },
      [['open', 'Open'], ['resolved', 'Resolved'], ['false_alarm', 'False alarm']].map(([value, label]) => h('option', { value, selected: conversation.status === value }, label)));
    const note = h('textarea', { class: 'input', rows: 1, maxlength: 5000, placeholder: 'What was the real cause? Future investigations can find this.', 'aria-label': 'Confirmed root cause', value: conversation.root_cause_note || '' });
    const shared = conversation.visibility === 'team';
    clear(titleHost,
      h('div', { class: 'conv-head' },
        h('h1', {}, conversation.title),
        h('a', { class: 'button compact', href: `/api/conversations/${conversation.id}/export`, download: `investigation-${conversation.id}.json` }, 'Export'),
        conversation.can_change && conversation.trigger !== 'auto' ? h('button', { class: 'button compact', type: 'button', onClick: () => share(!shared) }, shared ? 'Stop sharing' : 'Share with team') : null,
        can('admin') ? h('button', { class: 'button compact', type: 'button', onClick: removeConversation }, 'Delete') : null,
        h('a', { class: 'button compact primary', href: '#/investigate' }, 'New investigation')),
      h('p', { class: 'small muted conv-visibility' }, shared
        ? `Shared with your team${conversation.shared_by ? ` by ${conversation.shared_by}` : ''}. Engineers can read it, and viewers see its questions and answers.`
        : `Private to ${conversation.created_by || 'its owner'}. Admins can also read it.`),
      conversation.can_change ? h('div', { class: 'conv-outcome' },
        h('label', { class: 'field' }, h('span', {}, 'Status'), status),
        h('label', { class: 'field' }, h('span', {}, 'Confirmed root cause'), note),
        h('button', { class: 'button compact', type: 'button', onClick: () => patch({ root_cause_note: note.value }, 'Root cause saved') }, 'Save'))
        : conversation.root_cause_note ? h('p', { class: 'conv-outcome-read' }, h('b', {}, 'Confirmed root cause: '), conversation.root_cause_note) : null,
      (conversation.workflows || []).length ? h('p', { class: 'workflow-links small' }, 'Workflows for people to review: ',
        conversation.workflows.map((item, index) => [index ? ', ' : '', h('a', { href: `#/workflows/${item.id}` }, item.name)])) : null);
  }

  async function patch(body, message) {
    try {
      conversation = { ...conversation, ...(await api(`/api/conversations/${conversation.id}`, { method: 'PATCH', body })) };
      toast(message);
    } catch (error) { toast(error.message); }
  }

  async function share(on) {
    try {
      const outcome = await api(`/api/conversations/${conversation.id}/share`, { method: 'POST', body: { shared: on } });
      conversation = { ...conversation, visibility: outcome.visibility, shared_by: on ? live.me.email : '' };
      renderHeader();
      toast(on ? 'Shared with your team' : 'Private again');
    } catch (error) { toast(error.message); }
  }

  async function removeConversation() {
    const ok = await confirmDialog({ title: 'Delete this investigation?', body: 'This permanently deletes its messages, prompts, and queries from the log. The audit trail keeps a record that it was deleted.', action: 'Delete investigation' });
    if (!ok) return;
    try {
      await api(`/api/conversations/${conversation.id}`, { method: 'DELETE' });
      toast('Investigation deleted');
      location.hash = '#/investigate';
    } catch (error) { toast(error.message); }
  }

  function renderStarter() {
    const context = live.context;
    const suggestions = [];
    for (const alert of (context?.alerts?.firing || []).slice(0, 2)) suggestions.push(`Why is the ${alert.name} alert firing? Find the root cause.`);
    for (const target of (context?.targets?.down_list || []).slice(0, 1)) suggestions.push(`Why is ${target.instance} down?`);
    for (const item of (context?.anomalies || []).slice(0, 1)) suggestions.push(`${item.title} is ${formatValue(item.value, item.unit)} on ${labelText(item.labels)}. What is causing it?`);
    suggestions.push('What changed recently, and does anything look wrong?');
    suggestions.push('Which part of this system is most at risk right now?');
    clear(messagesHost, h('div', { class: 'starter' },
      h('h1', {}, 'What needs explaining?'),
      h('p', { class: 'body-text' }, 'Your prompt goes with the latest live context. The model sees the same alerts, targets, signals, and recent changes shown on the right.'),
      h('div', { class: 'suggestions' }, suggestions.slice(0, 4).map((text) => h('button', { type: 'button', onClick: () => { textarea.value = text; textarea.focus(); } }, text)))));
  }

  function renderConversation() {
    renderHeader();
    if (!conversation) {
      renderStarter();
      return;
    }
    clear(messagesHost);
    const toolsByMessage = new Map();
    for (const tool of conversation.tool_calls || []) {
      if (!toolsByMessage.has(tool.message_id)) toolsByMessage.set(tool.message_id, []);
      toolsByMessage.get(tool.message_id).push({ ...tool, arguments: safeJson(tool.arguments), ok: Boolean(tool.ok) });
    }
    for (const message of conversation.messages || []) {
      if (message.role === 'user') {
        messagesHost.append(h('div', { class: 'message-user' }, message.content));
      } else if (message.status !== 'running') {
        messagesHost.append(assistantBlock(message, toolsByMessage.get(message.id) || []));
      }
    }
  }

  function safeJson(text) {
    try { return JSON.parse(text); } catch (error) { return {}; }
  }

  function assistantBlock(message, tools) {
    const queries = tools.length;
    const body = h('div', { class: 'markdown' });
    renderMarkdown(body, message.content);
    const notices = safeJson(message.notices || '[]');
    return h('article', {},
      h('p', { class: 'message-meta' }, message.model, `, context version ${message.context_version ?? 'unknown'}, `, queries ? `${queries} ${queries === 1 ? 'query' : 'queries'} run` : 'answered from the live context',
        costText(message)),
      verificationBlock(safeJson(message.verification || '{}')),
      tools.length ? evidenceList(tools, false) : null,
      (Array.isArray(notices) ? notices : []).map((text) => h('p', { class: 'notice' }, text)),
      message.content ? body : null,
      message.status === 'failed' ? h('p', { class: 'error-note' }, message.error) : null,
      message.status === 'stopped' ? h('p', { class: 'notice' }, 'Stopped before finishing.') : null,
      message.status === 'complete' ? h('div', { class: 'form-actions' },
        h('button', { class: 'button compact', type: 'button', onClick: draftWorkflow }, 'Draft a remediation workflow'),
        h('span', { class: 'small muted' }, 'Drafts need a person’s review, and this system never runs them.')) : null);
  }

  function costText(message) {
    const tokens = (message.input_tokens || 0) + (message.output_tokens || 0);
    if (!tokens && !message.duration_ms) return '';
    const parts = [`, ${tokens.toLocaleString()} tokens`];
    if (message.cost_usd !== null && message.cost_usd !== undefined) parts.push(`, ${usd(message.cost_usd)}`);
    else parts.push(', cost unknown');
    if (message.duration_ms) parts.push(`, ${(message.duration_ms / 1000).toFixed(1)} s`);
    return parts.join('');
  }

  function verificationBlock(result) {
    if (!result || result.kind !== 'diagnosis') return null;
    const numbers = result.numbers || {};
    const queries = result.queries || {};
    const tone = { High: 'ok', Medium: 'warning', Low: 'warning', Unknown: 'neutral' }[result.confidence_final] || 'neutral';
    return h('div', { class: 'verification' },
      h('p', {}, h('span', { class: `badge ${tone}` }, `Confidence after checks: ${result.confidence_final || 'Not stated'}`),
        result.lowered ? h('span', { class: 'small muted' }, ` Lowered from ${result.confidence_stated}.`) : null),
      h('p', { class: 'small muted' },
        numbers.checked ? `${numbers.matched} of ${numbers.checked} cited numbers match the data. ` : 'No checkable numbers were cited. ',
        queries.checked ? `${queries.valid} of ${queries.checked} cited queries ran successfully.` : ''),
      (result.reasons || []).map((reason) => h('p', { class: 'small' }, reason)),
      numbers.unmatched?.length ? h('p', { class: 'small muted' }, `Not found in the data: ${numbers.unmatched.join(', ')}`) : null);
  }

  async function draftWorkflow() {
    toast('Asking the model to draft a workflow');
    try {
      const made = await api(`/api/conversations/${conversation.id}/draft-workflow`, { method: 'POST', body: {} });
      toast('Draft workflow saved');
      location.hash = `#/workflows/${made.id}`;
    } catch (error) { toast(error.message); }
  }

  function evidenceItem(tool) {
    const query = queryFromArgs(tool.arguments);
    const isPromql = tool.name === 'run_instant_query' || tool.name === 'run_range_query';
    return h('div', { class: 'evidence-item' },
      h('div', { class: 'evidence-head' },
        h('span', { class: 'small muted' }, toolLabel(tool.name)),
        h('code', {}, query),
        tool.ok ? null : h('span', { class: 'badge critical' }, 'Failed'),
        isPromql ? h('button', { class: 'link-button small', type: 'button', onClick: () => chartQuery(query) }, 'Chart it') : null),
      h('details', {}, h('summary', { class: 'small muted' }, 'Show result'), h('pre', { class: 'result' }, prettyResult(tool.result))));
  }

  function evidenceList(tools, open) {
    return h('details', { class: 'evidence', open }, h('summary', {}, `Queries the model ran (${tools.length})`), tools.map(evidenceItem));
  }

  function chartQuery(query) {
    const host = h('div');
    let chart = null;
    openDialog(h('div', {}, dialogHead('Query result, last hour'), h('code', {}, query), host), { wide: true, onClose: () => chart?.destroy() });
    chart = new TimeSeriesChart(host, { height: 320, title: 'Query result' });
    chart.showMessage('Running query');
    const end = Math.floor(Date.now() / 1000);
    const start = end - 3600;
    api('/api/metrics/query_range', { method: 'POST', body: { query, start, end, step: 60 } })
      .then((data) => chart.setData(data.result, start, end, 60))
      .catch((error) => chart.showMessage(error.message, true));
  }

  // Sending and streaming ---------------------------------------------------
  async function submit(event) {
    event.preventDefault();
    const prompt = textarea.value.trim();
    if (!prompt || activeRunId) return;
    clear(composerError);
    const { provider, model } = selectedParts();
    if (!model) {
      clear(composerError, h('p', { class: 'error-note' }, 'Choose a model first. ', can('admin') ? h('a', { href: '#/connections' }, 'Set up a model in Connections.') : 'Ask an admin to set one up.'));
      return;
    }
    sendButton.disabled = true;
    try {
      if (!conversationId) {
        const created = await api('/api/conversations', { method: 'POST', body: {} });
        conversationId = created.id;
        history.replaceState(null, '', `#/investigate/${conversationId}`);
        conversation = { ...created, messages: [], tool_calls: [], can_change: true };
      }
      const { run_id: runId } = await api(`/api/conversations/${conversationId}/messages`, {
        method: 'POST', body: { prompt, provider, model, use_tools: toolsCheck.checked },
      });
      textarea.value = '';
      conversation.title = conversation.title === 'New investigation' ? prompt.replace(/\s+/g, ' ').slice(0, 90) : conversation.title;
      if (!conversation.messages.length) clear(messagesHost);
      renderHeader();
      messagesHost.append(h('div', { class: 'message-user' }, prompt));
      attachRun(runId, false);
    } catch (error) {
      clear(composerError, h('p', { class: 'error-note' }, error.message));
    } finally {
      sendButton.disabled = false;
    }
  }

  async function stopRun() {
    if (activeRunId) await api(`/api/runs/${activeRunId}/stop`, { method: 'POST', body: {} }).catch(() => {});
  }

  function setRunning(running) {
    stopButton.hidden = !running;
    sendButton.hidden = running;
    textarea.disabled = running;
  }

  async function attachRun(runId, replaying) {
    activeRunId = runId;
    setRunning(true);
    const metaLine = h('p', { class: 'message-meta' }, 'Starting');
    const statusLine = h('span', {}, 'Starting');
    const status = h('p', { class: 'message-status', role: 'status' }, h('span', { class: 'spinner' }), statusLine);
    const notices = h('div');
    const evidenceHost = h('div');
    const body = h('div', { class: 'markdown' });
    messagesHost.append(h('article', {}, metaLine, evidenceHost, notices, body, status));
    const tools = [];
    let text = '';
    let renderPending = false;
    let waitingSince = null;
    let waitingText = '';
    let model = '';
    let provider = '';
    let contextVersion = null;

    function paintText() {
      if (renderPending) return;
      renderPending = true;
      setTimeout(() => {
        renderPending = false;
        renderMarkdown(body, text);
        keepScrolled();
      }, 120);
    }
    function paintMeta() {
      metaLine.textContent = `${model}${contextVersion !== null ? `, context version ${contextVersion}` : ''}`;
    }
    function paintEvidence() {
      clear(evidenceHost, tools.length ? evidenceList(tools, true) : null);
    }
    clearInterval(tickTimer);
    tickTimer = setInterval(() => {
      if (waitingSince === null) return;
      const seconds = Math.round((Date.now() - waitingSince) / 1000);
      let suffix = seconds >= 2 ? `, ${seconds} s` : '';
      if (provider === 'ollama' && seconds >= 20) suffix += '. Local models can take a few minutes, especially on a CPU.';
      statusLine.textContent = `${waitingText}${suffix}`;
    }, 1000);

    function handle(event) {
      switch (event.type) {
        case 'started':
          model = event.model;
          provider = event.provider;
          paintMeta();
          break;
        case 'status':
          waitingText = event.text;
          waitingSince = Date.now();
          statusLine.textContent = event.text;
          status.hidden = false;
          break;
        case 'context':
          contextVersion = event.version;
          paintMeta();
          break;
        case 'text':
          waitingSince = null;
          status.hidden = true;
          text += event.text;
          paintText();
          break;
        case 'text_reset':
          text = '';
          clear(body);
          break;
        case 'workflows':
          notices.append(h('p', { class: 'past-note' }, 'Workflows shared with the model: ', event.items.map((item, index) => [index ? ', ' : '', h('a', { href: `#/workflows/${item.id}` }, item.name)])));
          break;
        case 'tool_start':
          waitingText = `Running: ${toolLabel(event.name)}`;
          waitingSince = Date.now();
          statusLine.textContent = waitingText;
          status.hidden = false;
          break;
        case 'tool_result':
          tools.push({ name: event.name, arguments: event.arguments, result: event.result, ok: event.ok });
          paintEvidence();
          keepScrolled();
          break;
        case 'notice':
          notices.append(h('p', { class: 'notice' }, event.text));
          break;
        case 'past':
          notices.append(h('p', { class: 'past-note' }, 'Related past investigations were included: ', event.items.map((item, index) => [index ? ', ' : '', h('a', { href: `#/investigate/${item.id}` }, item.title)])));
          break;
        default:
          break;
      }
    }

    streamAbort = new AbortController();
    try {
      const response = await fetch(`/api/runs/${runId}/stream`, { signal: streamAbort.signal, credentials: 'same-origin' });
      if (!response.ok) throw new Error('This run is no longer available.');
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let newline = buffer.indexOf('\n');
        while (newline >= 0) {
          const line = buffer.slice(0, newline);
          buffer = buffer.slice(newline + 1);
          if (line.trim()) handle(JSON.parse(line));
          newline = buffer.indexOf('\n');
        }
      }
    } catch (error) {
      if (error.name === 'AbortError') return;
    }
    clearInterval(tickTimer);
    activeRunId = null;
    setRunning(false);
    if (!disposed) {
      await loadConversation();
      keepScrolled();
      textarea.focus();
    }
  }

  function keepScrolled() {
    const nearBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 260;
    if (nearBottom) window.scrollTo({ top: document.body.scrollHeight });
  }

  // Live context rail -------------------------------------------------------
  function renderRail(context) {
    if (!context) return;
    const health = context.health || {};
    const firing = context.alerts?.firing || [];
    const down = context.targets?.down_list || [];
    const anomalies = context.anomalies || [];
    const events = (context.events || []).slice(0, 6);
    const stale = health.status === 'unreachable' || health.status === 'partial';
    const metrics = context.coverage?.has_metrics !== false;
    const scroll = rail.scrollTop;
    clear(rail,
      h('div', { class: 'rail-head' }, h('h2', {}, 'Live context')),
      h('p', { class: 'small muted' }, health.status === 'not_configured' ? 'Nothing connected yet.' : `Version ${context.version}, updated ${timeAgo(context.as_of)}${context.sources?.length ? ` from ${context.sources.map((item) => item.name).join(', ')}` : ''}.`),
      stale ? h('p', { class: 'stale-banner small', style: 'margin:10px 0 0' }, `A source isn't answering. The model will be told this data is from ${timeAgo(health.last_ok)}.`) : null,
      health.status === 'not_configured' ? h('p', { class: 'setup-banner small' }, can('admin') ? h('a', { href: '#/connections' }, 'Add a connection') : 'Ask an admin to add a connection', ' so the model has data to work with.') : null,
      railGroup(`Problems right now (${(context.problems || []).length})`, (context.problems || []).slice(0, 8).map((item) => h('div', { class: 'rail-item' }, h('b', {}, item.title), h('small', {}, item.detail?.diagnosis?.cause || item.kind)))),
      metrics ? railGroup(`Firing alerts (${firing.length})`, firing.map((alert) => h('div', { class: 'rail-item' }, h('b', {}, alert.name), h('small', {}, alert.summary || labelText(alert.labels))))) : null,
      metrics ? railGroup(`Down targets (${down.length})`, down.map((target) => h('div', { class: 'rail-item' }, h('b', {}, target.instance), h('small', {}, `Job ${target.job}`)))) : null,
      metrics ? railGroup(`Out-of-range signals (${anomalies.length})`, anomalies.slice(0, 8).map((item) => h('div', { class: 'rail-item' }, h('b', {}, `${item.title}: ${formatValue(item.value, item.unit)}`), h('small', {}, labelText(item.labels))))) : null,
      railGroup('Recorded changes', (context.changes || []).slice(0, 5).map((change) => h('div', { class: 'rail-item' }, change.title, h('small', {}, `${change.origin}, ${timeAgo(change.ts)}`)))),
      railGroup('State changes', events.map((event) => h('div', { class: 'rail-item' }, event.title, h('small', {}, timeAgo(event.ts))))),
      h('p', { class: 'small muted', style: 'margin-top:16px' }, `Not available to the model: ${[...(context.coverage?.missing || []), context.coverage?.has_metrics === false ? 'metrics' : null, context.coverage?.has_logs ? null : 'logs', 'traces', context.coverage?.has_kubernetes ? null : 'the Kubernetes API'].filter(Boolean).join(', ')}.`),
      h('div', { class: 'form-actions' }, h('button', { class: 'button compact', type: 'button', onClick: showExact }, 'Show exact text sent')));
    rail.scrollTop = scroll;
  }

  function railGroup(title, items) {
    return h('section', { class: 'rail-group' }, h('h3', {}, title), items.length ? items : h('p', { class: 'rail-none' }, 'None'));
  }

  async function showExact() {
    const { provider } = selectedParts();
    const pre = h('pre', { class: 'prompt-text', tabindex: '0' }, 'Loading');
    const note = h('p', { class: 'small muted' });
    openDialog(h('div', {}, dialogHead('Text the model receives now'), note, pre), { wide: true });
    try {
      const preview = await api(`/api/context/prompt-preview${provider ? `?provider=${provider}` : ''}`);
      note.textContent = `Context version ${preview.version}, up to ${preview.max_chars.toLocaleString()} characters for this model. Your prompt is added after this, along with any earlier messages and related past investigations.`;
      pre.textContent = preview.text;
    } catch (error) {
      pre.textContent = error.message;
    }
  }

  const unsubscribe = live.subscribe((type, data) => {
    if (type === 'context') {
      renderRail(data);
      if (!conversation && !conversationId) renderStarter();
    }
    if (type === 'ai') loadModels();
  });

  if (params.prompt) textarea.value = params.prompt;
  renderRail(live.context);
  loadModels();
  loadConversation();

  return () => {
    disposed = true;
    unsubscribe();
    clearInterval(tickTimer);
    streamAbort?.abort();
  };
}
