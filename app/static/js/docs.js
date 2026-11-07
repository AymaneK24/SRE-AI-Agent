// The documentation space: its own layout, separate from the platform. The logo, or going back, returns to it.
import { h, clear } from './core.js';

const nav = document.getElementById('docs-nav');
const article = document.getElementById('doc-article');
const toc = document.getElementById('docs-toc');
const search = document.getElementById('docs-search');
const results = document.getElementById('docs-results');
const menu = document.getElementById('docs-menu');
const DEFAULT_PAGE = 'introduction';
let sections = [];
let current = null;

async function get(path) {
  const response = await fetch(path, { credentials: 'same-origin', headers: { Accept: 'application/json' } });
  if (response.status === 401 || response.status === 403) throw Object.assign(new Error('signin'), { signin: true });
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || 'The page couldn’t be loaded.');
  return response.json();
}

function slugFromPath() {
  const match = location.pathname.match(/^\/docs\/?([a-z0-9-]*)/);
  return (match && match[1]) || DEFAULT_PAGE;
}

function anchorId(text, used) {
  let id = text.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'section';
  while (used.has(id)) id += '-';
  used.add(id);
  return id;
}

// Theme ----------------------------------------------------------------------
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
document.getElementById('theme-toggle').addEventListener('click', () => {
  const theme = currentTheme() === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem('rcc-theme', theme); } catch (error) { /* The choice lasts only for this page. */ }
  renderThemeButton();
});
renderThemeButton();

// Navigation -----------------------------------------------------------------
function renderNav() {
  clear(nav, sections.map((section) => h('div', { class: 'docs-nav-section' },
    h('h2', {}, section.title),
    h('ul', {}, section.pages.map((page) => h('li', {},
      h('a', { href: `/docs/${page.slug}`, 'aria-current': page.slug === current ? 'page' : null, 'data-doc': page.slug }, page.title)))))));
}

function setMenu(open) {
  document.body.classList.toggle('docs-nav-open', open);
  menu.setAttribute('aria-expanded', String(open));
}
menu.addEventListener('click', () => setMenu(!document.body.classList.contains('docs-nav-open')));

// Page -----------------------------------------------------------------------
function enhance(container) {
  const used = new Set();
  const outline = [];
  for (const heading of container.querySelectorAll('h2, h3')) {
    heading.id = anchorId(heading.textContent, used);
    heading.append(h('a', { class: 'docs-anchor', href: `#${heading.id}`, 'aria-label': `Link to ${heading.textContent}` }, '#'));
    outline.push({ id: heading.id, text: heading.textContent.replace(/#$/, ''), level: heading.tagName === 'H3' ? 3 : 2 });
  }
  for (const block of container.querySelectorAll('pre')) {
    const code = block.querySelector('code');
    const copy = h('button', { class: 'docs-copy', type: 'button', 'aria-label': 'Copy this code' }, 'Copy');
    copy.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(code ? code.textContent : block.textContent);
        copy.textContent = 'Copied';
      } catch (error) { copy.textContent = 'Select and copy'; }
      setTimeout(() => { copy.textContent = 'Copy'; }, 1600);
    });
    block.classList.add('docs-pre');
    block.append(copy);
  }
  // A quote that starts with a bold word is a callout: Security, Note, Warning, or Tip.
  for (const quote of container.querySelectorAll('blockquote')) {
    const lead = quote.querySelector('p > strong:first-child');
    const kind = lead ? lead.textContent.replace(/[:.]$/, '').toLowerCase() : '';
    if (['security', 'note', 'warning', 'tip'].includes(kind)) quote.classList.add('callout', `callout-${kind}`);
  }
  for (const table of container.querySelectorAll('table')) {
    table.replaceWith(h('div', { class: 'docs-table' }, table.cloneNode(true)));
  }
  for (const link of container.querySelectorAll('a[href]')) {
    const href = link.getAttribute('href');
    if (/^https?:\/\//i.test(href)) {
      link.setAttribute('target', '_blank');
      link.setAttribute('rel', 'noopener noreferrer');
    } else if (href.startsWith('/docs/')) {
      link.dataset.doc = href.slice(6).split('#')[0];
    }
  }
  return outline;
}

function renderToc(outline) {
  clear(toc, outline.length > 1 ? [h('h2', {}, 'On this page'), h('ul', {}, outline.map((item) => h('li', { class: item.level === 3 ? 'sub' : null }, h('a', { href: `#${item.id}` }, item.text))))] : null);
}

function signInPanel() {
  clear(nav);
  clear(toc);
  clear(article, h('div', { class: 'docs-signin' },
    h('h1', {}, 'Sign in to read the documentation'),
    h('p', {}, 'The documentation is part of your organization’s console, so it’s only shown to people who are signed in.'),
    h('a', { class: 'button primary', href: '/#/signin' }, 'Sign in')));
}

async function show(slug, { push = false, hash = '' } = {}) {
  if (push) history.pushState({}, '', `/docs/${slug}${hash}`);
  current = slug;
  renderNav();
  setMenu(false);
  clear(article, h('p', { class: 'muted' }, 'Loading'));
  try {
    const page = await get(`/api/docs/${encodeURIComponent(slug)}`);
    const body = h('div', { class: 'markdown docs-content' });
    body.innerHTML = window.DOMPurify.sanitize(window.marked.parse(page.markdown, { gfm: true, breaks: false }),
      { USE_PROFILES: { html: true }, FORBID_TAGS: ['style', 'form', 'iframe'], FORBID_ATTR: ['style'] });
    const outline = enhance(body);
    clear(article,
      h('p', { class: 'docs-section-label' }, page.section),
      h('h1', { class: 'docs-title' }, page.title),
      body,
      h('nav', { class: 'docs-pager', 'aria-label': 'Previous and next pages' },
        page.prev ? h('a', { href: `/docs/${page.prev.slug}`, 'data-doc': page.prev.slug, class: 'prev' }, h('small', {}, 'Previous'), page.prev.title) : h('span'),
        page.next ? h('a', { href: `/docs/${page.next.slug}`, 'data-doc': page.next.slug, class: 'next' }, h('small', {}, 'Next'), page.next.title) : h('span')));
    renderToc(outline);
    document.title = `${page.title} · Root Cause Docs`;
    const target = hash || location.hash;
    const anchor = target && document.getElementById(decodeURIComponent(target.slice(1)));
    if (anchor) anchor.scrollIntoView();
    else window.scrollTo(0, 0);
    if (push) article.focus({ preventScroll: true });
  } catch (error) {
    if (error.signin) return signInPanel();
    clear(article, h('div', { class: 'docs-signin' }, h('h1', {}, 'Page not found'), h('p', {}, error.message),
      h('a', { class: 'button', href: `/docs/${DEFAULT_PAGE}`, 'data-doc': DEFAULT_PAGE }, 'Go to the introduction')));
    renderToc([]);
  }
}

// Links between pages stay inside the docs space, so the back button walks back through them.
document.addEventListener('click', (event) => {
  const link = event.target.closest('a[data-doc]');
  if (!link || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  const [, hash = ''] = link.getAttribute('href').split('#');
  if (link.dataset.doc === current && hash) {
    history.pushState({}, '', `#${hash}`);
    document.getElementById(hash)?.scrollIntoView();
    return;
  }
  hideResults();
  show(link.dataset.doc, { push: true, hash: hash ? `#${hash}` : '' });
});
window.addEventListener('popstate', () => {
  const slug = slugFromPath();
  if (slug !== current) show(slug);
  else if (location.hash) document.getElementById(decodeURIComponent(location.hash.slice(1)))?.scrollIntoView();
});

// Search ---------------------------------------------------------------------
let searchTimer = null;
let searchCount = 0;
function hideResults() {
  results.hidden = true;
  search.setAttribute('aria-expanded', 'false');
}
async function runSearch() {
  const query = search.value.trim();
  if (query.length < 2) return hideResults();
  const mine = ++searchCount;
  try {
    const data = await get(`/api/docs/search?q=${encodeURIComponent(query)}`);
    if (mine !== searchCount) return;
    clear(results, data.results.length
      ? data.results.map((item) => h('a', { class: 'docs-result', role: 'option', href: `/docs/${item.slug}`, 'data-doc': item.slug },
        h('b', {}, item.title), h('small', {}, item.section), h('span', {}, item.snippet)))
      : h('p', { class: 'docs-result-empty' }, 'Nothing matches. Try fewer or different words.'));
    results.hidden = false;
    search.setAttribute('aria-expanded', 'true');
  } catch (error) {
    if (error.signin) signInPanel();
  }
}
search.addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(runSearch, 180); });
search.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') { hideResults(); search.blur(); }
  if (event.key === 'Enter') { event.preventDefault(); results.querySelector('a')?.click(); }
  if (event.key === 'ArrowDown') { event.preventDefault(); results.querySelector('a')?.focus(); }
});
results.addEventListener('keydown', (event) => {
  const items = [...results.querySelectorAll('a')];
  const at = items.indexOf(document.activeElement);
  if (event.key === 'ArrowDown' && at < items.length - 1) { event.preventDefault(); items[at + 1].focus(); }
  if (event.key === 'ArrowUp') { event.preventDefault(); (at > 0 ? items[at - 1] : search).focus(); }
  if (event.key === 'Escape') { hideResults(); search.focus(); }
});
document.addEventListener('click', (event) => { if (!event.target.closest('.docs-search')) hideResults(); });
document.addEventListener('keydown', (event) => {
  if (event.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName)) {
    event.preventDefault();
    search.focus();
  }
});

// Start ----------------------------------------------------------------------
(async () => {
  try {
    sections = (await get('/api/docs')).sections;
  } catch (error) {
    if (error.signin) return signInPanel();
    clear(article, h('p', { class: 'error-note' }, error.message));
    return;
  }
  show(slugFromPath());
})();
