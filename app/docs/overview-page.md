# The overview

The overview is the first page after you sign in. It shows what's wrong now, the state of your systems, and everything the console reads.

## Problems right now

The list at the top shows active problems from every connection, worst first:

- **Firing alerts** from Prometheus, CloudWatch, Azure Monitor, or Alertmanager.
- **Targets down:** a scrape target that stopped answering.
- **Kubernetes workload problems:** crash loops, out-of-memory kills, image pull failures, missing Secrets, unschedulable pods, stuck rollouts, and node trouble. Each shows the console's diagnosis.
- **Failing rules** and **unusual signals**, such as an error rate out of its normal range.

When problems are linked, each symptom sits right under its **likely cause**, indented, with the reason (such as *Likely caused by payments/postgres-primary: payments-api is configured to call it*), and the cause says how many problems below it explains. Engineers see this; viewers see the plain list. [Likely cause](/docs/alerts-and-webhooks#likely-cause-one-root-many-symptoms)

Engineers see the full detail, with queries, labels, and log lines. Viewers see which workloads are failing, without addresses, queries, or logs.

Each problem has two shortcuts:

- **Investigate** opens a new investigation with the problem already described.
- **Runbooks** opens the runbook library filtered to that workload and that kind of problem. For example, a crash-looping `payment` Deployment shows runbooks for `payment` and the built-in *Containers keep restarting*.

## Cluster, Metrics, and Logs

The panel at the top right has a switch:

| View | Shows |
|---|---|
| **Cluster** | Pods running and ready, Deployments fully available, nodes, and problems per namespace |
| **Metrics** | Targets answering scrapes, and the four golden signals when your metrics include them |
| **Logs** | Lines and error-like lines in the last 5 minutes, and the streams with the most errors |

Your browser remembers your choice. With several metric sources, log stores, or clusters, a **Source**, **Log store**, or **Cluster** picker appears next to the switch:

- **Metrics:** *Automatic* shows the most important problem from any source, and the main source's key signal when nothing is wrong. Pick a source to see only that one.
- **Cluster** and **Logs:** pick which cluster or Loki to show. The main one is shown first.

A view is only offered when something feeds it: Cluster needs a Kubernetes connection, Metrics a metric source, and Logs a Loki connection that answers. Metrics and Logs are for engineers and above, so viewers see the Cluster view.

### Golden signals

When your metrics include them, the console charts the four golden signals:

| Signal | Question it answers |
|---|---|
| Latency | How long requests take |
| Traffic | How much is being asked |
| Errors | How many requests fail |
| Saturation | How full the system is |

With several metric sources, each box shows the worst value across all of them and names the source it came from. The **Source** picker above them shows one source only. The Metrics panels further down have the same picker, and each panel names its source.

It finds them by recognising common metric names, including Spring Boot, NGINX ingress, JVM, container, database, and queue metrics. If none are found, the section shows one line instead of empty charts.

### Error-like lines

In the Logs view, a line counts as **error-like** when it contains `error`, `exception`, `fatal`, or `panic`, in any letter case. It's a quick signal, not a severity level. Some programs write those words in harmless lines, so check a busy stream before treating it as broken. Loki's own records of the console's queries are left out of the count.

## Saved charts

Engineers can add a chart for any PromQL query with **Add panel**. Each saved chart's query also joins the live context, so the AI model sees it too. Viewers see the charts without the queries.

## What's watched

The bottom of the page lists everything the console reads, and which connection reads it:

- **Metric targets:** each scrape target by its IP address or host name, its job, which Prometheus watches it, whether it's up, and when it was last scraped. Targets that are down come first. Many targets from many Prometheus servers fit here, with **Show all** for long lists.
- **Log streams:** each Loki stream, by its `job` label, with lines and error-like lines in the last 5 minutes.
- **Kubernetes namespaces:** pods, ready pods, workloads, and problems in each namespace.

A stream shown as *shop (pods without an app label)* comes from pods with no `app` label. Log agents such as Promtail name streams `namespace/app`, so the app part is empty.

## When a connection stops answering

A banner says *A connection isn't answering*, with the error for engineers and the time of the last good data. The rest of the page keeps showing the last good data. See [Troubleshooting](/docs/troubleshooting) for common errors.
