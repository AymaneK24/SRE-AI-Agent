// Dependencies: the service dependency map, drawn left to right from callers to what they depend on.
// A broken workload is red; the likely cause of other problems wears a ring, and the calls that lead to it turn red.
import { api, clear, h, live } from './core.js';

const SVG = 'http://www.w3.org/2000/svg';
const NODE_W = 196;
const NODE_H = 56;
const GAP_X = 96;
const GAP_Y = 22;
const REFRESH_MS = 15000;

function s(tag, attrs, ...children) {
  const element = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key.startsWith('on') && typeof value === 'function') element.addEventListener(key.slice(2).toLowerCase(), value);
    else element.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    element.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return element;
}

function short(text, max) {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

function rate(value) {
  if (value === null || value === undefined) return '';
  if (value >= 100) return `${Math.round(value)} req/s`;
  if (value >= 1) return `${value.toFixed(1)} req/s`;
  return `${(value * 60).toFixed(1)} req/min`;
}

// Layout ------------------------------------------------------------------------------
// Layered: every workload sits one column right of the furthest caller, so calls read left to right.
// Calls that loop back are drawn but don't push columns. Rows are ordered to keep lines short.
function layout(nodes, edges) {
  const out = new Map(nodes.map((node) => [node.id, []]));
  const into = new Map(nodes.map((node) => [node.id, []]));
  for (const edge of edges) {
    out.get(edge.from).push(edge.to);
    into.get(edge.to).push(edge.from);
  }
  const back = new Set();
  const state = new Map();
  const visit = (id) => {
    state.set(id, 1);
    for (const next of out.get(id)) {
      if (state.get(next) === 1) back.add(`${id}>${next}`);
      else if (!state.get(next)) visit(next);
    }
    state.set(id, 2);
  };
  const ordered = [...nodes].sort((a, b) => into.get(a.id).length - into.get(b.id).length || a.name.localeCompare(b.name));
  for (const node of ordered) if (!state.get(node.id)) visit(node.id);
  const column = new Map();
  const columnOf = (id, seen = new Set()) => {
    if (column.has(id)) return column.get(id);
    if (seen.has(id)) return 0;
    seen.add(id);
    const callers = into.get(id).filter((from) => !back.has(`${from}>${id}`));
    const value = callers.length ? Math.max(...callers.map((from) => columnOf(from, seen) + 1)) : 0;
    column.set(id, value);
    return value;
  };
  for (const node of nodes) columnOf(node.id);
  // What depends on nothing, such as databases, queues and outside services, lines up in the last column.
  const last = Math.max(0, ...column.values());
  for (const node of nodes) if (!out.get(node.id).length && into.get(node.id).length) column.set(node.id, last);
  const columns = [];
  for (const node of nodes) (columns[column.get(node.id)] ||= []).push(node.id);
  for (const ids of columns) ids?.sort((a, b) => key(a).localeCompare(key(b)));
  function key(id) {
    const node = nodes.find((item) => item.id === id);
    return `${node.namespace || '~'}/${node.name}`;
  }
  // Barycenter sweeps: each workload moves toward the middle of what it connects to.
  const position = new Map();
  const index = () => columns.forEach((ids) => ids?.forEach((id, row) => position.set(id, row)));
  index();
  for (let sweep = 0; sweep < 6; sweep += 1) {
    const forward = sweep % 2 === 0;
    const range = forward ? columns.map((_, i) => i) : columns.map((_, i) => columns.length - 1 - i);
    for (const c of range) {
      const ids = columns[c];
      if (!ids) continue;
      const centre = (id) => {
        const near = forward ? into.get(id) : out.get(id);
        const known = near.filter((other) => position.has(other) && column.get(other) !== c);
        return known.length ? known.reduce((sum, other) => sum + position.get(other), 0) / known.length : position.get(id);
      };
      ids.sort((a, b) => centre(a) - centre(b));
      ids.forEach((id, row) => position.set(id, row));
    }
  }
  const tallest = Math.max(1, ...columns.map((ids) => ids?.length || 0));
  const placed = new Map();
  columns.forEach((ids, c) => {
    if (!ids) return;
    const offset = ((tallest - ids.length) * (NODE_H + GAP_Y)) / 2;
    ids.forEach((id, row) => placed.set(id, { x: c * (NODE_W + GAP_X), y: offset + row * (NODE_H + GAP_Y) }));
  });
  return { placed, back, width: columns.length * (NODE_W + GAP_X) - GAP_X, height: tallest * (NODE_H + GAP_Y) - GAP_Y };
}

export function mount(container, params) {
  let data = null;
  let selected = params.focus || null;
  let view = { x: 0, y: 0, k: 1 };
  let fitted = false;
  let disposed = false;
  let lastLoad = 0;
  let focusPoint = null;
  const filters = { cluster: '', namespace: '', search: '', problemsOnly: false, isolated: false };

  const svg = s('svg', { class: 'map-svg', role: 'img', 'aria-label': 'Service dependency map' });
  const stage = s('g');
  svg.append(
    s('defs', {},
      ['config', 'observed', 'failing'].map((kind) => s('marker', { id: `arrow-${kind}`, viewBox: '0 0 10 10', refX: '9', refY: '5', markerWidth: '7', markerHeight: '7', orient: 'auto-start-reverse' },
        s('path', { d: 'M0 0 10 5 0 10z', class: `map-arrow ${kind}` })))),
    stage);
  const canvas = h('div', { class: 'map-canvas' }, svg,
    h('div', { class: 'map-zoom' },
      h('button', { type: 'button', class: 'button compact', 'aria-label': 'Zoom in', onClick: () => zoomBy(1.25) }, '+'),
      h('button', { type: 'button', class: 'button compact', 'aria-label': 'Zoom out', onClick: () => zoomBy(0.8) }, '−'),
      h('button', { type: 'button', class: 'button compact', onClick: () => fit() }, 'Fit')));
  const side = h('aside', { class: 'map-side', 'aria-live': 'polite' });
  const note = h('div');
  const toolbar = h('div', { class: 'map-toolbar' });
  const legend = h('div', { class: 'map-legend' },
    h('span', {}, h('i', { class: 'legend-line observed' }), 'Seen in traces or a mesh'),
    h('span', {}, h('i', { class: 'legend-line config' }), 'From Kubernetes configuration'),
    h('span', {}, h('i', { class: 'legend-line failing' }), 'Failing calls, or the path to the likely cause'),
    h('span', {}, h('i', { class: 'legend-node cause' }), 'Likely cause'),
    h('span', {}, h('i', { class: 'legend-node critical' }), 'Has a problem'));
  const body = h('div', { class: 'map-body' }, h('p', { class: 'muted' }, 'Loading the map'));

  clear(container, h('div', { class: 'page' },
    h('h1', { class: 'page-title' }, 'What depends on what.'),
    h('p', { class: 'page-lede' }, 'Your services and the calls between them, built from Kubernetes and, when you have them, traces or a service mesh. When several things break at once, the map shows which one the others depend on.'),
    note, toolbar, body));

  // Data ------------------------------------------------------------------------------
  async function load(force = false) {
    if (!force && Date.now() - lastLoad < REFRESH_MS) return;
    lastLoad = Date.now();
    try {
      data = await api('/api/dependencies');
    } catch (error) {
      if (!disposed) clear(body, h('p', { class: 'error-note' }, error.message));
      return;
    }
    if (disposed) return;
    render();
  }

  function visible() {
    const byId = new Map(data.nodes.map((node) => [node.id, node]));
    const linked = new Set(data.edges.flatMap((edge) => [edge.from, edge.to]));
    const search = filters.search.trim().toLowerCase();
    let ids = new Set(data.nodes.filter((node) => {
      if (filters.cluster && node.cluster_id !== null && String(node.cluster_id) !== filters.cluster) return false;
      if (filters.namespace && node.namespace !== filters.namespace) return false;
      if (!filters.isolated && !linked.has(node.id) && !node.problems.length) return false;
      return true;
    }).map((node) => node.id));
    // Outside hosts and traced services have no namespace: keep those linked to a shown workload.
    for (const edge of data.edges) {
      if (ids.has(edge.from) && !byId.get(edge.to).namespace) ids.add(edge.to);
      if (ids.has(edge.to) && !byId.get(edge.from).namespace) ids.add(edge.from);
    }
    if (filters.problemsOnly) {
      const keep = new Set();
      for (const id of ids) {
        if (!byId.get(id).problems.length) continue;
        keep.add(id);
        for (const edge of data.edges) {
          if (edge.from === id && ids.has(edge.to)) keep.add(edge.to);
          if (edge.to === id && ids.has(edge.from)) keep.add(edge.from);
        }
      }
      ids = keep;
    }
    if (search) {
      const hits = [...ids].filter((id) => `${byId.get(id).namespace}/${byId.get(id).name}`.toLowerCase().includes(search));
      const keep = new Set(hits);
      for (const edge of data.edges) {
        if (keep.has(edge.from) && ids.has(edge.to)) keep.add(edge.to);
        if (hits.includes(edge.to) && ids.has(edge.from)) keep.add(edge.from);
      }
      ids = keep;
    }
    const nodes = data.nodes.filter((node) => ids.has(node.id));
    const edges = data.edges.filter((edge) => ids.has(edge.from) && ids.has(edge.to));
    return { nodes, edges, byId };
  }

  // Edges on a symptom's path to its likely cause.
  function causePaths(nodes) {
    const byName = new Map(nodes.map((node) => [node.namespace ? `${node.namespace}/${node.name}` : node.name, node.id]));
    const marked = new Set();
    for (const node of nodes) {
      for (const problem of node.problems) {
        const path = problem.caused_by?.path || [];
        for (let i = 0; i < path.length - 1; i += 1) {
          const a = byName.get(path[i]);
          const b = byName.get(path[i + 1]);
          if (a && b) marked.add(`${a}>${b}`);
        }
      }
    }
    return marked;
  }

  // Drawing ---------------------------------------------------------------------------
  function render() {
    renderNote();
    renderToolbar();
    if (!data.nodes.length) {
      clear(body, h('div', { class: 'empty-state' },
        h('p', {}, h('b', {}, 'Nothing on the map yet.')),
        h('p', { class: 'muted' }, 'Connect a Kubernetes cluster in Connections. The console reads each workload’s Services and configuration to find what it calls, and adds calls seen in traces or a service mesh when your Prometheus has them.'),
        h('p', {}, h('a', { href: '/docs/dependency-map' }, 'How the dependency map works'))));
      return;
    }
    const { nodes, edges, byId } = visible();
    if (!body.contains(canvas)) clear(body, h('div', { class: 'map-layout' }, canvas, side), legend);
    if (!nodes.length) {
      clear(stage);
      clear(side, h('p', { class: 'muted' }, 'No workload matches these filters.'));
      return;
    }
    const { placed, back, width, height } = layout(nodes, edges);
    const onPath = causePaths(nodes);
    const near = new Set();
    if (selected && placed.has(selected)) {
      near.add(selected);
      for (const edge of edges) {
        if (edge.from === selected) near.add(edge.to);
        if (edge.to === selected) near.add(edge.from);
      }
    } else if (selected && !byId.has(selected)) {
      selected = null;
    }
    const edgeLayer = s('g', { class: 'map-edges' });
    const nodeLayer = s('g', { class: 'map-nodes' });
    for (const edge of edges) {
      const a = placed.get(edge.from);
      const b = placed.get(edge.to);
      const failing = (edge.error_ratio || 0) >= 0.05 || onPath.has(`${edge.from}>${edge.to}`);
      const kind = failing ? 'failing' : edge.observed ? 'observed' : 'config';
      let d;
      if (back.has(`${edge.from}>${edge.to}`) || b.x <= a.x) {
        // A call back to the left loops below both boxes.
        const low = Math.max(a.y, b.y) + NODE_H + 26;
        d = `M${a.x + NODE_W / 2} ${a.y + NODE_H} C${a.x + NODE_W / 2} ${low} ${b.x + NODE_W / 2} ${low} ${b.x + NODE_W / 2} ${b.y + NODE_H + 2}`;
      } else {
        const x1 = a.x + NODE_W;
        const y1 = a.y + NODE_H / 2;
        const x2 = b.x - 4;
        const y2 = b.y + NODE_H / 2;
        const bend = Math.max(40, (x2 - x1) / 2);
        d = `M${x1} ${y1} C${x1 + bend} ${y1} ${x2 - bend} ${y2} ${x2} ${y2}`;
      }
      const dim = selected && !(edge.from === selected || edge.to === selected);
      const how = edge.sources.map((source) => data.source_labels[source] || source).join(' and ');
      const stats = edge.rps !== undefined && edge.rps !== null ? `, ${rate(edge.rps)}${edge.error_ratio ? `, ${Math.round(edge.error_ratio * 100)}% errors` : ''}` : '';
      edgeLayer.append(s('path', { d, class: `map-edge ${kind}${dim ? ' is-dim' : ''}`, 'marker-end': `url(#arrow-${kind})` },
        s('title', {}, `${label(byId.get(edge.from))} calls ${label(byId.get(edge.to))}. Known from ${how}${stats}.`)));
    }
    for (const node of nodes) {
      const at = placed.get(node.id);
      const dim = selected && !near.has(node.id);
      const classes = ['map-node', `status-${node.status}`, node.role ? `role-${node.role}` : '', node.kind === 'External' ? 'is-external' : '',
        node.kind === 'Service' ? 'is-traced' : '', node.id === selected ? 'is-selected' : '', dim ? 'is-dim' : ''].filter(Boolean).join(' ');
      const sub = node.kind === 'External' ? 'Outside service' : node.kind === 'Service' ? 'Seen in traces' : `${node.namespace} · ${node.kind}`;
      const group = s('g', {
        class: classes, transform: `translate(${at.x} ${at.y})`, tabindex: '0', role: 'button',
        'aria-label': `${label(node)}, ${node.status === 'ok' ? 'no problem' : node.role === 'cause' ? 'likely cause of other problems' : node.role === 'symptom' ? 'likely a symptom' : 'has a problem'}`,
        onClick: (event) => { event.stopPropagation(); select(node.id); },
        onKeydown: (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); select(node.id); } },
      },
      node.role === 'cause' ? s('rect', { class: 'map-ring', x: -6, y: -6, width: NODE_W + 12, height: NODE_H + 12, rx: 15 }) : null,
      s('rect', { class: 'map-box', width: NODE_W, height: NODE_H, rx: 11 }),
      s('rect', { class: 'map-bar', width: 5, height: NODE_H - 16, x: 8, y: 8, rx: 2.5 }),
      s('text', { class: 'map-name', x: 22, y: 24 }, short(node.name, 21)),
      s('text', { class: 'map-sub', x: 22, y: 42 }, short(sub, 27)),
      node.role === 'cause' ? s('g', { transform: `translate(${NODE_W - 92} -12)` }, s('rect', { class: 'map-tag cause', width: 86, height: 20, rx: 10 }), s('text', { class: 'map-tag-text', x: 43, y: 14, 'text-anchor': 'middle' }, 'Likely cause')) : null,
      node.role === 'symptom' ? s('g', { transform: `translate(${NODE_W - 70} -10)` }, s('rect', { class: 'map-tag symptom', width: 64, height: 18, rx: 9 }), s('text', { class: 'map-tag-text', x: 32, y: 13, 'text-anchor': 'middle' }, 'symptom')) : null,
      s('title', {}, label(node)));
      nodeLayer.append(group);
    }
    clear(stage, edgeLayer, nodeLayer);
    stage.dataset.width = width;
    stage.dataset.height = height;
    // Where to look first on a small screen: the likely cause, or else the first workload with a problem.
    const first = nodes.find((node) => node.role === 'cause') || nodes.find((node) => node.problems.length);
    focusPoint = first ? placed.get(first.id) : null;
    if (!fitted) fit(selected && placed.get(selected), true);
    else apply();
    renderSide(byId);
  }

  function label(node) {
    return node.namespace ? `${node.namespace}/${node.name}` : node.name;
  }

  function renderNote() {
    const parts = [];
    if (!data.observed && data.nodes.length) {
      parts.push(h('p', { class: 'map-note' }, 'These links come from Kubernetes configuration: what each workload is set up to call, not proof that it calls it now. ',
        'Add the service graph from Tempo or the OpenTelemetry collector, or Istio or Linkerd metrics, to see real calls with their rates and errors. ',
        h('a', { href: '/docs/dependency-map' }, 'How')));
    }
    clear(note, parts);
  }

  function renderToolbar() {
    if (toolbar.childElementCount) return;
    const clusters = data.clusters || [];
    const namespaces = [...new Set(data.nodes.map((node) => node.namespace).filter(Boolean))].sort();
    const search = h('input', { class: 'input', type: 'search', placeholder: 'Find a workload', 'aria-label': 'Find a workload',
      onInput: () => { filters.search = search.value; fitted = false; render(); } });
    const namespace = h('select', { class: 'input', 'aria-label': 'Namespace', onChange: () => { filters.namespace = namespace.value; fitted = false; render(); } },
      h('option', { value: '' }, 'All namespaces'), namespaces.map((name) => h('option', { value: name }, name)));
    const cluster = clusters.length > 1 ? h('select', { class: 'input', 'aria-label': 'Cluster', onChange: () => { filters.cluster = cluster.value; fitted = false; render(); } },
      h('option', { value: '' }, 'All clusters'), clusters.map((item) => h('option', { value: String(item.id) }, item.name))) : null;
    const problems = h('input', { type: 'checkbox', onChange: () => { filters.problemsOnly = problems.checked; fitted = false; render(); } });
    const isolated = h('input', { type: 'checkbox', onChange: () => { filters.isolated = isolated.checked; fitted = false; render(); } });
    clear(toolbar, search, cluster, namespace,
      h('label', { class: 'check' }, problems, 'Only problems and their neighbours'),
      h('label', { class: 'check' }, isolated, 'Show workloads with no known links'));
  }

  // The side panel ----------------------------------------------------------------------
  function renderSide(byId) {
    const node = selected && byId.get(selected);
    if (!node) {
      const causes = data.nodes.filter((item) => item.role === 'cause');
      const broken = data.nodes.filter((item) => item.problems.length);
      clear(side,
        h('h2', { class: 'map-side-title' }, causes.length ? 'Likely causes' : broken.length ? 'Workloads with problems' : 'All clear'),
        causes.length || broken.length
          ? h('ul', { class: 'map-list' }, (causes.length ? causes : broken).map((item) => h('li', {},
            h('button', { type: 'button', class: 'link-button', onClick: () => select(item.id) }, label(item)),
            h('span', { class: 'small muted' }, causes.length ? `${item.problems[0]?.affects?.length || 0} other problems depend on it` : item.problems[0]?.title || ''))))
          : h('p', { class: 'muted' }, 'No workload on the map has a problem right now.'),
        h('p', { class: 'small muted' }, `${data.nodes.length} workloads and services, ${data.edges.length} links. Select one to see what it calls and what calls it.`));
      return;
    }
    const callsOut = data.edges.filter((edge) => edge.from === node.id);
    const callsIn = data.edges.filter((edge) => edge.to === node.id);
    const link = (edge, other) => h('li', {},
      h('button', { type: 'button', class: 'link-button', onClick: () => select(other.id) }, label(other),
        other.status !== 'ok' ? h('span', { class: `dot ${other.status}`, 'aria-label': 'has a problem' }) : null),
      h('span', { class: 'small muted' }, [
        edge.sources.map((source) => data.source_labels[source] || source).join(' and '),
        edge.rps !== undefined && edge.rps !== null ? ` · ${rate(edge.rps)}` : '',
        edge.error_ratio ? ` · ${Math.round(edge.error_ratio * 100)}% errors` : '',
        edge.hosts?.length ? ` · ${edge.hosts.join(', ')}` : '',
      ].join('')));
    clear(side,
      h('div', { class: 'map-side-head' },
        h('h2', { class: 'map-side-title' }, label(node)),
        h('button', { type: 'button', class: 'button compact', onClick: () => select(null) }, 'Close')),
      h('p', { class: 'small muted' }, [node.kind === 'External' ? 'Outside service' : node.kind === 'Service' ? 'Service seen in traces' : node.kind, node.cluster, (node.services || []).length ? `Services: ${node.services.join(', ')}` : ''].filter(Boolean).join(' · ')),
      node.problems.length ? h('ul', { class: 'map-problems' }, node.problems.map((problem) => h('li', { class: `map-problem ${problem.severity}` },
        h('b', {}, problem.title),
        problem.caused_by ? h('p', { class: 'small' }, 'Likely caused by ',
          h('button', { type: 'button', class: 'link-button', onClick: () => select(problem.caused_by.node) }, problem.caused_by.title),
          `, which it depends on: ${(problem.caused_by.path || []).join(' → ')}.`) : null,
        problem.affects?.length ? h('p', { class: 'small' }, `Also failing because of it: ${problem.affects.join(', ')}.`) : null,
        h('a', { class: 'button compact', href: `#/investigate?${new URLSearchParams({ prompt: `${problem.title}. Find the root cause.`, focus: problem.fingerprint })}` }, 'Investigate'))))
        : h('p', { class: 'map-ok' }, h('span', { class: 'dot ok' }), 'No problem right now.'),
      h('h3', { class: 'map-side-sub' }, `Calls (${callsOut.length})`),
      callsOut.length ? h('ul', { class: 'map-list' }, callsOut.map((edge) => link(edge, byId.get(edge.to)))) : h('p', { class: 'small muted' }, 'Nothing known.'),
      h('h3', { class: 'map-side-sub' }, `Called by (${callsIn.length})`),
      callsIn.length ? h('ul', { class: 'map-list' }, callsIn.map((edge) => link(edge, byId.get(edge.from)))) : h('p', { class: 'small muted' }, 'Nothing known.'));
  }

  function select(id) {
    selected = id;
    const query = id ? `?focus=${encodeURIComponent(id)}` : '';
    history.replaceState(null, '', `#/dependencies${query}`);
    render();
  }

  // Pan and zoom ------------------------------------------------------------------------
  function apply() {
    stage.setAttribute('transform', `translate(${view.x} ${view.y}) scale(${view.k})`);
  }

  function fit(around, opening = false) {
    const box = svg.getBoundingClientRect();
    const width = Number(stage.dataset.width || 0);
    const height = Number(stage.dataset.height || 0);
    if (!box.width || !width) return;
    const pad = 36;
    // The canvas is as tall as the map needs at full width, between 360 pixels and two thirds of the screen.
    const natural = Math.min(1.1, (box.width - pad * 2) / width) * height + pad * 2 + 40;
    canvas.style.height = `${Math.round(Math.max(360, Math.min(natural, window.innerHeight * 0.68)))}px`;
    side.style.maxHeight = canvas.style.height;
    const tall = svg.getBoundingClientRect().height;
    view.k = Math.min(1.1, Math.max(0.25, Math.min((box.width - pad * 2) / width, (tall - pad * 2) / height)));
    view.x = (box.width - width * view.k) / 2;
    view.y = (tall - height * view.k) / 2;
    // Too small to read, as on a phone: open on the likely cause instead. Fit still shows everything.
    if (!around && opening && view.k < 0.55 && focusPoint) around = focusPoint;
    if (around) {
      view.k = Math.max(view.k, 0.8);
      view.x = box.width / 2 - (around.x + NODE_W / 2) * view.k;
      view.y = tall / 2 - (around.y + NODE_H / 2) * view.k;
    }
    fitted = true;
    apply();
  }

  function zoomBy(factor, cx, cy) {
    const box = svg.getBoundingClientRect();
    const x = cx ?? box.width / 2;
    const y = cy ?? box.height / 2;
    const k = Math.min(2.5, Math.max(0.2, view.k * factor));
    view.x = x - ((x - view.x) * k) / view.k;
    view.y = y - ((y - view.y) * k) / view.k;
    view.k = k;
    apply();
  }

  let drag = null;
  svg.addEventListener('pointerdown', (event) => {
    if (event.target.closest('.map-node')) return;
    drag = { x: event.clientX, y: event.clientY, vx: view.x, vy: view.y, moved: false };
    svg.setPointerCapture(event.pointerId);
  });
  svg.addEventListener('pointermove', (event) => {
    if (!drag) return;
    view.x = drag.vx + event.clientX - drag.x;
    view.y = drag.vy + event.clientY - drag.y;
    drag.moved = drag.moved || Math.abs(event.clientX - drag.x) + Math.abs(event.clientY - drag.y) > 4;
    apply();
  });
  svg.addEventListener('pointerup', () => {
    if (drag && !drag.moved && selected) select(null);
    drag = null;
  });
  svg.addEventListener('wheel', (event) => {
    event.preventDefault();
    const box = svg.getBoundingClientRect();
    zoomBy(event.deltaY < 0 ? 1.12 : 0.89, event.clientX - box.left, event.clientY - box.top);
  }, { passive: false });

  const unsubscribe = live.subscribe((type) => { if (type === 'context') load(); });
  load(true);
  return () => {
    disposed = true;
    unsubscribe();
  };
}
