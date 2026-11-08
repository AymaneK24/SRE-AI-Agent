# Certificates

The console is always served over HTTPS by Caddy. Production mode refuses to start with an `http://` public address. Choose a certificate when you run `deploy/setup-env.sh`, or later by changing `RCC_CADDYFILE` (and `RCC_CADDY_IMAGE` for choice 3) in `.env` and running `docker compose up -d`.

We recommend a **private** server, reached over your VPN or company network, with one of the first three choices. See [Private or public](/docs/deploy-azure-vm#private-or-public).

| # | Choice | Caddyfile | Server | Use it when | What you do |
|---|---|---|---|---|---|
| 1 | **Your company's certificate** (recommended) | `deploy/Caddyfile.internal` | Private | You have a company certificate authority | Put `cert.pem` and `key.pem` in `deploy/certs` |
| 2 | Made by this server | `deploy/Caddyfile.selfsigned` | Private | A trial, a small team, or only an IP address | Accept the browser warning, or install Caddy's root certificate on each device |
| 3 | Let's Encrypt through Azure DNS | `deploy/Caddyfile.azure-dns` | Private | You want a certificate every browser trusts, with nothing open to the internet | A public Azure DNS zone, and the `rcc-caddy-dns:2` image |
| 4 | Let's Encrypt over the internet | `deploy/Caddyfile` | Public | People must reach the console from anywhere | Open ports 80 and 443 to the internet |

## Your company's certificate

1. Ask your PKI team for a certificate for the console's name, with the name in the **subjectAltName**, and include the intermediate certificates.
2. Save the certificate chain (server certificate first, then intermediates) as `deploy/certs/cert.pem`, and the private key as `deploy/certs/key.pem`.
3. Set `RCC_CADDYFILE=./deploy/Caddyfile.internal` in `.env`, then `docker compose up -d`.

```bash
# Make a key and a certificate request to send to your PKI team
openssl req -new -newkey rsa:3072 -nodes -keyout deploy/certs/key.pem -out rcc.csr \
  -subj "/CN=rcc.company.internal" -addext "subjectAltName=DNS:rcc.company.internal"

# Check what you received
openssl x509 -in deploy/certs/cert.pem -noout -subject -issuer -enddate -ext subjectAltName
diff <(openssl x509 -in deploy/certs/cert.pem -noout -pubkey) <(openssl pkey -in deploy/certs/key.pem -pubout) && echo "key matches"

chmod 600 deploy/certs/key.pem
```

> **Security:** Never commit `deploy/certs/*.pem`. The repository ignores them. Note the expiry date: Caddy doesn't renew a certificate you supply.

Caddy runs as root inside its container, so it can read `key.pem` with mode 600 when the file is owned by root. On Kubernetes, put the pair in a TLS Secret instead: `kubectl -n rcc create secret tls rcc-tls --cert=cert.pem --key=key.pem`.

## Made by this server

Caddy creates its own certificate authority and signs a certificate for `RCC_DOMAIN`, which can be an IP address. Traffic is encrypted, but browsers can't confirm whose server it is, so they warn on the first visit.

To trust it on company computers, distribute Caddy's root certificate with your device management (Intune, Group Policy):

```bash
docker compose exec caddy cat /data/caddy/pki/authorities/local/root.crt > rcc-root.crt
```

Tools that call the console, such as Alertmanager, need the same file in their trusted CAs, for example `http_config.tls_config.ca_file`.

## Let's Encrypt through Azure DNS

Let's Encrypt can check that you control a name in two ways. Over the internet (choice 4), it connects to your server on port 80. Through DNS (choice 3, the **DNS-01** challenge), Caddy creates a temporary `_acme-challenge` TXT record in a public DNS zone and Let's Encrypt reads it, so nothing connects to your server.

What you need:

- A name under a domain you own, such as `rcc.company.com`, with a **public** Azure DNS zone for it that holds only the challenge records, and a **private** DNS zone of the same name that resolves it to the server's private address.
- The VM's managed identity with the **DNS Zone Contributor** role on that public zone only.
- Caddy built with the Azure DNS plugin: `docker build --pull -t rcc-caddy-dns:2 deploy/caddy-dns`.
- Outbound access to `acme-v02.api.letsencrypt.org`, `management.azure.com`, and public DNS resolvers.

[Deploy on an Azure VM](/docs/deploy-azure-vm#let-s-encrypt-through-azure-dns) has every command. Renewal is automatic, like choice 4.

> **Note:** Certificates from Let's Encrypt are published in public Certificate Transparency logs, so the console's name becomes publicly known, though nobody outside your network can resolve or reach it. If the name itself must stay secret, use your company's certificate.

## Let's Encrypt over the internet

Only for a [public server](/docs/deploy-azure-vm#public-setup).

Requirements:

- `RCC_DOMAIN` is a public DNS name that points at the server, such as `rcc.company.com`.
- Ports 80 and 443 reach Caddy from the internet. Let's Encrypt checks port 80 to confirm you control the name.

```bash
dig +short rcc.company.com                 # must show the server's public IP
docker compose logs caddy | grep -iE 'certificate|error'
```

Renewal is automatic, about 30 days before expiry. Caddy keeps certificates in the `caddy-data` volume, so don't delete it: Let's Encrypt limits how often a name can get a new certificate.

## Certificates the console checks

The console also checks the certificates of the systems it connects to:

| Connection | Private CA supported? |
|---|---|
| Kubernetes | Yes: paste the cluster's CA, and a TLS server name if needed |
| Prometheus, Loki, alert channels, SSO | No: they need a publicly trusted certificate, or `http` on a private network |
| PostgreSQL (`RCC_DATABASE_URL`) | Uses the driver's `sslmode`, such as `?sslmode=require` |

Turn off certificate checks only for a test system, never in production.
