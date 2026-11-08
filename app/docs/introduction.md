# Introduction

Root Cause Console is a self-hosted tool for SRE and platform teams. It connects to the monitoring you already run (Prometheus, Loki, Kubernetes, CloudWatch, Azure Monitor), keeps a live picture of what's wrong, and lets an AI model investigate why something broke.

It runs inside your own network, and we recommend keeping it private: reached over your VPN or company network, with no address on the internet. Each installation belongs to one company, with its own server, database, and keys.

## What it does

- **Watches your systems.** It reads metrics, logs, and cluster state every few seconds, and turns what's wrong into a short list of problems: firing alerts, targets that stopped answering, crash-looping pods, out-of-memory kills, failed rollouts, unusual error rates.
- **Investigates with AI.** Ask a question in plain words, such as "why is checkout failing?". The AI model looks at the live problems, decides what to check, runs read-only queries against your systems, and writes the root cause with the evidence it found.
- **Checks the answer.** After the model answers, the console compares the numbers it quotes with your real data, re-runs the queries it cites, and warns when the answer ignores an active problem.
- **Alerts people.** Problems go to Slack, Microsoft Teams, Google Chat, or email. Critical alerts can start an investigation automatically, so the email arrives with the findings.
- **Keeps a runbook library.** Engineers write step-by-step fixes for problems that come back. Everyone can read them, and they are linked from the problems they fix.
- **Reports on systems.** Projects group the sources behind one system and show how often each problem returns, how long it lasts, and patterns, with PDF reports.

## What it never does

The console only reads. It has no code that writes to your systems.

- The AI model gets read-only tools: metric queries, log queries, and Kubernetes reads.
- Runbooks are text for people. The console can't run a command.
- Alert messages have no action buttons.
- The Kubernetes account it needs can't read Secrets.

> **Security:** Read-only doesn't mean harmless. The console stores credentials for your monitoring systems, and admins can make it connect to internal addresses. Read [Security](/docs/security) before you put it into production.

## Is it an AI agent?

The investigation part is. When you ask a question, the model works in a loop: it reads the live context, chooses a tool, reads the result, and decides the next step. It can make up to 10 tool calls, then writes its answer. The model chooses its own steps, which makes it an agent rather than a chatbot.

It is a **read-only agent with a person in the loop.** It can look at everything you connect and change nothing. People decide and act, using the runbooks. Everything else in the console, such as polling, problem detection, alerts, and reports, is ordinary code that gives the same result every time.

## How the pieces fit

```
 Prometheus ─┐                     ┌─> Overview: problems, golden signals, what's watched
 Loki ───────┼─> Context engine ───┼─> Alerts: Slack, Teams, Google Chat, email
 Kubernetes ─┤   (polls, finds     ├─> Investigations: AI model + read-only tools + answer checks
 CloudWatch ─┤    problems)        ├─> Projects and PDF reports
 Azure ──────┘                     └─> Runbook library
 Alertmanager, CI/CD ─> webhooks ─┘
```

## Where to go next

| You want to | Read |
|---|---|
| Try it on your laptop | [Quick start](/docs/quick-start) |
| Understand the words used here | [Concepts](/docs/concepts) |
| Connect your systems | [Connections](/docs/connections) |
| Choose an AI model | [AI models](/docs/ai-models) |
| Install it for your team | [Deploy on an Azure VM](/docs/deploy-azure-vm) or [Deploy on AKS](/docs/deploy-aks) |
| Set up company sign-in | [Single sign-on](/docs/single-sign-on) |
