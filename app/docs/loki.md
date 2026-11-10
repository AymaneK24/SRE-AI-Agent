# Loki

Connect Grafana Loki in **Connections > Logs and Kubernetes > Loki logs**. The overview then shows log streams and error counts, and the AI model can search logs with `query_logs`.

## The form

| Field | What to enter |
|---|---|
| Address | Loki's base address, such as `http://loki.monitoring.svc.cluster.local:3100` or `https://loki.company.internal`. A path is allowed if Loki is served under one |
| Log selector | Optional. Which streams the overview counts. See [The log selector](#the-log-selector) |
| Read-only token | Optional. Sent as `Authorization: Bearer TOKEN`, for a Loki behind a gateway that checks tokens |
| Tenant | Only for a multi-tenant Loki. Sent as the `X-Scope-OrgID` header |

**Test connection** asks Loki for its label names, and shows how many it has.

## What the console reads

| Endpoint | Used for |
|---|---|
| `/loki/api/v1/labels` | Testing the connection |
| `/loki/api/v1/query` | Every poll: lines and error-like lines per stream in the last 5 minutes |
| `/loki/api/v1/query_range` | The model's `query_logs` searches, at most 40 lines each |

## The log selector

A **stream selector** picks which log streams to read, by their labels. It's always in braces:

```
{namespace="shop"}
```

The console uses your selector for the overview's log counts and the log summary the model receives. If you leave it empty, the console uses `{job=~".+"}`: every stream that has a `job` label.

### Matchers

Inside the braces, each matcher compares one label with a value. Separate matchers with commas, and a stream must match **all** of them.

| Matcher | Means | Example |
|---|---|---|
| `=` | Equals | `{namespace="shop"}` |
| `!=` | Doesn't equal | `{namespace!="kube-system"}` |
| `=~` | Matches a regular expression | `{namespace=~"shop|payments"}` |
| `!~` | Doesn't match a regular expression | `{app!~"debug-.*"}` |

Regular expressions use [RE2 syntax](https://github.com/google/re2/wiki/Syntax) and must match the **whole** value: `{app=~"api"}` matches only `api`, while `{app=~"api.*"}` also matches `api-gateway`.

### The rule Loki enforces

Loki refuses a selector that could match every stream in the store, because that query would read everything. **At least one matcher must select something on its own**: an `=` with a non-empty value, or an `=~` with an expression that can't match an empty value.

| Selector | Accepted? | Why |
|---|---|---|
| `{namespace="shop"}` | Yes | `=` with a value |
| `{namespace=~".+"}` | Yes | `.+` needs at least one character |
| `{namespace=~".*"}` | No | `.*` matches an empty value, so it selects everything |
| `{namespace!="kube-system"}` | No, on its own | Every matcher only excludes |
| `{namespace=~".+", namespace!="kube-system"}` | Yes | The first matcher selects, the second excludes |

Loki's error for a refused selector is: *queries require at least one regexp or equality matcher that does not have an empty-compatible value*.

The console fixes this one case for you: when your selector has no matcher that selects on its own, it adds `job=~".+"` at the front. So `{namespace!="kube-system"}` is used as `{job=~".+", namespace!="kube-system"}`. Queries the model writes are sent to Loki as written.

### How `{namespace=~".+", namespace!="kube-system"}` works

Read it left to right:

1. `namespace=~".+"`: take every stream that has a `namespace` label with at least one character. That's every stream from Kubernetes pods, and it satisfies Loki's rule.
2. `namespace!="kube-system"`: from those, drop the streams whose namespace is exactly `kube-system`.

The result: every Kubernetes log except the system namespace. Streams with no `namespace` label at all, such as logs from a VM, aren't included, because they fail the first matcher.

### Useful selectors

| Goal | Selector |
|---|---|
| One namespace | `{namespace="shop"}` |
| Several namespaces | `{namespace=~"shop|payments|checkout"}` |
| All Kubernetes logs except system ones | `{namespace=~".+", namespace!~"kube-system|kube-public|monitoring"}` |
| One cluster, when several send to one Loki | `{cluster="prod-eu"}` |
| One app in one cluster | `{cluster="prod-eu", app="payment"}` |
| VM logs collected by job | `{job=~"varlogs|syslog"}` |

> **Note:** Label names depend on your log agent. Promtail's Kubernetes setup usually adds `namespace`, `pod`, `container`, `app`, and `job` (as `namespace/app`). Grafana Alloy and Fluent Bit may use other names. Check yours with: `logcli labels`, or in Grafana Explore.

> **Security:** The selector isn't access control. It narrows the overview, but the model's own queries can read any stream the Loki connection can. To limit what the console can read, use a Loki tenant or a gateway token that only grants certain streams.

## LogQL the model uses

After the selector come filters, applied to each line:

| Filter | Keeps lines that |
|---|---|
| `|= "text"` | Contain the text |
| `!= "text"` | Don't contain it |
| `|~ "regex"` | Match a regular expression. `(?i)` makes it ignore case |
| `!~ "regex"` | Don't match it |

```
{namespace="shop", app="payment"} |= "connection refused"
{namespace="shop"} |~ "(?i)(error|exception)" != "healthcheck"
sum by (app) (count_over_time({namespace="shop"} |= "ERROR" [5m]))
```

The last one is a metric query: lines per app in the last 5 minutes. The overview's counts use the same form.

## Error-like lines

The overview counts a line as error-like when it matches `(?i)(error|exception|fatal|panic)`. Loki's own records of the console's queries contain those words too, so they're left out. Streams are grouped by their `job` label: a stream without one shows as `unknown`.

## Finding Loki's address

In Kubernetes:

```bash
kubectl get svc -A | grep -i loki
```

The Grafana Helm chart usually creates `loki` (port 3100) and, in scalable mode, `loki-gateway` (port 80). Use the gateway when there is one.

- **Console in the same cluster:** `http://loki-gateway.monitoring.svc.cluster.local` or `http://loki.monitoring.svc.cluster.local:3100`.
- **Docker Desktop, console in Docker Compose:** make the Service a LoadBalancer, then use `http://host.docker.internal:3100`:

```bash
kubectl -n NAMESPACE patch svc loki -p '{"spec":{"type":"LoadBalancer"}}'
```

- **Another network:** an internal load balancer or an ingress with TLS and a token. Never expose Loki to the internet without sign-in: logs often contain personal data and secrets.

Check it answers before connecting:

```bash
curl -fsS http://LOKI:3100/ready
curl -fsS http://LOKI:3100/loki/api/v1/labels
```

## Limits

- **Several Lokis** are fine: add one per environment and choose the [main one](/docs/connections#the-main-connection). Sending several clusters to one Loki with a `cluster` label also works.
- **Bearer tokens only.** A Loki that needs a username and password, such as Grafana Cloud, needs a gateway in front that accepts a token.
- **Certificates:** an `https` Loki needs a certificate from a publicly trusted authority.
