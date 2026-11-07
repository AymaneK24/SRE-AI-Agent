# Logs and audit

**Logs** keeps the record of what was asked, what the model received, what changed, and who did what. Engineers and above can open it.

| Tab | Who sees it | What it holds |
|---|---|---|
| Investigations | Engineers: their own and shared ones. Admins: all | Every investigation, searchable by prompt, answer, and root cause, with status: open, resolved, or false alarm |
| Prompts sent | Same as above | Each prompt exactly as it was sent to the model, including the system prompt and the live context |
| State changes | Engineers and above | Every problem that started or cleared, from every connection |
| Alerts sent | Admins and owners | Each message sent to an alert channel, and whether it was delivered |
| Audit trail | Admins and owners | Security-relevant actions: sign-ins, role changes, connection changes, shared investigations, runbook reviews, webhook rotations, and deletions |

## Why every prompt is kept

Prompts are stored exactly as sent, so you can check later what data left your network for a cloud model, and why the model answered as it did. Search them when an answer looks wrong: usually the context was missing something.

## The audit trail

Each entry is chained to the one before by a hash, per organization. The page checks the chain every time it opens:

- *Hash chain intact across N entries* means no entry has been changed or removed since it was written.
- *Hash chain broken at entry N* means the log was altered.

**Export CSV** and **Export JSON** download the whole trail.

> **Security:** Someone with write access to the database could rewrite the whole chain consistently. Export the audit trail regularly to a separate system, such as your SIEM or a write-once storage bucket, so a copy exists outside the console's reach.

## Retention

Owners set **Keep logs for (days)** in **Organization > Security policies**, from 7 to 3650 days (365 by default). Once an hour, investigations (with their prompts), events, and context snapshots older than that are deleted, and the deletion is itself written to the audit trail. The audit trail is kept. Project statistics and reports for a period need its events, so keep logs at least as long as the longest report you want to make.
