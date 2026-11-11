# Dependency map

**Dependencies** in the menu shows your services and the calls between them. When several things break at once, it shows which one the others depend on, so you fix the cause first instead of chasing its symptoms.

Engineers, admins and owners see the map. Viewers don't, because it describes how your systems are built.

## What you see

Calls read from left to right: a workload sits to the right of what calls it.

| On the map | Means |
|---|---|
| Solid line | A call seen in traces or a service mesh, with its rate and error share |
| Dashed line | A call known from Kubernetes configuration: the workload is set up to call it |
| Red line | Calls that fail (5% or more errors), or the path from a symptom to its likely cause |
| Red or amber box | The workload has a problem now |
| Ringed box, **Likely cause** | Other broken workloads depend on this one, and it depends on nothing that is broken |
| **symptom** tag | Its problem is likely a consequence of a broken dependency |
| Dashed box | An outside service, such as a payment API, or a service seen in traces that matches no workload |

Select a box to see what it calls, what calls it, how each link is known, and its problems, with a button to investigate them. Drag to move, scroll or use **+** and **−** to zoom, and **Fit** to see everything. **Only problems and their neighbours** keeps the map small during an incident.

On the **Overview**, each problem the map can explain says **Likely caused by …** or **Likely cause**, with **Show on map**.

## Where the links come from

The console builds the map itself as it polls. Nobody has to draw it, and it follows your deployments.

**From Kubernetes**, for every connected cluster:

- Each Service and the workloads its selector reaches.
- Each workload's environment variables that name a Service or a host, such as `DATABASE_URL=postgres://postgres-primary.data:5432/app`. A workload set up to reach a Service depends on the workloads behind it.
- Variables whose names look secret (`PASSWORD`, `TOKEN`, `KEY`, `SECRET` and similar) are never read. From the others, only the host name is kept: values, user names and passwords in URLs are dropped. Values loaded from Secrets or ConfigMaps aren't read at all.

The read-only access the console already has covers this. It reads `services`, which is in the [Kubernetes permissions](/docs/kubernetes) the setup gives it.

**From metrics**, when your Prometheus has them, read every 5 minutes:

| Source | Metric |
|---|---|
| Tempo or the OpenTelemetry collector service graph | `traces_service_graph_request_total` and `traces_service_graph_request_failed_total` |
| Istio | `istio_requests_total` |
| Linkerd | `response_total` |

These are real calls, so they are more reliable than configuration. Without them, the page says that its links come from configuration only.

## How the likely cause is chosen

For each problem on a workload, the console follows that workload's calls up to four steps. If it reaches a broken workload that depends on nothing broken itself, that one is the likely cause, and the problem is marked as its symptom. Workloads that are broken and call each other in a loop have no clear cause, so none of them is marked.

This is a lead, not a verdict. Two problems can happen at the same time without one causing the other. Investigations see the same lead and are told to check the likely cause first, with a tool that reads the map.

## Alerts

An alert message names the likely cause: *Likely caused by: Deployment data/postgres-primary is crash-looping (shop/checkout → shop/payments → data/postgres-primary)*. The cause's own message lists what else fails because of it. When problems start together, the grouped message leads with the likely cause.

To hear only about the cause, turn on **Hold alerts explained by a broken dependency** in **Connections > Alerts > Noise control**. A symptom is then held while its likely cause is still going on. It is off by default, so nothing is held unless you choose it.

## Limits

- A workload configured through a ConfigMap, a Secret, or a file inside its image has no configured links. Traces or a mesh fill that gap.
- A dependency in a namespace the console's token can't read is left out, not guessed.
- Names seen in traces are matched to workloads by name. A name used in several namespaces isn't matched, so it appears on its own.
