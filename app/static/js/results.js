// Results for people who read but don't investigate: each question and its answer, with nothing about how it ran.
import { api, clear, dateTime, h, renderMarkdown, statusBadge, timeAgo } from './core.js';

export function mount(container, params) {
  let selected = params.id ? Number(params.id) : null;
  let items = [];
  let disposed = false;
  const search = h('input', { class: 'input', type: 'search', placeholder: 'Search results', 'aria-label': 'Search results' });
  const listHost = h('nav', { class: 'workflow-list', 'aria-label': 'Results' });
  const detailHost = h('div');
  let debounce;

  clear(container, h('div', { class: 'page' },
    h('h1', { class: 'page-title' }, 'Investigation results'),
    h('p', { class: 'page-lede' }, 'What your team asked, and what it found. Engineers share results here, and automatic investigations of critical alerts appear by themselves.'),
    h('div', { class: 'workflow-layout' }, h('div', { class: 'stack' }, search, listHost), detailHost)));

  function renderList() {
    clear(listHost, items.length ? items.map((item) => h('a', {
      href: `#/investigate/${item.id}`, 'aria-current': item.id === selected ? 'true' : null,
      onClick: (event) => { event.preventDefault(); select(item.id); },
    }, item.title, h('small', {}, `${item.trigger === 'auto' ? 'Automatic, ' : ''}${timeAgo(item.updated_at)}`)))
      : h('p', { class: 'empty' }, search.value ? 'No results match this search.' : 'Nothing has been shared yet.'));
  }

  async function select(id) {
    selected = id;
    history.replaceState(null, '', `#/investigate/${id}`);
    renderList();
    try {
      const result = await api(`/api/results/${id}`);
      if (disposed) return;
      clear(detailHost,
        h('div', { class: 'conv-head' }, h('h2', {}, result.title), statusBadge(result.status)),
        result.root_cause_note ? h('section', { class: 'result-cause' }, h('h3', {}, 'Confirmed root cause'), h('p', {}, result.root_cause_note)) : null,
        result.exchanges.length ? null : h('p', { class: 'empty' }, 'No question has been asked here yet.'),
        h('div', { class: 'messages' }, result.exchanges.flatMap((exchange) => {
          const answer = h('div', { class: 'markdown' });
          if (exchange.answer) renderMarkdown(answer, exchange.answer);
          return [
            h('div', { class: 'message-user' }, exchange.question),
            h('article', {},
              h('p', { class: 'message-meta' }, dateTime(exchange.asked_at)),
              exchange.answer ? answer : h('p', { class: 'muted' }, exchange.status === 'running' ? 'The answer is still being written.' : 'This investigation finished without an answer.')),
          ];
        })));
    } catch (error) {
      clear(detailHost, h('p', { class: 'error-note' }, error.message));
    }
  }

  async function load() {
    try {
      const data = await api(`/api/results?${new URLSearchParams({ q: search.value, limit: '100' })}`);
      if (disposed) return;
      items = data.items;
      renderList();
      if (!selected && items.length) select(items[0].id);
      else if (selected) select(selected);
      else clear(detailHost);
    } catch (error) {
      clear(listHost, h('p', { class: 'error-note' }, error.message));
    }
  }

  search.addEventListener('input', () => { clearTimeout(debounce); debounce = setTimeout(load, 250); });
  load();
  return () => { disposed = true; clearTimeout(debounce); };
}
