// Overview: current status, what changed, and Grafana-style metric panels.
import { TimeSeriesChart, stepFor } from './charts.js';
import { api, can, clear, closeDialog, confirmDialog, dialogHead, formatValue, h, icon, labelText, live, openDialog, timeAgo, toast } from './core.js';

const RANGES = [['5m', 300], ['15m', 900], ['1h', 3600], ['6h', 21600], ['24h', 86400], ['7d', 604800]];
const REFRESH = [['Off', 0], ['5s', 5], ['15s', 15], ['30s', 30], ['1m', 60]];
const UNITS = [['short', 'Number'], ['percent', 'Percent'], ['seconds', 'Seconds'], ['bytes', 'Bytes'], ['req_per_second', 'Requests per second'], ['per_second', 'Per second']];
const GOLDEN = [['latency', 'Latency', 'How long requests take'], ['traffic', 'Traffic', 'How much is being asked'], ['errors', 'Errors', 'How many requests fail'], ['saturation', 'Saturation', 'How full the system is']];
const GROUP_RANK = { Services: 0, Kubernetes: 1, Hosts: 2, 'Your panels': 3, Prometheus: 4 };

function loadPref(key, fallback) {
  try {
    const value = Number(localStorage.getItem(key));
    return localStorage.getItem(key) === null || Number.isNaN(value) ? fallback : value;
  } catch (error) { return fallback; }
}
function loadText(key) {
  try { return localStorage.getItem(key); } catch (error) { return null; }
}
function savePref(key, value) {
  try { localStorage.setItem(key, String(value)); } catch (error) { /* Preference only lasts this session. */ }
}

function plural(count, one, many) { return `${count} ${count === 1 ? one : many}`; }

// Connections the overview can choose between. Older servers only send the main log store and cluster.
const METRIC_KINDS = new Set(['prometheus', 'cloudwatch', 'azure']);
function metricSources(context) {
  return (context?.sources || []).filter((item) => METRIC_KINDS.has(item.kind) && item.id !== undefined && item.id !== null);
}
function connectionsOf(context, kind) {
  if (Array.isArray(context?.connections)) return context.connections.filter((item) => item.kind === kind);
  const only = context?.integrations?.[kind];
  return only ? [{ ...only, kind, main: true }] : [];
}
function mainId(items) {
  return (items.find((item) => item.main) || items[0])?.id;
}
const signalKey = (sourceId, id) => `${sourceId ?? ''}:${id}`;

// A compact pill that names which connection a part of the page shows. It only appears when there's a choice.
function sourcePicker(label, options, value, onChange) {
  const select = h('select', { 'aria-label': label, onChange: () => onChange(select.value) },
    options.map(([optionValue, text]) => h('option', { value: optionValue, selected: String(optionValue) === String(value) }, text)));
  return h('label', { class: 'source-pick' }, h('span', { class: 'source-pick-label' }, label), h('span', { class: 'source-pick-box' }, select));
}

export function mount(container) {
  let range = loadPref('rcc-range', 3600);
  let refresh = loadPref('rcc-refresh', 15);
  let panels = [];
  let panelCharts = new Map();
  let heroChart = null;
  let heroKey = '';
  let timer = null;
  let fetching = false;

  const heroTitle = h('h1', {});
  const heroCopy = h('p', { class: 'hero-copy' });
  const heroActions = h('div', { class: 'hero-actions' });
  const heroLabel = h('p', { class: 'hero-chart-title' });
  const heroChartHost = h('div');
  const clusterHost = h('div', { class: 'cluster-panel' });
  const logsHost = h('div', { class: 'cluster-panel' });
  const metricsHost = h('div', {}, heroLabel, heroChartHost);
  const viewSwitch = h('div', { class: 'segments hero-switch', role: 'group', 'aria-label': 'What to show here' });
  let heroView = loadText('rcc-hero-view');
  // Which connection each part shows. Empty means automatic: problems first, then the main connection.
  const picks = { metrics: loadText('rcc-pick-metrics') || '', logs: loadText('rcc-pick-logs') || '', cluster: loadText('rcc-pick-cluster') || '', golden: loadText('rcc-pick-golden') || '', panels: loadText('rcc-pick-panels') || '' };
  const setPick = (name, value) => { picks[name] = value; savePref(`rcc-pick-${name}`, value); };
  const heroPickHost = h('span', { class: 'hero-pick' });
  const goldenPickHost = h('span', { class: 'head-actions' });
  const panelsPickHost = h('span');
  const staleBanner = h('div', { hidden: true });
  const attention = h('div');
  const changes = h('div');
  const jobs = h('div');
  const golden = h('div', { class: 'golden-grid' });
  const goldenSection = h('section', { class: 'section', 'aria-labelledby': 'golden-title' }, h('div', { class: 'section-head' }, h('h2', { id: 'golden-title' }, 'Golden signals'), goldenPickHost), golden);
  const panelGrid = h('div', { class: 'panels' });
  const rangeControl = h('div', { class: 'segments', role: 'group', 'aria-label': 'Time range' });
  const refreshControl = h('div', { class: 'segments', role: 'group', 'aria-label': 'Refresh interval' });

  clear(container, h('div', { class: 'page' },
    staleBanner,
    h('section', { class: `hero${can('engineer') ? '' : ' hero-solo'}`, 'aria-labelledby': 'hero-title' },
      h('div', {}, heroTitle, heroCopy, heroActions),
      h('div', { class: 'hero-panel' }, h('div', { class: 'hero-controls' }, viewSwitch, heroPickHost), can('engineer') ? metricsHost : null, clusterHost, logsHost)),
    goldenSection,
    h('section', { class: 'section status-grid' },
      h('div', {}, h('div', { class: 'section-head' }, h('h2', {}, 'Needs attention')), attention),
      h('div', {}, h('div', { class: 'section-head' }, h('h2', {}, 'What changed')), changes)),
    h('section', { class: 'section', 'aria-label': 'Metric panels' },
      h('div', { class: 'section-head' }, h('h2', {}, 'Metrics')),
      h('div', { class: 'dash-bar' },
        h('span', { class: 'control-label' }, 'Time range', rangeControl),
        h('span', { class: 'control-label' }, 'Refresh', refreshControl),
        panelsPickHost,
        h('div', { class: 'head-actions' },
          h('button', { class: 'button compact', type: 'button', onClick: () => fetchAll() }, 'Refresh now'),
          can('engineer') ? h('button', { class: 'button compact primary', type: 'button', onClick: () => editPanel() }, 'Add panel') : null)),
      panelGrid),
    can('engineer') ? h('section', { class: 'section' }, h('div', { class: 'section-head' }, h('h2', {}, 'What’s watched')),
      h('p', { class: 'small muted', style: 'margin:-6px 0 12px' }, 'Each server, endpoint, log stream, and namespace this console reads, and which connection reads it.'), jobs) : null));
  heroTitle.id = 'hero-title';

  function signalValue(signal) {
    if (typeof signal.value === 'number') return signal.value;
    const values = (signal.series || []).map((item) => item.value).filter((value) => typeof value === 'number');
    return values.length ? Math.max(...values) : null;
  }

  function renderGolden(context) {
    // Golden signals come from metrics. With only logs or a cluster connected, four "No data" tiles would only add noise.
    goldenSection.hidden = context.coverage?.has_metrics === false;
    const panels = container.querySelector('[aria-label="Metric panels"]');
    if (panels) panels.hidden = context.coverage?.has_metrics === false;
    const sources = metricSources(context);
    const names = new Map(sources.map((item) => [item.id, item.name]));
    const several = sources.length > 1 && can('engineer');
    if (several && picks.golden && !names.has(Number(picks.golden))) setPick('golden', '');
    clear(goldenPickHost, several ? sourcePicker('Source', [['', 'All sources, worst value'], ...sources.map((item) => [item.id, item.name])], picks.golden,
      (value) => { setPick('golden', value); renderGolden(live.context); }) : null);
    const signals = (context.signals || []).filter((signal) => signal.golden && signalValue(signal) !== null
      && (!several || !picks.golden || signal.source_id === Number(picks.golden)));
    golden.classList.toggle('golden-grid', Boolean(signals.length) || !context.as_of);
    if (!signals.length && context.as_of) {
      // Four "No data" tiles say less than one sentence.
      clear(golden, h('p', { class: 'small muted' }, 'No latency, traffic, error, or saturation metrics were found yet. They appear when your sources collect request, HTTP, or container metrics.'));
      return;
    }
    clear(golden, GOLDEN.map(([id, label, hint]) => {
      const worst = signals.filter((signal) => signal.golden === id)
        .sort((a, b) => (b.unit === 'percent') - (a.unit === 'percent') || signalValue(b) - signalValue(a))[0];
      const value = worst ? signalValue(worst) : null;
      const over = worst && worst.limit !== null && worst.limit !== undefined && value > worst.limit;
      return h('article', { class: `golden${over ? ' is-over' : ''}` },
        h('h3', {}, label),
        h('b', {}, worst ? formatValue(value, worst.unit) : 'No data'),
        h('p', { class: 'small muted' }, worst ? `${worst.title}${over ? `, over its limit of ${formatValue(worst.limit, worst.unit)}` : ''}` : hint),
        worst && several && !picks.golden && names.has(worst.source_id) ? h('p', { class: 'golden-source' }, names.get(worst.source_id)) : null);
    }));
  }

  function renderControls() {
    clear(rangeControl, RANGES.map(([label, value]) => h('button', {
      type: 'button', 'aria-pressed': String(value === range),
      onClick: () => { range = value; savePref('rcc-range', value); renderControls(); fetchAll(); },
    }, label)));
    clear(refreshControl, REFRESH.map(([label, value]) => h('button', {
      type: 'button', 'aria-pressed': String(value === refresh),
      onClick: () => { refresh = value; savePref('rcc-refresh', value); renderControls(); schedule(); },
    }, label)));
  }

  function schedule() {
    clearInterval(timer);
    if (refresh > 0) timer = setInterval(() => { if (!document.hidden) fetchAll(); }, refresh * 1000);
  }

  function chooseHeroSignal(context) {
    const sources = metricSources(context);
    const chosen = picks.metrics && sources.some((item) => item.id === Number(picks.metrics)) ? Number(picks.metrics) : null;
    const main = mainId(sources);
    const inScope = (sourceId) => chosen === null || sourceId === chosen;
    const signalById = new Map((context.signals || []).map((signal) => [signalKey(signal.source_id, signal.id), signal]));
    const rank = (sourceId, id) => GROUP_RANK[signalById.get(signalKey(sourceId, id))?.group] ?? 9;
    const anomalies = (context.anomalies || []).filter((item) => inScope(item.source_id))
      .sort((a, b) => rank(a.source_id, a.signal_id) - rank(b.source_id, b.signal_id));
    if (anomalies.length) {
      const item = anomalies[0];
      return { title: item.title, query: item.query, unit: item.unit, limit: item.limit, source_id: item.source_id, reason: 'Out of its normal range' };
    }
    const proms = sources.filter((item) => item.kind === 'prometheus' && item.status === 'ok' && inScope(item.id));
    const prom = proms.find((item) => item.id === main) || proms[0];
    const downOn = (context.targets?.down_list || []).map((target) => target.source_id);
    const promDown = proms.find((item) => downOn.includes(item.id)) || ((context.targets?.down || 0) > 0 && chosen === null ? prom : null);
    if (promDown) {
      return { title: 'Targets answering scrapes', query: 'up', unit: 'short', limit: null, source_id: promDown.id, reason: 'A value of 0 means the target is down' };
    }
    // Nothing is wrong: show the main source's key signal, so the chart doesn't jump to whichever source was added last.
    const preferred = (context.signals || [])
      .filter((signal) => signal.series.length && inScope(signal.source_id))
      .sort((a, b) => (a.source_id === main ? 0 : 1) - (b.source_id === main ? 0 : 1)
        || (GROUP_RANK[a.group] ?? 9) - (GROUP_RANK[b.group] ?? 9) || a.panel_priority - b.panel_priority)[0];
    if (preferred) return { title: preferred.title, query: preferred.query, unit: preferred.unit, limit: preferred.limit, source_id: preferred.source_id, reason: 'Key signal' };
    // No key signal yet: whether each target answers is always worth showing, so the view is never empty.
    if (prom) return { title: 'Targets answering scrapes', query: 'up', unit: 'short', limit: null, source_id: prom.id, reason: 'No key signal was found yet. 1 means the target is up, 0 that it is down' };
    return null;
  }

  async function fetchHero(signal) {
    const end = Math.floor(Date.now() / 1000);
    const start = end - Math.min(range, 3600);
    const step = stepFor(end - start);
    try {
      const data = await api('/api/metrics/query_range', { method: 'POST', body: { query: signal.query, start, end, step, source_id: signal.source_id } });
      heroChart?.setData(data.result, start, end, step, 'Waiting for enough samples to draw this chart.');
    } catch (error) {
      if (error.status === 502 && heroChart?.plot) return;
      if (error.status !== 401) heroChart?.showMessage(error.message, true);
    }
  }

  function renderContext(context) {
    if (!context) return;
    const health = context.health || {};
    const tone = context.headline?.tone || 'idle';
    heroTitle.textContent = context.headline?.text || '';
    heroTitle.className = `tone-${tone}`;
    const configured = health.status !== 'not_configured';
    const targets = context.targets || {};
    const anomalyCount = (context.anomalies || []).length;
    if (!configured) {
      clear(heroCopy, 'Connect Prometheus, CloudWatch, Azure Monitor, Loki, or a Kubernetes cluster. Any one is enough. This console then keeps ', h('b', {}, 'a live picture of what is running and what is broken'), ' and gives it to the AI model whenever you ask it to investigate.');
      clear(heroActions, can('admin') ? h('a', { class: 'button primary large', href: '#/connections' }, 'Add a connection') : h('span', { class: 'muted' }, 'Ask an admin to add a connection.'));
    } else if (context.as_of) {
      const watched = [...(context.sources || []).map((item) => item.name), ...Object.values(context.integrations || {}).map((item) => item.name)].filter(Boolean);
      const k8sCount = (context.problems || []).filter((item) => item.kind === 'k8s').length;
      if (!context.coverage?.has_metrics) {
        clear(heroCopy, 'Watching ', h('b', {}, watched.join(', ') || 'your connections'), `. Updated ${timeAgo(context.as_of)}`,
          k8sCount ? [', with ', h('b', {}, plural(k8sCount, 'Kubernetes problem', 'Kubernetes problems')), '.'] : '.');
      } else if (can('engineer')) {
        clear(heroCopy, 'Watching ', h('b', {}, plural(targets.total || 0, 'target', 'targets')), ' on ', h('b', {}, (context.sources || []).map((item) => item.name).join(', ') || 'your sources'),
          '. Live context version ', h('b', {}, context.version), ` updated ${timeAgo(context.as_of)} and holds `,
          h('b', {}, plural(context.coverage?.signal_count || 0, 'signal', 'signals')), anomalyCount ? `, with ${plural(anomalyCount, 'signal', 'signals')} out of range.` : '.');
      } else {
        clear(heroCopy, 'Watching ', h('b', {}, plural(targets.total || 0, 'target', 'targets')), `. Updated ${timeAgo(context.as_of)}.`, anomalyCount ? ` ${plural(anomalyCount, 'signal is', 'signals are')} out of range.` : '');
      }
      clear(heroActions,
        can('engineer') ? h('a', { class: 'button primary large', href: '#/investigate' }, 'Start an investigation') : h('a', { class: 'button large', href: '#/investigate' }, 'See investigation results'),
        can('engineer') ? h('span', { class: 'muted' }, 'The latest context goes with your prompt.') : null);
    } else {
      clear(heroCopy, health.error || 'Waiting for the first response from the metric source.');
      clear(heroActions, can('admin') ? h('a', { class: 'button large', href: '#/connections' }, 'Check the connection') : null);
    }

    const degraded = health.status === 'unreachable' || health.status === 'partial';
    staleBanner.hidden = !degraded;
    if (degraded) {
      staleBanner.className = 'stale-banner';
      staleBanner.style.marginTop = '20px';
      clear(staleBanner, h('b', {}, 'A connection isn’t answering. '), can('engineer') ? (health.error || '') : '', health.last_ok ? ` The last good data is from ${timeAgo(health.last_ok)}.` : '');
    }

    renderGolden(context);
    renderAttention(context);
    renderChanges(context.events || []);
    renderWatched(context);

    const view = chooseView(context);
    renderCluster(context, view === 'cluster');
    renderLogs(context, view === 'logs');
    metricsHost.hidden = view !== 'metrics';
    const signal = view === 'metrics' && configured && can('engineer') ? chooseHeroSignal(context) : null;
    const key = signal ? `${signal.source_id}|${signal.query}|${signal.limit}` : '';
    if (key !== heroKey) {
      heroKey = key;
      heroChart?.destroy();
      heroChart = null;
      clear(heroChartHost);
      clear(heroLabel);
      if (signal) {
        const from = metricSources(context).length > 1 ? metricSources(context).find((item) => item.id === signal.source_id)?.name : null;
        clear(heroLabel, h('b', {}, signal.title), from ? [' on ', h('b', {}, from)] : null, `. ${signal.reason}.`);
        heroChart = new TimeSeriesChart(heroChartHost, { unit: signal.unit, height: 360, limit: signal.limit, title: signal.title, showTable: false, endValue: true, ariaLabel: `${signal.title} chart` });
        heroChart.signal = signal;
        heroChart.showMessage('Loading chart');
        fetchHero(signal);
      }
    } else if (heroChart) {
      fetchHero(heroChart.signal);
    }
    // Nothing to chart: say so, rather than leaving the view empty.
    if (!signal && view === 'metrics' && can('engineer')) {
      const targets = context.targets || {};
      clear(heroLabel, h('b', {}, 'Metrics'), targets.total
        ? `: ${targets.up || 0} of ${plural(targets.total, 'target', 'targets')} answering. No key signal to chart yet.`
        : ': no key signal to chart yet. One appears when a source collects request, error, or saturation metrics.');
    }
  }

  // The right side shows one of: the cluster, a key metric chart, or log streams. Each viewer's choice is remembered.
  function chooseView(context) {
    const counts = connectionsOf(context, 'kubernetes').some((item) => item.counts) || context.cluster_counts;
    const views = [];
    if (counts) views.push(['cluster', 'Cluster']);
    if (context.coverage?.has_metrics !== false && can('engineer')) views.push(['metrics', 'Metrics']);
    if (connectionsOf(context, 'loki').some((item) => item.status === 'ok') && can('engineer')) views.push(['logs', 'Logs']);
    const names = views.map(([name]) => name);
    const view = names.includes(heroView) ? heroView : names[0] || '';
    viewSwitch.hidden = views.length < 2;
    clear(viewSwitch, views.map(([name, label]) => h('button', {
      type: 'button', 'aria-pressed': String(name === view),
      onClick: () => { heroView = name; savePref('rcc-hero-view', name); renderContext(live.context); },
    }, label)));
    renderHeroPicker(context, view);
    return view;
  }

  // The picker for the view on the right: which metric source, log store, or cluster it shows.
  function renderHeroPicker(context, view) {
    let picker = null;
    if (can('engineer') && view === 'metrics') {
      const sources = metricSources(context);
      if (sources.length > 1) {
        picker = sourcePicker('Source', [['', 'Automatic'], ...sources.map((item) => [item.id, `${item.name}${item.main ? ' (main)' : ''}`])], picks.metrics,
          (value) => { setPick('metrics', value); renderContext(live.context); });
      }
    }
    for (const [name, kind, label] of [['cluster', 'kubernetes', 'Cluster'], ['logs', 'loki', 'Log store']]) {
      const items = connectionsOf(context, kind);
      if (view === name && items.length > 1 && can('engineer')) {
        picker = sourcePicker(label, items.map((item) => [item.id, `${item.name}${item.main ? ' (main)' : ''}${item.status === 'ok' ? '' : ', not answering'}`]), chosenConnection(context, kind, name)?.id,
          (value) => { setPick(name, value); renderContext(live.context); });
      }
    }
    clear(heroPickHost, picker);
  }

  function chosenConnection(context, kind, pickName) {
    const items = connectionsOf(context, kind);
    return items.find((item) => String(item.id) === String(picks[pickName])) || items.find((item) => item.main) || items[0] || null;
  }

  function renderCluster(context, show) {
    const kube = chosenConnection(context, 'kubernetes', 'cluster');
    const counts = kube?.counts || context.cluster_counts;
    clusterHost.hidden = !show;
    if (!show) { clear(clusterHost); return; }
    const parts = [];
    if (counts) {
      parts.push(h('p', { class: 'hero-chart-title' }, h('b', {}, kube?.name || 'Kubernetes'), counts.namespaces ? `, ${plural(counts.namespaces, 'namespace', 'namespaces')}` : ''),
        h('div', { class: 'stat-row cluster-stats' },
          h('div', { class: `stat${counts.pods_running < counts.pods ? ' is-bad' : ''}` }, h('b', {}, `${counts.pods_running} / ${counts.pods}`), h('span', {}, 'Pods running and ready')),
          h('div', { class: 'stat' }, h('b', {}, `${counts.deployments_available} / ${counts.deployments}`), h('span', {}, 'Deployments fully available')),
          counts.nodes ? h('div', { class: 'stat' }, h('b', {}, `${counts.nodes_ready} / ${counts.nodes}`), h('span', {}, 'Nodes ready')) : null,
          h('div', { class: 'stat' }, h('b', {}, String(counts.restarts)), h('span', {}, 'Container restarts'))));
    }
    if (can('engineer') && kube?.warning_events?.length) {
      parts.push(h('p', { class: 'hero-chart-title cluster-subhead' }, h('b', {}, 'Latest warnings')),
        h('ul', { class: 'rows' }, kube.warning_events.slice(0, 4).map((event) => h('li', { class: 'row' }, icon('warning'),
          h('span', { class: 'row-title' }, `${event.reason}: ${event.object}`), h('span', { class: 'row-side' }, event.last_seen ? timeAgo(event.last_seen) : ''),
          h('span', { class: 'row-detail' }, event.message)))));
    }
    clear(clusterHost, parts);
  }

  function renderLogs(context, show) {
    const loki = chosenConnection(context, 'loki', 'logs');
    logsHost.hidden = !show;
    if (!show || !loki) { clear(logsHost); return; }
    if (loki.status !== 'ok') { clear(logsHost, h('p', { class: 'error-note' }, `${loki.name} isn’t answering. ${loki.error || ''}`)); return; }
    const streams = loki.streams || [];
    const total = streams.reduce((sum, row) => sum + row.lines_5m, 0);
    const errors = streams.reduce((sum, row) => sum + row.errors_5m, 0);
    clear(logsHost,
      h('p', { class: 'hero-chart-title' }, h('b', {}, loki.name || 'Loki'), `, ${plural(streams.length, 'stream', 'streams')}, last 5 minutes`),
      h('div', { class: 'stat-row cluster-stats' },
        h('div', { class: 'stat' }, h('b', {}, Math.round(total).toLocaleString()), h('span', {}, 'Lines written')),
        h('div', { class: `stat${errors ? ' is-bad' : ''}` }, h('b', {}, Math.round(errors).toLocaleString()), h('span', {}, 'Error-like lines'))),
      h('p', { class: 'hero-chart-title cluster-subhead' }, h('b', {}, 'Streams with the most errors')),
      streams.length ? h('ul', { class: 'rows' }, streams.slice(0, 5).map((row) => h('li', { class: 'row' }, icon(row.errors_5m > 0 ? 'warning' : 'ok'),
        h('span', { class: 'row-title' }, streamName(row.job)), h('span', { class: 'row-side' }, `${Math.round(row.errors_5m)} errors in ${Math.round(row.lines_5m)} lines`))))
        : h('p', { class: 'small muted' }, 'No stream wrote anything in the last 5 minutes.'));
  }

  // Runbooks for a Kubernetes problem: ones written for this workload, or for this kind of problem.
  const PROBLEM_SIGNALS = { crashloop: 'kube_restarts', oomkilled: 'kube_oom', not_ready: 'kube_not_ready', rollout_stuck: 'kube_not_ready' };
  function runbookQuery(detail) {
    if (!detail.workload) return null;
    const query = new URLSearchParams({ text: detail.workload });
    if (PROBLEM_SIGNALS[detail.problem]) query.set('signal', PROBLEM_SIGNALS[detail.problem]);
    return query.toString();
  }

  // Promtail names a stream namespace/app; pods without an app label end up as "namespace/".
  function streamName(job) {
    return job.endsWith('/') ? `${job.slice(0, -1)} (pods without an app label)` : job;
  }

  // Investigate one problem: the investigation is focused on it, so only its evidence goes to the model.
  function investigateLink(prompt, workflowQuery, fingerprint) {
    const query = new URLSearchParams({ prompt });
    if (fingerprint) query.set('focus', fingerprint);
    return h('span', { class: 'row-buttons' },
      workflowQuery ? h('a', { class: 'button compact', href: `#/workflows?${workflowQuery}` }, 'Runbooks') : null,
      can('engineer') ? h('a', { class: 'button compact', href: `#/investigate?${query}` }, 'Investigate') : null);
  }

  // What the dependency map says: the likely cause of this problem, or what else fails because of it.
  function dependencyNote(detail) {
    if (!can('engineer') || !detail) return null;
    const map = detail.map_node ? h('a', { class: 'map-link', href: `#/dependencies?focus=${encodeURIComponent(detail.map_node)}` }, 'Show on map') : null;
    if (detail.caused_by) {
      return h('span', { class: 'dependency-note' }, 'Likely caused by ', h('b', {}, detail.caused_by.title), ', which it depends on. ', map);
    }
    if (detail.affects?.length) {
      return h('span', { class: 'dependency-note is-cause' }, h('b', {}, 'Likely cause. '), `Also failing because of it: ${detail.affects.slice(0, 4).join(', ')}${detail.affects.length > 4 ? ` and ${detail.affects.length - 4} more` : ''}. `, map);
    }
    return null;
  }

  function renderAttention(context) {
    const rows = [];
    // An alert about a workload the console already reports is shown on that row, not as a second problem.
    for (const alert of (context.alerts?.firing || []).filter((item) => !item.merged_into)) {
      const severe = ['critical', 'page', 'error'].includes(String(alert.severity).toLowerCase());
      rows.push(h('li', { class: 'row' }, icon(severe ? 'critical' : 'warning'),
        h('span', { class: 'row-title' }, alert.name, ' ', h('span', { class: 'muted small' }, `${alert.severity} alert`)),
        h('span', { class: 'row-side' }, investigateLink(`Why is the ${alert.name} alert firing (${labelText(alert.labels)})? Find the root cause.`, `alert=${encodeURIComponent(alert.name)}`, alert.fingerprint)),
        h('span', { class: 'row-detail' }, `${alert.summary || (can('engineer') ? labelText(alert.labels) : 'Alert firing')}. Firing since ${timeAgo(alert.active_at)}.`, dependencyNote(alert))));
    }
    for (const target of context.targets?.down_list || []) {
      rows.push(h('li', { class: 'row' }, icon('critical'),
        h('span', { class: 'row-title' }, `${target.instance} is down`),
        h('span', { class: 'row-side' }, investigateLink(`Why is target ${target.instance} in job ${target.job} down?`, 'alert=targetdown', target.fingerprint)),
        h('span', { class: 'row-detail' }, `Job ${target.job}. `, target.error || 'No error was reported.')));
    }
    for (const item of (context.problems || []).filter((problem) => problem.kind === 'k8s')) {
      const detail = item.detail || {};
      const why = detail.diagnosis?.cause || (detail.pods?.length ? `Pods: ${detail.pods.slice(0, 3).join(', ')}` : '');
      rows.push(h('li', { class: 'row' }, icon(item.severity === 'critical' ? 'critical' : 'warning'),
        h('span', { class: 'row-title' }, item.title),
        h('span', { class: 'row-side' }, investigateLink(`${item.title}. Find the root cause.`, runbookQuery(detail), item.fingerprint)),
        h('span', { class: 'row-detail' }, can('engineer') ? why : 'Kubernetes workload problem',
          detail.also_firing?.length ? h('span', { class: 'also-firing' }, `Also firing in Prometheus: ${detail.also_firing.join(', ')}`) : null,
          dependencyNote(detail))));
    }
    for (const rule of context.rules?.failing || []) {
      rows.push(h('li', { class: 'row' }, icon('warning'),
        h('span', { class: 'row-title' }, `Rule failing: ${rule.name}`), h('span', {}),
        h('span', { class: 'row-detail' }, rule.error)));
    }
    for (const item of context.anomalies || []) {
      const usual = item.baseline !== null && item.baseline !== undefined ? `, usually ${formatValue(item.baseline, item.unit)}` : '';
      const limit = item.kind === 'over_limit' && item.limit !== null ? `, limit ${formatValue(item.limit, item.unit)}` : '';
      rows.push(h('li', { class: 'row' }, icon('warning'),
        h('span', { class: 'row-title' }, `${item.title}: ${formatValue(item.value, item.unit)}`),
        h('span', { class: 'row-side' }, investigateLink(`${item.title} is ${formatValue(item.value, item.unit)} on ${labelText(item.labels)}${usual}. What is causing this?`, `signal=${encodeURIComponent(item.signal_id)}`, item.fingerprint)),
        h('span', { class: 'row-detail' }, can('engineer') ? labelText(item.labels) : 'Out of range', usual, limit, dependencyNote(item))));
    }
    if (rows.length) clear(attention, h('ul', { class: 'rows scroll-rows' }, rows));
    else clear(attention, h('p', { class: 'empty' }, context.as_of ? 'No alerts, down targets, failing workloads, failing rules, or unusual signals.' : 'Nothing to show until something is connected.'));
  }

  function renderChanges(events) {
    if (!events.length) {
      clear(changes, h('p', { class: 'empty' }, 'No changes since this console connected. Alerts, targets, and unusual signals appear here when their state changes.'));
      return;
    }
    clear(changes, h('ul', { class: 'rows scroll-rows' }, events.slice(0, 30).map((event) => h('li', { class: 'row' },
      icon(event.severity === 'critical' ? 'critical' : event.severity === 'warning' ? 'warning' : 'ok'),
      h('span', { class: 'row-title' }, event.title),
      h('time', { class: 'row-side', datetime: event.ts, title: new Date(event.ts).toLocaleString() }, timeAgo(event.ts))))));
  }

  let showAllTargets = false;
  function table(head, rows) {
    return h('div', { class: 'table-wrap' }, h('table', { class: 'data-table' },
      h('thead', {}, h('tr', {}, head.map(([label, num]) => h('th', { scope: 'col', class: num ? 'num' : '' }, label)))),
      h('tbody', {}, rows)));
  }

  // Everything this console reads: metric targets by address, log streams, and cluster namespaces.
  function renderWatched(context) {
    const parts = [];
    const targets = context.targets?.list || [];
    if (targets.length) {
      const shown = showAllTargets ? targets : targets.slice(0, 25);
      const health = { up: ['ok', 'Up'], down: ['critical', 'Down'] };
      parts.push(h('h3', { class: 'watched-head' }, `Metric targets (${targets.length})`),
        table([['Target'], ['Job'], ['Watched by'], ['Status'], ['Last scrape']], shown.map((row) => h('tr', {},
          h('td', { class: 'cell-title' }, row.instance, row.error ? h('div', { class: 'small', style: 'font-weight:400;overflow-wrap:anywhere' }, row.error) : null),
          h('td', {}, row.job), h('td', {}, row.source),
          h('td', {}, h('span', { class: `badge ${(health[row.health] || ['neutral'])[0]}` }, (health[row.health] || [null, 'Unknown'])[1])),
          h('td', { class: 'small muted' }, row.last_scrape ? timeAgo(row.last_scrape) : '')))),
        targets.length > 25 ? h('button', { class: 'button compact', type: 'button', style: 'margin-top:10px', onClick: () => { showAllTargets = !showAllTargets; renderWatched(live.context); } },
          showAllTargets ? 'Show fewer' : `Show all ${targets.length}`) : null);
    }
    for (const loki of connectionsOf(context, 'loki').filter((item) => item.status === 'ok')) {
      const streams = loki.streams || [];
      parts.push(h('h3', { class: 'watched-head' }, `Log streams (${streams.length})`, h('span', { class: 'small muted' }, ` read by ${loki.name || 'Loki'}`)),
        streams.length ? table([['Stream'], ['Lines in 5 min', true], ['Error-like lines', true]], streams.slice(0, 50).map((row) => h('tr', {},
          h('td', { class: 'cell-title' }, streamName(row.job)), h('td', { class: 'num' }, Math.round(row.lines_5m).toLocaleString()),
          h('td', { class: 'num' }, row.errors_5m ? h('span', { class: 'badge warning' }, Math.round(row.errors_5m).toLocaleString()) : '0'))))
          : h('p', { class: 'empty' }, 'No stream wrote anything in the last 5 minutes.'));
    }
    for (const kube of connectionsOf(context, 'kubernetes').filter((item) => item.status === 'ok')) {
      const namespaces = kube.namespaces || [];
      parts.push(h('h3', { class: 'watched-head' }, `Kubernetes namespaces (${namespaces.length})`, h('span', { class: 'small muted' }, ` read from ${kube.name || 'the cluster'}`)),
        table([['Namespace'], ['Workloads', true], ['Pods ready', true], ['Problems', true]], namespaces.map((row) => h('tr', {},
          h('td', { class: 'cell-title' }, row.name), h('td', { class: 'num' }, row.workloads), h('td', { class: 'num' }, `${row.ready} / ${row.pods}`),
          h('td', { class: 'num' }, row.problems ? h('span', { class: 'badge critical' }, row.problems) : '0')))));
    }
    clear(jobs, parts.length ? parts : h('p', { class: 'empty' }, live.context?.as_of ? 'Nothing reports targets, log streams, or namespaces yet.' : 'Add a connection to see what it watches.'));
  }

  // Panels -----------------------------------------------------------------
  async function loadPanels() {
    try { panels = await api('/api/panels'); } catch (error) { panels = []; }
    renderPanels();
    fetchAll();
  }

  function limitFor(query, sourceId) {
    const match = (live.context?.signals || []).find((signal) => signal.query === query && (sourceId === undefined || sourceId === null || signal.source_id === sourceId));
    return match ? match.limit : null;
  }

  // Panels can be narrowed to one source. Viewers don't get the source list, so they always see every panel.
  function visiblePanels() {
    const sources = metricSources(live.context);
    if (!can('engineer') || sources.length < 2 || !picks.panels) return panels;
    return panels.filter((panel) => panel.source_id === Number(picks.panels));
  }

  function renderPanelsPicker() {
    const sources = metricSources(live.context);
    if (picks.panels && !sources.some((item) => item.id === Number(picks.panels))) picks.panels = '';
    clear(panelsPickHost, can('engineer') && sources.length > 1 ? sourcePicker('Source', [['', 'All sources'], ...sources.map((item) => [item.id, item.name])], picks.panels,
      (value) => { setPick('panels', value); renderPanels(); fetchAll(); }) : null);
  }

  function renderPanels() {
    for (const chart of panelCharts.values()) chart.destroy();
    panelCharts = new Map();
    renderPanelsPicker();
    const all = panels;
    const visible = visiblePanels();
    if (!visible.length) {
      const connected = live.context?.health?.status && live.context.health.status !== 'not_configured';
      panelGrid.style.display = 'block';
      clear(panelGrid, h('p', { class: 'empty' }, all.length ? 'No panel reads this source yet. Use Add panel and choose it as the metric source.' : !can('engineer') ? 'Your team hasn’t added any charts yet.' : connected
        ? 'Panels appear here once your source returns matching metrics. Use Add panel to chart any query now.'
        : 'Connect a metric source to see panels.'));
      return;
    }
    panelGrid.style.display = '';
    clear(panelGrid);
    const sourceNames = new Map(metricSources(live.context).map((item) => [item.id, item.name]));
    const showSource = can('engineer') && sourceNames.size > 1 && !picks.panels;
    for (const panel of visible) {
      const host = h('div');
      panelGrid.append(h('article', { class: 'panel', 'aria-label': panel.title },
        h('div', { class: 'panel-head' },
          h('h3', { title: panel.title }, panel.title),
          showSource && sourceNames.has(panel.source_id) ? h('span', { class: 'panel-source' }, sourceNames.get(panel.source_id)) : null,
          h('div', { class: 'panel-actions' },
            can('engineer') ? [
              h('a', { href: `#/investigate?prompt=${encodeURIComponent(`Look at "${panel.title}" (query: ${panel.query}). Is anything wrong, and why?`)}` }, 'Investigate'),
              h('button', { type: 'button', onClick: () => editPanel(panel) }, 'Edit'),
              h('button', { type: 'button', onClick: () => removePanel(panel) }, 'Remove')] : null)),
        panel.query ? h('code', { class: 'panel-query', title: panel.query }, panel.query) : null,
        host));
      const chart = new TimeSeriesChart(host, { unit: panel.unit || 'short', height: 230, title: panel.title, limit: limitFor(panel.query, panel.source_id), ariaLabel: `${panel.title} chart. Values are in the table below.` });
      chart.showMessage('Loading chart');
      panelCharts.set(panel.id, chart);
    }
  }

  async function fetchAll() {
    const panels = visiblePanels();
    if (fetching || !panels.length) return;
    fetching = true;
    const end = Math.floor(Date.now() / 1000);
    const step = stepFor(range);
    const start = end - range;
    await Promise.all(panels.map(async (panel) => {
      const chart = panelCharts.get(panel.id);
      try {
        // Viewers ask for a saved panel by its number. They can't send a query of their own.
        const data = can('engineer')
          ? await api('/api/metrics/query_range', { method: 'POST', body: { query: panel.query, start, end, step, source_id: panel.source_id } })
          : await api(`/api/panels/${panel.id}/series?${new URLSearchParams({ start, end, step })}`);
        chart?.setData(data.result, start, end, step);
      } catch (error) {
        // If the source is down, keep the last chart. The banner above explains the outage.
        if (error.status === 502 && chart?.plot) return;
        if (error.status !== 401) chart?.showMessage(error.message, true);
      }
    }));
    fetching = false;
  }

  async function removePanel(panel) {
    const ok = await confirmDialog({ title: `Remove "${panel.title}"?`, body: 'This removes the panel from the dashboard and stops tracking its query in the live context.', action: 'Remove panel' });
    if (!ok) return;
    await api(`/api/panels/${panel.id}`, { method: 'DELETE' });
    toast('Panel removed');
    loadPanels();
  }

  async function editPanel(panel) {
    const title = h('input', { class: 'input', required: true, maxlength: 120, value: panel?.title || '' });
    const query = h('textarea', { class: 'input query-input', required: true, rows: 3, spellcheck: 'false', list: 'metric-names', value: panel?.query || '' });
    const queryName = h('input', { class: 'input mono', list: 'metric-names', placeholder: 'Search metric names', 'aria-label': 'Search metric names', spellcheck: 'false' });
    const unit = h('select', { class: 'input' }, UNITS.map(([value, label]) => h('option', { value, selected: (panel?.unit || 'short') === value }, label)));
    const metricList = h('datalist', { id: 'metric-names' });
    const previewHost = h('div');
    const result = h('div');
    let previewChart = null;

    const sourceSelect = h('select', { class: 'input' }, (live.context?.sources || []).filter((item) => item.status === 'ok').map((item) => h('option', { value: item.id, selected: panel?.source_id === item.id }, item.name)));

    async function preview() {
      clear(result);
      const end = Math.floor(Date.now() / 1000);
      const start = end - 3600;
      previewChart?.destroy();
      previewChart = new TimeSeriesChart(previewHost, { unit: unit.value, height: 200, title: title.value });
      previewChart.showMessage('Running query');
      try {
        const data = await api('/api/metrics/query_range', { method: 'POST', body: { query: query.value, start, end, step: 60, source_id: Number(sourceSelect.value) || null } });
        previewChart.setData(data.result, start, end, 60);
      } catch (error) {
        previewChart.showMessage(error.message, true);
      }
    }

    async function save(event) {
      event.preventDefault();
      clear(result);
      const body = { title: title.value.trim(), query: query.value.trim(), unit: unit.value, source_id: Number(sourceSelect.value) || null };
      try {
        if (panel) await api(`/api/panels/${panel.id}`, { method: 'PUT', body });
        else await api('/api/panels', { method: 'POST', body });
        closeDialog();
        toast(panel ? 'Panel saved' : 'Panel added');
        loadPanels();
      } catch (error) {
        clear(result, h('p', { class: 'form-result bad' }, error.message));
      }
    }

    queryName.addEventListener('change', () => {
      if (queryName.value) { query.value = query.value ? `${query.value} ${queryName.value}`.trim() : queryName.value; queryName.value = ''; }
    });
    openDialog(h('form', { onSubmit: save },
      dialogHead(panel ? 'Edit panel' : 'Add panel'),
      h('div', { class: 'form-grid' },
        h('label', { class: 'field span-4' }, h('span', {}, 'Title'), title),
        h('label', { class: 'field span-2' }, h('span', {}, 'Unit'), unit),
        h('label', { class: 'field span-6' }, h('span', {}, 'Metric source'), sourceSelect),
        h('label', { class: 'field span-6' }, h('span', {}, 'Query'), query, h('small', {}, 'This query also joins the live context the AI model reads.')),
        h('div', { class: 'field span-6' }, queryName, metricList)),
      previewHost, result,
      h('div', { class: 'form-actions' },
        h('button', { class: 'button primary', type: 'submit' }, panel ? 'Save panel' : 'Add panel'),
        h('button', { class: 'button', type: 'button', onClick: preview }, 'Run query'))),
    { onClose: () => previewChart?.destroy() });
    try {
      const names = await api(`/api/metrics/names?source_id=${sourceSelect.value}`);
      clear(metricList, names.slice(0, 3000).map((name) => h('option', { value: name })));
    } catch (error) { /* Metric suggestions are optional. */ }
  }

  renderControls();
  renderContext(live.context);
  loadPanels();
  schedule();
  const unsubscribe = live.subscribe((type, data) => {
    if (type === 'context') renderContext(data);
    if (type === 'panels_changed') loadPanels();
    if (type === 'theme') {
      renderPanels();
      fetchAll();
      heroKey = '';
      renderContext(live.context);
    }
  });
  const sourceIds = (context) => (context?.sources || []).map((item) => item.id).join(',');
  let knownSources = sourceIds(live.context);
  const unsubscribeServer = live.subscribe((type, data) => {
    if (type === 'context' && sourceIds(data) !== knownSources) {
      knownSources = sourceIds(data);
      loadPanels();
    }
  });

  return () => {
    unsubscribe();
    unsubscribeServer();
    clearInterval(timer);
    heroChart?.destroy();
    for (const chart of panelCharts.values()) chart.destroy();
  };
}
