// Public pages: the first page, sign-in, sign-up, email verification, password pages, and policies.
import { api, clear, h, live, toast } from './core.js';

function field(label, control, hint) {
  return h('label', { class: 'field' }, h('span', {}, label), control, hint ? h('small', {}, hint) : null);
}

function formError(host, error) {
  clear(host, h('p', { class: 'form-result bad', role: 'alert' }, error.message || error));
}

const HERO_CHART = `<svg viewBox="0 0 640 400" role="img" aria-label="Illustration of an incident: a release is followed by rising database connections and failed requests">
  <line x1="20" y1="350" x2="470" y2="350" stroke="var(--line)"/>
  <line x1="205" y1="30" x2="205" y2="350" stroke="var(--muted)" stroke-dasharray="4 4"/>
  <text x="213" y="42" font-size="13" fill="var(--body)">Release</text>
  <path d="M20 340 L60 338 L100 341 L140 339 L180 340 L205 339 L240 330 L280 318 L320 322 L360 318 L400 322 L470 319" fill="none" stroke="var(--muted)" stroke-width="2"/>
  <path d="M20 300 L80 302 L140 298 L205 301 L235 240 L265 165 L295 112 L330 110 L380 112 L430 109 L470 111" fill="none" stroke="var(--ink)" stroke-width="3"/>
  <path d="M20 345 L120 344 L205 345 L255 340 L295 300 L330 215 L365 185 L410 180 L470 183" fill="none" stroke="var(--critical)" stroke-width="2.5"/>
  <circle cx="470" cy="111" r="6" fill="var(--ink)"/><circle cx="470" cy="183" r="6" fill="var(--critical)"/><circle cx="470" cy="319" r="6" fill="var(--muted)"/>
  <text x="484" y="107" font-size="15" font-weight="600" fill="var(--ink)">Database</text><text x="484" y="125" font-size="15" font-weight="600" fill="var(--ink)">connections</text>
  <text x="484" y="188" font-size="15" font-weight="600" fill="var(--critical)">Failed requests</text>
  <text x="484" y="324" font-size="15" fill="var(--body)">Traffic, flat</text>
</svg>`;

export function landing(container) {
  const info = live.info || {};
  const open = info.signup_open === true;
  const chart = h('div');
  chart.innerHTML = HERO_CHART;
  clear(container, h('div', { class: 'page' },
    h('section', { class: 'landing-hero' },
      h('div', {},
        info.company ? h('p', { class: 'muted', style: 'margin-bottom:10px' }, `${info.company}’s private console`) : null,
        h('h1', {}, 'Your alert fired. Now find out why.'),
        h('p', { class: 'hero-copy' }, 'Root Cause Console keeps ', h('b', {}, 'a live picture of your metrics, alerts, logs, and changes'),
          ', then asks an AI model to investigate when something goes wrong. You get the likely cause, the evidence behind it, and steps for your team to review.'),
        h('div', { class: 'hero-actions' },
          h('a', { class: 'button primary large', href: open ? '#/signup' : '#/signin' }, open ? 'Start a free trial' : 'Sign in'),
          open ? h('span', { class: 'muted' }, '14 days, with no card.') : h('span', { class: 'muted' }, 'Need access? Ask your administrator for an invitation.'))),
      h('div', {}, chart, h('p', { class: 'illustration-note' }, 'Illustration of an incident, not real data.'))),
    h('section', { class: 'section', 'aria-labelledby': 'how' },
      h('h2', { id: 'how', style: 'margin-bottom:18px' }, 'How it works'),
      h('ol', { class: 'steps-grid' },
        h('li', {}, h('b', {}, '1. Connect'), h('p', {}, 'Add Prometheus, CloudWatch, or Azure Monitor, plus optional Loki logs, Kubernetes, and deployment events.')),
        h('li', {}, h('b', {}, '2. Watch'), h('p', {}, 'The console tracks alerts, targets, and unusual signals in real time, and records what changed.')),
        h('li', {}, h('b', {}, '3. Investigate'), h('p', {}, 'Ask a question, or start automatically on critical alerts. Use Ollama on your own machine or Claude over the internet.')),
        h('li', {}, h('b', {}, '4. Decide'), h('p', {}, 'Read the checked answer and its remediation workflow. Your team carries out any fix.')))),
    h('section', { class: 'section two-col' },
      h('div', {},
        h('h2', { style: 'margin-bottom:12px' }, 'Built to be checked'),
        h('ul', { class: 'plain-list' },
          h('li', {}, h('b', {}, 'Evidence checks. '), 'Cited numbers are compared with your data, and confidence is lowered when they don’t match.'),
          h('li', {}, h('b', {}, 'Other explanations. '), 'Every diagnosis must name a competing cause, and “unknown” is an accepted answer.'),
          h('li', {}, h('b', {}, 'False alarms. '), 'Your team marks what was noise, and reports stop counting it.'),
          h('li', {}, h('b', {}, 'Cost tracking. '), 'See tokens, cost, and time per investigation, and set a monthly budget.'))),
      h('div', {},
        h('h2', { style: 'margin-bottom:12px' }, 'Read-only by design'),
        h('ul', { class: 'plain-list' },
          h('li', {}, h('b', {}, 'It never changes your systems. '), 'Models can only read data, and remediation workflows are instructions for people.'),
          h('li', {}, h('b', {}, 'Alerts only inform. '), 'Slack, email, Google Chat, and Teams messages have nothing that can trigger an action.'),
          h('li', {}, h('b', {}, 'Every prompt is recorded. '), 'Logs keep the exact text sent to the model, and each action names a person.'),
          info.single_company
            ? h('li', {}, h('b', {}, 'Your own installation. '), 'This server and its database serve one company only. ', h('a', { href: '#/legal/security' }, 'Read the security policy.'))
            : h('li', {}, h('b', {}, 'Each company is separate. '), 'Accounts, data, and credentials are isolated per organization. ', h('a', { href: '#/legal/security' }, 'Read the security policy.'))))),
    h('p', { class: 'creator' }, `Created by ${info.creator || 'Aymane KENBOUCH'}.`, h('span', {}, 'Root Cause Console is built for SRE and platform teams who want answers they can verify.'))));
  return () => {};
}

export function footer() {
  const info = live.info || {};
  return h('footer', { class: 'site-footer', id: 'site-footer' }, h('div', { class: 'site-footer-inner' },
    h('span', {}, `Root Cause Console, by ${info.creator || 'Aymane KENBOUCH'}`),
    h('nav', { 'aria-label': 'Policies' },
      h('a', { href: '#/legal/security' }, 'Security'), h('a', { href: '#/legal/privacy' }, 'Privacy'),
      h('a', { href: '#/legal/terms' }, 'Terms'), h('a', { href: '#/legal/acceptable-use' }, 'Acceptable use'),
      info.contact_email ? h('a', { href: `mailto:${info.contact_email}` }, 'Contact') : null)));
}

export function signin(container, params) {
  const login = h('input', { class: 'input', type: 'email', required: true, autocomplete: 'username', autofocus: true });
  const password = h('input', { class: 'input', type: 'password', required: true, autocomplete: 'current-password' });
  const result = h('div');
  const ssoEmail = h('input', { class: 'input', type: 'email', autocomplete: 'email', placeholder: 'you@company.com', 'aria-label': 'Work email for single sign-on' });
  if (params.sso === 'failed') formError(result, 'Single sign-on didn’t finish. Try again, or contact your admin.');
  if (params.verified) clear(result, h('p', { class: 'form-result ok', role: 'status' }, params.verified === 'changed' ? 'Your email is changed. Sign in with the new address.' : 'Your email is verified. You can sign in now.'));
  async function resend() {
    await api('/api/auth/resend-verification', { method: 'POST', body: { email: login.value.trim() } }).catch(() => {});
    toast('If that account needs verifying, we sent a new email');
  }
  async function submit(event) {
    event.preventDefault();
    clear(result);
    try {
      await api('/api/auth/login', { method: 'POST', body: { email: login.value.trim(), password: password.value } });
      location.hash = '#/overview';
      location.reload();
    } catch (error) {
      formError(result, error);
      if (/Verify your email/.test(error.message)) {
        result.append(h('button', { class: 'link-button small', type: 'button', onClick: resend }, 'Send a new verification email'));
      }
    }
  }
  async function sso(event) {
    event.preventDefault();
    try {
      const data = await api('/api/auth/sso/start', { method: 'POST', body: { email: ssoEmail.value.trim() } });
      location.href = data.redirect;
    } catch (error) { formError(result, error); }
  }
  clear(container, h('div', { class: 'narrow-page' },
    h('h1', {}, 'Sign in'),
    h('p', { class: 'lede' }, 'Use your email address and password.'),
    h('form', { class: 'stack', onSubmit: submit },
      field('Email', login), field('Password', password), result,
      h('button', { class: 'button primary', type: 'submit' }, 'Sign in')),
    h('p', { class: 'after-form' }, h('a', { href: '#/forgot' }, 'Forgot your password?')),
    h('details', { class: 'after-form' }, h('summary', {}, 'Sign in with SSO'),
      h('form', { class: 'stack', style: 'margin-top:10px', onSubmit: sso }, ssoEmail, h('button', { class: 'button', type: 'submit' }, 'Continue'))),
    live.info?.signup_open ? h('p', { class: 'after-form' }, 'New here? ', h('a', { href: '#/signup' }, 'Start a free trial.')) : h('p', { class: 'after-form' }, 'Need access? Ask your administrator for an invitation.')));
  return () => {};
}

export function signup(container) {
  if (!live.info?.signup_open) {
    clear(container, h('div', { class: 'narrow-page' }, h('h1', {}, 'Accounts are by invitation'), h('p', { class: 'lede' }, 'Ask your administrator to invite you. You’ll get an email with a link to set your password.')));
    return () => {};
  }
  const company = h('input', { class: 'input', required: true, maxlength: 120, autocomplete: 'organization', autofocus: true });
  const name = h('input', { class: 'input', required: true, maxlength: 120, autocomplete: 'name' });
  const email = h('input', { class: 'input', type: 'email', required: true, autocomplete: 'email' });
  const password = h('input', { class: 'input', type: 'password', required: true, minlength: 12, autocomplete: 'new-password' });
  const terms = h('input', { type: 'checkbox', required: true });
  const result = h('div');
  async function submit(event) {
    event.preventDefault();
    clear(result);
    try {
      await api('/api/auth/signup', { method: 'POST', body: { company: company.value.trim(), name: name.value.trim(), email: email.value.trim(), password: password.value, accept_terms: terms.checked } });
      clear(container, h('div', { class: 'narrow-page' }, h('h1', {}, 'Check your email'),
        h('p', { class: 'lede' }, `If this address can be used, a verification link is on its way to ${email.value.trim()}. It works for 24 hours.`),
        live.info?.email_configured === false ? h('p', { class: 'notice' }, 'No email server is set up on this installation, so the link was written to the server log.') : null));
    } catch (error) { formError(result, error); }
  }
  clear(container, h('div', { class: 'narrow-page' },
    h('h1', {}, 'Start your trial'),
    h('p', { class: 'lede' }, '14 days with 3 users, 1 metric source, and 50 investigations.'),
    h('form', { class: 'stack', onSubmit: submit },
      field('Company', company), field('Your name', name), field('Work email', email), field('Password', password, 'At least 12 characters.'),
      h('label', { class: 'check' }, terms, h('span', {}, 'I accept the ', h('a', { href: '#/legal/terms' }, 'terms'), ' and ', h('a', { href: '#/legal/privacy' }, 'privacy notice'), '.')),
      result, h('button', { class: 'button primary', type: 'submit' }, 'Create account')),
    h('p', { class: 'after-form' }, 'Already have an account? ', h('a', { href: '#/signin' }, 'Sign in.'))));
  return () => {};
}

export function verify(container, params) {
  const message = h('p', { class: 'lede' }, 'Checking your link');
  clear(container, h('div', { class: 'narrow-page' }, h('h1', {}, 'Verify your email'), message));
  api('/api/auth/verify', { method: 'POST', body: { token: params.token || '' } })
    .then((outcome) => {
      history.replaceState(null, '', '#/signin?verified=1');
      signin(container, { verified: outcome?.email_changed ? 'changed' : '1' });
    })
    .catch((error) => { message.textContent = error.message; });
  return () => {};
}

export function forgot(container) {
  const email = h('input', { class: 'input', type: 'email', required: true, autocomplete: 'email', autofocus: true });
  const result = h('div');
  async function submit(event) {
    event.preventDefault();
    try {
      await api('/api/auth/forgot-password', { method: 'POST', body: { email: email.value.trim() } });
      clear(result, h('p', { class: 'form-result ok', role: 'status' }, 'If that address has an account, a reset link is on its way. It works for one hour.'));
    } catch (error) { formError(result, error); }
  }
  clear(container, h('div', { class: 'narrow-page' }, h('h1', {}, 'Reset your password'),
    h('p', { class: 'lede' }, 'Enter your email address, and we’ll send you a link.'),
    h('form', { class: 'stack', onSubmit: submit }, field('Email', email), result, h('button', { class: 'button primary', type: 'submit' }, 'Send reset link')),
    h('p', { class: 'after-form' }, h('a', { href: '#/signin' }, 'Back to sign in'))));
  return () => {};
}

export function reset(container, params) {
  const password = h('input', { class: 'input', type: 'password', required: true, minlength: 12, autocomplete: 'new-password', autofocus: true });
  const result = h('div');
  async function submit(event) {
    event.preventDefault();
    try {
      await api('/api/auth/reset-password', { method: 'POST', body: { token: params.token || '', password: password.value } });
      toast('Password changed');
      location.hash = '#/signin';
    } catch (error) { formError(result, error); }
  }
  clear(container, h('div', { class: 'narrow-page' }, h('h1', {}, 'Choose a new password'),
    h('form', { class: 'stack', style: 'margin-top:20px', onSubmit: submit }, field('New password', password, 'At least 12 characters.'), result, h('button', { class: 'button primary', type: 'submit' }, 'Change password'))));
  return () => {};
}

export function accept(container, params) {
  const name = h('input', { class: 'input', required: true, autocomplete: 'name', autofocus: true });
  const password = h('input', { class: 'input', type: 'password', required: true, minlength: 12, autocomplete: 'new-password' });
  const result = h('div');
  async function submit(event) {
    event.preventDefault();
    try {
      await api('/api/auth/accept-invite', { method: 'POST', body: { token: params.token || '', name: name.value.trim(), password: password.value } });
      location.hash = '#/overview';
      location.reload();
    } catch (error) { formError(result, error); }
  }
  clear(container, h('div', { class: 'narrow-page' }, h('h1', {}, 'Join your team'),
    h('p', { class: 'lede' }, 'Add your name and choose a password.'),
    h('form', { class: 'stack', onSubmit: submit }, field('Your name', name), field('Password', password, 'At least 12 characters.'), result, h('button', { class: 'button primary', type: 'submit' }, 'Create account'))));
  return () => {};
}

export function changePassword(container) {
  const forced = live.me?.must_change_password;
  // The account a new installation starts with has a placeholder address, so this is when it gets a real one.
  const placeholder = forced && live.me.email.endsWith('@rootcause.local');
  const name = h('input', { class: 'input', required: true, maxlength: 120, autocomplete: 'name', value: live.me?.name || '' });
  const email = h('input', { class: 'input', type: 'email', required: true, autocomplete: 'email', placeholder: 'you@company.com' });
  const current = h('input', { class: 'input', type: 'password', required: true, autocomplete: 'current-password', autofocus: true });
  const next = h('input', { class: 'input', type: 'password', required: true, minlength: 12, autocomplete: 'new-password' });
  const result = h('div');
  async function submit(event) {
    event.preventDefault();
    clear(result);
    try {
      if (placeholder) await api('/api/auth/me', { method: 'PATCH', body: { name: name.value.trim(), email: email.value.trim(), current_password: current.value } });
      await api('/api/auth/change-password', { method: 'POST', body: { current_password: current.value, new_password: next.value } });
      toast('Password changed');
      location.hash = '#/overview';
      location.reload();
    } catch (error) { formError(result, error); }
  }
  clear(container, h('div', { class: 'narrow-page' }, h('h1', {}, forced ? 'Set up your account' : 'Change your password'),
    h('p', { class: 'lede' }, forced ? 'The first password can’t be kept, so choose your own to continue.' : 'This signs you out on your other devices.'),
    h('form', { class: 'stack', onSubmit: submit },
      placeholder ? [field('Your name', name), field('Your email', email, 'You’ll sign in with this from now on.')] : null,
      field('Current password', current), field('New password', next, 'At least 12 characters. Avoid your name and common words.'), result,
      h('button', { class: 'button primary', type: 'submit' }, forced ? 'Continue' : 'Change password'))));
  return () => {};
}

const LEGAL = {
  security: ['Security policy', [
    ['Access', ['Every page and API call needs a signed-in account, apart from public pages and health checks.', 'Roles limit what each person can see and do: viewer, engineer, admin, and owner. Investigations are private to the person who started them, until they share them. Admins and owners can read all of them.', 'Passwords need at least 12 characters and are stored with Argon2. Repeated failed sign-ins stop the device trying and slow further attempts, and they can’t lock the account’s owner out.', 'Organizations can require single sign-on, shorten sessions, and limit which email domains can be invited.']],
    ['Your installation', ['Each company has its own server and database, so other companies can’t reach its data through this software.', 'Whoever administers the server and database can read the data stored there, so limit that access.']],
    ['Credentials', ['Passwords, API keys, tokens, and webhook addresses for connections are encrypted at rest and never returned to the browser.', 'Obvious credentials are removed from telemetry before it is logged or sent to a model. This uses pattern matching, so it can miss some secrets.']],
    ['Read-only operation', ['The service only reads from monitored systems, so use credentials limited to read access.', 'AI models have read-only tools. Remediation workflows are instructions for people, and the service has no feature that runs them.', 'Alert messages only inform, and contain no controls that act on your systems.']],
    ['AI data handling', ['When an organization uses Claude, its prompts and live context, including host and service names, are sent to Anthropic. An admin must allow this first, and an owner can block it by policy.', 'When an organization uses its own Ollama server, this data goes only to that server.', 'Telemetry text is treated as untrusted data. Prompt injection can’t be fully prevented, so verify answers before acting.']],
    ['Logging and retention', ['Each prompt is stored exactly as sent. Settings changes and sign-ins go in a hash-chained audit trail, which owners and admins can export.', 'Each organization sets its own retention period. An owner can delete the organization and its data.']],
    ['Network and application', ['Public deployments use HTTPS, secure cookies, strict browser security headers, and checks on the host and origin of each request.', 'Cloud metadata addresses are always blocked as connection targets.']],
    ['Reporting a problem', ['Send security reports to the contact address on this site. Include steps to reproduce the problem, and please allow time for a fix before sharing details.']],
  ]],
  privacy: ['Privacy notice', [
    ['What is collected', ['Account details: name, email address, company, role, sign-in times, and IP addresses in security logs.', 'Customer content: connection settings, telemetry read from connected systems, prompts, model answers, and workflows.', 'Usage data: number of investigations, tokens, and costs, which are used for limits and budgets.']],
    ['Why it is used', ['To provide the service, secure it, and send account and alert emails. Customer content isn’t used to train AI models by this service.']],
    ['Who receives it', ['Anthropic, when an organization chooses Claude.', 'The email and hosting providers used for this installation.', 'Services an organization connects, such as Slack or Google, for its own alerts.']],
    ['How long it is kept', ['Customer content is kept for the organization’s retention period, 365 days by default. Audit trail entries stay for security purposes.', 'An owner can delete the organization at any time.']],
    ['Your rights', ['Contact the service operator to access, correct, or delete your personal data. Rights depend on the law that applies to you.']],
  ]],
  terms: ['Terms of service', [
    ['The service', ['Root Cause Console helps teams investigate operational problems. It runs on your own infrastructure.']],
    ['AI output', ['AI answers can be wrong or incomplete, and they aren’t professional advice. You remain responsible for checking them and for every action you take on your systems.']],
    ['Your responsibilities', ['Keep your credentials safe, connect only systems you’re authorized to monitor, use read-only credentials, and follow the acceptable use policy.']],
    ['Costs', ['AI provider charges are your responsibility.']],
    ['Availability and liability', ['The service is provided as is, without a guarantee of uninterrupted operation. To the extent the law allows, liability is limited as set out in your contract.']],
    ['Governing law', ['These terms follow the law named in your contract with the service operator.']],
  ]],
  'acceptable-use': ['Acceptable use policy', [
    ['Don’t', ['Connect systems you aren’t authorized to access.', 'Use the service to attack, scan, or probe other people’s systems.', 'Send secrets or regulated data to an AI provider unless your organization allows it.', 'Send unwanted messages through alert channels.']],
    ['If rules are broken', ['Your administrator can remove access.']],
  ]],
};

export function legal(container, params) {
  const doc = LEGAL[params.id] || LEGAL.security;
  clear(container, h('div', { class: 'page' }, h('article', { class: 'legal' },
    h('h1', {}, doc[0]),
    live.info?.legal_reviewed ? null : h('p', { class: 'legal-draft', role: 'note' }, 'Draft. Have a lawyer review this before relying on it.'),
    doc[1].map(([title, items]) => [h('h2', {}, title), h('ul', {}, items.map((item) => h('li', {}, item)))]))));
  return () => {};
}
