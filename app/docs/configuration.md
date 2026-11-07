# Configuration

The console reads its settings from environment variables once, at start. With Docker Compose they live in `.env`; on Kubernetes, in the `rcc-config` ConfigMap and the `rcc-secrets` Secret. After a change, restart: `docker compose up -d`, or `kubectl -n rcc rollout restart deployment/console`.

Everything else, such as connections, AI models, alert channels, roles, and policies, is set in the app and stored in the database.

## Address and access

| Variable | Default | Meaning |
|---|---|---|
| `RCC_DOMAIN` | None | The host name or IP address Caddy serves, such as `rcc.company.com` |
| `RCC_PUBLIC_URL` | `http://localhost:8000` | The full address people use. Links in emails use it. Must be `https://` in production mode |
| `RCC_ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1]` | Host names the console answers to, separated by commas. The public address's host is always added. Requests for any other host get `400` |
| `RCC_HTTPS_PORT`, `RCC_HTTP_PORT` | `443`, `80` | Ports Caddy publishes |
| `RCC_CADDYFILE` | `./deploy/Caddyfile.selfsigned` | Which certificate setup: `Caddyfile.internal` (company certificate, recommended), `Caddyfile.selfsigned`, `Caddyfile.azure-dns` (Let's Encrypt through Azure DNS), or `Caddyfile` (Let's Encrypt over the internet). See [Certificates](/docs/certificates) |
| `RCC_CADDY_IMAGE` | `caddy:2-alpine` | The Caddy image. `rcc-caddy-dns:2`, built from `deploy/caddy-dns`, for `Caddyfile.azure-dns` |
| `AZURE_SUBSCRIPTION_ID`, `AZURE_RESOURCE_GROUP_NAME` | None | Only for `Caddyfile.azure-dns`: where the public DNS zone is. Caddy signs in with the VM's managed identity |
| `RCC_TRUST_PROXY` | `false` | Trust `X-Forwarded-*` headers. The Compose and Kubernetes files set it to `true`, because Caddy is in front. Never set it when the console is reached directly |
| `RCC_HOST`, `RCC_PORT` | `0.0.0.0`, `8000` in the image | Where the app listens inside its container |

## Mode and security

| Variable | Default | Meaning |
|---|---|---|
| `RCC_MODE` | `self_hosted` | `production` adds start-up checks: HTTPS address, a secret key, PostgreSQL, your own admin email and password, real host names. The Compose and Kubernetes files set `production`. `hosted` is for a shared multi-company service and isn't the normal setup |
| `RCC_SECRET_KEY` | None | Encrypts every saved credential. 64 hex characters: `openssl rand -hex 32`. **Never change or lose it**: saved credentials can't be read without it |
| `RCC_TARGET_ALLOWLIST` | Empty, allowing any private address | Host names, IP addresses, and ranges the console may connect to. See [Connections](/docs/connections#where-the-console-is-allowed-to-connect) |
| `RCC_ALLOW_PRIVATE_TARGETS` | `true` except in hosted mode | Allow private addresses as targets |
| `RCC_ALLOW_LOOPBACK_TARGETS` | `false` | Allow `localhost` as a target. Only for running the app directly during development |
| `RCC_METRICS_TOKEN` | None | Bearer token for scraping the console's own `/metrics`. Without it, only an owner's session can read them |

## Database

| Variable | Default | Meaning |
|---|---|---|
| `RCC_DATABASE_URL` | Empty, meaning SQLite in `RCC_DATA_DIR` | PostgreSQL address, such as `postgresql://rcc:PASSWORD@postgres:5432/rcc`. Add `?sslmode=require` for a managed database. Production mode requires PostgreSQL |
| `POSTGRES_PASSWORD` | None | Password of the bundled PostgreSQL container |
| `RCC_DATA_DIR` | `/data` in the image | Where SQLite and temporary files go |

## The first administrator

| Variable | Meaning |
|---|---|
| `RCC_ADMIN_EMAIL` | The first owner's email, used to sign in |
| `RCC_ADMIN_NAME` | Their name |
| `RCC_ADMIN_PASSWORD` | Their first password. They must change it at first sign-in |
| `RCC_COMPANY_NAME` | The organization's name, shown in the app and emails |

These are only used when the database has no account yet. Changing them later doesn't change existing accounts.

## Email

| Variable | Default | Meaning |
|---|---|---|
| `RCC_SMTP_HOST` | None | Your mail server, such as `smtp.office365.com`, your company relay, or `smtp.gmail.com` |
| `RCC_SMTP_PORT` | `587` | |
| `RCC_SMTP_TLS` | `starttls` | `starttls` (port 587), `ssl` (port 465), or `none` (only for a relay on a trusted network) |
| `RCC_SMTP_USERNAME`, `RCC_SMTP_PASSWORD` | None | Sign-in. For Gmail, an app password, which needs two-step verification on the account |
| `RCC_SMTP_FROM` | None | The address mail comes from. Use the address the account is allowed to send as |
| `RCC_ALERT_EMAIL_WAIT_SECONDS` | `600` | How long a critical alert's email waits for its automatic investigation |

Without an email server, invitation links are shown in the app for you to send, and password reset links are written to the console's log.

## AI models

| Variable | Default | Meaning |
|---|---|---|
| `RCC_OLLAMA_URL` | `http://localhost:11434`, and `http://host.docker.internal:11434` in `.env` | The default Ollama address. It can be changed in the app |
| `RCC_GEMINI_API_KEY` | None | A Gemini key. When set, it can't be changed in the app |
| `ANTHROPIC_API_KEY` | None | A Claude key. When set, it can't be changed in the app |

Keys entered in the app are encrypted with `RCC_SECRET_KEY` instead. Prefer that, so keys aren't in a plain-text file.

## Other

| Variable | Meaning |
|---|---|
| `RCC_IMAGE` | The image name Compose builds and runs. Default `root-cause-console:latest` |
| `RCC_CONTACT_EMAIL` | A contact address shown on public pages. Empty means none is shown |
| `RCC_OPERATOR_NAME` | Who operates this installation, shown on public pages |
| `RCC_LEGAL_REVIEWED` | Set to `true` once your legal team has reviewed the terms pages |

## Settings in the app

| Setting | Where | Who |
|---|---|---|
| Connections, AI models, alert channels, webhooks | Connections | Admins |
| Members and roles | Organization > Members | Admins |
| Monthly AI budget, tokens per investigation | Organization > Usage | Admins |
| Shortest password, session length, allowed email domains, require single sign-on, allow cloud AI, log retention | Organization > Security policies | Owners |
| Single sign-on | Organization > Single sign-on | Owners |
| Automatic investigations | Connections > Alerts | Admins |
