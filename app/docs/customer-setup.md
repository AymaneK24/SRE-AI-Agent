# Set up a customer's environment

This page lists everything to prepare on the customer's side, so the console detects as many problems as possible: workload failures, machine trouble, application errors, and the changes that cause them. Hand it to the customer's platform team before the first meeting.

Nothing of the console runs in the customer's cluster. The console connects **out** to their systems with read-only credentials. What runs on their side is standard open-source monitoring, which most teams already have.

## What to install, and what it covers

43 kinds of problems were checked against each setup: crash loops, out of memory, image pull errors, unschedulable pods, stuck rollouts, nodes not ready, high CPU, disks filling up, HTTP error rates, slow responses, database and queue trouble, application errors in logs, and the changes behind them.

| Setup | What the customer installs | Fully detected | Partly | Not detected |
|---|---|---|---|---|
| **A.** Kubernetes account only | One YAML file: a read-only account. Nothing runs in the cluster | 13 | 8 | 22 |
| **B.** A + their existing Prometheus | Nothing new, if they already run Prometheus with node-exporter, kube-state-metrics and cAdvisor | 28 | 8 | 7 |
| **C.** B + their existing Loki | Nothing new, if they already send logs to Loki | 29 | 7 | 7 |
| **D.** A + kube-prometheus-stack + Loki | The monitoring chart from this project, Loki, and a log collector | **42** | **1** | **0** |

**Recommended:** setup D, plus changes sent from their CI/CD pipeline. It's the only setup that also covers disks filling up, persistent volumes, certificates and the Kubernetes control plane. Each step below says what it adds, so a customer can stop at any of them.

Alertmanager and Grafana are **not** needed. The console reads firing alerts from Prometheus directly and sends its own messages.

## Before you start

| Need | Detail |
|---|---|
| People | Someone with cluster-admin rights for steps 1 to 3 (once), and someone who can change the CI/CD pipeline for step 4 |
| Tools | `kubectl` pointed at the cluster, and `helm` 3 |
| Network | The console's server must reach each address below. Open these from the console's server only, never from the internet |
| Time | About an hour for everything |

| From the console to | Port | Used for |
|---|---|---|
| The Kubernetes API server | `443` or `6443` | Workloads, events, nodes, crash logs |
| Prometheus | `9090` | Firing alerts, targets, signals, queries |
| Loki | `3100`, or `80` for its gateway | Log counts and log searches |
| Slack, Teams, Google Chat, email server | `443`, `587` | Alert messages |
| `api.anthropic.com` or `generativelanguage.googleapis.com` | `443` | Only if they use Claude or Gemini. Ollama stays inside their network |

From the customer's pipeline **to** the console: `443`, only for step 4.

> **Security:** Set `RCC_TARGET_ALLOWLIST` on the console to exactly these addresses, so it can't be pointed anywhere else. See [Connections](/docs/connections#where-the-console-is-allowed-to-connect).

## 1. A read-only Kubernetes account

Adds: crash loops, out of memory, image pull errors, configuration errors, unschedulable and never-ready pods, stuck rollouts, deployments without ready replicas, nodes not ready or under pressure, warning events, and the last log lines of crashed containers.

```bash
kubectl apply -f deploy/k8s/reader-rbac.yaml
kubectl auth can-i list pods --as=system:serviceaccount:rcc:rcc-reader -A      # yes
kubectl auth can-i get secrets --as=system:serviceaccount:rcc:rcc-reader -A    # no
kubectl -n rcc create token rcc-reader --duration=720h                        # the token
```

The account can only `get` and `list` pods, pod logs, events, nodes, services and deployments. It can't read Secrets or change anything. Then collect the API server address and its CA certificate, and fill in **Connections > Logs and Kubernetes > Kubernetes**. Every detail, including namespace-only access, tokens that don't expire, and AKS, EKS and GKE, is in [Kubernetes](/docs/kubernetes).

With several clusters, repeat this step for each one and give each a clear name, such as `aks-prod-westeurope`.

## 2. Prometheus with the standard alert rules

Adds: high CPU and memory, disks full or filling up, disk and network trouble, CPU throttling, autoscalers at their maximum, persistent volumes filling up, StatefulSets, DaemonSets and Jobs, quota, the Kubernetes control plane, and about 100 standard alert rules. Without any rule, the console also spots signals far outside their own last hour.

**If they already run Prometheus**, check it has node-exporter, kube-state-metrics and the kubelet's cAdvisor metrics, then connect it read-only. Their own alert rules come with it.

```bash
curl -s http://PROMETHEUS:9090/api/v1/label/job/values     # expect node-exporter, kube-state-metrics, kubelet
```

**If they don't**, install kube-prometheus-stack from this project. The production profile keeps 15 days of history on a 50 GB volume, measures disk use, and keeps Prometheus off the nodes' ports:

```bash
PROFILE=production bash deploy/examples/monitoring/install.sh
```

| Profile | For | Differences |
|---|---|---|
| `laptop` (default) | Docker Desktop, kind, a test cluster | 3 days of history, no volume, reachable on every node at port `30090`, no disk metrics (Docker Desktop can't mount the host's disk) |
| `production` | A customer's real cluster | 15 days on a 50 GB volume, disk metrics and the clock-skew rule on, reachable only inside the cluster |

Pin a chart version for repeatable installs: `CHART_VERSION=80.0.0 PROFILE=production bash ...`. Then make Prometheus reachable from the console:

- **Console in the same cluster:** `prometheus-operated.monitoring.svc.cluster.local`, port `9090`.
- **Console elsewhere:** an internal load balancer, or an ingress with TLS and sign-in. See [Prometheus](/docs/prometheus#prometheus-in-a-kubernetes-cluster). Never expose Prometheus to the internet without sign-in.

On AKS, EKS and GKE, the control plane is managed by the cloud provider and hidden, so its rules (API server, etcd, scheduler) stay quiet. That's expected.

### Application signals

HTTP error rates, slow responses and traffic drops need metrics from the application or its ingress, which no cluster tool can invent:

- **Ingress:** ingress controllers expose request counts and latencies per route. Turn on the controller's metrics and a ServiceMonitor. The console recognises the NGINX ingress metrics as golden signals; the model can query any other controller's metrics during investigations.
- **Applications:** Spring Boot (Micrometer), Node.js (`prom-client`), Go and Python clients expose `http_server_requests_seconds` or similar. Add a `ServiceMonitor` or `PodMonitor` for each.
- **Databases and queues:** add the matching exporter (PostgreSQL, MySQL, Redis, Kafka, RabbitMQ). The console recognises their usual metrics.

The monitoring chart picks up every ServiceMonitor, PodMonitor and PrometheusRule in any namespace, so application teams can add their own without touching it.

## 3. Logs: Loki and a collector

Adds: errors and exceptions in every application's logs, with history, and log searches during investigations.

**If they already run Loki**, connect it read-only. Check that streams have a `job` label, or set the [log selector](/docs/loki#the-log-selector) to the labels they use.

**If they don't**, install Loki and a collector:

1. **Loki.** Follow Grafana's [monolithic Helm installation](https://grafana.com/docs/loki/latest/setup/install/helm/install-monolithic/). The chart now lives at `https://grafana-community.github.io/helm-charts` as `grafana-community/loki`, with `deploymentMode: Monolithic` and `commonConfig.replication_factor: 1` for a single instance. Give it object storage (S3, Azure Blob, GCS) or a persistent volume for production.
2. **The collector: Grafana Alloy.** Promtail reached end of life on 2 March 2026; Alloy replaces it. This project's values collect every pod's logs through the Kubernetes API and add the labels the console uses:

```bash
# Set LOKI_URL in the file first, such as http://loki-gateway.loki.svc.cluster.local/loki/api/v1/push
helm repo add grafana https://grafana.github.io/helm-charts
helm upgrade --install alloy grafana/alloy -n logging --create-namespace -f deploy/examples/logging/alloy-values.yaml
```

Each stream gets `namespace`, `pod`, `container`, `app`, and `job` set to `namespace/container`. Keep the `job` label: the console counts lines and errors per job.

Then make Loki reachable from the console, as in [Loki](/docs/loki#finding-loki-s-address), and connect it.

> **Privacy:** Logs often contain personal data and secrets. Before the customer uses Claude or Gemini, agree what may leave their network: the console sends log lines to the model only during investigations, and every prompt is recorded in **Logs**. See [Legal and privacy](/docs/legal-and-privacy). Ollama keeps everything inside their network.

## 4. Changes from the CI/CD pipeline

Adds: *what changed just before the problem*, the most common root cause. The console already sees new images and revisions in the cluster. The pipeline adds who deployed what, from where.

Create the addresses in **Connections > Alerts > Incoming webhooks**, store the changes address as a secret in their CI system, and send one call per deployment:

```bash
curl -fsS -X POST "$RCC_CHANGES_URL" -H 'Content-Type: application/json' \
  -d '{"title": "Deployed checkout-api 2.4.1", "service": "checkout-api", "origin": "GitHub Actions"}'
```

GitHub and GitLab webhooks are understood directly when the console is reachable from the internet. See [Send changes from a pipeline](/docs/alerts-and-webhooks#send-changes-from-a-pipeline).

**Optional:** an existing Alertmanager can also push its alerts to the alerts address. It isn't needed when Prometheus is connected, and the console never sends the same alert twice.

## 5. In the console

With everything connected, set these up with the customer:

| Where | What to set | Why |
|---|---|---|
| **Connections** | One connection per Prometheus, Loki and cluster, each with a clear name. **Make main** on the production ones | The overview shows the main ones first. Each part of the page has a **Source** picker for the others |
| **Connections > Alerts** | An email or chat channel per team. Critical only for on-call, warning and critical for the team channel | Every problem reaches the right people. [Alert channels](/docs/alerts-and-webhooks#alert-channels) |
| **Connections > Alerts > Noise control** | Keep the defaults at first: 60 seconds' grace for critical, 5 minutes for warning, 90 seconds' grouping, 12 messages an hour per channel | Blips aren't sent, an incident with ten symptoms is one message, and nobody learns to ignore alerts. [Noise control](/docs/alerts-and-webhooks#noise-control-fewer-better-messages) |
| **Connections > Alerts > Heartbeat** | The address of a check in an outside service such as Healthchecks.io, every 5 minutes | If the console itself stops, someone is told. [Heartbeat](/docs/alerts-and-webhooks#heartbeat-know-when-the-console-itself-stops) |
| **Investigate critical alerts automatically** | On, with a limit per hour | The email arrives with the likely cause, evidence charts and a PDF. [Automatic investigations](/docs/alerts-and-webhooks#automatic-investigations) |
| **Connections > AI model** | Claude, Gemini, or Ollama for data that must stay inside | [AI models](/docs/ai-models) |
| **Connections > Cost savings** | Prompt caching, gathering evidence first, a cheaper model for query rounds, short answers | Cuts AI spend without losing accuracy. [Cost savings](/docs/ai-models#cost-savings) |
| **Projects** | One project per application, with its namespaces and sources | Health and reports per application. Issues marked **false alarm** or **expected** stop counting, and stop being sent. [False alarms](/docs/projects-and-reports#false-alarms) |
| **Organization** | Invite the team: viewers, engineers, admins. Single sign-on if they have one | [Roles](/docs/roles-and-permissions), [Single sign-on](/docs/single-sign-on) |

A problem that is already running when a connection is added, such as a deployment that can't pull its image, is sent too: the console doesn't wait for it to start again.

## Resources on their side

| Component | CPU (request) | Memory | Storage |
|---|---|---|---|
| Prometheus, production profile | 0.5 core | 2 to 6 GB | 50 GB volume |
| Prometheus operator, kube-state-metrics | 0.1 core | 128 to 512 MB | None |
| node-exporter | 0.02 core per node | 32 to 128 MB per node | None |
| Alloy | 0.05 core | 128 to 512 MB | None |
| Loki, single instance | 0.5 to 1 core | 1 to 4 GB | Object storage or a volume |

Prometheus memory grows with the number of pods and metrics. Plan about 2 GB per 1,000 pods, and watch it the first week.

## Check that it works

```bash
kubectl -n monitoring get pods                                   # every pod Running
kubectl -n logging get pods                                      # Alloy on every node
curl -s http://PROMETHEUS:9090/api/v1/rules | grep -c '"name":"Kube'   # rule groups loaded
curl -s http://LOKI:3100/loki/api/v1/label/job/values            # namespace/container names
```

Then, on a **test** cluster, apply the failure simulation and watch the console find each problem within a few minutes, and the alerts arrive:

```bash
kubectl apply -f deploy/examples/shop-simulation.yaml
kubectl -n shop get pods -w
kubectl delete namespace shop        # when done
```

Each service in it fails in a known way: a refused database connection, out of memory, a wrong probe, a missing Secret, an image that doesn't exist, a wrong port, too much memory requested. See [Accuracy benchmark](/docs/accuracy-benchmark).

## Handover checklist

- [ ] Read-only Kubernetes account applied, `can-i get secrets` answers *no*, token expiry in the calendar
- [ ] Prometheus with node-exporter, kube-state-metrics and the standard rules, reachable only from the console
- [ ] Loki and Alloy, streams with a `job` label, reachable only from the console
- [ ] Changes sent from the pipeline, with the address stored as a CI secret
- [ ] `RCC_TARGET_ALLOWLIST` set on the console to these addresses
- [ ] Channels per team, noise control reviewed, automatic investigations on with a limit
- [ ] AI model chosen, and what may be sent to it agreed in writing
- [ ] Projects created, team invited
- [ ] Simulation run on a test cluster, alerts received
