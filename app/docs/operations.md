# Operations

Day-to-day running: backups, updates, health, monitoring the console itself, and rotating secrets.

## What to back up

| Item | Why | How |
|---|---|---|
| The PostgreSQL database | Everything: accounts, connections, investigations, events, reports, runbooks | `sh deploy/backup.sh` daily |
| `RCC_SECRET_KEY` | Without it, saved credentials in a backup can't be read | Your vault or password manager, **separately** from database backups |
| `.env` | All settings | Your vault |
| `deploy/certs` | A company certificate and key | Your PKI or vault |
| The `caddy-data` volume | Let's Encrypt certificates. Optional: Caddy gets new ones | Volume or VM backup |

```bash
# Docker Compose: writes backups/rcc-TIMESTAMP.sql.gz, keeps 14 days
sh deploy/backup.sh
# Kubernetes, in-cluster PostgreSQL
kubectl -n rcc exec statefulset/postgres -- pg_dump -U rcc -d rcc | gzip > rcc-$(date +%F).sql.gz
```

Copy backups off the server, and test a restore every few months.

> **Security:** A database backup holds encrypted credentials, investigation text, and host names. Store backups encrypted, with access limited like the server itself.

## Restore

Restore into an **empty** database, with the **same** `RCC_SECRET_KEY`:

```bash
docker compose stop console
docker compose exec -T postgres dropdb -U rcc rcc
docker compose exec -T postgres createdb -U rcc rcc
gunzip -c backups/rcc-TIMESTAMP.sql.gz | docker compose exec -T postgres psql -U rcc -d rcc
docker compose start console
```

## Update

```bash
sh deploy/backup.sh
git pull
docker compose build --pull console
docker compose up -d
docker compose logs -f console
```

The database schema is upgraded on start, keeping your data. Ask people to reload with Ctrl+Shift+R so their browser loads the new pages.

## Health and logs

| Check | Meaning |
|---|---|
| `GET /healthz` | The process is up |
| `GET /readyz` | The database answers |
| `docker compose ps` | Each container's state and health |
| `docker compose logs console` | The app's log: start-up problems, connection errors, and invitation or reset links when no email server is set |

The image has no shell, so `docker exec ... sh` doesn't work by design. Use the logs.

## Monitor the console itself

The console exposes Prometheus metrics at `/metrics`. Caddy blocks that path on the public address, so scrape the console directly on its internal port, with a token:

```bash
# .env
RCC_METRICS_TOKEN=$(openssl rand -hex 32)
```

```yaml
# Prometheus scrape config, from a Prometheus on the same Docker network or cluster
- job_name: root-cause-console
  metrics_path: /metrics
  authorization:
    credentials_file: /etc/prometheus/rcc-metrics-token
  static_configs:
    - targets: ["console:8000"]
```

The scraper must use a host name in `RCC_ALLOWED_HOSTS`, or the console answers `400`. Add `console` to that list when scraping by Service name.

## Scan the images

```bash
docker compose build --pull console
trivy image --severity HIGH,CRITICAL,MEDIUM root-cause-console:latest
trivy image postgres:16-alpine
trivy image caddy:2-alpine
```

The console's image is built on Alpine in two stages, runs as user 10001, and has no shell, package manager, or pip. Security fixes arrive when you rebuild, so rebuild at least monthly.

## Rotate secrets

| Secret | How |
|---|---|
| Connection credentials (tokens, keys, passwords) | Create a new one at the source, paste it into the connection, save, then revoke the old one |
| AI API keys | Paste the new key and **Save key**, then delete the old one at Google or Anthropic |
| Webhook addresses | **Replace addresses**, then update Alertmanager and your pipelines |
| A person's sessions | Changing their role, or deactivating them, ends their sessions |
| `POSTGRES_PASSWORD` | `docker compose exec postgres psql -U rcc -c "ALTER USER rcc PASSWORD 'NEW'"`, then update `.env` and `docker compose up -d` |
| `RCC_ADMIN_PASSWORD` | Only used once, for the first account. Each person changes their own password under their name |
| `RCC_SECRET_KEY` | Can't be rotated in place yet. A new key makes every saved credential unreadable, so they'd all have to be entered again |

## Scaling

Run **one** console. Live investigations, rate limits, and monitoring are held in one process, so several copies would each poll your systems and send duplicate alerts. Give it more CPU and memory instead. PostgreSQL can be a managed, highly available service.

## Known limits

- Rate limits are per process and reset on restart.
- No distributed traces. The console can only find causes its connections can show.
