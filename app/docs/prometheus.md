# Prometheus

Add a Prometheus server in **Connections > Metric sources > Prometheus**. You can add as many as you need, such as one per cluster or region.

## The form

| Field | What to enter |
|---|---|
| Display name | How people will recognise it: `prod-eu`, `staging`, `shop-cluster` |
| Read every (seconds) | 15 by default |
| Protocol | `http` or `https` |
| IP address or host name | Only the host, such as `10.20.0.15` or `prometheus.company.internal`. Pasting a full address such as `https://prom.company.com:9090/prom` fills the protocol, port, and path for you |
| Port | `9090` for a plain Prometheus, `443` for most managed and proxied ones |
| Path prefix | Only if Prometheus is served under a path, such as `prometheus` behind an ingress |
| Sign-in | See [Sign-in methods](#sign-in-methods) |
| Check the server's TLS certificate | On for `https`. Turn it off only for a test server |

**Test connection** shows the Prometheus version, the response time, and how many targets are up.

## What the console reads

Only these read endpoints of the Prometheus HTTP API:

| Endpoint | Used for |
|---|---|
| `/api/v1/status/buildinfo` | The version, when testing |
| `/api/v1/targets` | Targets, and which are down |
| `/api/v1/alerts` | Firing alerts |
| `/api/v1/rules` | Failing rules |
| `/api/v1/label/__name__/values` | Finding metric names, for golden signals and the model's `list_metrics` |
| `/api/v1/query`, `/api/v1/query_range` | Golden signals, saved charts, and the model's queries |

Servers that answer the same API, such as Thanos Query, Grafana Mimir, or VictoriaMetrics, should work for queries. Some don't serve `/api/v1/targets` or `/api/v1/alerts`, so the target and alert lists stay empty for them. These haven't been tested.

## Sign-in methods

| Sign-in | Use it for | What you enter |
|---|---|---|
| None | Prometheus on your internal network | Nothing |
| Username and password | Prometheus behind a proxy with basic auth | Username and password |
| Bearer token | A proxy or gateway that expects `Authorization: Bearer` | The token |
| Amazon Managed Prometheus | AWS AMP workspaces | Region, access key ID, and secret access key |
| Azure Managed Prometheus | An Azure Monitor workspace | Tenant ID, client ID, and client secret |
| Google Managed Prometheus | Google Cloud Managed Service for Prometheus | A service account JSON key |

### Prometheus in a Kubernetes cluster

Find its Service:

```bash
kubectl get svc -A | grep -i prometheus
```

With kube-prometheus-stack, it's usually `prometheus-operated` or `RELEASE-kube-prometheus-prometheus` on port `9090` in the `monitoring` namespace.

- **Console in the same cluster:** host `prometheus-operated.monitoring.svc.cluster.local`, port `9090`, protocol `http`.
- **Console outside the cluster:** expose Prometheus on an internal address. Prefer an internal load balancer or an ingress with TLS and basic auth over a public one, since Prometheus has no sign-in of its own:

```bash
# An internal Azure load balancer (AKS). Other clouds have their own annotation.
kubectl -n monitoring patch svc RELEASE-kube-prometheus-prometheus -p \
  '{"metadata":{"annotations":{"service.beta.kubernetes.io/azure-load-balancer-internal":"true"}},"spec":{"type":"LoadBalancer"}}'
kubectl -n monitoring get svc RELEASE-kube-prometheus-prometheus -w   # wait for EXTERNAL-IP
```

> **Security:** Never expose Prometheus to the internet without sign-in. Its API reveals every host, service, and label in your systems.

### Amazon Managed Prometheus

| Field | Value |
|---|---|
| Protocol, port | `https`, `443` |
| Host | `aps-workspaces.REGION.amazonaws.com` |
| Path prefix | `workspaces/WORKSPACE-ID` |
| Sign-in | Amazon Managed Prometheus, with the region |

Create an IAM user or role allowed only `aps:QueryMetrics`, `aps:GetSeries`, `aps:GetLabels`, and `aps:GetMetricMetadata` on that workspace. When the console runs on EC2 or EKS with an IAM role, leave both keys empty and the standard AWS credential chain is used.

```bash
aws amp list-workspaces --query 'workspaces[].[alias,workspaceId]' --output table
```

### Azure Managed Prometheus

| Field | Value |
|---|---|
| Protocol, port | `https`, `443` |
| Host | The workspace's query endpoint host, such as `NAME-abcd.westeurope.prometheus.monitor.azure.com` |
| Sign-in | Azure Managed Prometheus: tenant ID, client ID, client secret |

```bash
# The query endpoint of the Azure Monitor workspace
az monitor account show -n WORKSPACE -g RESOURCE_GROUP --query metrics.prometheusQueryEndpoint -o tsv

# An app registration that can only read it
az ad sp create-for-rbac --name rcc-prometheus-reader --role "Monitoring Data Reader" \
  --scopes $(az monitor account show -n WORKSPACE -g RESOURCE_GROUP --query id -o tsv)
```

The second command prints `appId` (client ID), `password` (client secret), and `tenant`. The console asks Microsoft Entra ID for a token for `https://prometheus.monitor.azure.com`.

### Google Managed Prometheus

| Field | Value |
|---|---|
| Protocol, port | `https`, `443` |
| Host | `monitoring.googleapis.com` |
| Path prefix | `v1/projects/PROJECT_ID/location/global/prometheus` |
| Sign-in | Google Managed Prometheus, with a service account JSON key |

```bash
gcloud iam service-accounts create rcc-reader
gcloud projects add-iam-policy-binding PROJECT_ID \
  --member serviceAccount:rcc-reader@PROJECT_ID.iam.gserviceaccount.com --role roles/monitoring.viewer
gcloud iam service-accounts keys create rcc-reader.json --iam-account rcc-reader@PROJECT_ID.iam.gserviceaccount.com
```

Paste the JSON, then delete the file: `shred -u rcc-reader.json`.

## Certificates

With **Check the server's TLS certificate** on, the certificate must be valid for the host you entered and signed by a publicly trusted authority. A Prometheus with a certificate from a private company CA isn't supported yet for metric sources. Put it behind a proxy with a trusted certificate, or use `http` on a private network segment.

## Many Prometheus servers

Add each one as its own source with a clear name. The overview's **What's watched** shows every target with the source that watches it, and the model picks the source for each query. When the same target is scraped by two servers, it appears once per server.

## Common errors

| Error | Cause and fix |
|---|---|
| `points to this server itself` | You entered `localhost` or `127.0.0.1`. In Docker, use `host.docker.internal` or the host's IP address |
| `isn't in this installation's list of allowed targets` | Add the host or range to `RCC_TARGET_ALLOWLIST` |
| Connection refused, or a timeout | The console's server can't reach that address and port. See [Test the path](/docs/finding-connection-details#test-the-path-before-you-connect) |
| `401` or `403` | Wrong credentials, or the identity lacks the read role |
| Certificate verify failed | A private CA, or the certificate doesn't name that host. See [Certificates](#certificates) |
