# AI accuracy benchmark

How often an investigation finds the real root cause depends mostly on the AI model, then on what's connected. This page explains how to measure it on your own setup, with scenarios whose causes are known.

> **Note:** No accuracy scores ship with this software. Results depend on the model, its size, your hardware, what's connected, and your systems, so a score measured elsewhere wouldn't tell you much. Measure the models you're considering with the method below before you rely on them.

## What decides accuracy

| Factor | Why it matters |
|---|---|
| **Model size and quality** | The biggest factor. Larger models follow multi-step chains (*payment crashes → it can't reach postgres → the postgres Service has no pods*) that small models miss |
| **Tool use** | A model that can run queries checks its guesses. Without tools, it only reads the live context. Small models often call tools wrongly, loop on the same call, or ask questions instead of investigating |
| **What's connected** | The model can only find causes your connections show. A missing Secret shows in Kubernetes events, not in metrics. Bad logs need Loki or pod logs |
| **Context size** | With Ollama, a small `num_ctx` cuts the context off, so the model never sees the problem list |
| **The question** | *Why is payment crash-looping in shop?* gives the model a starting point. *What's wrong?* doesn't |
| **The diagnosis in code** | For known Kubernetes failures, the console's own diagnosis is already in the context, which helps every model |

### What we observed while developing

These are observations from building and testing the console, not measured scores:

- 3B models, such as `llama3.2` and `qwen2.5:3b`, often answered without calling tools, called them in the wrong format, repeated the same call, or asked the user a question instead of investigating. The small-model mode, the forced answer in the last round, and the console's own diagnosis were added because of this.
- Hosted models with tool use, such as Gemini Flash and Claude, followed the evidence through several tool calls and named the underlying cause more often.
- Every model sometimes stated a number or a query result it hadn't checked. That's why every answer is checked against your data.

## The test scenarios

`deploy/examples/shop-simulation.yaml` creates a namespace `shop` where each service fails in a known way. Apply it to a test cluster connected to the console:

```bash
kubectl apply -f deploy/examples/shop-simulation.yaml
kubectl -n shop get pods -w      # failures appear within 1 to 3 minutes
```

| # | Service | Question to ask | Correct root cause | Partly right |
|---|---|---|---|---|
| 1 | payment | Why is payment crash-looping in shop? | The `postgres` Service has no pods behind it, so payment's connection to `postgres:5432` is refused and it exits | Says *can't reach the database*, without finding the Service has no pods |
| 2 | image-resizer | Why does image-resizer keep restarting? | Killed for running out of memory at its 32Mi limit (OOMKilled) | Says *restarting* or *crashing* without OOM |
| 3 | inventory | Why does inventory keep restarting? | Its liveness probe checks `/healthz`, which doesn't exist, so Kubernetes kills it | Says *probe failing* without the wrong path |
| 4 | checkout | Why won't checkout start? | It refers to the Secret `checkout-db-credentials`, which doesn't exist | Says *configuration error* without naming the Secret |
| 5 | recommendations | Why isn't recommendations running? | The image tag `nginx:9.9.9-doesnotexist` doesn't exist | Says *image pull error* without the tag |
| 6 | search | Why is search never ready? | The readiness probe checks port 8081, but nginx listens on 80 | Says *not ready* without the port |
| 7 | reporting | Why is reporting pending? | It asks for 200Gi of memory, more than any node has | Says *can't be scheduled* without the reason |
| 8 | orders | Is orders healthy? | Yes. It's running, and its ERROR lines are retried timeouts calling payment, a symptom of #1 | Calls orders broken |

Scenario 8 checks that the model doesn't invent a problem. Scenario 1 needs the most steps, and is the best single test of a model.

## How to run it

1. Apply the scenarios and wait until every failure shows on the overview.
2. Choose the model in **Connections > AI model**, with **Let the model run queries** on.
3. For each scenario, start a **new** investigation and ask the question exactly as written. A new investigation keeps earlier answers from helping.
4. Score each answer with the rubric below, and note the time and cost the investigation shows.
5. Repeat for each model. Run each scenario at least 3 times per model, because answers vary between runs.

## Scoring

| Score | When |
|---|---|
| 2 | Names the correct root cause and cites evidence that shows it |
| 1 | Partly right, as in the table, or right with no evidence |
| 0 | Wrong, invented, or no answer |

Also note:

- **Checks:** did the console's answer checks flag invented numbers or failed queries?
- **Tool calls:** how many, and were any repeated or malformed?
- **Time and cost:** shown on each investigation.

A model's accuracy is its total score divided by the maximum (2 × scenarios × runs).

## Record your results

Keep results with the model name, its size, the hardware, and the date, because new model versions change them:

```
date,model,hardware,tools,scenario,run,score,tool_calls,seconds,cost_usd,notes
2026-11-07,qwen2.5:7b,8-core CPU 32GB,on,1,1,1,4,412,0,said database unreachable; didn't check the Service
2026-11-07,gemini-3-flash-preview,hosted,on,1,1,2,5,38,0.004,found postgres Service has no endpoints
```

These two rows only show the format. They aren't measured results.

## Testing on your own incidents

The scenarios test common Kubernetes failures. Your own incidents test what matters to you:

1. Pick past incidents whose root cause is known and written down.
2. While a similar failure is happening, or on a staging copy, ask the question the on-call engineer would have asked.
3. Score it the same way.

> **Tip:** Record the real cause at the end of each investigation (*What was the real cause?*). Over time, that list becomes your own benchmark, and future investigations can search it.
