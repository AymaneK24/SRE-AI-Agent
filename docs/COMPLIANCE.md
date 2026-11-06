# Standards mapping

This maps the console's features to the standards an SRE or audit team usually asks about.

**This is not a certification or compliance claim.** ISO certification applies to an organization's
management system, not to one piece of software. These features can support your controls, but your
organization still has to decide, document, and operate them. Have your security or compliance team
review this mapping, including the control numbers, against your own copy of each standard.

## ISO/IEC 27001:2022 Annex A

| Control | How the console supports it | What you still need to do |
|---|---|---|
| 5.15, 8.3 Access control | Every page needs a signed-in account, with four roles, invitation-only accounts, one company per installation, and email domain limits. | Review access regularly, and remove leavers. |
| 5.23 Cloud services | Claude stays off until someone allows sending telemetry to Anthropic. That choice is audited. | Approve Anthropic as a supplier, and check your data classification rules. |
| 8.5 Secure authentication | Argon2 passwords, a forced change of the first password, email invitations, limits on failed sign-ins that can't lock the owner out, HttpOnly and Secure cookies, single sign-on, and sessions ended on a role or password change. | Use single sign-on with two-factor sign-in, since the app has no built-in two-factor option. |
| 8.9 Configuration management | Configuration comes from environment variables and audited settings changes. Dependencies are pinned. | Keep your own deployment configuration under change control. |
| 8.10 Information deletion | Retention is configurable, and old investigations, prompts, snapshots, and events are purged automatically. | Set a period that matches your policy. |
| 8.11 Data masking | Obvious passwords, tokens, and URL credentials are removed from telemetry before logging or sending to a model. | Pattern matching can miss secrets. Host and service names are not masked. |
| 8.15 Logging | Every prompt is stored in full with its response, model, tokens, and timing. The audit trail is append-only and hash-chained. | Forward logs to your SIEM. A person with full database access could rewrite the whole chain, so store checkpoints elsewhere for stronger assurance. |
| 8.16 Monitoring | `/healthz`, `/readyz`, and `/metrics` expose the console's own health. | Scrape and alert on them. |
| 8.24 Cryptography | Saved credentials use Fernet encryption (AES-128-CBC with HMAC-SHA256). | The database itself isn't encrypted. Use encrypted disks, and set `RCC_SECRET_KEY` from a secrets manager so the key isn't stored beside the data. |
| 8.25, 8.28, 8.29 Secure development | Input validation, parameterized SQL, Content-Security-Policy, Host and Origin checks, static analysis, and automated tests in CI. | Add your own code review, dependency scanning, and penetration testing. |

## ISO/IEC 42001:2023 AI management

The console supports these AI management concerns, though the standard's requirements apply to your organization:

- **Human oversight:** models have read-only tools, and fixes are only suggestions for a person to carry out.
- **Traceability:** each answer records the model, the exact prompt, the context version, and every query run.
- **Transparency:** answers show whether queries were run, and the exact text sent can be viewed.
- **Known limits:** answers can be wrong. You should measure accuracy on past incidents before relying on them.

## ISO/IEC 25010:2023 software quality

| Quality | Evidence |
|---|---|
| Functional suitability | Automated tests cover context updates, logging, security checks, and model adapters. |
| Reliability | Polling survives source failures, marks data as stale, and recovers by itself. Runs always end with a recorded outcome. |
| Security | See the Annex A table. |
| Interaction capability | Keyboard-reachable controls, visible focus, value tables beside charts, status shown with text as well as color, and reduced-motion support. No formal WCAG audit has been done. |
| Maintainability | Separate modules for source, context, providers, and logs. Adding a collector means adding a new source module. |

## Known gaps before production use

- SQLite suits one machine, and PostgreSQL is recommended otherwise. Only one app instance can run at a time.
- Prompt injection through telemetry text is reduced by read-only tools and prompt instructions, but it can't be fully prevented.
- No formal threat model, penetration test, or accessibility audit has been done.
- No accuracy scores have been measured with real models.
- The policy pages are templates that need legal review.
