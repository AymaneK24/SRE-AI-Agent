# Alerts and webhooks

The console tells people when something goes wrong (alert channels), and listens to other tools that already know (incoming webhooks).

## Alert channels

Admins add channels in **Connections > Alerts**.

| Channel | What you need |
|---|---|
| Slack | An Incoming Webhook address from your Slack workspace |
| Microsoft Teams | A webhook address from a Teams Workflows flow |
| Google Chat | A space's webhook address |
| Email | An email server set up with `RCC_SMTP_*`, then recipient addresses. See [Configuration](/docs/configuration#email) |

For each channel, choose **Warning and critical** or **Critical only**, then **Send test**.

Messages describe the problem and link to the console. They have no buttons that change anything.

> **Security:** Alert messages contain host names, queries, and findings, whatever the recipient's role in the console. Choose each channel's recipients with care, and treat Slack and Teams webhook addresses like passwords: anyone with one can post to that channel.

### Automatic investigations

With **Investigate critical alerts automatically** on, a critical alert starts an investigation by itself:

- **Email** waits for the investigation, then sends one message with the alert, the findings, charts of the evidence, commands from matching reviewed runbooks, and a PDF. It waits at most 10 minutes (`RCC_ALERT_EMAIL_WAIT_SECONDS`). After that the alert goes out, and the findings follow in a second email.
- **Slack, Teams, and Google Chat** get the alert straight away, then a short summary when the investigation finishes.
- If the investigation fails, or the console restarts, the alert still goes out, without findings. The waiting email is saved in the database, so it can't be lost.

**Most per hour** (5 by default) limits automatic investigations, because each one uses your AI budget.

## One problem, one alert

With both a Kubernetes connection and a Prometheus that has Kubernetes alert rules (such as kube-prometheus-stack), the same problem can be seen twice: the console finds a crash loop itself, and Prometheus fires `KubePodCrashLooping` for it a few minutes later. The console treats them as one problem:

- A Prometheus alert whose labels name a workload the console already reports (`namespace` with `deployment`, `statefulset`, `daemonset`, `job_name`, or a `pod` of that workload), or a node it already reports (`node`), joins that problem. It shows on its row as *Also firing in Prometheus: KubePodCrashLooping*, and isn't counted again in the headline.
- Only the first one to start is announced. The other starts and ends without a notification, and without a second automatic investigation.
- Alerts about anything else, such as your own application rules, are announced as usual.

## Noise control: fewer, better messages

Every problem passes these checks before anyone is messaged. Nothing is hidden in the console; only the message is held back, and each held-back message is counted with its reason. Change them in **Connections → Alerts → Noise control** (administrators).

| Step | What it does | Default |
| --- | --- | --- |
| False-alarm verdicts | A problem your team marked *false alarm* or *expected* in a project report is no longer messaged. | On |
| Flapping | A problem that starts 3 times or more in an hour gets one message saying it keeps starting and stopping. Further starts stay quiet until it has been stable for 30 minutes. | On |
| Grace period | The problem must still be there after a short wait. A blip that clears by itself is never sent and never starts an automatic investigation. | Critical 60 s, warning 5 min |
| Grouping | Problems that start within the same window become one message, listing what they share: a node in trouble, the same namespace or cluster, a dependency they all mention (such as `postgres:5432`), or a change recorded in the 20 minutes before. One automatic investigation then looks at all of them. | 90 s |
| Hourly limit | Each channel gets at most this many messages an hour. The rest arrive as one summary when the hour is up. | 12 |

Recovery messages skip these checks and go only to channels that heard about the problem.

The panel also shows the last 7 days: problems started, messages sent, and how many were held back for each reason. If *held back* stays near zero while people complain about noise, shorten nothing and look at the alert rules themselves: a rule that fires for normal behaviour should be fixed at the source.

**Why it matters for cost.** An email or chat message costs almost nothing. What costs money is the automatic investigation a critical message starts, since each one calls the AI model, and the hours people spend reading alerts that needed no action. The grace period stops investigations for blips, and grouping turns an incident with ten symptoms into one investigation instead of ten.

Server-wide defaults: `RCC_ALERT_GRACE_SECONDS`, `RCC_ALERT_WARNING_GRACE_SECONDS`, `RCC_ALERT_GROUP_SECONDS`.

## Incoming webhooks

Two addresses let other tools push to the console:

| Address | Who sends to it | What it does |
|---|---|---|
| `/api/hooks/alertmanager/TOKEN` | Prometheus Alertmanager | Records firing alerts as events, notifies your channels, and can start automatic investigations |
| `/api/hooks/changes/TOKEN` | GitHub, GitLab, CI/CD pipelines, scripts | Records deployments and changes, so investigations and projects can link a failure to the change before it |

Webhooks only add events. Nothing sent to them can change a system.

### Create the addresses

Admins select **Create addresses** under **Connections > Incoming webhooks**. Copy both addresses straight away: they are shown once, and the console stores only a hash of the token. **Replace addresses** makes new ones and stops the old ones working.

> **Security:** The address is the password. Its token is 32 random bytes, so it can't be guessed, but anyone who has the address can send fake alerts and changes. Keep it out of Git, screenshots, and tickets. If one leaks, replace the addresses.

### Connect Alertmanager

Add a receiver to `alertmanager.yml`:

```yaml
route:
  receiver: root-cause-console
  group_wait: 30s
  repeat_interval: 4h
receivers:
  - name: root-cause-console
    webhook_configs:
      - url_file: /etc/alertmanager/secrets/rcc-webhook/url   # or url: https://...
        send_resolved: false
```

`url_file` reads the address from a file, so it doesn't sit in the config. On Kubernetes, store it in a Secret:

```bash
kubectl -n monitoring create secret generic rcc-webhook \
  --from-literal=url='https://rcc.company.com/api/hooks/alertmanager/TOKEN'
```

With **kube-prometheus-stack**, mount it and set the receiver in your Helm values:

```yaml
alertmanager:
  alertmanagerSpec:
    secrets: [rcc-webhook]          # mounted at /etc/alertmanager/secrets/rcc-webhook
  config:
    route:
      receiver: root-cause-console
    receivers:
      - name: root-cause-console
        webhook_configs:
          - url_file: /etc/alertmanager/secrets/rcc-webhook/url
            send_resolved: false
```

```bash
helm upgrade RELEASE prometheus-community/kube-prometheus-stack -n monitoring -f values.yaml
```

If the console uses a certificate that Caddy made itself, Alertmanager must trust it. Copy Caddy's root certificate, then point `http_config.tls_config.ca_file` at it:

```bash
docker compose exec caddy cat /data/caddy/pki/authorities/local/root.crt > caddy-root.crt
```

Only `firing` alerts are recorded. An alert's `severity` label of `critical`, `page`, or `error` makes it critical; anything else is a warning. When a Prometheus source is also connected, its own poll sends the notification, so channels don't get the alert twice.

### Send changes from a pipeline

Send JSON with a `title`, and optionally `service`, `origin`, `detail`, and `ts` (ISO 8601):

```bash
curl -fsS -X POST "$RCC_CHANGES_URL" -H 'Content-Type: application/json' \
  -d '{"title": "Deployed checkout-api 2.4.1", "service": "checkout-api", "origin": "GitHub Actions"}'
```

Store `RCC_CHANGES_URL` as a secret in your CI system, never in the pipeline file.

In GitHub, a repository webhook (Settings > Webhooks, content type `application/json`) is understood directly: pushes, deployments, releases, and finished workflow runs. GitLab's project webhooks are understood too. Both need the console to be reachable from the internet, so for an internal console send changes from your pipeline runners instead.

### Limits

| Limit | Value |
|---|---|
| Calls | 120 a minute |
| Body size | 1 MB |
| Alerts per call | 100 |
| Wrong token | `404 Unknown webhook address` |

### Test a webhook by hand

```bash
curl -fsS -X POST "https://rcc.company.com/api/hooks/alertmanager/TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"alerts":[{"status":"firing","labels":{"alertname":"TestAlert","severity":"warning"},"annotations":{"summary":"Testing the webhook"}}]}'
```

The answer is `{"accepted": 1}`, and the alert appears on the overview. For a test from inside Docker Desktop, see [Deploy on localhost](/docs/deploy-localhost#reach-the-console-from-the-cluster).
