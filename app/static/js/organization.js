// Organization administration: usage, members, security policies, and single sign-on.
import { api, can, clear, confirmDialog, dialogHead, h, live, openDialog, timeAgo, toast, usd } from './core.js';

const TABS = [['overview', 'Usage'], ['members', 'Members'], ['security', 'Security policies'], ['sso', 'Single sign-on']];

export function mount(container, params) {
  const allowed = TABS.filter(([id]) => id === 'overview' || can(id === 'members' ? 'admin' : 'owner'));
  let tab = allowed.some(([id]) => id === params.id) ? params.id : 'overview';
  const tabs = h('div', { class: 'tabs', role: 'tablist', 'aria-label': 'Organization' });
  const body = h('div');
  const title = h('h1', { class: 'page-title' }, live.me?.org_name || 'Organization');
  clear(container, h('div', { class: 'page' }, title, tabs, body));

  function renderTabs() {
    clear(tabs, allowed.map(([id, label]) => h('button', { type: 'button', role: 'tab', 'aria-selected': String(id === tab), onClick: () => { tab = id; renderTabs(); load(); } }, label)));
  }

  async function load() {
    clear(body, h('p', { class: 'muted' }, 'Loading'));
    try {
      if (tab === 'overview') await overview();
      if (tab === 'members') await members();
      if (tab === 'security') await security();
      if (tab === 'sso') await sso();
    } catch (error) { clear(body, h('p', { class: 'error-note' }, error.message)); }
  }

  async function overview() {
    const usage = await api('/api/org/usage');
    const budget = h('input', { class: 'input', type: 'number', min: 0, step: '0.01', value: usage.budget_usd ?? '' });
    const tokens = h('input', { class: 'input', type: 'number', min: 1000, value: usage.max_tokens_per_investigation ?? '' });
    const spendResult = h('div');
    async function saveSpend(event) {
      event.preventDefault();
      try {
        await api('/api/org/spend', { method: 'PUT', body: { monthly_budget_usd: budget.value === '' ? null : Number(budget.value), max_tokens_per_investigation: tokens.value === '' ? null : Number(tokens.value), prices: {} } });
        clear(spendResult, h('p', { class: 'form-result ok', role: 'status' }, 'Limits saved.'));
      } catch (error) { clear(spendResult, h('p', { class: 'form-result bad', role: 'alert' }, error.message)); }
    }
    clear(body, h('section', {},
      h('h2', {}, 'AI use this month'),
      h('div', { class: 'stat-row' },
        h('div', { class: 'stat' }, h('b', {}, usd(usage.spent_usd)), h('span', {}, 'Spent')),
        h('div', { class: 'stat' }, h('b', {}, usd(usage.forecast_usd)), h('span', {}, 'Forecast for the month')),
        h('div', { class: 'stat' }, h('b', {}, String(usage.investigations)), h('span', {}, 'Answers')),
        h('div', { class: 'stat' }, h('b', {}, usd(usage.avg_cost_usd)), h('span', {}, 'Average cost')),
        h('div', { class: 'stat' }, h('b', {}, `${usage.avg_duration_s} s`), h('span', {}, 'Average time'))),
      h('p', { class: 'small muted' }, 'The forecast assumes the rest of the month looks like the days so far. Ollama shows $0 because it has no per-use charge, though your hardware has its own costs.'),
      usage.by_model.length ? h('div', { class: 'table-wrap', style: 'margin:12px 0' }, h('table', { class: 'data-table' },
        h('thead', {}, h('tr', {}, ['Model', 'Calls', 'Tokens in', 'Tokens out', 'Cost'].map((label, index) => h('th', { scope: 'col', class: index ? 'num' : null }, label)))),
        h('tbody', {}, usage.by_model.map((item) => h('tr', {}, h('td', {}, item.model), h('td', { class: 'num' }, item.calls), h('td', { class: 'num' }, Number(item.input_tokens).toLocaleString()), h('td', { class: 'num' }, Number(item.output_tokens).toLocaleString()), h('td', { class: 'num' }, usd(item.cost_usd))))))) : null,
      h('form', { class: 'form-grid', style: 'max-width:720px;margin-top:14px', onSubmit: saveSpend },
        h('label', { class: 'field span-3' }, h('span', {}, 'Monthly budget (US dollars)'), budget, h('small', {}, 'Leave empty for no limit. New investigations stop when it is reached.')),
        h('label', { class: 'field span-3' }, h('span', {}, 'Most tokens per investigation'), tokens, h('small', {}, 'The model is asked to finish when it is reached.')),
        h('div', { class: 'span-6' }, spendResult, h('button', { class: 'button primary compact', type: 'submit' }, 'Save limits')))));
  }

  function removeOrg(name) {
    const input = h('input', { class: 'input', 'aria-label': 'Organization name' });
    openDialog(h('form', { class: 'stack', onSubmit: async (event) => {
      event.preventDefault();
      try {
        await api('/api/org', { method: 'DELETE', body: { confirm_name: input.value } });
        location.hash = '#/';
        location.reload();
      } catch (error) { toast(error.message); }
    } }, dialogHead('Delete this organization?'), h('p', { class: 'body-text' }, `This can’t be undone. Type ${name} to confirm.`), input, h('button', { class: 'button danger', type: 'submit' }, 'Delete organization')));
  }

  async function members() {
    const rows = await api('/api/org/members');
    const email = h('input', { class: 'input invite-email', type: 'email', required: true, 'aria-label': 'Email address', placeholder: 'name@company.com' });
    const personName = h('input', { class: 'input invite-name', required: true, maxlength: 120, 'aria-label': 'Their name', placeholder: 'Their name' });
    const role = h('select', { class: 'input', 'aria-label': 'Role', style: 'width:auto' }, ['viewer', 'engineer', 'admin', can('owner') ? 'owner' : null].filter(Boolean).map((value) => h('option', { value }, value)));
    const result = h('div');
    async function invite(event) {
      event.preventDefault();
      try {
        const outcome = await api('/api/org/members/invite', { method: 'POST', body: { email: email.value.trim(), name: personName.value.trim(), role: role.value } });
        if (outcome.invite_link) {
          await load();
          body.prepend(h('div', { style: 'margin-bottom:14px' }, h('p', { class: 'notice' }, 'No email server is set up, so send this person the link yourself. It works for 72 hours, and only once.'), h('p', { class: 'key-box', style: 'margin-top:8px' }, outcome.invite_link)));
        } else {
          toast('Invitation sent');
          load();
        }
      } catch (error) { clear(result, h('p', { class: 'form-result bad', role: 'alert' }, error.message)); }
    }
    clear(body,
      h('form', { class: 'filter-row', onSubmit: invite }, personName, email, role, h('button', { class: 'button primary compact', type: 'submit' }, 'Invite')), result,
      h('p', { class: 'small muted', style: 'margin-bottom:10px' }, 'Viewers see status and shared results. Engineers investigate, and follow and edit workflows. Admins manage connections, alerts, and members, and can read every investigation. Owners also manage security policies and single sign-on.'),
      h('div', { class: 'table-wrap' }, h('table', { class: 'data-table' },
        h('thead', {}, h('tr', {}, ['Person', 'Role', 'Status', 'Last sign-in', ''].map((label) => h('th', { scope: 'col' }, label)))),
        h('tbody', {}, rows.map((member) => {
          const roleSelect = h('select', { class: 'input', style: 'width:auto', 'aria-label': `Role for ${member.email}`, disabled: member.role === 'owner' && !can('owner'), onChange: () => change(member, { role: roleSelect.value }) },
            ['viewer', 'engineer', 'admin', 'owner'].map((value) => h('option', { value, selected: member.role === value, disabled: value === 'owner' && !can('owner') }, value)));
          return h('tr', {},
            h('td', {}, h('b', {}, member.name), h('div', { class: 'small muted' }, member.email)),
            h('td', {}, roleSelect),
            h('td', {}, member.pending_invite ? h('span', { class: 'badge warning' }, 'Invited') : member.is_active ? h('span', { class: 'badge ok' }, 'Active') : h('span', { class: 'badge neutral' }, 'Deactivated')),
            h('td', {}, member.last_login_at ? timeAgo(member.last_login_at) : 'Never'),
            h('td', {}, member.id === live.me.id ? h('span', { class: 'small muted' }, 'You') : h('span', { class: 'row-buttons' },
              h('button', { class: 'button compact', type: 'button', onClick: () => change(member, { is_active: !member.is_active }) }, member.is_active ? 'Deactivate' : 'Reactivate'),
              h('button', { class: 'button compact', type: 'button', onClick: () => remove(member) }, 'Remove'))));
        })))));
  }

  async function change(member, patch) {
    try {
      await api(`/api/org/members/${member.id}`, { method: 'PATCH', body: patch });
      toast('Access updated, and their sessions were ended');
    } catch (error) { toast(error.message); }
    load();
  }

  async function remove(member) {
    const ok = await confirmDialog({ title: `Remove ${member.email}?`, body: 'They lose access immediately. Their logged actions stay in the audit trail.', action: 'Remove person' });
    if (!ok) return;
    try {
      await api(`/api/org/members/${member.id}`, { method: 'DELETE' });
      toast('Person removed');
    } catch (error) { toast(error.message); }
    load();
  }

  async function security() {
    const [org, retention] = await Promise.all([api('/api/org'), api('/api/org/retention')]);
    const policies = org.policies;
    const min = h('input', { class: 'input', type: 'number', min: 12, max: 64, value: policies.password_min_length });
    const hours = h('input', { class: 'input', type: 'number', min: 1, max: 720, value: policies.session_hours });
    const domains = h('input', { class: 'input', value: policies.allowed_email_domains.join(', '), placeholder: 'company.com' });
    const requireSso = h('input', { type: 'checkbox', checked: policies.require_sso });
    const cloud = h('input', { type: 'checkbox', checked: policies.allow_cloud_ai });
    const days = h('input', { class: 'input', type: 'number', min: 7, max: 3650, value: retention.retention_days });
    const result = h('div');
    async function save(event) {
      event.preventDefault();
      clear(result);
      try {
        await api('/api/org/policies', { method: 'PUT', body: { password_min_length: Number(min.value), session_hours: Number(hours.value), allowed_email_domains: domains.value.split(',').map((item) => item.trim()).filter(Boolean), require_sso: requireSso.checked, allow_cloud_ai: cloud.checked } });
        await api('/api/org/retention', { method: 'PUT', body: { retention_days: Number(days.value) } });
        clear(result, h('p', { class: 'form-result ok', role: 'status' }, 'Security policies saved.'));
      } catch (error) { clear(result, h('p', { class: 'form-result bad', role: 'alert' }, error.message)); }
    }
    clear(body, h('form', { class: 'form-grid', style: 'max-width:760px', onSubmit: save },
      h('label', { class: 'field span-3' }, h('span', {}, 'Shortest password (characters)'), min, h('small', {}, 'The minimum allowed is 12.')),
      h('label', { class: 'field span-3' }, h('span', {}, 'Sign people out after (hours)'), hours),
      h('label', { class: 'field span-6' }, h('span', {}, 'Only invite these email domains'), domains, h('small', {}, 'Separate with commas. Leave empty to allow any domain.')),
      h('label', { class: 'field span-3' }, h('span', {}, 'Keep logs for (days)'), days, h('small', {}, 'The audit trail is kept.')),
      h('div', { class: 'span-6 stack' },
        h('label', { class: 'check' }, requireSso, 'Require single sign-on for everyone'),
        h('label', { class: 'check' }, cloud, 'Allow cloud AI models, such as Claude')),
      h('div', { class: 'span-6' }, result, h('button', { class: 'button primary', type: 'submit' }, 'Save policies'))),
    h('p', { class: 'after-form' }, h('a', { href: '#/legal/security' }, 'Read the full security policy'), '. Audit logs are in ', h('a', { href: '#/logs/audit' }, 'Logs'), '.'));
  }

  async function sso() {
    const current = await api('/api/org/sso');
    const enabled = h('input', { type: 'checkbox', checked: current.enabled });
    const issuer = h('input', { class: 'input', value: current.issuer, placeholder: 'https://login.company.com' });
    const clientId = h('input', { class: 'input', value: current.client_id });
    const secret = h('input', { class: 'input', type: 'password', autocomplete: 'off', placeholder: current.has_secret ? 'Saved. Enter a new one to replace it.' : '' });
    const domains = h('input', { class: 'input', value: current.domains.join(', '), placeholder: 'company.com' });
    const role = h('select', { class: 'input' }, ['viewer', 'engineer'].map((value) => h('option', { value, selected: current.default_role === value }, value)));
    const result = h('div');
    async function save(event) {
      event.preventDefault();
      clear(result);
      try {
        await api('/api/org/sso', { method: 'PUT', body: { enabled: enabled.checked, issuer: issuer.value.trim(), client_id: clientId.value.trim(), client_secret: secret.value, domains: domains.value.split(',').map((item) => item.trim()).filter(Boolean), default_role: role.value } });
        clear(result, h('p', { class: 'form-result ok', role: 'status' }, 'Single sign-on saved.'));
      } catch (error) { clear(result, h('p', { class: 'form-result bad', role: 'alert' }, error.message)); }
    }
    clear(body, h('p', { class: 'body-text', style: 'margin-bottom:12px' }, 'Connect any OpenID Connect provider, such as Microsoft Entra ID, Okta, or Google Workspace. Use this redirect address:'),
      h('p', { class: 'key-box', style: 'margin-bottom:16px' }, current.redirect_uri),
      h('form', { class: 'form-grid', style: 'max-width:760px', onSubmit: save },
        h('label', { class: 'check span-6' }, enabled, 'Turn on single sign-on'),
        h('label', { class: 'field span-6' }, h('span', {}, 'Issuer address'), issuer),
        h('label', { class: 'field span-3' }, h('span', {}, 'Client ID'), clientId), h('label', { class: 'field span-3' }, h('span', {}, 'Client secret'), secret),
        h('label', { class: 'field span-3' }, h('span', {}, 'Email domains'), domains), h('label', { class: 'field span-3' }, h('span', {}, 'Role for new people'), role),
        h('div', { class: 'span-6' }, result, h('button', { class: 'button primary', type: 'submit' }, 'Save single sign-on'))));
  }

  renderTabs();
  load();
  return () => {};
}
