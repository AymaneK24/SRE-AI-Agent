// App entry: routing, header, theme, and account state.
import { api, can, clear, closeDialog, connectLive, disconnectLive, h, live, modelName, refreshAi, setAuthHandler } from './core.js';
import { mount as mountAccount } from './account.js';
import { mount as mountConnections } from './connections.js';
import { mount as mountInvestigate } from './investigate.js';
import { mount as mountLogs } from './logs.js';
import { mount as mountOrganization } from './organization.js';
import { mount as mountOverview } from './overview.js';
import { mount as mountProjects } from './projects.js';
import { accept, changePassword, footer, forgot, landing, legal, reset, signin, signup, verify } from './public.js';
import { mount as mountResults } from './results.js';
import { mount as mountWorkflows } from './workflows.js';

// Each page's lowest role. The server enforces the same limits on every API call, so this only decides what to show.
const APP_ROUTES = {
  overview: ['viewer', mountOverview],
  projects: ['viewer', mountProjects],
  investigate: ['viewer', (container, params) => (can('engineer') ? mountInvestigate(container, params) : mountResults(container, params))],
  workflows: ['viewer', mountWorkflows],
  logs: ['engineer', mountLogs],
  connections: ['admin', mountConnections],
  organization: ['admin', mountOrganization],
  account: ['viewer', mountAccount],
  'change-password': ['viewer', changePassword],
};
const NAV = [['overview', 'Overview'], ['projects', 'Projects'], ['investigate', 'Investigate'], ['workflows', 'Workflows'], ['logs', 'Logs'], ['connections', 'Connections'], ['organization', 'Organization']];
const PUBLIC_ROUTES = { '': landing, signin, signup, verify, forgot, reset, accept, legal };
const view = document.getElementById('view');
let unmount = null;

function parseHash() {
  const raw = location.hash.replace(/^#\/?/, '');
  const [path, queryString] = raw.split('?');
  const [name, id] = path.split('/');
  const params = { id };
  new URLSearchParams(queryString || '').forEach((value, key) => { params[key] = value; });
  return { name: name || '', params };
}

function renderNav(name) {
  const nav = document.getElementById('site-nav');
  const account = document.getElementById('account-area');
  const me = live.me;
  const links = me && !me.must_change_password ? NAV.filter(([id]) => can(APP_ROUTES[id][0])) : [];
  clear(nav, links.map(([id, label]) => h('a', { href: `#/${id}`, 'aria-current': id === name ? 'page' : null }, id === 'investigate' && !can('engineer') ? 'Investigations' : label)),
    links.length ? h('a', { href: '/docs' }, 'Docs') : null);
  if (me) {
    clear(account,
      h('a', { class: 'account-name', href: '#/account', title: `${me.email}, ${me.role}. Your account.` }, me.name),
      h('button', { class: 'button compact', type: 'button', onClick: signOut }, 'Sign out'));
  } else {
    clear(account,
      name === 'signin' ? null : h('a', { class: 'button compact', href: '#/signin' }, 'Sign in'),
      live.info?.signup_open && name !== 'signup' ? h('a', { class: 'button compact primary', href: '#/signup' }, 'Start free trial') : null);
  }
}

async function signOut() {
  await api('/api/auth/logout', { method: 'POST', body: {} }).catch(() => {});
  live.me = null;
  disconnectLive();
  location.hash = '#/';
  location.reload();
}

function route() {
  let { name, params } = parseHash();
  const me = live.me;
  if (me && me.must_change_password && name !== 'legal') name = 'change-password';
  else if (me && (name === '' || name === 'signin' || name === 'signup')) name = 'overview';
  else if (!me && APP_ROUTES[name]) {
    name = 'signin';
    params = {};
  } else if (me && APP_ROUTES[name] && !can(APP_ROUTES[name][0])) {
    name = 'overview';
    params = {};
    history.replaceState(null, '', '#/overview');
  } else if (!APP_ROUTES[name] && !(name in PUBLIC_ROUTES)) {
    name = me ? 'overview' : '';
  }
  if (unmount) unmount();
  closeDialog();
  window.scrollTo(0, 0);
  renderNav(name);
  document.getElementById('live-chip').hidden = !me || me.must_change_password;
  const chip = document.getElementById('model-chip');
  chip.hidden = !me || me.must_change_password || !can('engineer');
  if (can('admin')) chip.setAttribute('href', '#/connections');
  else chip.removeAttribute('href');
  const titles = { overview: 'Overview', account: 'Your account', investigate: can('engineer') ? 'Investigate' : 'Investigation results', workflows: 'Workflows', logs: 'Logs', connections: 'Connections', organization: 'Organization', projects: 'Projects', signin: 'Sign in', signup: 'Start your trial', legal: 'Policies' };
  document.title = titles[name] ? `${titles[name]}, Root Cause Console` : 'Root Cause Console';
  const render = APP_ROUTES[name]?.[1] || PUBLIC_ROUTES[name];
  unmount = render(view, params);
}

function renderLiveChip() {
  const chip = document.getElementById('live-chip');
  const context = live.context;
  const status = context?.health?.status;
  let tone = 'neutral';
  let text = 'Connecting';
  if (!live.connected && context) { tone = 'critical'; text = 'Connection lost'; }
  else if (status === 'ok') { tone = 'ok'; text = 'Live'; }
  else if (status === 'partial') { tone = 'warning'; text = 'Some connections down'; }
  else if (status === 'unreachable') { tone = 'critical'; text = 'Connections not answering'; }
  else if (status === 'not_configured') { text = 'Nothing connected'; }
  clear(chip, h('span', { class: `dot ${tone}` }), text);
}

function renderModelChip() {
  document.getElementById('model-chip').textContent = live.ai ? modelName(live.ai) : '';
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem('rcc-theme', theme); } catch (error) { /* The choice only lasts this session. */ }
  renderThemeButton();
  live.emit('theme', theme);
}

function currentTheme() {
  return document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
}

function renderThemeButton() {
  const button = document.getElementById('theme-toggle');
  const dark = currentTheme() === 'dark';
  button.setAttribute('aria-label', dark ? 'Switch to light theme' : 'Switch to dark theme');
  button.innerHTML = dark
    ? '<svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true"><circle cx="10" cy="10" r="3.6" fill="currentColor"/><path d="M10 1.8v2.4M10 15.8v2.4M1.8 10h2.4M15.8 10h2.4M4.2 4.2l1.7 1.7M14.1 14.1l1.7 1.7M15.8 4.2l-1.7 1.7M5.9 14.1l-1.7 1.7" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>'
    : '<svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true"><path d="M16.5 12.4A7 7 0 0 1 7.6 3.5a7 7 0 1 0 8.9 8.9Z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>';
}

async function start() {
  setAuthHandler(() => {
    live.me = null;
    disconnectLive();
    location.hash = '#/signin';
    route();
  });
  renderThemeButton();
  document.getElementById('theme-toggle').addEventListener('click', () => setTheme(currentTheme() === 'dark' ? 'light' : 'dark'));
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { renderThemeButton(); live.emit('theme'); });
  live.subscribe((type) => {
    if (type === 'context' || type === 'connection') renderLiveChip();
    if (type === 'ai') renderModelChip();
    if (type === 'me') renderNav(parseHash().name);
  });
  live.info = await api('/api/public/info').catch(() => ({}));
  live.me = await api('/api/auth/me').catch(() => null);
  document.getElementById('site-footer').replaceWith(footer());
  if (live.me && !live.me.must_change_password) {
    connectLive();
    refreshAi().catch(() => {});
    live.context = await api('/api/context').catch(() => null);
    renderLiveChip();
  }
  window.addEventListener('hashchange', route);
  route();
}

start();
