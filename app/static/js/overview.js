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
  const staleBanner = h('div', { hidden: true });
  const attention = h('div');
  const changes = h('div');
  const jobs = h('div');
  const golden = h('div', { class: 'golden-grid' });
  const goldenSection = h('section', { class: 'section', 'aria-labelledby': 'golden-title' }, h('div', { class: 'section-head' }, h('h2', { id: 'golden-title' }, 'Golden signals')), golden);
  const panelGrid = h('div', { class: 'panels' });
  const rangeControl = h('div', { class: 'segments', role: 'group', 'aria-label': 'Time range' });
  const refreshControl = h('div', { class: 'segments', role: 'group', 'aria-label': 'Refresh interval' });

  clear(container, h('div', { class: 'page' },
    staleBanner,
    h('section', { class: `hero${can('engineer') ? '' : ' hero-solo'}`, 'aria-labelledby': 'hero-title' },
      h('div', {}, heroTitle, heroCopy, heroActions),
      h('div', {}, viewSwitch, can('engineer') ? metricsHost : null, clusterHost, logsHost)),
    goldenSection,
    h('section', { class: 'section status-grid' },
      h('div', {}, h('div', { class: 'section-head' }, h('h2', {}, 'Needs attention')), attention),
      h('div', {}, h('div', { class: 'section-head' }, h('h2', {}, 'What changed')), changes)),
    h('section', { class: 'section', 'aria-label': 'Metric panels' },
      h('div', { class: 'section-head' }, h('h2', {}, 'Metrics')),
      h('div', { class: 'dash-bar' },
        h('span', { class: 'control-label' }, 'Time range', rangeControl),
        h('span', { class: 'control-label' }, 'Refresh', refreshControl),
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
    const signals = (context.signals || []).filter((signal) => signal.golden && signalValue(signal) !== null);
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
        h('p', { class: 'small muted' }, worst ? `${worst.title}${over ? `, over its limit of ${formatValue(worst.limit, worst.unit)}` : ''}` : hint));
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
    const signalById = new Map((context.signals || []).map((signal) => [signal.id, signal]));
    const anomalies = [...(context.anomalies || [])].sort((a, b) => {
      const groupA = GROUP_RANK[signalById.get(a.signal_id)?.group] ?? 9;
      const groupB = GROUP_RANK[signalById.get(b.signal_id)?.group] ?? 9;
      return groupA - groupB;
    });
    if (anomalies.length) {
      const item = anomalies[0];
      return { title: item.title, query: item.query, unit: item.unit, limit: item.limit, source_id: item.source_id, reason: 'Out of its normal range' };
    }
    const prom = (context.sources || []).find((item) => item.kind === 'prometheus' && item.status === 'ok');
    if ((context.targets?.down || 0) > 0 && prom) {
      return { title: 'Targets answering scrapes', query: 'up', unit: 'short', limit: null, source_id: prom.id, reason: 'A value of 0 means the target is down' };
    }
    const preferred = [...(context.signals || [])]
      .filter((signal) => signal.series.length)
      .sort((a, b) => (GROUP_RANK[a.group] ?? 9) - (GROUP_RANK[b.group] ?? 9) || a.panel_priority - b.panel_priority)[0];
    if (preferred) return { title: preferred.title, query: preferred.query, unit: preferred.unit, limit: preferred.limit, source_id: preferred.source_id, reason: 'Key signal' };
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
      clear(staleBanner, h('b', {}, 'A metric source isn’t answering. '), can('engineer') ? (health.error || '') : '', health.last_ok ? ` The last good data is from ${timeAgo(health.last_ok)}.` : '');
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
    const key = signal ? `${signal.query}|${signal.limit}` : '';
    if (key !== heroKey) {
      heroKey = key;
      heroChart?.destroy();
      heroChart = null;
      clear(heroChartHost);
      clear(heroLabel);
      if (signal) {
        clear(heroLabel, h('b', {}, signal.title), `. ${signal.reason}.`);
        heroChart = new TimeSeriesChart(heroChartHost, { unit: signal.unit, height: 360, limit: signal.limit, title: signal.title, showTable: false, endValue: true, ariaLabel: `${signal.title} chart` });
        heroChart.signal = signal;
        heroChart.showMessage('Loading chart');
        fetchHero(signal);
      }
    } else if (heroChart) {
      fetchHero(heroChart.signal);
    }
  }

  // The right side shows one of: the cluster, a key metric chart, or log streams. Each viewer's choice is remembered.
  function chooseView(context) {
    const counts = context.integrations?.kubernetes?.counts || context.cluster_counts;
    const views = [];
    if (counts) views.push(['cluster', 'Cluster']);
    if (context.coverage?.has_metrics !== false && can('engineer')) views.push(['metrics', 'Metrics']);
    if (context.integrations?.loki?.status === 'ok' && can('engineer')) views.push(['logs', 'Logs']);
    const names = views.map(([name]) => name);
    const view = names.includes(heroView) ? heroView : names[0] || '';
    viewSwitch.hidden = views.length < 2;
    clear(viewSwitch, views.map(([name, label]) => h('button', {
      type: 'button', 'aria-pressed': String(name === view),
      onClick: () => { heroView = name; savePref('rcc-hero-view', name); renderContext(live.context); },
    }, label)));
    return view;
  }

  function renderCluster(context, show) {
    const kube = context.integrations?.kubernetes;
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
    const loki = context.integrations?.loki;
    logsHost.hidden = !show;
    if (!show || !loki) { clear(logsHost); return; }
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

  function investigateLink(prompt, workflowQuery) {
    return h('span', { class: 'row-buttons' },
      workflowQuery ? h('a', { class: 'button compact', href: `#/workflows?${workflowQuery}` }, 'Runbooks') : null,
      can('engineer') ? h('a', { class: 'button compact', href: `#/investigate?prompt=${encodeURIComponent(prompt)}` }, 'Investigate') : null);
  }

  function renderAttention(context) {
    const rows = [];
    for (const alert of context.alerts?.firing || []) {
      const severe = ['critical', 'page', 'error'].includes(String(alert.severity).toLowerCase());
      rows.push(h('li', { class: 'row' }, icon(severe ? 'critical' : 'warning'),
        h('span', { class: 'row-title' }, alert.name, ' ', h('span', { class: 'muted small' }, `${alert.severity} alert`)),
        h('span', { class: 'row-side' }, investigateLink(`Why is the ${alert.name} alert firing (${labelText(alert.labels)})? Find the root cause.`, `alert=${encodeURIComponent(alert.name)}`)),
        h('span', { class: 'row-detail' }, `${alert.summary || (can('engineer') ? labelText(alert.labels) : 'Alert firing')}. Firing since ${timeAgo(alert.active_at)}.`)));
    }
    for (const target of context.targets?.down_list || []) {
      rows.push(h('li', { class: 'row' }, icon('critical'),
        h('span', { class: 'row-title' }, `${target.instance} is down`),
        h('span', { class: 'row-side' }, investigateLink(`Why is target ${target.instance} in job ${target.job} down?`, 'alert=targetdown')),
        h('span', { class: 'row-detail' }, `Job ${target.job}. `, target.error || 'No error was reported.')));
    }
    for (const item of (context.problems || []).filter((problem) => problem.kind === 'k8s')) {
      const detail = item.detail || {};
      const why = detail.diagnosis?.cause || (detail.pods?.length ? `Pods: ${detail.pods.slice(0, 3).join(', ')}` : '');
      rows.push(h('li', { class: 'row' }, icon(item.severity === 'critical' ? 'critical' : 'warning'),
        h('span', { class: 'row-title' }, item.title),
        h('span', { class: 'row-side' }, investigateLink(`${item.title}. Find the root cause.`, runbookQuery(detail))),
        h('span', { class: 'row-detail' }, can('engineer') ? why : 'Kubernetes workload problem')));
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
        h('span', { class: 'row-side' }, investigateLink(`${item.title} is ${formatValue(item.value, item.unit)} on ${labelText(item.labels)}${usual}. What is causing this?`, `signal=${encodeURIComponent(item.signal_id)}`)),
        h('span', { class: 'row-detail' }, can('engineer') ? labelText(item.labels) : 'Out of range', usual, limit)));
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
    const loki = context.integrations?.loki;
    if (loki?.status === 'ok') {
      const streams = loki.streams || [];
      parts.push(h('h3', { class: 'watched-head' }, `Log streams (${streams.length})`, h('span', { class: 'small muted' }, ` read by ${loki.name || 'Loki'}`)),
        streams.length ? table([['Stream'], ['Lines in 5 min', true], ['Error-like lines', true]], streams.slice(0, 50).map((row) => h('tr', {},
          h('td', { class: 'cell-title' }, streamName(row.job)), h('td', { class: 'num' }, Math.round(row.lines_5m).toLocaleString()),
          h('td', { class: 'num' }, row.errors_5m ? h('span', { class: 'badge warning' }, Math.round(row.errors_5m).toLocaleString()) : '0'))))
          : h('p', { class: 'empty' }, 'No stream wrote anything in the last 5 minutes.'));
    }
    const kube = context.integrations?.kubernetes;
    if (kube?.status === 'ok') {
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

  function limitFor(query) {
    const match = (live.context?.signals || []).find((signal) => signal.query === query);
    return match ? match.limit : null;
  }

  function renderPanels() {
    for (const chart of panelCharts.values()) chart.destroy();
    panelCharts = new Map();
    if (!panels.length) {
      const connected = live.context?.health?.status && live.context.health.status !== 'not_configured';
      panelGrid.style.display = 'block';
      clear(panelGrid, h('p', { class: 'empty' }, !can('engineer') ? 'Your team hasn’t added any charts yet.' : connected
        ? 'Panels appear here once your source returns matching metrics. Use Add panel to chart any query now.'
        : 'Connect a metric source to see panels.'));
      return;
    }
    panelGrid.style.display = '';
    clear(panelGrid);
    for (const panel of panels) {
      const host = h('div');
      panelGrid.append(h('article', { class: 'panel', 'aria-label': panel.title },
        h('div', { class: 'panel-head' },
          h('h3', { title: panel.title }, panel.title),
          h('div', { class: 'panel-actions' },
            can('engineer') ? [
              h('a', { href: `#/investigate?prompt=${encodeURIComponent(`Look at "${panel.title}" (query: ${panel.query}). Is anything wrong, and why?`)}` }, 'Investigate'),
              h('button', { type: 'button', onClick: () => editPanel(panel) }, 'Edit'),
              h('button', { type: 'button', onClick: () => removePanel(panel) }, 'Remove')] : null)),
        panel.query ? h('code', { class: 'panel-query', title: panel.query }, panel.query) : null,
        host));
      const chart = new TimeSeriesChart(host, { unit: panel.unit || 'short', height: 230, title: panel.title, limit: limitFor(panel.query), ariaLabel: `${panel.title} chart. Values are in the table below.` });
      chart.showMessage('Loading chart');
      panelCharts.set(panel.id, chart);
    }
  }

  async function fetchAll() {
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
