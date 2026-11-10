# What's new

The changes in this version, with links to the full description.

## Alerts you can trust

- **Noise control.** Every problem passes five checks before anyone is messaged: false-alarm verdicts, flapping, a grace period, grouping, and an hourly limit per channel. Blips that fix themselves are never sent, and problems that start together arrive as one message, with what they share: a node, a namespace, a cluster, a dependency they all mention, or a change just before. Held-back messages are counted per reason. [Noise control](/docs/alerts-and-webhooks#noise-control-fewer-better-messages)
- **One problem, one alert.** A Prometheus alert about a workload the console already reports joins that problem instead of sending a second message. [One problem, one alert](/docs/alerts-and-webhooks#one-problem-one-alert)
- **Problems found at startup are sent.** A deployment that is already broken when the console starts, or when its cluster is connected, now sends its alert. Problems already announced before a restart aren't sent again.
- **False alarms stay quiet.** An issue marked *false alarm* or *expected* in a project is no longer messaged. [False alarms](/docs/projects-and-reports#false-alarms)

## Several connections of each kind

- Several Prometheus servers, Lokis and Kubernetes clusters, each with its own name. One of each kind is the **main** one. [Connections](/docs/connections#many-sources-one-view)
- **Source** pickers on the overview show any connection's golden signals, logs, cluster and panels.

## Investigations

- **Investigate only what you're working on.** Tick problems in *Live context*, and only they, with their logs, metrics and cluster state, go to the model. [Investigations](/docs/investigations#investigate-only-what-you-re-working-on)
- **Short answer or full report**, evidence charts in the answer, and a PDF report to download. [Short answer or full report](/docs/investigations#short-answer-or-full-report)
- **More accurate private models.** The console runs the obvious queries first, corrects common LogQL mistakes, and falls back to the main connection when a model names one that doesn't exist.
- **Costs for admins only.** Everyone sees which model answered. Tokens, cost and time show to admins.

## Lower AI costs

Prompt caching, evidence gathered before the model starts, a cheaper model for query rounds, focused prompts and short answers, with the month's savings in **Connections > Cost savings**. [Cost savings](/docs/ai-models#cost-savings)

## Setup

- **[Set up a customer's environment](/docs/customer-setup):** everything to install on the customer's side for full coverage, with what each step adds.
- **Monitoring installer** with a `production` profile, and a Grafana Alloy example for logs, since Promtail reached end of life. [Prometheus](/docs/prometheus#no-prometheus-in-the-cluster-yet)

## Interface

- One dropdown style across the console, with keyboard support, in light and dark themes.
- A footer with the documentation, policies and contact. The documentation link moved there from the top bar.
