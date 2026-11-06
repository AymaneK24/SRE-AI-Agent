# Security policy

Have your security team review this before production use. An independent test on 2026-11-05 found 20 issues. [docs/PENTEST-RESPONSE.md](docs/PENTEST-RESPONSE.md) lists what changed for each one. This version hasn't been independently re-tested yet.

## Report a problem

Email the contact address on the deployment's site, or the repository owner. Include steps to reproduce it, and please allow time for a fix before sharing details.

## Design

**Read-only.** The software has no feature that changes monitored systems. AI models get read-only tools, workflows are text for people, and alert messages have no action buttons. A test fails if process-running or remote-shell code appears in the app.

**Accounts.** Passwords use Argon2, with at least 12 characters and a list of common passwords refused. Sessions are stored as hashes on the server, in HttpOnly, SameSite=Lax cookies, marked Secure over HTTPS. People sign in with their email, and changing it needs the current password and a confirmation from the new address. Repeated failed sign-ins stop that device for 15 minutes and slow further attempts on the account, but can't lock its owner out. Each person can have at most 5 sessions. Sign-in, reset, and sign-up responses don't reveal whether an address has an account.

**First admin.** The standard install creates this account from `RCC_ADMIN_EMAIL` and a random `RCC_ADMIN_PASSWORD`, and that password must be changed at first sign-in. Only when running the app directly for development does `admin@rootcause.local` / `admin` exist, and production mode refuses to start that way.

**Access.** The server checks roles on every request, and each person's investigations are private until they share them. Viewers get a reduced view: no addresses, queries, labels, prompts, or settings. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full table. Admins and owners can read every investigation, so give those roles to few people.

**One company per installation.** Each company has its own server, database, keys, and accounts. Accounts exist only by invitation, and the sign-up route is closed. The standard install always uses HTTPS, and production mode refuses plain HTTP. Whoever administers the server or database can read what is stored there, so limit that access.

**Secrets.** Saved credentials are encrypted with a key from `RCC_SECRET_KEY`, and they're never returned to the browser. One-time email tokens and webhook tokens are stored as hashes.

**Outgoing connections.** Link-local and cloud metadata addresses are always blocked. So are this server's own addresses, disguised IP addresses, and services like nip.io. Every check repeats when connecting, which stops DNS rebinding, and redirects aren't followed. Private addresses are allowed, so an admin can connect internal systems. Anyone with the admin role can therefore make this server send requests to its internal network, so give that role only to trusted people, or limit targets with `RCC_TARGET_ALLOWLIST`.

**Container.** A two-stage build on Alpine, running as a non-root user with one entrypoint. The final image has no pip, package manager, or shell. The app's code is owned by root, the root filesystem is read-only in the standard install, and all Linux capabilities are dropped.

**Browser.** Strict Content-Security-Policy with no inline scripts or styles, HSTS over HTTPS, Host and Origin checks, JSON-only write requests with repeated keys refused, a request size limit, security headers on every error, and sanitized model output. Names and labels refuse markup, control characters, and a leading spreadsheet formula character.

**AI data.** Claude stays off until an admin allows sending data to Anthropic, and an owner can block it by policy. Obvious secrets are removed by pattern matching, which can miss some. Telemetry is marked as untrusted data, but **prompt injection can't be fully prevented**, so a log line could mislead a model. Because models have only read-only tools, the damage is limited to a wrong answer.

**Audit.** Security-relevant actions go into a per-organization hash chain, which anyone with the admin role can verify and export. Someone with database write access could rewrite the whole chain, so export it to a separate system.

## Known gaps

- Rate limits are per process and reset on restart.
- There's no CAPTCHA, because many installations have no internet access. A distributed password attack is slowed, not stopped.
- Object IDs are sequential numbers. Access checks protect each object, so a guessed ID returns 404.
- There's no built-in two-factor sign-in, so use single sign-on for this.
- The database isn't encrypted by the app, so use disk encryption.
- Invitation and reset links are written to the server log when no email server is set up, so limit who can read it.
- Dependencies need regular updates. CI checks them against known vulnerabilities on each push, so run it regularly.

## Your responsibilities

- Use read-only credentials for every connection
- Keep `.env` private, and back up the database and keys
- Apply operating system and dependency updates
- Export audit logs, and review access regularly
