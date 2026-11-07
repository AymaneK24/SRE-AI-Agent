# Certificates

The console is always served over HTTPS by Caddy. Production mode refuses to start with an `http://` public address. Choose one of three certificates when you run `deploy/setup-env.sh`, or later by changing `RCC_CADDYFILE` in `.env` and running `docker compose up -d`.

| Choice | Caddyfile | Use it when | What you do |
|---|---|---|---|
| Let's Encrypt | `deploy/Caddyfile` | The console has a public DNS name | Open ports 80 and 443 to the internet. Caddy gets and renews the certificate itself |
| Your company's certificate | `deploy/Caddyfile.internal` | An internal DNS name, and a company certificate authority | Put `cert.pem` and `key.pem` in `deploy/certs` |
| Made by this server | `deploy/Caddyfile.selfsigned` | Only an IP address, or a trial | Accept the browser warning, or trust Caddy's root certificate on each computer |

## Let's Encrypt

Requirements:

- `RCC_DOMAIN` is a public DNS name that points at the server, such as `rcc.company.com`.
- Ports 80 and 443 reach Caddy from the internet. Let's Encrypt checks port 80 to confirm you control the name.

```bash
dig +short rcc.company.com                 # must show the server's public IP
docker compose logs caddy | grep -iE 'certificate|error'
```

Renewal is automatic, about 30 days before expiry. Caddy keeps certificates in the `caddy-data` volume, so don't delete it: Let's Encrypt limits how often a name can get a new certificate.

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

## Certificates the console checks

The console also checks the certificates of the systems it connects to:

| Connection | Private CA supported? |
|---|---|
| Kubernetes | Yes: paste the cluster's CA, and a TLS server name if needed |
| Prometheus, Loki, alert channels, SSO | No: they need a publicly trusted certificate, or `http` on a private network |
| PostgreSQL (`RCC_DATABASE_URL`) | Uses the driver's `sslmode`, such as `?sslmode=require` |

Turn off certificate checks only for a test system, never in production.
