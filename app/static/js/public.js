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
          h('a', { class: 'button primary large', href: open ? '#/signup' : '#/signin' }, open ? 'Create an account' : 'Sign in'),
          open ? null : h('span', { class: 'muted' }, 'Need access? Ask your administrator for an invitation.'))),
      h('div', {}, chart, h('p', { class: 'illustration-note' }, 'Illustration of an incident, not real data.'))),
    h('section', { class: 'section', 'aria-labelledby': 'how' },
      h('h2', { id: 'how', style: 'margin-bottom:18px' }, 'How it works'),
      h('ol', { class: 'steps-grid' },
        h('li', {}, h('b', {}, '1. Connect'), h('p', {}, 'Add Prometheus, CloudWatch, or Azure Monitor, plus optional Loki logs, Kubernetes, and deployment events.')),
        h('li', {}, h('b', {}, '2. Watch'), h('p', {}, 'The console tracks alerts, targets, and unusual signals in real time, and records what changed.')),
        h('li', {}, h('b', {}, '3. Investigate'), h('p', {}, 'Ask a question, or start automatically on critical alerts. Use Ollama on your own server, or Gemini or Claude over the internet.')),
        h('li', {}, h('b', {}, '4. Decide'), h('p', {}, 'Read the checked answer and its remediation workflow. Your team carries out any fix.')))),
    h('section', { class: 'section two-col' },
      h('div', {},
        h('h2', { style: 'margin-bottom:12px' }, 'Built to be checked'),
        h('ul', { class: 'plain-list' },
          h('li', {}, h('b', {}, 'Evidence checks. '), 'Cited numbers are compared with your data, and confidence is lowered when they don’t match.'),
          h('li', {}, h('b', {}, 'Other explanations. '), 'The model is asked to weigh a competing cause, and “unknown” is an accepted answer.'),
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
            : h('li', {}, h('b', {}, 'Each company is separate. '), 'Accounts, data, and credentials are isolated per organization. ', h('a', { href: '#/legal/security' }, 'Read the security policy.')))))));
  return () => {};
}

const LOGO = '<svg viewBox="0 0 32 32" width="22" height="22" aria-hidden="true"><path d="M3 24l8-4 6 3 7-13" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/><circle cx="26" cy="7" r="4" fill="var(--critical)"/></svg>';

// About 3.5 cm tall on a desktop: the product, the documentation, the policies, and how to reach someone.
export function footer() {
  const info = live.info || {};
  const signedIn = Boolean(live.me && !live.me.must_change_password);
  const column = (title, ...links) => h('div', { class: 'footer-col' }, h('h2', {}, title), h('ul', {}, links.filter(Boolean).map((link) => h('li', {}, link))));
  const logo = h('span', { class: 'footer-logo' });
  logo.innerHTML = LOGO;
  return h('footer', { class: 'site-footer', id: 'site-footer' }, h('div', { class: 'site-footer-inner' },
    h('div', { class: 'footer-brand' },
      h('a', { class: 'footer-name', href: '#/' }, logo, 'Root Cause Console'),
      h('p', {}, 'Finds why production broke. It reads your systems and never changes them.'),
      h('p', { class: 'footer-fine' }, `© ${new Date().getFullYear()} Root Cause Console`, info.operator ? ` · Operated by ${info.operator}` : '')),
    signedIn ? column('Documentation',
      h('a', { href: '/docs' }, 'Documentation'),
      h('a', { href: '/docs/whats-new' }, 'What’s new'),
      h('a', { href: '/docs/customer-setup' }, 'Customer setup')) : null,
    h('nav', { class: 'footer-col footer-legal', 'aria-label': 'Policies' }, h('h2', {}, 'Legal'), h('ul', {},
      [['privacy', 'Privacy'], ['terms', 'Terms'], ['cookies', 'Cookies'], ['acceptable-use', 'Acceptable use'], ['security', 'Security'], ['legal-notice', 'Legal notice']]
        .map(([slug, label]) => h('li', {}, h('a', { href: `#/legal/${slug}` }, label))))),
    column('Support',
      info.contact_email ? h('a', { href: `mailto:${info.contact_email}` }, 'Contact us') : null,
      signedIn ? h('a', { href: '/docs/troubleshooting' }, 'Troubleshooting') : null,
      h('a', { href: '#/legal/security' }, 'Report a vulnerability'))));
}

// Every form that collects personal data links to the privacy notice.
function privacyNote(prefix = 'How your details are used: ') {
  return h('p', { class: 'after-form muted' }, prefix, h('a', { href: '#/legal/privacy' }, 'privacy notice'), '.');
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
    live.info?.signup_open ? h('p', { class: 'after-form' }, 'New here? ', h('a', { href: '#/signup' }, 'Create an account.')) : h('p', { class: 'after-form' }, 'Need access? Ask your administrator for an invitation.'),
    privacyNote()));
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
    h('h1', {}, 'Create your organization'),
    h('p', { class: 'lede' }, 'You’ll be its owner, and can invite your team afterwards.'),
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
    h('p', { class: 'after-form' }, h('a', { href: '#/signin' }, 'Back to sign in')), privacyNote()));
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
    h('form', { class: 'stack', onSubmit: submit }, field('Your name', name), field('Password', password, 'At least 12 characters.'), result, h('button', { class: 'button primary', type: 'submit' }, 'Create account')),
    h('p', { class: 'after-form muted' }, 'By creating your account, you agree to the ', h('a', { href: '#/legal/terms' }, 'terms'), ' and ',
      h('a', { href: '#/legal/acceptable-use' }, 'acceptable use policy'), '. Read how your details are used in the ', h('a', { href: '#/legal/privacy' }, 'privacy notice'), '.')));
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
      h('button', { class: 'button primary', type: 'submit' }, forced ? 'Continue' : 'Change password')),
    placeholder ? privacyNote() : null));
  return () => {};
}

// The software's creator. The operator below is whoever runs this installation, usually the company that installed it.
const CREATOR = { name: 'Aymane KENBOUCH', email: 'aymanekenbouch@gmail.com', github: 'https://github.com/AymaneK24' };
const LEGAL_UPDATED = '8 November 2026';

function mail(address) { return h('a', { href: `mailto:${address}` }, address); }
function link(href, text) { return h('a', { href, target: '_blank', rel: 'noopener noreferrer' }, text || href); }

function legalDocs(info) {
  const operator = info.operator || 'The organization that runs this installation';
  // A function, so each use gets its own link: a page element can only appear in one place.
  const contact = () => (info.contact_email
    ? ['Contact ', operator === 'The organization that runs this installation' ? 'it' : operator, ' at ', mail(info.contact_email), '.']
    : ['Ask your administrator for its contact details.']);
  return {
    privacy: ['Privacy notice', [
      ['Who is responsible', [
        [operator, ' runs this installation of Root Cause Console and decides how it is used, so it is responsible for your personal data here. It is the controller under the GDPR, and the responsable du traitement under Moroccan law 09-08. ', ...contact()],
        ['The software is made by ', CREATOR.name, '. This installation runs on infrastructure chosen by its operator and sends no data to the software’s creator.'],
      ]],
      ['What is collected', [
        'Your account: name, work email address, role, a hash of your password (never the password itself), and, with single sign-on, your provider’s user ID.',
        'Security records: sign-in times, IP addresses, and browser type for active sessions, and an audit trail of security-relevant actions.',
        'Work content: the connections your administrators set up, monitoring data read from them, your questions to the AI model, its answers, runbooks, reports, and notes.',
        'Usage: number of investigations, tokens, cost, and time, used for budgets and limits.',
        'Alert recipients: email addresses an administrator adds to alert channels.',
        'Nothing is collected for advertising or marketing, and no analytics or tracking tools are used.',
      ]],
      ['Why it is used', [
        'To run the service for your organization: sign-in, roles, investigations, alerts, and reports. The legal basis is your organization’s legitimate interest in operating its systems, or the contract it has with you.',
        'To keep it secure: detecting failed sign-ins, ending sessions, and the audit trail. The legal basis is legitimate interest in security.',
        'To send emails you need: invitations, password resets, security notices, and alerts. There are no marketing emails.',
        'Your data isn’t sold, isn’t used for advertising, and isn’t used by this software to train AI models. No decision with legal effect on you is made automatically.',
      ]],
      ['Who receives it', [
        'AI providers, only if an administrator turns them on and accepts sending data to them: Google (Gemini) or Anthropic (Claude) receive questions and the live monitoring context, which can include host names, service names, and log lines. Their own terms decide what they keep. With Ollama, data stays on your organization’s server.',
        'The email server the operator configured, to deliver emails.',
        'Services your administrators connect for alerts, such as Slack, Microsoft Teams, or Google Chat, and your single sign-on provider.',
        'The operator’s hosting provider, which stores the server and its backups.',
      ]],
      ['Transfers outside your country', [
        'Google and Anthropic may process data in other countries, including the United States. Before turning them on, the operator is responsible for the safeguards the law requires, such as standard contractual clauses under the GDPR, or the CNDP’s authorization for transfers under law 09-08.',
      ]],
      ['How long it is kept', [
        'Investigations, prompts, and monitoring events: for the retention period your organization sets, 365 days by default, then deleted automatically.',
        'Sessions: until you sign out, or for the session length your organization sets, 12 hours by default.',
        'Your account: until an administrator removes it. Audit trail entries, which can include your name and email, are kept longer for security and accountability.',
        'Backups follow the operator’s own backup policy.',
      ]],
      ['Your rights', [
        'You can ask to access, correct, or delete your personal data, to object to or restrict its use, and, under the GDPR, to receive it in a portable form. Rights depend on the law that applies to you.',
        ['To use them, ask your administrator or the operator. ', ...contact(), ' An administrator can remove your account and delete your private investigations.'],
        ['You can complain to a data protection authority. In Morocco, that is the CNDP (', link('https://www.cndp.ma', 'cndp.ma'), '). In the European Union, it is the authority in your country.'],
      ]],
      ['Cookies', [['Only one cookie is used, to keep you signed in. See the ', h('a', { href: '#/legal/cookies' }, 'cookie policy'), '.']]],
      ['Children', ['This is a work tool for adults. It isn’t meant for anyone under 18, and accounts are only created by invitation from an organization.']],
      ['Changes', [`Last updated ${LEGAL_UPDATED}. The operator will tell users about important changes.`]],
    ]],
    cookies: ['Cookie policy', [
      ['What is used', [
        '“rcc_session”, a cookie that keeps you signed in. It is strictly necessary, set by this site only, can’t be read by scripts, and lasts until you sign out or your session ends, 12 hours by default.',
        '“rcc-theme”, kept in your browser’s storage, remembers the light or dark theme you chose.',
        '“rcc-hero-view”, “rcc-range”, and “rcc-refresh”, kept in your browser’s storage, remember the overview view, chart time range, and refresh interval you chose.',
      ]],
      ['What isn’t used', [
        'No analytics, advertising, or tracking cookies, no third-party cookies, and no social media pixels. Fonts and scripts are served by this installation itself, so opening a page contacts no other site.',
      ]],
      ['Why there is no cookie banner', [
        'Consent is required for cookies that aren’t needed for the service. The session cookie is strictly necessary to sign you in, and the stored preferences only remember choices you made, so neither needs consent.',
      ]],
      ['How to control them', [
        'You can delete them in your browser’s settings at any time. Without the session cookie, you can’t stay signed in. Without the preferences, the pages use their defaults.',
      ]],
      ['Changes', [`Last updated ${LEGAL_UPDATED}.`]],
    ]],
    terms: ['Terms of service', [
      ['Who these terms are between', [
        [operator, ' (“the operator”) runs this installation and gives you access to it. Root Cause Console is software made by ', CREATOR.name, ' (“the creator”). If your organization has its own agreement with the operator, that agreement takes priority over these terms.'],
      ]],
      ['The software', [
        [`Root Cause Console is proprietary software. Copyright 2026 ${CREATOR.name}. All rights reserved.`],
        'It is used under a license from the creator. You may use it as part of your work for your organization. Don’t copy, resell, sublicense, or reverse engineer it, except as the law allows or a written license from the creator permits.',
      ]],
      ['Who may use it', [
        'People of 18 or older, invited by an organization, for their work. Each account belongs to one person: don’t share it.',
      ]],
      ['Your account', [
        'Keep your password and sessions safe, and tell your administrator straight away if you think someone else has used your account. Your administrators decide your role and can change or remove it.',
      ]],
      ['Your organization’s content', [
        'Your organization keeps ownership of its data and content. The operator processes it only to run the service, as described in the privacy notice. The creator receives none of it.',
      ]],
      ['AI output', [
        'AI answers can be wrong, incomplete, or out of date, and they aren’t professional advice. Check the evidence before acting. You remain responsible for every action you and your team take on your systems. The software only reads data, and nothing it suggests runs by itself.',
      ]],
      ['Other services', [
        'AI providers, email servers, alert channels, and sign-in providers that your administrators connect have their own terms and charges. Charges from AI providers are your organization’s responsibility.',
      ]],
      ['Acceptable use', [['Follow the ', h('a', { href: '#/legal/acceptable-use' }, 'acceptable use policy'), '.']]],
      ['Suspension and closing accounts', [
        'Administrators can suspend or remove accounts at any time, for example when someone leaves or breaks these terms. Your organization can stop using the service and delete the installation. Data is then deleted as the privacy notice describes.',
      ]],
      ['No warranty', [
        'The software is provided as it is, without guarantees that it will be uninterrupted, error-free, or fit for a particular purpose, to the extent the law allows.',
      ]],
      ['Liability', [
        'To the extent the law allows, neither the operator nor the creator is liable for indirect or consequential losses, or for losses caused by acting on AI output without checking it. Nothing in these terms limits liability that can’t be limited by law, or your rights as a consumer where they apply.',
      ]],
      ['Law and disputes', [
        'These terms follow the law and courts named in your organization’s agreement with the operator. Without one, the law of the country where the operator is established applies.',
      ]],
      ['Changes and contact', [[`Last updated ${LEGAL_UPDATED}. The operator will tell users about important changes. `, ...contact()]]],
    ]],
    'acceptable-use': ['Acceptable use policy', [
      ['Use it for', ['Monitoring and investigating systems your organization operates, or is authorized to operate.']],
      ['Don’t', [
        'Connect systems you aren’t authorized to monitor, or use credentials that aren’t yours to use.',
        'Use the service to attack, scan, or probe systems, or point connections at addresses to reach networks you aren’t allowed to reach.',
        'Try to bypass roles, sign-in, rate limits, budgets, or other security controls, or to read other people’s private investigations.',
        'Send secrets, health data, payment card data, or other regulated data to an AI provider unless your organization allows it.',
        'Use the AI features against Google’s or Anthropic’s usage policies, when those providers are turned on.',
        'Send spam, harassment, or unlawful content through alert channels, runbooks, or reports.',
        'Share your account, or copy, resell, or reverse engineer the software beyond what the law or your license allows.',
      ]],
      ['Report problems', ['Report security problems privately to the operator, with steps to reproduce them, and give them time to fix it before sharing details.']],
      ['If rules are broken', ['Administrators can suspend or remove access, and the operator can take further steps under its agreements and the law.']],
      ['Changes', [`Last updated ${LEGAL_UPDATED}.`]],
    ]],
    security: ['Security policy', [
      ['Access', ['Every page and API call needs a signed-in account, apart from public pages and health checks.', 'Roles limit what each person can see and do: viewer, engineer, admin, and owner. Investigations are private to the person who started them, until they share them. Admins and owners can read all of them.', 'Passwords need at least 12 characters and are stored with Argon2. Repeated failed sign-ins stop the device trying and slow further attempts, and they can’t lock the account’s owner out.', 'Organizations can require single sign-on, shorten sessions, and limit which email domains can be invited.']],
      ['Your installation', ['Each company has its own server and database, so other companies can’t reach its data through this software. A private server, reached over VPN or the company network, is recommended.', 'Whoever administers the server and database can read the data stored there, so limit that access.']],
      ['Credentials', ['Passwords, API keys, tokens, and webhook addresses for connections are encrypted at rest and never returned to the browser.', 'Obvious credentials are removed from telemetry before it is logged or sent to a model. This uses pattern matching, so it can miss some secrets.']],
      ['Read-only operation', ['The service only reads from monitored systems, so use credentials limited to read access.', 'AI models have read-only tools. Remediation workflows are instructions for people, and the service has no feature that runs them.', 'Alert messages only inform, and contain no controls that act on your systems.']],
      ['AI data handling', ['When an organization uses Gemini or Claude, its prompts and live context, including host and service names, are sent to Google or Anthropic. An admin must allow each one first, and an owner can block them by policy.', 'When an organization uses its own Ollama server, this data goes only to that server.', 'Telemetry text is treated as untrusted data. Prompt injection can’t be fully prevented, so verify answers before acting.']],
      ['Logging and retention', ['Each prompt is stored exactly as sent. Settings changes and sign-ins go in a hash-chained audit trail, which owners and admins can export.', 'Each organization sets its own retention period.']],
      ['Network and application', ['HTTPS, secure cookies, strict browser security headers, and checks on the host and origin of each request.', 'Cloud metadata addresses are always blocked as connection targets.']],
      ['Reporting a problem', [['Send security reports to the operator. ', ...contact(), ' Include steps to reproduce the problem, and please allow time for a fix before sharing details. Problems in the software itself can also go to its creator at ', mail(CREATOR.email), '.']]],
    ]],
    'legal-notice': ['Legal notice', [
      ['This installation', [
        info.operator ? ['Operated by ', info.operator, '.'] : 'Operated by the organization that installed it. Its administrator can show its name here with RCC_OPERATOR_NAME.',
        info.operator_address ? ['Address: ', info.operator_address, '.'] : null,
        info.contact_email ? ['Contact: ', mail(info.contact_email), '.'] : 'Ask your administrator for the operator’s contact details.',
        'Hosted on infrastructure chosen by the operator.',
      ].filter(Boolean)],
      ['The software', [
        [`Root Cause Console, copyright 2026 ${CREATOR.name}. All rights reserved. Proprietary software, used under a license from its creator.`],
        ['Questions about the software, licenses, or bugs: ', mail(CREATOR.email), ', or ', link(CREATOR.github, 'GitHub'), '. For anything about your account or your data on this installation, contact the operator instead.'],
      ]],
      ['Third-party components', [
        ['Fonts: Schibsted Grotesk and JetBrains Mono, under the SIL Open Font License 1.1. Libraries: marked (MIT), DOMPurify (Apache 2.0 or MPL 2.0), uPlot (MIT). ', link('/vendor/THIRD-PARTY-LICENSES.txt', 'Read their licenses'), '.'],
      ]],
    ]],
  };
}

export function legal(container, params) {
  const docs = legalDocs(live.info || {});
  const doc = docs[params.id] || docs.privacy;
  clear(container, h('div', { class: 'page' }, h('article', { class: 'legal' },
    h('h1', {}, doc[0]),
    live.info?.legal_reviewed ? null : h('p', { class: 'legal-draft', role: 'note' }, 'Draft. Have a lawyer review this before relying on it.'),
    doc[1].map(([title, items]) => [h('h2', {}, title), h('ul', {}, items.map((item) => h('li', {}, item)))]))));
  return () => {};
}
