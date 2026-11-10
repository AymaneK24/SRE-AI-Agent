# Investigations

An investigation is a conversation with the AI model about your systems. Engineers and above can start one from **Investigate**, or from **Investigate** next to a problem on the overview.

## Investigate only what you're working on

In the **Live context** panel, tick the problems you want explained. A bar above the prompt shows what's in focus. With a focus:

- Only those problems go to the model, with their alerts, targets and signals, and only the Kubernetes and log detail of their namespaces. Other problems are left out, so the model doesn't drift to an unrelated one and fewer tokens are sent.
- The prompt can stay empty: **Investigate** asks for the root cause of the chosen problems.
- **Investigate** buttons on the overview open an investigation already focused on that one problem, and automatic investigations of an alert focus on that alert.

**Show exact text sent** shows the focused text.

## What happens when you ask

1. **Your question goes with the live context.** The model first receives the problems right now, with the console's diagnosis of each, then firing alerts, down targets, golden signals, recent changes, and log error counts. Log lines are marked as untrusted data.
2. **The console gathers the first evidence itself.** For each problem in view, it runs the obvious read-only queries before the model starts: the error lines of a crashing workload, its Kubernetes description, the chart of a signal that went out of range, or whether a down target answered over the last hour. These show as *Console* in the evidence list. A model of any size starts with evidence, and API models need fewer rounds.
3. **The model investigates.** It chooses read-only tools, reads the results, and decides what to check next. It can run up to 4 tools per round and 10 in total, and the same call is never run twice.
4. **It answers.** In its last round, the model can't call tools, so it has to write an answer. If it still doesn't, the console writes a short answer from what was found, so an investigation never ends in an error page.
5. **The console checks the answer.** See [How answers are checked](#how-answers-are-checked).

The page shows each tool call and its result as it happens. When the model writes a log query in a form Loki can't read (single quotes, the text to find put in the selector as `message=`, `| error` instead of `|= "error"`), the console corrects it and tells the model what it changed, instead of failing the query. An invented connection or source number falls back to the main one in the same way.

### Short answer or full report

Next to the model, choose **Short answer** (summary, cause with confidence, at most four pieces of evidence, three next checks, under 250 words) or **Full report** (every section, including the competing explanation and remediation). An admin sets the default in [Connections, Cost savings](/docs/ai-models#cost-savings). Automatic investigations always write a full report for the email and PDF.

### Evidence charts and the PDF report

Under an answer, **Evidence charts** draws each metric query behind it over the hour before the answer. **Download report (PDF)** gives the answer as a report with the same charts, for an incident review or a ticket.

### Who sees what

Everyone sees which model answered. The tokens, cost, time and context version of each answer, and queries that failed before the console corrected them, are shown to admins only.

## The tools the model can use

Tools are offered only for what is connected.

| Tool | Needs | What it does |
|---|---|---|
| `run_instant_query` | A metric source | Runs a query now, such as PromQL, and returns each series' value |
| `run_range_query` | A metric source | Runs a query over a recent window and returns first, last, min, max, mean, and when the biggest change happened |
| `list_metrics` | A metric source | Finds metric names that contain some text |
| `query_logs` | Loki | Runs a LogQL query over a recent window |
| `get_kubernetes_state` | Kubernetes | Reads problems, warning events, unhealthy pods, Deployments, Services, or nodes |
| `describe_object` | Kubernetes | Like `kubectl describe` for a pod or workload: status, restarts, exit codes, limits, events |
| `get_pod_logs` | Kubernetes | Reads a pod's last lines, or the crashed container's with `previous` |
| `get_recent_changes` | Nothing | Lists deployments and changes from the last 24 hours |
| `search_past_investigations` | Nothing | Searches earlier investigations and their confirmed root causes |

Every tool only reads. Models of about 8 billion parameters or fewer get a shorter prompt and the 4 most useful tools. See [Small models](/docs/ai-models#small-models).

## Asking good questions

- **Name what's failing:** *Why is the payment Deployment in shop crash-looping?* works better than *what's wrong?*
- **Say when it started:** *Checkout errors went up around 14:10. What changed?*
- **Ask for the next check:** *What should I look at to confirm this?*
- **Ask follow-ups** in the same investigation. The model keeps the earlier answers and evidence.

Good starting questions are offered on an empty investigation, such as *What changed recently, and does anything look wrong?*

## How answers are checked

After each answer, the console checks it against your data and shows the result under the answer:

- **Numbers:** numbers the answer quotes are compared with the values its queries returned.
- **Queries:** queries the answer cites are run again. A query that fails or returns nothing is flagged.
- **Errors as evidence:** if the answer quotes text that is really a failed query's error message, it's flagged.
- **Missed problems:** if the answer never mentions a problem that is active right now, the console says which one.

These checks catch invented numbers and broken queries. They can't prove that a diagnosis is right, so read the evidence.

> **Note:** If the model answers without a root cause, a summary, or evidence, and *Ask again if an answer is incomplete* is on, the console asks the model once more. On a computer without a GPU, this second answer can take a while.

## Privacy and sharing

- An investigation is **private** to the engineer who started it. Other engineers can't open it, and it isn't used as related past work for anyone else.
- **Share with team** lets engineers read it, and viewers see its question and answer.
- **Automatic investigations** of critical alerts are always visible to the team, because no single person owns them.
- **Admins and owners** can read every investigation, because someone has to be able to oversee what's sent to an AI model.

> **Security:** An answer can name hosts, addresses, and services. Read it before you share it, especially if viewers outside your team have accounts.

## After the investigation

- **Record the real cause:** answer *What was the real cause?* Future investigations find it through `search_past_investigations`, and projects list it under *Root causes your team confirmed*.
- **Draft a runbook:** **Draft a remediation workflow** asks the model to write the fix as steps. The draft needs a person's review, and nothing ever runs it. See [Runbooks](/docs/runbooks).
- **Delete:** deleting removes the messages, prompts, and queries from the log. The audit trail keeps a record that it was deleted.

## Limits and costs

| Limit | Where to set it |
|---|---|
| Most model rounds per prompt (default 6) | Connections, AI model |
| Longest answer, in tokens | Connections, AI model |
| Most tokens per investigation | Organization, Usage |
| Monthly budget in US dollars | Organization, Usage. New investigations stop when it's reached |

Each investigation records its tokens, cost, and time. Ollama shows $0, because it has no charge per use. See [AI models](/docs/ai-models).
