# Projects and reports

A project groups the metric sources and Kubernetes namespaces behind one system, such as *Shop* or *Payments*. Its page shows the system's status, how often each problem came back, how long it lasted, timing patterns, and PDF reports.

## Create a project

Engineers and above select **New project** on **Projects**, then choose:

- **Metric sources in this project:** any of the connected metric sources.
- **Kubernetes namespaces in this project:** namespaces from the connected cluster, such as `shop` and `shop-jobs`. Only problems in these namespaces count towards the project.

Choose at least one source or namespace. A project can mix both. Deleting a project, which needs an admin, deletes its saved reports. Sources and recorded events stay.

## What the project page shows

| Section | What it means |
|---|---|
| Status | Live state of its sources and namespaces, and workload problems right now |
| Incidents | Problems that started close together, counted as one outage |
| Average time to clear | How long incidents lasted |
| Average time between incidents | How often they happen |
| Time with no incident | The share of the period with nothing wrong |
| Recurring issues | Each problem that happened more than once, how often, and how long it lasted |
| Patterns | Problems that tend to start together, or soon after a deployment |
| Root causes your team confirmed | Causes recorded at the end of investigations |

All numbers are calculated from recorded events, never by the AI model. Recurring issues and patterns need time to appear after you connect a source. Patterns point to what to check, and don't prove a cause.

## False alarms

An issue that keeps clearing within minutes gets a suggestion that it may be a false alarm. Only a person's verdict changes anything. An engineer can mark an issue as:

- **False alarm:** the alert was wrong.
- **Expected:** it happens on purpose, such as a nightly batch job.

Both stop that issue counting towards project health and the real totals, and stop its alert messages: it still shows in the console, but nobody is messaged about it. Reports say how many occurrences were set aside. An admin can turn the second part off in [Noise control](/docs/alerts-and-webhooks#noise-control-fewer-better-messages).

### When a label ends

A label is a judgement about the system as it was when someone made it, so it doesn't mute an alert for ever. It ends on its own when:

| What happened | Example |
| --- | --- |
| **Its time is up** | 30 days by default. Admins choose 7, 30, 90 days or *until something changes* in Noise control. |
| **A change touched the same service** | A deploy, Helm upgrade or other recorded change to the same workload, service or namespace, after the label was set. |
| **It got worse** | Its value goes 20% above the highest seen when it was labelled, or it lasts much longer than any earlier occurrence (1.5 times the longest, and at least 10 minutes more). |
| **Its rule changed** | The query or the limit behind the alert is different. |

The issue then shows **Label ended** with the reason, counts as *not reviewed* again, and its alert goes out with one line saying why it's back, such as *Marked a false alarm by Pat on 2 Nov; that label ended: Helm upgrade payments-db 3.8.0 came after it*. A problem that is still active when its label ends gets its alert straight away.

While a label applies, the issue shows **Expires in N days**. **Renew** starts a new period from today, and remembers what the issue looks like now. Setting, renewing and ending a label are all in the audit log.

## Reports

**Generate report** saves the project's numbers for a period of 1 to 365 days (30 by default) and creates a PDF.

- **Without AI notes**, the PDF can be downloaded straight away.
- **With AI notes**, the model writes a short commentary in the background. The download appears when the notes are finished, and the page checks every few seconds. Notes are marked as AI opinion, they receive the calculated sentences to repeat as they are, and numbers in them that don't appear in the data are flagged in the PDF. If the notes fail, **Try AI notes again** retries, and the report itself is kept.

A report is a fixed record: it keeps the numbers as they were when it was made.

Reports stay with engineers and above until an engineer selects **Share**. Viewers can then download it, and **Stop sharing** makes it engineers only again.

> **Note:** AI notes use your AI budget. With Ollama on a computer without a GPU, writing them can take several minutes. A note that takes longer than 20 minutes is marked as failed.
