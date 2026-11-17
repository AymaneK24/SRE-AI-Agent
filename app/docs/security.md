# Security

The console holds read access to your monitoring and, through it, a detailed map of your systems. Treat it like any other privileged internal tool. This page explains how it's protected, and what you should do.

## How it's protected

**Read-only by design.** There's no feature that changes a monitored system. The AI model has read-only tools, runbooks are text for people, and alert messages have no action buttons. A test fails if code that runs processes or remote shells appears in the app.

**Accounts.** Passwords use Argon2, with at least 12 characters, and common passwords are refused. Sessions are stored as hashes on the server, in `HttpOnly`, `SameSite=Lax` cookies marked `Secure` over HTTPS. Repeated failed sign-ins pause that device for 15 minutes and slow further attempts on the account, without locking its owner out. Each person can have at most 5 sessions. Sign-in and reset answers don't reveal whether an email has an account. Accounts exist only by invitation or single sign-on.

**Access.** Roles are checked on the server for every request, and investigations are private until shared. Viewers get a reduced view, with no addresses, queries, labels, prompts, or settings. See [Roles and permissions](/docs/roles-and-permissions).

**Secrets.** Saved credentials and API keys are encrypted with `RCC_SECRET_KEY`, and never returned to the browser. Webhook tokens and one-time email tokens are stored as hashes.

**Outgoing connections.** One guard checks every connection the server makes: cloud metadata and link-local addresses, the server's own addresses, disguised IP addresses, and wildcard DNS services are always refused; checks repeat when connecting, which stops DNS rebinding; and redirects aren't followed. `RCC_TARGET_ALLOWLIST` narrows it further.

**Browser.** A strict Content Security Policy with no inline scripts or styles, HSTS over HTTPS, `Host` and `Origin` checks, JSON-only write requests that refuse repeated keys, a request size limit, and sanitised model output.

**Container.** Built in two stages on Alpine, running as user 10001, with a read-only root filesystem, all Linux capabilities dropped, and `no-new-privileges`. The image has no shell, package manager, or pip.

**AI data.** Claude and Gemini stay off until an admin consents for each, and an owner can block them. Obvious secrets are removed by pattern before sending. Telemetry is marked as untrusted data, and the model is told never to follow instructions in it.

**Audit.** Security-relevant actions are written to a hash-chained audit trail that admins can verify and export.

## Production checklist

### Network

- [ ] Run a **private** server: no public IP, reached over VPN, ExpressRoute, or your office network. If it must be on the internet, allow only known IP ranges, require single sign-on, and consider a WAF.
- [ ] Publish only 443, and 80 only for Let's Encrypt over the internet. Never publish 8000 or 5432.
- [ ] Give the VM outbound access through a NAT gateway or Azure Firewall, and administer it through Azure Bastion rather than an open SSH port.
- [ ] Use your company's certificate, or Let's Encrypt through Azure DNS, so nothing needs inbound internet access.
- [ ] Set `RCC_TARGET_ALLOWLIST` to the systems the console should reach.
- [ ] Restrict Ollama, Prometheus, and Loki so only the console's server (and your people) can reach them.

### Identity

- [ ] Turn on [single sign-on](/docs/single-sign-on), with two-factor sign-in at your provider, then **Require single sign-on**.
- [ ] Keep the break-glass account's password in your vault, and watch the audit trail for its use.
- [ ] Few admins, one or two owners, most people viewers.
- [ ] Limit invitations to your email domains in **Security policies**.
- [ ] Review members every quarter.

### Secrets

- [ ] Store `.env` (or the Kubernetes Secret) in a vault, and back up `RCC_SECRET_KEY` separately from database backups.
- [ ] `chmod 600 .env`, and never commit it.
- [ ] Use read-only credentials for every connection, with the permissions listed in each connection's page.
- [ ] Note expiry dates: Kubernetes tokens, Azure client secrets, company certificates.
- [ ] Prefer keys entered in the app (encrypted) over `ANTHROPIC_API_KEY` or `RCC_GEMINI_API_KEY` in `.env`.

### Data

- [ ] Decide whether a cloud AI model may receive your host names and logs. If not, turn off **Allow cloud AI models** and use Ollama.
- [ ] Set log retention to what your policies allow.
- [ ] Encrypt the server's disks and your backups.
- [ ] Export the audit trail regularly to your SIEM.

### Operations

- [ ] Rebuild the image at least monthly with `--pull`, and scan it.
- [ ] Apply OS updates (`unattended-upgrades` on Ubuntu).
- [ ] Test a restore.

## If you think the console was compromised

1. Take it off the network, or stop it: `docker compose stop console`.
2. Export the audit trail and check sign-ins, role changes, connection changes, and webhook rotations.
3. Revoke and re-create every credential stored in it: Kubernetes tokens, cloud keys, Loki and Prometheus tokens, AI keys, alert channel webhooks.
4. Replace the webhook addresses.
5. End every session by changing each person's role and back, or deactivate them until you're done.
6. Rebuild from a fresh image, and restore the database from a backup made before the incident if you can't trust it.

## Known gaps

- Rate limits are per process and reset on restart.
- There's no CAPTCHA, because many installations have no internet access, so a distributed password attack is slowed, not stopped. Single sign-on avoids this.
- There's no built-in two-factor sign-in. Use single sign-on.
- The database isn't encrypted by the app. Use disk encryption.
- Prompt injection can't be fully prevented. A log line could mislead the model into a wrong answer, but it can't make the console change anything.
- Someone with write access to the database could rewrite the audit chain consistently, which is why exports matter.
- `RCC_SECRET_KEY` can't be rotated in place yet.

## Report a vulnerability

For a problem in one installation, email the contact address on its legal notice. For a problem in the software itself, email its creator at [aymanekenbouch@gmail.com](mailto:aymanekenbouch@gmail.com). Include steps to reproduce it. Please allow time for a fix before sharing details. The project's `SECURITY.md` has the full policy, and `docs/PENTEST-RESPONSE.md` lists how each finding of the last independent test was addressed.
