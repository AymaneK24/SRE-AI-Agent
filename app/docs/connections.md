# Connections

Admins add connections in **Connections**. Engineers and viewers see what's connected, without addresses or credentials.

## What each connection adds

| Connection | Adds to the overview | Adds to investigations | How many |
|---|---|---|---|
| [Prometheus](/docs/prometheus) | Firing alerts, targets, golden signals, saved charts | `run_instant_query`, `run_range_query`, `list_metrics` | Many |
| [Amazon CloudWatch](/docs/cloud-sources#amazon-cloudwatch) | Alarms, chosen metrics, CloudTrail changes | Metric and log queries | Many |
| [Azure Monitor](/docs/cloud-sources#azure-monitor) | Alerts, chosen metrics, Activity Log changes | Metric and log queries | Many |
| [Loki](/docs/loki) | Log streams and error counts | `query_logs` | Many |
| [Kubernetes](/docs/kubernetes) | Workload problems, diagnoses, namespaces | `get_kubernetes_state`, `describe_object`, `get_pod_logs` | Many |
| [Alertmanager and CI/CD](/docs/alerts-and-webhooks#incoming-webhooks) | Alerts and changes, pushed by webhook | `get_recent_changes` | One pair of addresses |

Any single connection is enough. The more you connect, the more evidence the model can check, and the more problems are found: see [Set up a customer's environment](/docs/customer-setup) for what each one covers.

## Many sources, one view

**Metric sources** can be as many as you need: several Prometheus servers (one per cluster, per region, or per team), plus CloudWatch and Azure Monitor. Each has a display name, such as `prod-eu` or `staging`. Everywhere the console shows a target or an alert, it says which source it came from, and the model chooses the source for each query.

**Loki and Kubernetes** can also be several: one Loki per environment, or one cluster per region. Give each a name you'll recognise, such as `aks-prod-westeurope`, because that name appears wherever you choose between them. Use **Add another Loki** or **Add another cluster** under Logs and Kubernetes.

When a cluster's problem appears in Needs attention or What changed, its name follows in brackets, so the same workload in two clusters stays two separate problems.

### The main connection

With two or more of a kind, one is the **main** one. It's the one the overview shows first, and the one investigations use when the model doesn't name another. Until you choose, the oldest is main. An admin changes it with **Make main** next to the connection. Problems in the others are still found and announced; main only decides what's shown first.

## The network path

The console connects **out** to your systems. Nothing needs to connect in, except people's browsers and the optional webhooks.

- The console's server must reach each address you enter. Test it from that server first: see [Finding connection details](/docs/finding-connection-details#test-the-path-before-you-connect).
- Inside Docker, `localhost` means the console's own container. Use `host.docker.internal` for something on the same machine, or the machine's IP address.
- **Test connection** checks the address, the credentials, and the certificate before you save.

## Read every (seconds)

Each metric source is read every 15 seconds by default, from 5 to 600. Shorter is fresher but puts more load on the source. Loki and Kubernetes are read on the same cycle as the console's poll.

**Pause** stops reading a source without deleting it. **Remove** deletes the connection and its panels. Logged investigations stay.

## Credentials

- Use **read-only** credentials for every connection. Each page in this section lists the smallest permissions that work.
- Credentials are encrypted with `RCC_SECRET_KEY` before they're stored, and never sent back to the browser. A saved credential shows as *Saved. Enter a new value to replace it.*
- A saved CA certificate shows as its subject and expiry date, never in full.

## Where the console is allowed to connect

Every outgoing connection passes one guard:

- **Always refused:** cloud metadata addresses (such as `169.254.169.254`), link-local addresses, the console's own addresses, disguised IP addresses, and wildcard DNS services such as `nip.io`. Every check repeats when connecting, which stops DNS rebinding, and redirects aren't followed.
- **Allowed by default:** private addresses such as `10.0.0.0/8`, because your monitoring is usually internal.
- **Your own list:** set `RCC_TARGET_ALLOWLIST` to allow only certain host names, IP addresses, and ranges, separated by commas, for example `RCC_TARGET_ALLOWLIST=prometheus.monitoring.svc,loki.company.internal,10.20.0.0/16`. A host name must match exactly, and an address must be inside a listed range. With a list set, everything else is refused.

The same guard and list apply to Ollama, alert channels (Slack, Teams, and Google Chat webhooks), and your single sign-on provider. So when you set a list, include those too, for example `hooks.slack.com`, and `login.microsoftonline.com` and `graph.microsoft.com` for Microsoft Entra ID. Claude and Gemini are reached at fixed addresses (`api.anthropic.com` and `generativelanguage.googleapis.com`), which your firewall controls.

> **Security:** Anyone with the admin role can make the server send requests into your internal network, because adding connections is their job. Give that role to few people, and set `RCC_TARGET_ALLOWLIST` in production. See [Security](/docs/security).
