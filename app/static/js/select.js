// One dropdown for the whole console. Every native <select> gets a button and a list drawn in the
// console's own style, so the open list looks the same in every browser and theme.
//
// The native <select> stays in the page, hidden, and remains the source of truth: code keeps reading
// and setting select.value, and choosing an item fires the usual change event. The button follows
// any change to the select: its value, its options, or being disabled.

const VALUE = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value');
const INDEX = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'selectedIndex');
let open = null;
let counter = 0;

function nameOf(select) {
  if (select.getAttribute('aria-label')) return select.getAttribute('aria-label');
  const label = select.closest('label');
  if (!label) return '';
  const text = [...label.childNodes].filter((node) => !node.contains?.(select) && !node.classList?.contains('select'))
    .map((node) => node.textContent).join(' ').trim();
  return text.split('\n')[0].trim();
}

function sync(select) {
  const parts = select._rccSelect;
  if (!parts) return;
  const chosen = select.options[INDEX.get.call(select)];
  parts.value.textContent = chosen ? chosen.textContent : '';
  parts.button.disabled = select.disabled;
  parts.button.hidden = select.hidden;
  const name = nameOf(select);
  parts.button.setAttribute('aria-label', name ? `${name}: ${parts.value.textContent}` : parts.value.textContent);
  parts.button.title = parts.value.textContent;
}

function close(focusButton = false) {
  if (!open) return;
  const { select, list } = open;
  list.remove();
  select._rccSelect.button.setAttribute('aria-expanded', 'false');
  if (focusButton) select._rccSelect.button.focus();
  open = null;
}

function place(button, list) {
  const box = button.getBoundingClientRect();
  const width = Math.max(box.width, 160);
  list.style.minWidth = `${width}px`;
  list.style.left = `${Math.min(box.left, window.innerWidth - width - 8)}px`;
  const below = window.innerHeight - box.bottom;
  const height = Math.min(list.scrollHeight, 320);
  if (below < height + 12 && box.top > below) {
    list.style.top = '';
    list.style.bottom = `${window.innerHeight - box.top + 6}px`;
  } else {
    list.style.bottom = '';
    list.style.top = `${box.bottom + 6}px`;
  }
}

function choose(select, index) {
  close(true);
  if (index === INDEX.get.call(select)) return;
  INDEX.set.call(select, index);
  sync(select);
  select.dispatchEvent(new Event('input', { bubbles: true }));
  select.dispatchEvent(new Event('change', { bubbles: true }));
}

function show(select) {
  close();
  const { button } = select._rccSelect;
  const list = document.createElement('ul');
  list.className = 'select-list';
  list.setAttribute('role', 'listbox');
  list.id = `rcc-list-${++counter}`;
  list.setAttribute('aria-label', nameOf(select) || 'Options');
  list.tabIndex = -1;
  const current = INDEX.get.call(select);
  const items = [];
  [...select.options].forEach((option, index) => {
    const item = document.createElement('li');
    item.className = 'select-item';
    item.id = `${list.id}-${index}`;
    item.setAttribute('role', 'option');
    item.setAttribute('aria-selected', String(index === current));
    item.textContent = option.textContent;
    if (option.disabled) item.setAttribute('aria-disabled', 'true');
    item.addEventListener('mousedown', (event) => event.preventDefault());
    item.addEventListener('click', () => { if (!option.disabled) choose(select, index); });
    item.addEventListener('mousemove', () => highlight(index));
    items.push(item);
    list.append(item);
  });
  let active = current >= 0 ? current : 0;
  function highlight(index) {
    active = index;
    items.forEach((item, at) => item.classList.toggle('is-active', at === index));
    list.setAttribute('aria-activedescendant', items[index]?.id || '');
    items[index]?.scrollIntoView({ block: 'nearest' });
  }
  let typed = '';
  let typedAt = 0;
  list.addEventListener('keydown', (event) => {
    const step = (from, by) => {
      let index = from;
      for (let tries = 0; tries < items.length; tries += 1) {
        index = Math.max(0, Math.min(items.length - 1, index + by));
        if (!select.options[index].disabled) return index;
      }
      return from;
    };
    if (event.key === 'ArrowDown') highlight(step(active, 1));
    else if (event.key === 'ArrowUp') highlight(step(active, -1));
    else if (event.key === 'Home') highlight(step(-1, 1));
    else if (event.key === 'End') highlight(step(items.length, -1));
    else if (event.key === 'Enter' || event.key === ' ') { if (!select.options[active]?.disabled) choose(select, active); }
    else if (event.key === 'Escape') close(true);
    else if (event.key === 'Tab') { close(); return; }
    else if (event.key.length === 1) {
      typed = Date.now() - typedAt > 700 ? event.key.toLowerCase() : typed + event.key.toLowerCase();
      typedAt = Date.now();
      const found = items.findIndex((item) => item.textContent.toLowerCase().startsWith(typed));
      if (found >= 0) highlight(found);
    } else return;
    event.preventDefault();
  });
  // A modal dialog sits above the page, so a list opened from inside one must live in it.
  (select.closest('dialog[open]') || document.body).append(list);
  place(button, list);
  highlight(active);
  button.setAttribute('aria-expanded', 'true');
  button.setAttribute('aria-controls', list.id);
  open = { select, list };
  list.focus({ preventScroll: true });
}

function enhance(select) {
  if (select._rccSelect || select.multiple || select.size > 1 || select.dataset.native !== undefined) return;
  const button = document.createElement('button');
  button.type = 'button';
  button.className = `select-button ${select.className}`.trim();
  if (select.getAttribute('style')) button.setAttribute('style', select.getAttribute('style'));
  button.setAttribute('aria-haspopup', 'listbox');
  button.setAttribute('aria-expanded', 'false');
  const value = document.createElement('span');
  value.className = 'select-value';
  button.append(value);
  button.addEventListener('click', () => (open?.select === select ? close(true) : show(select)));
  button.addEventListener('keydown', (event) => {
    if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(event.key)) { event.preventDefault(); show(select); }
  });
  select._rccSelect = { button, value };
  select.classList.add('select-native');
  select.tabIndex = -1;
  select.setAttribute('aria-hidden', 'true');
  select.before(button);
  // Code that sets .value or .selectedIndex directly fires no event, so the button is told here.
  Object.defineProperty(select, 'value', { configurable: true, get() { return VALUE.get.call(this); }, set(next) { VALUE.set.call(this, next); sync(this); } });
  Object.defineProperty(select, 'selectedIndex', { configurable: true, get() { return INDEX.get.call(this); }, set(next) { INDEX.set.call(this, next); sync(this); } });
  select.addEventListener('change', () => sync(select));
  new MutationObserver(() => sync(select)).observe(select, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['disabled', 'hidden', 'selected', 'aria-label'] });
  sync(select);
}

function scan(root) {
  if (root.nodeType !== 1) return;
  if (root.tagName === 'SELECT') enhance(root);
  else root.querySelectorAll?.('select').forEach(enhance);
}

export function enhanceSelects(root = document.body) {
  scan(root);
  new MutationObserver((records) => {
    for (const record of records) record.addedNodes.forEach(scan);
    if (open && !open.select.isConnected) close();
  }).observe(root, { childList: true, subtree: true });
  document.addEventListener('mousedown', (event) => {
    if (open && !open.list.contains(event.target) && !open.select._rccSelect.button.contains(event.target)) close();
  });
  window.addEventListener('resize', () => close());
  document.addEventListener('scroll', (event) => { if (open && !open.list.contains(event.target)) close(); }, true);
}
