# Legal and privacy

For the company that runs an installation. When you install Root Cause Console, **you are the operator**: you decide how it's used and you hold the data, so you're responsible for it under data protection law. The creator of the software receives nothing from your installation.

> **Warning:** This page and the policy pages are a clean baseline, not legal advice. Have a lawyer check them before you rely on them, above all if your monitoring data includes customers' personal data, health data, or payment data.

## The policy pages

Every installation shows these pages in its footer, signed in or not. They fill in your details from the settings below.

| Page | Says |
|---|---|
| Privacy notice | Who is responsible, what is collected, why, who receives it, transfers abroad, how long it's kept, people's rights, and where to complain |
| Cookie policy | The one sign-in cookie and the preferences kept in the browser, and why no consent banner is needed |
| Terms of service | Who the terms are between, the software license, who may use it, content ownership, AI output, closing accounts, warranty, liability, and law |
| Acceptable use | What people mustn't do with the console |
| Security policy | How the console protects data, and where to report problems |
| Legal notice | Your company's name, address, and contact, and the software's copyright and third-party licenses |

Until your lawyer has reviewed them, each page shows *Draft. Have a lawyer review this before relying on it.* Set `RCC_LEGAL_REVIEWED=true` afterwards.

## Your details

Set these in `.env` (`deploy/setup-env.sh` asks for them), then run `docker compose up -d`:

```bash
RCC_OPERATOR_NAME=Your Company SARL
RCC_OPERATOR_ADDRESS=Street, postcode, city, country
RCC_CONTACT_EMAIL=privacy@company.com
RCC_LEGAL_REVIEWED=false
```

Use a shared address such as `privacy@` for the contact, not a person's own mailbox, so requests still arrive when people change roles. Without these settings, the pages tell people to ask their administrator.

## Which laws apply

It depends on where the people whose data you hold are, not only where your company is:

| Law | When it applies | What it means here |
|---|---|---|
| **GDPR** | Your users, or people named in your logs, are in the EU, even if your company is in Casablanca | You're the controller. The AI providers you turn on are processors: sign their data processing terms, and use standard contractual clauses for transfers outside the EU |
| **Morocco, law 09-08** | You process personal data in Morocco | Declare the processing to the **CNDP**, and get its authorization before transferring personal data abroad, for example to Google or Anthropic |
| **US state laws** such as California's CCPA | Your users are in those states | Mostly about selling data and advertising, which the console doesn't do |
| **WCAG** | Accessibility | The usual standard to build to. The console's colors and keyboard access were checked against WCAG 2.1 AA, but a full accessibility audit hasn't been done |

## Cookies, and why there's no banner

The console sets one cookie, `rcc_session`, which keeps people signed in, and keeps a few preferences in the browser (`rcc-theme`, `rcc-hero-view`, `rcc-range`, `rcc-refresh`). It loads nothing from other websites: no analytics, ads, pixels, chat widgets, or external fonts. Strictly necessary cookies and remembered choices don't need consent, so a banner would only be decoration.

You can check this yourself: open the console in a private window, open the browser's developer tools (Application, then Cookies, and the Network tab), and look. Only your console's own address appears.

## Data that leaves your installation

Only where an admin turns it on:

| Destination | What | Turned on by |
|---|---|---|
| Google (Gemini) | Questions and live monitoring context, which can include host names, service names, and log lines | An admin, with an explicit consent box. An owner can block it |
| Anthropic (Claude) | The same | The same |
| Your email server | Invitations, password resets, security notices, alerts | `RCC_SMTP_*` |
| Slack, Teams, Google Chat | Alert messages | An admin adds the channel |
| Your single sign-on provider | Sign-in | An owner sets it up |

Before you turn on a cloud AI model, check that your monitoring data may leave your network under your policies, and record the decision. Ollama keeps everything on your own server.

## Answering a deletion request

When someone asks for their personal data to be deleted:

1. In **Organization > Members**, select **Remove** next to them, and tick **Also delete their private investigations**. This deletes their account, sessions, and the investigations only they could see.
2. Their shared investigations belong to your team's work. Delete any that are about them personally, from **Investigate**.
3. Remove their address from any alert email channel in **Connections > Alerts**.
4. Their name and email stay in the audit trail, which you keep for security and accountability. That's usually a legitimate reason to keep it, so tell them it remains, and for how long.
5. Older copies leave your backups as those backups expire.
6. If you turned on Gemini or Claude, data already sent to them follows their own retention rules.

Answer within the legal deadline: one month under the GDPR.

## Emails

The console sends only service emails: invitations, password resets, security notices, and alerts. There are no marketing emails, so no unsubscribe link is required. Every alert email says why it arrived and how to stop it: ask an administrator to remove the address from the alert channel.

## Age

The console is a work tool, used by invitation. The terms limit it to people of 18 or older, so there's no age check or parental consent.

## Licenses

Root Cause Console is open source under the Apache License, Version 2.0. Fonts and libraries served to browsers are under open licenses that allow commercial use, listed in the legal notice and in `/vendor/THIRD-PARTY-LICENSES.txt`.
