# Deploy Root Cause Console

It runs inside your own infrastructure, in the same way as a self-hosted tool such as SonarQube Community. The server connects to your systems directly, and your telemetry stays there unless you choose Claude as the AI model.

## Install

You need a Linux server with Docker, 2 CPUs, and 4 GB of memory. It must be able to reach the systems you want to monitor. On Ubuntu, `sh deploy/install-docker.sh` installs Docker.

```bash
sh deploy/setup-env.sh
docker compose up -d --build
```

This starts the app, PostgreSQL, and Caddy for HTTPS. Only Caddy has published ports. The app refuses to start with unsafe settings.

The script asks a few questions, writes `.env` with new secrets, and prints your first password. **Save `.env` somewhere safe outside the server.** Without `RCC_SECRET_KEY`, saved credentials can't be read.

Open the address it printed, and sign in with your email and that password. You must choose a new password. Then:

1. **Connections:** add what you have: metric sources, Loki, a Kubernetes cluster. Any one of them is enough. Then choose an AI model.
2. **Projects:** group sources by system.
3. **Organization > Members:** invite your team.

**The AI model must be reachable too.** Use an Ollama server on your network, or allow outbound access to `api.anthropic.com` for Claude or `generativelanguage.googleapis.com` for Gemini. Inside Docker, use `host.docker.internal` for something on the same machine, not `localhost`.

## Choose a certificate

The script asks which one fits your network:

We recommend a private server, reached over VPN or your company network, with one of the first three choices.

| Choice | Server | Use it when | What you do |
|---|---|---|---|
| 1. Your company's certificate (recommended) | Private | You have an internal host name and a company certificate authority | Put `cert.pem` and `key.pem` in `deploy/certs` |
| 2. Made by this server | Private | You only have an IP address, or are trying it out | Accept the browser warning on first visit, or install Caddy's root certificate on each device |
| 3. Let's Encrypt through Azure DNS | Private | You want a certificate browsers trust, with nothing open to the internet | Build `deploy/caddy-dns`, and give the VM's identity DNS Zone Contributor on a public Azure DNS zone |
| 4. Let's Encrypt over the internet | Public | People must reach the console from anywhere | Open ports 80 and 443 to the internet |

To change later, edit `RCC_CADDYFILE` in `.env`, then run `docker compose up -d`.

## Limit where the server connects

Admins can connect internal systems, which is how this product works. To allow only certain targets, add this to `.env`:

```bash
RCC_TARGET_ALLOWLIST=prometheus,10.20.0.0/16
```

This server's own addresses, cloud metadata addresses, disguised IP addresses, and services like nip.io are always refused. For a target on the app's own machine when running it directly without Docker, set `RCC_ALLOW_LOOPBACK_TARGETS=true`.

## Run on Kubernetes

`deploy/k8s` holds files for the app, PostgreSQL, Caddy, the read-only account (`reader-rbac.yaml`), and optional NetworkPolicies (`networkpolicy.yaml`). The **Deploy on AKS** page in the console's docs walks through every command. Keep the app at one replica, because it holds live runs and monitoring in memory. Replace `ACR_NAME` and `IMAGE_TAG`, and create the `rcc-secrets` Secret and the `rcc-config` and `caddyfile` ConfigMaps first. Put `RCC_MODE=production`, `RCC_PUBLIC_URL`, `RCC_ALLOWED_HOSTS`, `RCC_ADMIN_EMAIL`, and `RCC_ADMIN_NAME` in the ConfigMap, and `RCC_ADMIN_PASSWORD`, `RCC_SECRET_KEY`, `POSTGRES_PASSWORD`, and `RCC_DATABASE_URL` in the Secret. Storage uses Azure's `managed-csi` class, so change it on another platform.

## With no internet access on the server

Build the images on a machine with internet access, copy them across, then start without building:

```bash
docker build -t root-cause-console:latest .
docker pull postgres:16-alpine
docker pull caddy:2-alpine
docker save root-cause-console:latest postgres:16-alpine caddy:2-alpine | gzip > rcc-images.tar.gz
# On the server:
gunzip -c rcc-images.tar.gz | docker load
sh deploy/setup-env.sh
docker compose up -d --no-build
```

With no internet access, Let's Encrypt can't work, so choose one of the other two certificates.

## Add people

Go to **Organization > Members** and invite each person with their name and email. People sign in with their email, and can change their name and email under their name at the top of the page. There's no public sign-up, so nobody can create an account without an invitation.

If no email server is set up, the console shows the invitation link so you can send it yourself. Password reset emails need an email server. Without one, the reset link is written to the console's log.

To use a company sign-in such as Microsoft Entra ID or Okta, set it up in **Organization > Single sign-on**.

## Operate it

| Task | How |
|---|---|
| Back up | `sh deploy/backup.sh`, daily by cron. Copy the files to another location |
| Restore | `gunzip -c FILE.sql.gz \| docker compose exec -T postgres psql -U rcc -d rcc` on an empty database, with the same `RCC_SECRET_KEY` |
| Update | Back up, replace the project files without touching `.env`, then run `docker compose up -d --build`. The database is upgraded on start, keeping your data. **Upgrading from an earlier version:** add `RCC_ADMIN_EMAIL`, `RCC_ADMIN_NAME`, `RCC_DOMAIN`, and `RCC_CADDYFILE` to `.env` first, and sign in with your email. An account that had no email gets `USERNAME@rootcause.local` until you change it |
| Health | `/healthz` shows that the process is up, and `/readyz` shows that the database is reachable |
| Metrics | `/metrics`, with `RCC_METRICS_TOKEN` or an owner's session. Caddy blocks it on the HTTPS address |
| Audit log | **Logs > Audit trail**. Export it regularly, because someone with database access could rewrite the whole chain |

## Keep the image secure

The image is built in two stages on Alpine, runs as a non-root user (ID 10001), and has one entrypoint: `python -m app`. It has no pip, package manager, or shell, so `docker exec ... sh` doesn't work by design. Use `docker compose logs console` to see what's happening.

Security fixes only arrive when you rebuild, so do this regularly and scan the result:

```bash
docker compose build --pull console
docker compose up -d
trivy image --severity HIGH,CRITICAL,MEDIUM root-cause-console:latest
```

Scan `postgres:16-alpine` and `caddy:2-alpine` too, because they're part of what you run.

## Connect alerts

**Slack:** create an Incoming Webhook in your Slack workspace, then add it in **Connections > Alerts**.

**What an alert email contains:** with **Investigate critical alerts automatically** on, the email for a critical alert waits for the investigation, then carries the findings, charts of the evidence, commands from your reviewed workflows, and a PDF report. It waits 10 minutes at most (`RCC_ALERT_EMAIL_WAIT_SECONDS`). After that the alert goes out, and the findings follow in a second email. If the investigation fails or the console restarts, the alert goes out without findings. Slack, Teams, and Google Chat get the alert straight away, then a short summary. These emails contain queries and host names, whatever the recipient's role, so choose a channel's recipients with care.

**Email:** set `RCC_SMTP_HOST`, `RCC_SMTP_PORT`, `RCC_SMTP_USERNAME`, `RCC_SMTP_PASSWORD`, and `RCC_SMTP_FROM`, using only the address in `RCC_SMTP_FROM`. Your company's mail relay is usually the easiest choice. Gmail needs an app password and internet access to `smtp.gmail.com` on port 587. Then add an email channel in **Connections > Alerts**.

**Alertmanager:** select **Create addresses** under **Incoming webhooks**, then add this to `alertmanager.yml`:

```yaml
receivers:
  - name: root-cause-console
    webhook_configs:
      - url: PASTE_THE_ALERTMANAGER_ADDRESS
```

**Deployments:** send a JSON event from your pipeline to the changes address. Projects use these to spot problems that follow a change:

```bash
curl -X POST "$RCC_CHANGES_URL" -H 'Content-Type: application/json' \
  -d '{"title": "Deployed checkout-api 2.4.1", "service": "checkout-api"}'
```

## Read-only credentials for sources

| Source | Give it |
|---|---|
| Amazon CloudWatch | `CloudWatchReadOnlyAccess`, plus `cloudtrail:LookupEvents` for change events |
| Azure Monitor | The **Monitoring Reader** role, plus **Log Analytics Reader** for logs |
| Amazon Managed Prometheus | `aps:QueryMetrics`, `aps:GetSeries`, `aps:GetLabels`, and `aps:GetMetricMetadata` |
| Google Managed Prometheus | **Monitoring Viewer** |
| Kubernetes | A ServiceAccount token with the read-only role below. It never needs Secrets |

### A read-only Kubernetes account

The console reads pods and their logs, events, Deployments, Services, and nodes. It never reads Secrets or ConfigMap contents, and it has no code that writes to the cluster. `deploy/k8s/reader-rbac.yaml` creates the `rcc` namespace, a `rcc-reader` ServiceAccount, and a ClusterRole with only `get` and `list` on those resources:

```bash
kubectl apply -f deploy/k8s/reader-rbac.yaml
kubectl -n rcc create token rcc-reader --duration=720h
```

Leaving out `nodes` is fine: node problems then aren't watched, and the console says so. Leaving out `pods/log` means no crash logs in investigations. `services` lets an investigation check whether a Service reaches any ready pod, which explains most "connection refused" errors.

**Connecting to the cluster:**

- **Console inside the cluster:** tick *The console runs inside this cluster*. It uses its own mounted service account, so there's no address or token to enter. Bind the role above to that service account.
- **Console outside the cluster:** enter the API address and the token, and paste the cluster's CA certificate. In a kubeconfig it's `certificate-authority-data`, base64-decoded: `kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' | base64 -d`.
- **Docker Desktop:** the API certificate names `kubernetes.docker.internal`, but a container reaches the host as `host.docker.internal`. Use `https://host.docker.internal:6443` as the address, paste the CA as above, and set **TLS server name** to `kubernetes.docker.internal`.

## What has and hasn't been tested

**Tested together:** PostgreSQL 16, Caddy with a certificate it made itself, real Prometheus, and each role's pages in a real browser. The Dockerfile's steps and the container's restrictions were run on a stand-in base image, because container registries weren't reachable in the test environment. `docker compose config` and the Kubernetes files were checked for validity, but not run.

**Not tested:** the real `python:3.12-slim`, `postgres`, and `caddy` images, the full Compose stack running together, a Kubernetes cluster, Let's Encrypt, a company certificate, the no-internet steps, and real Gmail or Slack delivery. Do a trial install first.

CloudWatch was tested with the real AWS SDK against a simulator. Azure Monitor, managed Prometheus sign-in, Kubernetes (including a private CA and TLS server name), Gemini, and single sign-on were tested against fake servers that follow each API's documented format. Try each with a real account first.
