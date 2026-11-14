# What's new

The changes in this version, with links to the full description.

## Alerts you can trust

- **Likely cause.** Problems happening now are linked when there is evidence: the same workload, a failing node, a workload configured to call another, or a log line that names it. Messages lead with the likely cause and list each symptom with its reason; the Overview nests symptoms under their cause; investigations test it as a hypothesis. Time alone never links two problems, and nothing is ever hidden. [Likely cause](/docs/alerts-and-webhooks#likely-cause-one-root-many-symptoms)
- **False-alarm labels expire.** A *false alarm* or *expected* label ends after 30 days (adjustable), after a deploy to the same service, when the problem gets worse than when it was labelled, or when its rule changes. The alert then comes back and says why, and **Renew** keeps a label that still holds. [When a label ends](/docs/projects-and-reports#when-a-label-ends)

- **Noise control.** Every problem passes five checks before anyone is messaged: false-alarm verdicts, flapping, a grace period, grouping, and an hourly limit per channel. Blips that fix themselves are never sent, and problems that start together arrive as one message, with what they share: a node, a namespace, a cluster, a dependency they all mention, or a change just before. Held-back messages are counted per reason. [Noise control](/docs/alerts-and-webhooks#noise-control-fewer-better-messages)
- **One problem, one alert.** A Prometheus alert about a workload the console already reports joins that problem instead of sending a second message. [One problem, one alert](/docs/alerts-and-webhooks#one-problem-one-alert)
- **Problems found at startup are sent.** A deployment that is already broken when the console starts, or when its cluster is connected, now sends its alert. Problems already announced before a restart aren't sent again.
- **Alerts survive a restart.** A problem whose message was still waiting out its grace period when the console stopped is sent after it starts again. Recoveries still reach the channels that heard about the problem. [When a channel doesn't answer](/docs/alerts-and-webhooks#when-a-channel-doesn-t-answer-or-the-console-restarts)
- **Messages a channel refused are sent later.** If Slack, Teams, Google Chat or the email server is down, the message is tried again for about a day and arrives marked *Delayed message*.
- **Heartbeat.** While it is watching, the console calls an address of your choice every few minutes, so a service such as Healthchecks.io alerts you if the console itself stops. It also tells your channels when it can't complete its own checks. [Heartbeat](/docs/alerts-and-webhooks#heartbeat-know-when-the-console-itself-stops)
- **The console watches its own polling.** A polling loop that ends is started again within 30 seconds, and `/healthz` answers 503 when one is stuck, so Docker or Kubernetes restarts the console.
- **A source that stops answering is announced.** With a grace period set, "stopped answering" was wrongly held back as a blip. It is now sent while the source is still down.
- **Alertmanager webhooks without a metric source work with a grace period.** Their alerts are sent at once, because Alertmanager already waited before calling, and a critical one starts a single automatic investigation.
- **Monitoring continues after a failed check.** One failed poll, such as during a database restart, no longer stops the console watching that company. The page shows that the last check failed until the next one succeeds.
- **Several clusters are read at the same time**, so one slow cluster no longer delays the others' alerts. Signing in and building PDF reports no longer pause live updates.
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
