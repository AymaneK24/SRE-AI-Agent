// Connections: metric sources, logs and Kubernetes, alert channels, webhooks, and AI models.
import { api, can, clear, confirmDialog, h, live, refreshAi, statusBadge, timeAgo, toast } from './core.js';

function field(label, control, hint, span = 'span-2', extra = '') {
  return h('label', { class: `field ${span} ${extra}` }, h('span', {}, label), control, hint ? h('small', {}, hint) : null);
}

function showResult(host, ok, text) {
  clear(host, h('p', { class: `form-result ${ok ? 'ok' : 'bad'}`, role: ok ? 'status' : 'alert' }, text));
}

const CHANNEL_LABEL = { slack: 'Slack', email: 'Email', google_chat: 'Google Chat', teams: 'Microsoft Teams' };

export function mount(container) {
  let disposed = false;
  let kind = 'prometheus';
  let editing = null;
  const addingKinds = new Set();
  const editable = can('admin');
  const collectorsHost = h('div', { class: 'collectors', role: 'group', 'aria-label': 'Metric collectors' });
  const formHost = h('div');
  const listHost = h('div');
  const integrationsHost = h('div');
  // Heartbeat ------------------------------------------------------------------
  // A console that stops is silent. An outside service notices when the heartbeat stops.
  async function renderHeartbeat() {
    const settings = await api('/api/org/heartbeat');
    if (disposed) return;
    const enabled = h('input', { type: 'checkbox', checked: settings.enabled });
    const url = h('input', { class: 'input', type: 'url', autocomplete: 'off', spellcheck: 'false',
      placeholder: settings.url_set ? `Saved (${settings.url_host}). Enter a new address to replace it.` : 'https://hc-ping.com/your-check-id' });
    const every = h('input', { class: 'input', type: 'number', min: 1, max: 1440, step: 1, value: settings.every_minutes });
    const result = h('div');
    const states = {
      ok: ['badge ok', `Last heartbeat sent ${timeAgo(settings.last_ok_at)}`],
      failed: ['badge critical', `The address didn’t accept the last heartbeat: ${settings.last_error}`],
      held: ['badge warning', `Held back, so the outside service raises the alarm: ${settings.last_error}`],
      never: ['badge neutral', 'No heartbeat sent yet'],
    };
    const [tone, text] = states[settings.last_status] || states.never;
    const body = () => ({ enabled: enabled.checked, url: url.value.trim() || null, every_minutes: Number(every.value) });
    clear(heartbeatHost,
      h('h3', { class: 'noise-title' }, 'Heartbeat'),
      h('p', { class: 'body-text' }, 'If the console itself stops, it can’t tell you: no message looks the same as no problem. With a heartbeat, the console calls an address every few minutes while it is watching. A service such as Healthchecks.io, Better Stack or Cronitor alerts you when the calls stop.'),
      settings.enabled ? h('p', { class: 'heartbeat-state' }, h('span', { class: tone }, text)) : null,
      h('form', { class: 'savings-form heartbeat-form', 'aria-label': 'Heartbeat', onSubmit: async (event) => {
        event.preventDefault();
        try {
          await api('/api/org/heartbeat', { method: 'PUT', body: body() });
          toast('Heartbeat saved');
          renderHeartbeat();
        } catch (error) { showResult(result, false, error.message); }
      } },
        h('label', { class: 'check' }, enabled, h('b', {}, 'Send a heartbeat while the console is watching')),
        h('div', { class: 'form-grid' },
          field('Address to call', url, 'Treated as a secret: it is stored encrypted and never shown again.', 'span-4'),
          field('Every, minutes', every, 'Set the service’s grace time to at least twice this.', 'span-2')),
        result,
        h('div', { class: 'form-actions' },
          h('button', { class: 'button primary', type: 'submit' }, 'Save heartbeat'),
          h('button', { class: 'button', type: 'button', onClick: async () => {
            try {
              if (url.value.trim()) await api('/api/org/heartbeat', { method: 'PUT', body: body() });
              const out = await api('/api/org/heartbeat/test', { method: 'POST', body: {} });
              showResult(result, out.status === 'ok', out.status === 'ok' ? 'The address accepted a test heartbeat.' : out.error);
            } catch (error) { showResult(result, false, error.message); }
          } }, 'Send a test'))));
  }

  const channelsHost = h('div');
  const webhooksHost = h('div');
  const aiHost = h('div');
  const savingsHost = h('div');
  const noiseHost = h('div');
  const heartbeatHost = h('div');

  clear(container, h('div', { class: 'page' },
    h('h1', { class: 'page-title' }, 'Point it at your systems.'),
    h('p', { class: 'page-lede' }, 'Connect your metrics, choose who gets alerts, and choose which AI model investigates. The console only reads from your systems. It never changes them.'),
    h('section', { class: 'section', 'aria-labelledby': 'sources-title' },
      h('div', { class: 'section-head' }, h('h2', { id: 'sources-title' }, 'Metric sources')),
      collectorsHost,
      h('div', { class: 'connect-grid' }, editable ? formHost : null, h('div', {}, h('h3', { style: 'margin-bottom:8px' }, 'Connected sources'), listHost))),
    h('section', { class: 'section', 'aria-labelledby': 'alerts-title' },
      h('div', { class: 'section-head' }, h('h2', { id: 'alerts-title' }, 'Alerts')),
      h('p', { class: 'read-only-banner' }, h('b', {}, 'Alerts are read-only. '), 'Messages describe the problem and link to suggested steps. They can’t change anything.'),
      channelsHost, editable ? noiseHost : null, editable ? heartbeatHost : null),
    h('section', { class: 'section', 'aria-labelledby': 'more-title' },
      h('div', { class: 'section-head' }, h('h2', { id: 'more-title' }, 'Logs and Kubernetes')),
      h('p', { class: 'body-text', style: 'margin-bottom:14px' }, 'These add evidence to investigations. Use a read-only token.'),
      integrationsHost),
    h('section', { class: 'section', 'aria-labelledby': 'hooks-title' },
      h('div', { class: 'section-head' }, h('h2', { id: 'hooks-title' }, 'Incoming webhooks')),
      webhooksHost),
    h('section', { class: 'section', 'aria-labelledby': 'ai-title' },
      h('div', { class: 'section-head' }, h('h2', { id: 'ai-title' }, 'AI model')), aiHost),
    editable ? h('section', { class: 'section', 'aria-labelledby': 'savings-title' },
      h('div', { class: 'section-head' }, h('h2', { id: 'savings-title' }, 'Cost savings')), savingsHost) : null));

  async function renderCollectors() {
    const collectors = await api('/api/collectors');
    clear(collectorsHost, collectors.map((item) => item.implemented
      ? h('button', { class: 'collector', type: 'button', 'aria-pressed': String(kind === item.id), onClick: () => { kind = item.id; editing = null; renderCollectors(); renderForm(); } }, h('b', {}, item.name), h('span', {}, item.detail))
      : h('div', { class: 'collector is-off', 'aria-disabled': 'true' }, h('b', {}, item.name), h('span', {}, item.detail))));
  }

  // Metric source forms ------------------------------------------------------
  function renderForm() {
    if (!editable) return;
    const row = editing;
    const cfg = row?.config || {};
    const name = h('input', { class: 'input', required: true, maxlength: 80, value: row?.name || '', placeholder: 'production' });
    const poll = h('input', { class: 'input', type: 'number', min: 5, max: 600, value: row?.poll_seconds || 15 });
    const result = h('div');
    const secretHint = row?.has_secret ? 'Saved. Enter a new value to replace it.' : '';
    let readBody;
    let fields;

    const metricsBox = () => h('textarea', { class: 'input query-input', rows: 3, placeholder: kind === 'cloudwatch' ? 'AWS/EC2|CPUUtilization|InstanceId=i-0abc|Average' : '/subscriptions/ID/resourceGroups/RG/providers/Microsoft.Sql/servers/S/databases/D|cpu_percent|Average', value: (cfg.metrics || []).map((item) => item.query).join('\n') });
    const metricList = (box) => box.value.split('\n').map((line) => line.trim()).filter(Boolean).map((query) => {
      const existing = (cfg.metrics || []).find((item) => item.query === query);
      return existing || { title: query.split('|')[1] || query, query, unit: 'short' };
    });

    if (kind === 'prometheus') {
      const scheme = h('select', { class: 'input' }, ['http', 'https'].map((value) => h('option', { value, selected: (cfg.scheme || 'http') === value }, value)));
      const host = h('input', { class: 'input', required: true, placeholder: '192.168.1.20', value: cfg.host || '', autocomplete: 'off', spellcheck: 'false', maxlength: 253 });
      const port = h('input', { class: 'input', required: true, type: 'number', min: 1, max: 65535, value: cfg.port || 9090 });
      const prefix = h('input', { class: 'input', placeholder: 'Optional', value: cfg.path_prefix || '', spellcheck: 'false' });
      const auth = h('select', { class: 'input' }, [['none', 'None'], ['basic', 'Username and password'], ['bearer', 'Bearer token'], ['aws_sigv4', 'Amazon Managed Prometheus'], ['azure_entra', 'Azure Managed Prometheus'], ['google', 'Google Managed Prometheus']].map(([value, label]) => h('option', { value, selected: (cfg.auth_type || 'none') === value }, label)));
      const username = h('input', { class: 'input', value: cfg.username || '', autocomplete: 'off' });
      const secret = h('input', { class: 'input', type: 'password', autocomplete: 'new-password', placeholder: secretHint });
      const region = h('input', { class: 'input', value: cfg.region || '', placeholder: 'eu-west-1' });
      const accessKey = h('input', { class: 'input', autocomplete: 'off', placeholder: secretHint });
      const secretKey = h('input', { class: 'input', type: 'password', autocomplete: 'new-password', placeholder: secretHint });
      const tenant = h('input', { class: 'input', value: cfg.tenant_id || '' });
      const client = h('input', { class: 'input', value: cfg.client_id || '' });
      const clientSecret = h('input', { class: 'input', type: 'password', autocomplete: 'new-password', placeholder: secretHint });
      const account = h('textarea', { class: 'input query-input', rows: 3, placeholder: secretHint || 'Paste the service account JSON' });
      const verify = h('input', { type: 'checkbox', checked: cfg.verify_tls !== false });
      const authFields = h('div', { class: 'form-grid span-6' });
      const renderAuth = () => clear(authFields,
        auth.value === 'basic' ? field('Username', username, null, 'span-3') : null,
        ['basic', 'bearer'].includes(auth.value) ? field(auth.value === 'basic' ? 'Password' : 'Token', secret, 'Encrypted before it is stored.', 'span-3') : null,
        auth.value === 'aws_sigv4' ? [field('AWS region', region, null, 'span-2'), field('Access key ID', accessKey, null, 'span-2'), field('Secret access key', secretKey, null, 'span-2')] : null,
        auth.value === 'azure_entra' ? [field('Tenant ID', tenant, null, 'span-2'), field('Client ID', client, null, 'span-2'), field('Client secret', clientSecret, null, 'span-2')] : null,
        auth.value === 'google' ? field('Service account JSON', account, 'Needs monitoring read access. Encrypted before it is stored.', 'span-6') : null,
        scheme.value === 'https' ? h('label', { class: 'check span-6' }, verify, 'Check the server’s TLS certificate') : null);
      auth.addEventListener('change', renderAuth);
      scheme.addEventListener('change', renderAuth);
      host.addEventListener('blur', () => {
        const value = host.value.trim();
        if (!/^https?:\/\//i.test(value)) return;
        try {
          const url = new URL(value);
          scheme.value = url.protocol.replace(':', '');
          host.value = url.hostname;
          port.value = url.port || (url.protocol === 'https:' ? 443 : 80);
          if (url.pathname && url.pathname !== '/') prefix.value = url.pathname.replace(/^\/|\/$/g, '');
          renderAuth();
        } catch (error) { /* The server reports an invalid address when testing. */ }
      });
      fields = [
        field('Protocol', scheme, null, 'span-1', 'keep-half'), field('IP address or host name', host, null, 'span-3'), field('Port', port, null, 'span-2', 'keep-half'),
        field('Path prefix', prefix, 'Amazon Managed Prometheus uses workspaces/WORKSPACE-ID.', 'span-6'), field('Sign-in', auth, null, 'span-6'), authFields,
      ];
      renderAuth();
      readBody = () => ({
        config: { scheme: scheme.value, host: host.value.trim(), port: Number(port.value), path_prefix: prefix.value.trim(), auth_type: auth.value, username: username.value, verify_tls: verify.checked, region: region.value.trim(), tenant_id: tenant.value.trim(), client_id: client.value.trim() },
        secrets: { secret: secret.value, access_key: accessKey.value.trim(), secret_key: secretKey.value, client_secret: clientSecret.value, service_account_json: account.value },
      });
    } else if (kind === 'cloudwatch') {
      const region = h('input', { class: 'input', required: true, value: cfg.region || '', placeholder: 'eu-west-1' });
      const accessKey = h('input', { class: 'input', autocomplete: 'off', placeholder: secretHint });
      const secretKey = h('input', { class: 'input', type: 'password', autocomplete: 'new-password', placeholder: secretHint });
      const metrics = metricsBox();
      fields = [
        field('AWS region', region, null, 'span-2'), field('Access key ID', accessKey, 'Use a read-only IAM user.', 'span-2'), field('Secret access key', secretKey, 'Encrypted before it is stored.', 'span-2'),
        field('Metrics to track, one per line', metrics, 'Written as namespace|metric|dimension=value|statistic.', 'span-6'),
      ];
      readBody = () => ({ config: { region: region.value.trim(), metrics: metricList(metrics) }, secrets: { access_key: accessKey.value.trim(), secret_key: secretKey.value } });
    } else {
      const subscription = h('input', { class: 'input', required: true, value: cfg.subscription_id || '' });
      const tenant = h('input', { class: 'input', required: true, value: cfg.tenant_id || '' });
      const client = h('input', { class: 'input', required: true, value: cfg.client_id || '' });
      const clientSecret = h('input', { class: 'input', type: 'password', autocomplete: 'new-password', placeholder: secretHint });
      const workspace = h('input', { class: 'input', value: cfg.workspace_id || '', placeholder: 'Optional, for log queries' });
      const metrics = metricsBox();
      fields = [
        field('Subscription ID', subscription, null, 'span-3'), field('Log Analytics workspace ID', workspace, null, 'span-3'),
        field('Tenant ID', tenant, null, 'span-2'), field('Client ID', client, 'Give it the Monitoring Reader role.', 'span-2'), field('Client secret', clientSecret, 'Encrypted before it is stored.', 'span-2'),
        field('Metrics to track, one per line', metrics, 'Written as /resource/id|metric|Average.', 'span-6'),
      ];
      readBody = () => ({ config: { subscription_id: subscription.value.trim(), tenant_id: tenant.value.trim(), client_id: client.value.trim(), workspace_id: workspace.value.trim(), metrics: metricList(metrics) }, secrets: { client_secret: clientSecret.value } });
    }

    const payload = () => ({ kind, name: name.value.trim() || 'Metric source', poll_seconds: Number(poll.value), ...readBody() });
    async function test() {
      clear(result, h('p', { class: 'form-result muted' }, 'Testing connection'));
      try {
        const outcome = await api('/api/sources/test', { method: 'POST', body: { ...payload(), id: row?.id ?? null } });
        if (outcome.ok) showResult(result, true, `Connected to ${outcome.version} in ${outcome.latency_ms} ms. ${outcome.detail || `${outcome.targets_up} of ${outcome.targets_total} targets are up.`}`);
        else showResult(result, false, outcome.error);
      } catch (error) { showResult(result, false, error.message); }
    }
    async function save(event) {
      event.preventDefault();
      clear(result);
      try {
        const body = payload();
        const saved = row ? await api(`/api/sources/${row.id}`, { method: 'PUT', body }) : await api('/api/sources', { method: 'POST', body });
        toast(`Connected to ${saved.name}`);
        editing = null;
        renderForm();
        renderSources();
      } catch (error) { showResult(result, false, error.message); }
    }
    clear(formHost, h('form', { onSubmit: save, 'aria-label': 'Metric source' },
      h('h3', { style: 'margin-bottom:10px' }, row ? `Edit ${row.name}` : 'Add a source'),
      h('div', { class: 'form-grid' }, field('Display name', name, null, 'span-3'), field('Read every (seconds)', poll, null, 'span-3'), fields),
      result,
      h('div', { class: 'form-actions' },
        h('button', { class: 'button primary', type: 'submit' }, row ? 'Save and connect' : 'Connect'),
        h('button', { class: 'button', type: 'button', onClick: test }, 'Test connection'),
        row ? h('button', { class: 'button', type: 'button', onClick: () => { editing = null; renderForm(); } }, 'Cancel') : null)));
  }

  async function renderSources() {
    const rows = await api('/api/sources');
    if (disposed) return;
    const statusById = new Map((live.context?.sources || []).map((item) => [item.id, item]));
    if (!rows.length) {
      clear(listHost, h('p', { class: 'empty' }, 'No sources connected yet.'));
      return;
    }
    const mainId = mainOf(rows, live.context?.main?.metrics);
    clear(listHost, h('ul', { class: 'rows' }, rows.map((row) => {
      const state = statusById.get(row.id);
      const address = row.kind === 'prometheus' ? `${row.config.scheme}://${row.config.host}:${row.config.port}${row.config.path_prefix ? `/${row.config.path_prefix}` : ''}` : row.kind === 'cloudwatch' ? `CloudWatch, ${row.config.region}` : `Azure Monitor, ${row.config.subscription_id}`;
      return h('li', { class: 'row', style: 'grid-template-columns:minmax(0,1fr) auto' },
        h('span', {},
          h('span', { class: 'row-title' }, row.name), ' ',
          rows.length > 1 && row.id === mainId ? h('span', { class: 'badge neutral main-badge' }, 'Main') : null, ' ',
          !row.enabled ? h('span', { class: 'badge neutral' }, 'Paused') : state?.status === 'ok' ? h('span', { class: 'badge ok' }, 'Answering') : state ? h('span', { class: 'badge critical' }, 'Not answering') : null,
          h('span', { class: 'small muted', style: 'display:block;overflow-wrap:anywhere' }, address),
          state?.error ? h('span', { class: 'small', style: 'display:block;overflow-wrap:anywhere' }, state.error) : null),
        editable ? h('span', { style: 'display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end' },
          rows.length > 1 && row.id !== mainId ? h('button', { class: 'button compact', type: 'button', title: 'The overview shows this source first, and investigations use it when no source is named.', onClick: () => makeMain('metrics', row, renderSources) }, 'Make main') : null,
          h('button', { class: 'button compact', type: 'button', onClick: async () => { await api(`/api/sources/${row.id}/enabled`, { method: 'POST', body: { enabled: !row.enabled } }); renderSources(); } }, row.enabled ? 'Pause' : 'Resume'),
          h('button', { class: 'button compact', type: 'button', onClick: () => { kind = row.kind; editing = row; renderCollectors(); renderForm(); window.scrollTo({ top: collectorsHost.offsetTop - 80 }); } }, 'Edit'),
          h('button', { class: 'button compact', type: 'button', onClick: () => removeSource(row) }, 'Remove')) : null);
    })));
  }

  // The main connection of a kind: the one shown first, and used when nothing else is named.
  function mainOf(rows, saved) {
    return rows.some((row) => row.id === saved) ? saved : rows[0]?.id;
  }

  async function makeMain(connectionKind, row, rerender) {
    try {
      const out = await api('/api/connections/main', { method: 'PUT', body: { kind: connectionKind, id: row.id } });
      if (live.context) live.context = { ...live.context, main: out.main };
      toast(`${row.name} is now the main one`);
      rerender();
    } catch (error) { toast(error.message); }
  }

  async function removeSource(row) {
    const ok = await confirmDialog({ title: `Remove ${row.name}?`, body: 'This removes the connection and its panels. Logged investigations stay in the database.', action: 'Remove source' });
    if (!ok) return;
    try {
      await api(`/api/sources/${row.id}`, { method: 'DELETE' });
      toast(`Removed ${row.name}`);
      editing = null;
      renderForm();
      renderSources();
    } catch (error) { toast(error.message); }
  }

  // Alert channels -----------------------------------------------------------
  async function renderChannels() {
    if (!editable) {
      clear(channelsHost, h('p', { class: 'body-text' }, 'An admin manages alert channels.'));
      return;
    }
    const [rows, auto] = await Promise.all([api('/api/org/channels'), api('/api/org/auto-investigate')]);
    if (disposed) return;
    const channelKind = h('select', { class: 'input' }, Object.entries(CHANNEL_LABEL).map(([value, label]) => h('option', { value }, label)));
    const name = h('input', { class: 'input', required: true, maxlength: 80, placeholder: 'On-call team' });
    const severity = h('select', { class: 'input' }, [['warning', 'Warning and critical'], ['critical', 'Critical only']].map(([value, label]) => h('option', { value }, label)));
    const url = h('input', { class: 'input', type: 'url', placeholder: 'https://hooks.slack.com/services/...', autocomplete: 'off' });
    const recipients = h('input', { class: 'input', placeholder: 'team@company.com, you@company.com' });
    const targetField = h('div', { class: 'span-6' });
    const result = h('div');
    const renderTarget = () => clear(targetField, channelKind.value === 'email'
      ? field('Send to', recipients, 'Separate with commas.', '')
      : field('Webhook address', url, { slack: 'From a Slack Incoming Webhook.', google_chat: 'From a Google Chat space webhook.', teams: 'From a Teams Workflows webhook.' }[channelKind.value], ''));
    channelKind.addEventListener('change', renderTarget);
    renderTarget();
    async function add(event) {
      event.preventDefault();
      clear(result);
      try {
        await api('/api/org/channels', { method: 'POST', body: { kind: channelKind.value, name: name.value.trim(), min_severity: severity.value, url: url.value.trim(), recipients: recipients.value.split(',').map((item) => item.trim()).filter(Boolean) } });
        toast('Channel added');
        renderChannels();
      } catch (error) { showResult(result, false, error.message); }
    }
    const autoCheck = h('input', { type: 'checkbox', checked: auto.enabled });
    const perHour = h('input', { class: 'input', type: 'number', min: 1, max: 60, value: auto.per_hour || 5, style: 'width:90px', 'aria-label': 'Most automatic investigations per hour' });
    const saveAuto = async () => {
      try {
        await api('/api/org/auto-investigate', { method: 'PUT', body: { enabled: autoCheck.checked, per_hour: Number(perHour.value) } });
        toast('Saved');
      } catch (error) { toast(error.message); }
    };
    autoCheck.addEventListener('change', saveAuto);
    perHour.addEventListener('change', saveAuto);
    clear(channelsHost,
      h('div', { class: 'connect-grid' },
        h('form', { onSubmit: add, 'aria-label': 'Add alert channel' }, h('h3', { style: 'margin-bottom:10px' }, 'Add a channel'),
          h('div', { class: 'form-grid' }, field('Type', channelKind, null, 'span-2'), field('Name', name, null, 'span-2'), field('Send', severity, null, 'span-2'), targetField),
          result, h('div', { class: 'form-actions' }, h('button', { class: 'button primary', type: 'submit' }, 'Add channel'))),
        h('div', {}, h('h3', { style: 'margin-bottom:8px' }, 'Channels'),
          rows.length ? h('ul', { class: 'rows' }, rows.map((row) => h('li', { class: 'row', style: 'grid-template-columns:minmax(0,1fr) auto' },
            h('span', {}, h('span', { class: 'row-title' }, row.name), h('span', { class: 'small muted', style: 'display:block;overflow-wrap:anywhere' }, `${CHANNEL_LABEL[row.kind]}, ${row.min_severity === 'critical' ? 'critical only' : 'warning and critical'}${row.recipients.length ? `, ${row.recipients.join(', ')}` : ''}`)),
            h('span', { class: 'row-buttons' },
              h('button', { class: 'button compact', type: 'button', onClick: async () => { try { const out = await api(`/api/org/channels/${row.id}/test`, { method: 'POST', body: {} }); toast(out.status === 'sent' ? 'Test sent' : out.error); } catch (error) { toast(error.message); } } }, 'Send test'),
              h('button', { class: 'button compact', type: 'button', onClick: async () => { await api(`/api/org/channels/${row.id}`, { method: 'DELETE' }); renderChannels(); } }, 'Remove'))))) : h('p', { class: 'empty' }, 'No channels yet.'))),
      h('div', { class: 'form-actions' }, h('label', { class: 'check' }, autoCheck, 'Investigate critical alerts automatically, then email the findings with charts and a PDF'), h('label', { class: 'check' }, 'Most per hour', perHour)),
      h('p', { class: 'small muted', style: 'margin-top:6px' }, 'This uses your AI budget and license allowance.'));
  }

  // Noise control ------------------------------------------------------------
  // Fewer, better messages: what is held back, and why.
  async function renderNoise() {
    const settings = await api('/api/org/noise');
    if (disposed) return;
    const week = settings.week || {};
    const held = week.held_back || {};
    const number = (value, min, max, step = 1) => h('input', { class: 'input', type: 'number', min, max, step, value });
    const graceCritical = number(settings.grace_critical_s, 0, 3600, 15);
    const graceWarning = number(settings.grace_warning_s, 0, 7200, 30);
    const groupWindow = number(settings.group_window_s, 0, 900, 15);
    const perHour = number(settings.max_per_hour, 1, 500);
    const flapping = h('input', { type: 'checkbox', checked: settings.flapping });
    const verdicts = h('input', { type: 'checkbox', checked: settings.respect_verdicts });
    const labelDays = h('select', { class: 'input', 'aria-label': 'How long a false alarm label lasts' },
      [[7, '7 days'], [30, '30 days'], [90, '90 days'], [0, 'Until something changes']].map(([value, text]) => h('option', { value, selected: Number(settings.label_days ?? 30) === value }, text)));
    const result = h('div');
    const stat = (value, label, hint) => h('div', { class: 'stat', title: hint || '' }, h('b', {}, String(value ?? 0)), h('span', {}, label));
    clear(noiseHost,
      h('h3', { class: 'noise-title' }, 'Noise control'),
      h('p', { class: 'body-text' }, 'Every problem passes these checks before anyone is messaged. Nothing is hidden in the console: only the messages are held back.'),
      h('div', { class: 'stat-row savings-stats' },
        stat(week.problems_started, 'Problems started, last 7 days'),
        stat(week.messages_sent, 'Messages sent'),
        stat(week.held_back_total, 'Held back'),
        stat(held.blip || 0, 'Cleared within the grace period', 'Short blips that fixed themselves.'),
        stat(held.grouped || 0, 'Sent together with another problem'),
        stat((held.flapping || 0) + (held.muted || 0) + (held.rate_limit || 0), 'Flapping, marked false alarm, or over the limit')),
      h('form', { class: 'savings-form', 'aria-label': 'Noise control', onSubmit: async (event) => {
        event.preventDefault();
        try {
          await api('/api/org/noise', { method: 'PUT', body: { grace_critical_s: Number(graceCritical.value), grace_warning_s: Number(graceWarning.value),
            group_window_s: Number(groupWindow.value), max_per_hour: Number(perHour.value), flapping: flapping.checked, respect_verdicts: verdicts.checked, label_days: Number(labelDays.value) } });
          toast('Noise control saved');
          renderNoise();
        } catch (error) { showResult(result, false, error.message); }
      } },
        h('ol', { class: 'savings-list noise-steps' },
          h('li', {}, h('b', {}, 'Grace period'),
            h('p', {}, 'A problem must still be there after this wait. Blips that clear by themselves are never sent, and never start an automatic investigation.'),
            h('div', { class: 'form-grid' }, field('Critical, seconds', graceCritical, null, 'span-2'), field('Warning, seconds', graceWarning, null, 'span-2'))),
          h('li', {}, h('b', {}, 'Group problems that start together'),
            h('p', {}, 'Problems starting within this window become one message, with what they share: a node, a namespace, a cluster, a dependency they all mention such as postgres:5432, or a change recorded just before. One automatic investigation looks at all of them.'),
            h('div', { class: 'form-grid' }, field('Window, seconds', groupWindow, null, 'span-2'))),
          h('li', {}, h('label', { class: 'check' }, flapping, h('b', {}, 'Calm flapping problems')),
            h('p', {}, 'Something that starts 3 times or more in an hour gets one message saying so. Further starts stay quiet until it has been stable for 30 minutes.')),
          h('li', {}, h('label', { class: 'check' }, verdicts, h('b', {}, 'Respect false-alarm verdicts')),
            h('p', {}, 'Problems your team marked as a false alarm or expected, in a project report, are no longer messaged. They still show in the console. '
              + 'A label ends on its own when its time is up, when a deploy or change touches the same service, when the problem gets worse than when it was labelled, or when its alert rule changes. The alert then goes out and says why.'),
            h('div', { class: 'form-grid' }, field('Default length, for labels set without one', labelDays, null, 'span-2'))),
          h('li', {}, h('b', {}, 'Most messages per channel, per hour'),
            h('p', {}, 'Beyond this, a channel gets one summary of what was held back when the hour is up.'),
            h('div', { class: 'form-grid' }, field('Messages per hour', perHour, null, 'span-2'))),
          h('li', {}, h('b', {}, 'Already in place'),
            h('p', {}, 'One problem, one alert: a Prometheus alert about a workload the console already reports joins it. A message isn’t repeated for 15 minutes. Recovery messages go only where the problem was sent. Unusual signals must stay unusual for two polls.'))),
        result,
        h('div', { class: 'form-actions' }, h('button', { class: 'button primary', type: 'submit' }, 'Save noise control'))));
  }

  // Cost savings -------------------------------------------------------------
  // How investigations keep API costs down, and what that saved this month.
  async function renderSavings() {
    const [savings, claudeModels, geminiModels] = await Promise.all([
      api('/api/ai/savings'),
      api('/api/ai/providers/claude/models').catch(() => ({ models: [] })),
      api('/api/ai/providers/gemini/models').catch(() => ({ models: [] })),
    ]);
    if (disposed) return;
    const month = savings.month || {};
    const money = (value) => `$${Number(value || 0).toFixed(value >= 10 ? 0 : 2)}`;
    const caching = h('input', { type: 'checkbox', checked: savings.prompt_caching });
    const gather = h('input', { type: 'checkbox', checked: savings.gather_evidence });
    let style = savings.answer_style;
    const styleControl = h('div', { class: 'segments', role: 'group', 'aria-label': 'Default answer length' });
    const paintStyle = () => clear(styleControl, [['short', 'Short answer'], ['report', 'Full report']].map(([value, label]) => h('button', {
      type: 'button', 'aria-pressed': String(style === value), onClick: () => { style = value; paintStyle(); } }, label)));
    paintStyle();
    const modelPicker = (provider, models, current) => {
      const ids = models.map((item) => item.id);
      const options = [['', 'The main model does everything'], ...models.map((item) => [item.id, item.label || item.id])];
      if (current && !ids.includes(current)) options.push([current, current]);
      return h('select', { class: 'input', 'aria-label': `${provider} model for query rounds` }, options.map(([value, label]) => h('option', { value, selected: value === (current || '') }, label)));
    };
    const claudePick = modelPicker('Claude', claudeModels.models || [], savings.evidence_model?.claude);
    const geminiPick = modelPicker('Gemini', geminiModels.models || [], savings.evidence_model?.gemini);
    const result = h('div');
    const stat = (value, label, hint) => h('div', { class: 'stat', title: hint || '' }, h('b', {}, value), h('span', {}, label));
    clear(savingsHost,
      h('p', { class: 'body-text', style: 'margin-bottom:6px' }, 'An investigation sends the instructions, the live context and the evidence to the model on every round. These settings cut what is sent, and what each token costs.'),
      h('div', { class: 'stat-row savings-stats' },
        stat(money(month.cost_usd), 'Spent on API models this month', month.cache_saved_usd ? `It would have been ${money((month.cost_usd || 0) + month.cache_saved_usd)} without prompt caching.` : ''),
        stat(money(month.cache_saved_usd), 'Saved by prompt caching', 'Compared with sending every round at the full input price.'),
        stat(`${Math.round((month.cached_share || 0) * 100)}%`, 'Of input tokens read from cache', `${Number(month.cached_tokens || 0).toLocaleString()} tokens`),
        stat(String(month.console_queries || 0), 'Queries the console ran for the model', 'Each is a round the model didn’t have to pay for.'),
        stat(String(month.focused_investigations || 0), 'Focused investigations', 'Only the chosen problems’ evidence was sent.'),
        stat(month.evidence_calls ? `${month.evidence_calls} · ${money(month.evidence_cost_usd)}` : '0', 'Query rounds on the cheaper model')),
      h('form', { class: 'savings-form', 'aria-label': 'Cost savings', onSubmit: async (event) => {
        event.preventDefault();
        try {
          await api('/api/ai/savings', { method: 'PUT', body: { prompt_caching: caching.checked, gather_evidence: gather.checked, answer_style: style,
            evidence_model: { claude: claudePick.value, gemini: geminiPick.value } } });
          toast('Cost savings saved');
          renderSavings();
        } catch (error) { showResult(result, false, error.message); }
      } },
        h('ul', { class: 'savings-list' },
          h('li', {}, h('label', { class: 'check' }, caching, h('b', {}, 'Prompt caching')),
            h('p', {}, 'The part of the prompt that stays the same between rounds is read from Claude’s cache at about a tenth of the input price, and faster. Gemini does this by itself on recent models; the console keeps that part first so it can.')),
          h('li', {}, h('label', { class: 'check' }, gather, h('b', {}, 'Gather evidence before the model starts')),
            h('p', {}, 'For the problems being investigated, the console runs the obvious queries itself: the workload’s error lines, its Kubernetes state, the chart of the signal that went out of range. API models need fewer rounds, and private models get evidence they would struggle to query for.')),
          h('li', {}, h('b', {}, 'Focus on chosen problems'),
            h('p', {}, 'In Investigate, people tick the problems they care about in the live context. Only those problems, and their logs, metrics and cluster state, are sent. Investigate buttons on the overview do this for one problem. Always on.')),
          h('li', {}, h('b', {}, 'A cheaper model for the query rounds'),
            h('p', {}, 'A small, fast model runs the queries; the main model reads what it found and writes the answer. Choose the same provider’s smaller model, such as Haiku or Flash-Lite.'),
            h('div', { class: 'form-grid' }, field('With Claude', claudePick, null, 'span-3'), field('With Gemini', geminiPick, null, 'span-3'))),
          h('li', {}, h('b', {}, 'Default answer length'), styleControl,
            h('p', {}, 'Short answers give the cause, the evidence and next checks in under 250 words. People can still ask for a full report, and automatic investigations always write one for the email and PDF.'))),
        result,
        h('div', { class: 'form-actions' }, h('button', { class: 'button primary', type: 'submit' }, 'Save cost savings'))));
  }

  // Logs and Kubernetes ------------------------------------------------------
  async function renderIntegrations() {
    const rows = await api('/api/integrations');
    if (disposed) return;
    const adding = new Set(addingKinds);
    // One form per connection. A kind can have several, such as one Loki per environment or one cluster per region.
    const form = (integrationKind, row, kindTitle, placeholder, extraLabel, extraKey, hint, ofKind, mainId) => {
      const title = row ? row.name : ofKind.length ? 'New connection' : null;
      const nameInput = h('input', { class: 'input', required: true, maxlength: 80, value: row?.name || (ofKind.length ? '' : kindTitle), placeholder: integrationKind === 'kubernetes' ? 'production-eu' : 'Loki production', spellcheck: 'false' });
      const url = h('input', { class: 'input', type: 'url', value: row?.config.url || '', placeholder });
      const extra = h('input', { class: 'input', value: row?.config[extraKey] || '', spellcheck: 'false' });
      const token = h('input', { class: 'input', type: 'password', autocomplete: 'new-password', placeholder: row?.has_secret ? 'Saved. Enter a new one to replace it.' : 'Optional' });
      const result = h('div');
      const kube = integrationKind === 'kubernetes';
      const cfg = row?.config || {};
      const tenant = h('input', { class: 'input', value: cfg.tenant || '', spellcheck: 'false', maxlength: 100, placeholder: 'Optional' });
      const inCluster = h('input', { type: 'checkbox', checked: Boolean(cfg.in_cluster) });
      const verify = h('input', { type: 'checkbox', checked: cfg.verify_tls !== false });
      const serverName = h('input', { class: 'input', value: cfg.tls_server_name || '', spellcheck: 'false', placeholder: 'Optional, such as kubernetes.docker.internal' });
      const caCert = h('textarea', { class: 'input query-input', rows: 4, spellcheck: 'false', placeholder: '-----BEGIN CERTIFICATE-----' });
      // A saved CA shows as one line. Pasting a new one replaces it; leaving the box empty keeps it.
      let removeCa = false;
      const caInfo = cfg.ca_info;
      const caSummary = h('div', { class: 'small' });
      const caField = field('CA certificate', caCert, 'The cluster’s CA, in PEM form. In a kubeconfig it’s certificate-authority-data, base64-decoded.', '');
      if (caInfo) {
        caField.hidden = true;
        clear(caSummary, h('span', { class: 'muted' }, 'CA certificate saved: '), caInfo.subject,
          caInfo.expires ? h('span', { class: 'muted' }, `, expires ${caInfo.expires.slice(0, 10)}`) : null, ' ',
          h('button', { class: 'button compact', type: 'button', onClick: () => { caField.hidden = false; caSummary.hidden = true; caCert.focus(); } }, 'Replace'), ' ',
          h('button', { class: 'button compact', type: 'button', onClick: () => { removeCa = true; caSummary.textContent = 'The saved CA certificate will be removed when you save.'; } }, 'Remove'));
      }
      const kubeFields = kube ? [
        h('label', { class: 'check' }, inCluster, 'The console runs inside this cluster. Use its own service account, with no address or token.'),
        caInfo ? caSummary : null, caField,
        field('TLS server name', serverName, 'Use when you connect through another address than the one on the certificate, such as host.docker.internal.', ''),
        h('label', { class: 'check' }, verify, 'Verify the certificate. Only turn this off on a test cluster.'),
      ] : [];
      const body = () => ({ kind: integrationKind, id: row?.id ?? null, name: nameInput.value.trim(), config: kube
        ? { url: url.value.trim(), [extraKey]: extra.value.trim(), in_cluster: inCluster.checked, verify_tls: verify.checked, tls_server_name: serverName.value.trim(), ca_cert: caCert.value.trim(), remove_ca: removeCa }
        : { url: url.value.trim(), [extraKey]: extra.value.trim(), tenant: tenant.value.trim() }, secrets: { token: token.value } });
      return h('form', { 'aria-label': title, onSubmit: async (event) => {
        event.preventDefault();
        try {
          const saved = await api('/api/integrations', { method: 'POST', body: body() });
          toast(`${saved.name} saved`);
          if (!row) addingKinds.delete(integrationKind);
          renderIntegrations();
        } catch (error) { showResult(result, false, error.message); }
      } },
      title ? h('h3', { class: 'connection-title' }, title, ' ', row ? statusBadge('active') : null, ofKind.length > 1 && row?.id === mainId ? h('span', { class: 'badge neutral main-badge' }, 'Main') : null) : null,
      h('div', { class: 'stack' }, field('Name', nameInput, ofKind.length || !row ? 'Shown wherever you choose between connections.' : null, ''), field('Address', url, null, ''), field(extraLabel, extra, hint, ''), field('Read-only token', token, 'Encrypted before it is stored.', ''),
        kube ? null : field('Tenant', tenant, 'Only for a multi-tenant Loki. Sent as X-Scope-OrgID.', ''), ...kubeFields),
      result,
      editable ? h('div', { class: 'form-actions' },
        h('button', { class: 'button primary compact', type: 'submit' }, 'Save'),
        h('button', { class: 'button compact', type: 'button', onClick: async () => {
          try {
            const out = await api('/api/integrations/test', { method: 'POST', body: { ...body(), name: body().name || kindTitle } });
            showResult(result, out.ok, out.ok ? out.detail : out.error);
          } catch (error) { showResult(result, false, error.message); }
        } }, 'Test connection'),
        row && ofKind.length > 1 && row.id !== mainId ? h('button', { class: 'button compact', type: 'button', title: 'The overview shows this one first, and investigations use it when none is named.', onClick: () => makeMain(integrationKind, row, renderIntegrations) }, 'Make main') : null,
        row ? h('button', { class: 'button compact', type: 'button', onClick: async () => {
          const ok = await confirmDialog({ title: `Remove ${row.name}?`, body: 'The console stops reading it. Past investigations stay.', action: 'Remove connection' });
          if (!ok) return;
          await api(`/api/integrations/${row.id}`, { method: 'DELETE' });
          renderIntegrations();
        } }, 'Remove') : h('button', { class: 'button compact', type: 'button', onClick: () => { addingKinds.delete(integrationKind); renderIntegrations(); } }, ofKind.length ? 'Cancel' : 'Clear')) : null);
    };
    const group = (integrationKind, kindTitle, addLabel, ...rest) => {
      const ofKind = rows.filter((item) => item.kind === integrationKind);
      const mainId = mainOf(ofKind, live.context?.main?.[integrationKind]);
      const forms = ofKind.map((row) => form(integrationKind, row, kindTitle, ...rest, ofKind, mainId));
      if (!ofKind.length || adding.has(integrationKind)) forms.push(form(integrationKind, null, kindTitle, ...rest, ofKind, mainId));
      return h('div', { class: 'connection-group' },
        h('div', { class: 'connection-group-head' }, h('h3', {}, kindTitle, ofKind.length > 1 ? h('span', { class: 'muted' }, ` (${ofKind.length})`) : null),
          editable && ofKind.length && !adding.has(integrationKind) ? h('button', { class: 'button compact', type: 'button', onClick: () => { addingKinds.add(integrationKind); renderIntegrations(); } }, addLabel) : null),
        h('div', { class: 'connection-forms' }, forms));
    };
    clear(integrationsHost, h('div', { class: 'two-col' },
      group('loki', 'Loki logs', 'Add another Loki', 'http://loki.internal:3100', 'Log selector', 'selector', 'Optional. For example: {job=~"api.*"}'),
      group('kubernetes', 'Kubernetes', 'Add another cluster', 'https://kubernetes.internal:6443', 'Namespace', 'namespace', 'Optional. Leave empty for all the namespaces this token can read.')));
  }

  // Webhooks -----------------------------------------------------------------
  async function renderWebhooks() {
    if (!editable) {
      clear(webhooksHost, h('p', { class: 'body-text' }, 'An admin manages webhooks.'));
      return;
    }
    const status = await api('/api/org/webhooks');
    if (disposed) return;
    const result = h('div');
    clear(webhooksHost,
      h('p', { class: 'body-text' }, 'Send Alertmanager alerts and deployment events here. Changes appear in the live context, so the model can link a failure to the change before it.'),
      h('div', { class: 'form-actions' }, h('button', { class: 'button compact', type: 'button', onClick: async () => {
        const urls = await api('/api/org/webhooks/rotate', { method: 'POST', body: {} });
        clear(result,
          h('p', { class: 'notice' }, 'Copy these now, because they won’t be shown again. Treat them like passwords.'),
          h('p', { class: 'small', style: 'margin-top:8px' }, 'Alertmanager webhook_config url'), h('p', { class: 'key-box' }, urls.alertmanager),
          h('p', { class: 'small', style: 'margin-top:8px' }, 'GitHub, GitLab, or your own tools. Send JSON with a title and service.'), h('p', { class: 'key-box' }, urls.changes));
      } }, status.configured ? 'Replace addresses' : 'Create addresses')),
      status.configured ? h('p', { class: 'small muted' }, 'Addresses exist. Replacing them stops the old ones from working.') : null, result);
  }

  // AI model -----------------------------------------------------------------
  async function renderAi() {
    const ai = await refreshAi();
    if (disposed) return;
    if (!editable) {
      clear(aiHost, h('p', { class: 'body-text' }, `This organization uses ${{ claude: 'Claude', gemini: 'Gemini' }[ai.provider] || 'Ollama'}. An admin can change this.`));
      return;
    }
    const providerOllama = h('input', { type: 'radio', name: 'ai-provider', value: 'ollama', checked: ai.provider === 'ollama' });
    const providerClaude = h('input', { type: 'radio', name: 'ai-provider', value: 'claude', checked: ai.provider === 'claude' });
    const providerGemini = h('input', { type: 'radio', name: 'ai-provider', value: 'gemini', checked: ai.provider === 'gemini' });
    const ollamaHost = h('div');
    const claudeHost = h('div');
    const geminiHost = h('div');
    const gem = ai.gemini || { model: '', max_tokens: 8192, use_tools: true, consent: false, strict_answers: true, key_source: 'none' };
    const geminiKey = h('input', { class: 'input', type: 'password', autocomplete: 'off', placeholder: gem.key_source === 'saved' ? 'Saved. Enter a new key to replace it.' : gem.key_source === 'environment' ? 'Set by RCC_GEMINI_API_KEY' : 'AIza...', disabled: gem.key_source === 'environment' });
    const geminiModel = h('select', { class: 'input' });
    const geminiStatus = h('small', {});
    const geminiTokens = h('input', { class: 'input', type: 'number', min: 256, max: 65536, step: 256, value: gem.max_tokens });
    const geminiTools = h('input', { type: 'checkbox', checked: gem.use_tools });
    const geminiStrict = h('input', { type: 'checkbox', checked: gem.strict_answers });
    const geminiConsent = h('input', { type: 'checkbox', checked: gem.consent });
    const result = h('div');
    const ollamaUrl = h('input', { class: 'input', value: ai.ollama.url, spellcheck: 'false' });
    const ollamaModel = h('select', { class: 'input' });
    const ollamaStatus = h('small', {});
    const numCtx = h('input', { class: 'input', type: 'number', min: 2048, max: 1048576, step: 1024, value: ai.ollama.num_ctx });
    const ollamaTools = h('input', { type: 'checkbox', checked: ai.ollama.use_tools });
    const ollamaStrict = h('input', { type: 'checkbox', checked: ai.ollama.strict_answers });
    const smallModel = h('select', { class: 'input' },
      h('option', { value: 'auto' }, 'Automatic, from the size in the model’s name'), h('option', { value: 'on' }, 'Always'), h('option', { value: 'off' }, 'Never'));
    smallModel.value = ai.ollama.small_model || 'auto';
    const claudeKey = h('input', { class: 'input', type: 'password', autocomplete: 'off', placeholder: ai.claude.key_source === 'saved' ? 'Saved. Enter a new key to replace it.' : ai.claude.key_source === 'environment' ? 'Set by ANTHROPIC_API_KEY' : 'sk-ant-...', disabled: ai.claude.key_source === 'environment' });
    const claudeModel = h('select', { class: 'input' });
    const claudeStatus = h('small', {});
    const maxTokens = h('input', { class: 'input', type: 'number', min: 256, max: 64000, step: 256, value: ai.claude.max_tokens });
    const claudeTools = h('input', { type: 'checkbox', checked: ai.claude.use_tools });
    const claudeStrict = h('input', { type: 'checkbox', checked: ai.claude.strict_answers });
    const consent = h('input', { type: 'checkbox', checked: ai.claude.consent });
    const maxRounds = h('input', { class: 'input', type: 'number', min: 1, max: 12, value: ai.max_rounds });

    async function loadOllama() {
      ollamaStatus.textContent = 'Looking for models';
      const data = await api(`/api/ai/providers/ollama/models?url=${encodeURIComponent(ollamaUrl.value.trim())}`);
      const current = ollamaModel.value || ai.ollama.model;
      clear(ollamaModel, data.models.length ? data.models.map((item) => h('option', { value: item.id }, item.detail ? `${item.label} (${item.detail})` : item.label)) : h('option', { value: '' }, 'No models found'));
      if (data.models.some((item) => item.id === current)) ollamaModel.value = current;
      ollamaStatus.textContent = data.ok ? (data.models.length ? `Found ${data.models.length} ${data.models.length === 1 ? 'model' : 'models'}.` : 'Ollama is running but has no models. Run: ollama pull llama3.2') : data.error;
    }
    async function loadClaude() {
      claudeStatus.textContent = 'Loading models';
      const data = await api('/api/ai/providers/claude/models');
      const current = claudeModel.value || ai.claude.model;
      clear(claudeModel, data.models.map((item) => h('option', { value: item.id }, item.label)));
      if (data.models.some((item) => item.id === current)) claudeModel.value = current;
      else if (data.models.length && !data.ok) { claudeModel.prepend(h('option', { value: current }, current)); claudeModel.value = current; }
      claudeStatus.textContent = data.ok ? `Your key can use ${data.models.length} ${data.models.length === 1 ? 'model' : 'models'}.` : data.error;
    }
    async function loadGemini() {
      geminiStatus.textContent = 'Loading models';
      const data = await api('/api/ai/providers/gemini/models');
      const current = geminiModel.value || gem.model;
      clear(geminiModel, data.models.length ? data.models.map((item) => h('option', { value: item.id }, item.label)) : h('option', { value: current }, current || 'No models yet'));
      if (data.models.some((item) => item.id === current)) geminiModel.value = current;
      geminiStatus.textContent = data.ok ? `Your key can use ${data.models.length} Gemini ${data.models.length === 1 ? 'model' : 'models'}.` : data.error;
    }
    const keyActions = { claude: [claudeKey, claudeStatus, loadClaude], gemini: [geminiKey, geminiStatus, loadGemini] };
    async function saveKey(name) {
      const [input, status, reload] = keyActions[name];
      const value = input.value.trim();
      if (!value) { status.textContent = 'Enter an API key first.'; return; }
      try {
        await api(`/api/ai/${name}-key`, { method: 'PUT', body: { api_key: value } });
        input.value = '';
        input.placeholder = 'Saved. Enter a new key to replace it.';
        toast('API key saved');
        await refreshAi();
        await reload();
      } catch (error) { status.textContent = error.message; }
    }
    async function removeKey(name) {
      await api(`/api/ai/${name}-key`, { method: 'DELETE' });
      toast('API key removed');
      renderAi();
    }
    const toggleProviders = () => { ollamaHost.hidden = !providerOllama.checked; claudeHost.hidden = !providerClaude.checked; geminiHost.hidden = !providerGemini.checked; };
    providerOllama.addEventListener('change', toggleProviders);
    providerClaude.addEventListener('change', toggleProviders);
    providerGemini.addEventListener('change', toggleProviders);
    async function save(event) {
      event.preventDefault();
      clear(result);
      try {
        await api('/api/ai/settings', { method: 'PUT', body: {
          provider: providerClaude.checked ? 'claude' : providerGemini.checked ? 'gemini' : 'ollama', max_rounds: Number(maxRounds.value),
          ollama: { url: ollamaUrl.value.trim(), model: ollamaModel.value, num_ctx: Number(numCtx.value), use_tools: ollamaTools.checked, strict_answers: ollamaStrict.checked, small_model: smallModel.value },
          claude: { model: claudeModel.value || ai.claude.model, max_tokens: Number(maxTokens.value), use_tools: claudeTools.checked, consent: consent.checked, strict_answers: claudeStrict.checked },
          gemini: { model: geminiModel.value || gem.model, max_tokens: Number(geminiTokens.value), use_tools: geminiTools.checked, consent: geminiConsent.checked, strict_answers: geminiStrict.checked },
        } });
        await refreshAi();
        showResult(result, true, 'AI model settings saved.');
      } catch (error) { showResult(result, false, error.message); }
    }
    clear(ollamaHost, h('div', { class: 'form-grid' },
      field('Ollama address', ollamaUrl, 'Inside Docker, use http://host.docker.internal:11434 or the host’s IP address.', 'span-3'),
      h('label', { class: 'field span-3' }, h('span', {}, 'Model'), ollamaModel, ollamaStatus),
      field('Model memory (tokens)', numCtx, 'Larger values fit more context but use more RAM.', 'span-3'),
      field('Small-model mode', smallModel, 'For models of about 8B or fewer: a shorter prompt, the 4 most useful tools, and the computed diagnosis as the starting point.', 'span-3'),
      h('div', { class: 'stack span-3', style: 'align-self:center' },
        h('label', { class: 'check' }, ollamaTools, 'Let the model run queries. Small models are often unreliable at this.'),
        h('label', { class: 'check' }, ollamaStrict, 'Ask again if an answer is incomplete. On a CPU, this can take a while.')),
      h('div', { class: 'span-6' }, h('button', { class: 'button compact', type: 'button', onClick: () => loadOllama().catch((error) => { ollamaStatus.textContent = error.message; }) }, 'Find models'))));
    clear(claudeHost, h('div', { class: 'form-grid' },
      h('p', { class: 'body-text small span-6' }, 'This uses an API key from the ', h('a', { href: 'https://platform.claude.com', target: '_blank', rel: 'noopener noreferrer' }, 'Claude Console'), '. A claude.ai sign-in doesn’t connect here, and API use is billed separately from Claude plans.'),
      h('label', { class: 'field span-3' }, h('span', {}, 'API key'), claudeKey, h('small', {}, 'Encrypted before it is stored and never shown again.')),
      h('label', { class: 'field span-3' }, h('span', {}, 'Model'), claudeModel, claudeStatus),
      ai.claude.key_source === 'environment' ? null : h('div', { class: 'span-6', style: 'display:flex;gap:8px;flex-wrap:wrap' },
        h('button', { class: 'button compact', type: 'button', onClick: () => saveKey('claude') }, 'Save key'),
        ai.claude.key_source === 'saved' ? h('button', { class: 'button compact', type: 'button', onClick: () => removeKey('claude') }, 'Remove key') : null),
      field('Longest answer (tokens)', maxTokens, null, 'span-3'),
      h('div', { class: 'stack span-3', style: 'align-self:center' }, h('label', { class: 'check' }, claudeTools, 'Let the model run queries'), h('label', { class: 'check' }, claudeStrict, 'Ask again if an answer is incomplete')),
      h('label', { class: 'check span-6' }, consent, 'Allow sending prompts and live context, including host and service names, to Anthropic')));
    clear(geminiHost, h('div', { class: 'form-grid' },
      h('p', { class: 'body-text small span-6' }, 'This uses a Gemini API key from ', h('a', { href: 'https://aistudio.google.com/apikey', target: '_blank', rel: 'noopener noreferrer' }, 'Google AI Studio'), '. Each request carries the whole conversation, so the console doesn’t rely on Google storing anything between requests. Google’s API terms decide what Google keeps of what it receives.'),
      h('label', { class: 'field span-3' }, h('span', {}, 'API key'), geminiKey, h('small', {}, 'Encrypted before it is stored and never shown again.')),
      h('label', { class: 'field span-3' }, h('span', {}, 'Model'), geminiModel, geminiStatus),
      gem.key_source === 'environment' ? null : h('div', { class: 'span-6', style: 'display:flex;gap:8px;flex-wrap:wrap' },
        h('button', { class: 'button compact', type: 'button', onClick: () => saveKey('gemini') }, 'Save key'),
        gem.key_source === 'saved' ? h('button', { class: 'button compact', type: 'button', onClick: () => removeKey('gemini') }, 'Remove key') : null),
      field('Longest answer (tokens)', geminiTokens, null, 'span-3'),
      h('div', { class: 'stack span-3', style: 'align-self:center' }, h('label', { class: 'check' }, geminiTools, 'Let the model run queries'), h('label', { class: 'check' }, geminiStrict, 'Ask again if an answer is incomplete')),
      h('label', { class: 'check span-6' }, geminiConsent, 'Allow sending prompts and live context, including host and service names, to Google')));
    clear(aiHost, h('form', { onSubmit: save, 'aria-label': 'AI model settings' },
      h('div', { class: 'provider-grid' },
        h('label', { class: 'provider-option' }, providerOllama, h('b', {}, 'Ollama'), h('span', {}, 'Runs on your own server. Your data stays there, and speed depends on your hardware.')),
        h('label', { class: 'provider-option' }, providerClaude, h('b', {}, 'Claude'), h('span', {}, 'Runs at Anthropic over the internet. Needs an API key and sends your context there.')),
        h('label', { class: 'provider-option' }, providerGemini, h('b', {}, 'Gemini'), h('span', {}, 'Runs at Google over the internet. Needs an API key and sends your context there.'))),
      ollamaHost, claudeHost, geminiHost,
      h('div', { class: 'form-grid', style: 'margin-top:14px' }, field('Most model rounds per prompt', maxRounds, 'Limits how many times a model can run queries and continue.', 'span-3')),
      result, h('div', { class: 'form-actions' }, h('button', { class: 'button primary', type: 'submit' }, 'Save AI settings'))));
    toggleProviders();
    loadOllama().catch((error) => { ollamaStatus.textContent = error.message; });
    loadClaude().catch((error) => { claudeStatus.textContent = error.message; });
    loadGemini().catch((error) => { geminiStatus.textContent = error.message; });
  }

  renderCollectors().catch(() => {});
  renderForm();
  renderSources().catch((error) => clear(listHost, h('p', { class: 'error-note' }, error.message)));
  renderChannels().catch((error) => clear(channelsHost, h('p', { class: 'error-note' }, error.message)));
  renderIntegrations().catch((error) => clear(integrationsHost, h('p', { class: 'error-note' }, error.message)));
  renderWebhooks().catch((error) => clear(webhooksHost, h('p', { class: 'error-note' }, error.message)));
  renderAi().catch((error) => clear(aiHost, h('p', { class: 'error-note' }, error.message)));
  if (editable) renderSavings().catch((error) => clear(savingsHost, h('p', { class: 'error-note' }, error.message)));
  if (editable) renderNoise().catch((error) => clear(noiseHost, h('p', { class: 'error-note' }, error.message)));
  if (editable) renderHeartbeat().catch((error) => clear(heartbeatHost, h('p', { class: 'error-note' }, error.message)));
  const unsubscribe = live.subscribe((type) => { if (type === 'context') renderSources().catch(() => {}); });
  return () => { disposed = true; unsubscribe(); };
}
