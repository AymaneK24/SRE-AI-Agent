# AI models

The console works with three kinds of AI model. Admins set them up in **Connections > AI model**. Engineers can pick which model answers each question on the **Investigate** page, without seeing addresses or keys.

## Choosing one

| | Ollama | Gemini | Claude |
|---|---|---|---|
| Runs | On your own server | At Google | At Anthropic |
| Your data | Stays on your network | Sent to Google | Sent to Anthropic |
| Needs | A machine with enough RAM, ideally a GPU | An API key from Google AI Studio | An API key from the Claude Console |
| Cost | Your hardware only | Per token | Per token |
| Speed | Depends on your hardware: minutes on a CPU | Seconds | Seconds |
| Investigation quality | Depends heavily on model size | Good with tools | Good with tools |

How good the answers are depends far more on the model than on anything else. See [AI accuracy benchmark](/docs/accuracy-benchmark) for how to measure it on your own systems.

## Ollama

### Install and reach it

Install Ollama on a machine the console's server can reach, then pull a model:

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5:7b
curl -s http://localhost:11434/api/tags      # lists installed models
```

By default Ollama listens only on `127.0.0.1`. The console in Docker reaches it at `http://host.docker.internal:11434` when both are on the same machine and Docker maps that name to the host (the project's Compose file does).

When Ollama runs on another machine, make it listen on the network:

```bash
sudo systemctl edit ollama
#   [Service]
#   Environment="OLLAMA_HOST=0.0.0.0:11434"
sudo systemctl restart ollama
```

> **Security:** Ollama has no sign-in. Anyone who can reach port 11434 can use your models and see their names. Allow only the console's server in the firewall, for example `sudo ufw allow from CONSOLE_IP to any port 11434`, and never publish it to the internet.

On Windows with WSL, when Ollama runs inside WSL, the console reaches it at the WSL address shown by `hostname -I` inside WSL, such as `http://172.27.171.169:11434`. That address can change when WSL restarts. If Ollama is installed as a snap, `sudo snap get ollama` lists its settings, including the address it listens on.

### Which model

| Model | RAM needed | Runs well on | Notes |
|---|---|---|---|
| `llama3.2` (3B), `qwen2.5:3b` | About 3 GB | Any recent CPU | Fast, but often unreliable at choosing tools and at multi-step reasoning |
| `qwen2.5:7b`, `qwen2.5-coder:7b` | About 6 to 8 GB | A GPU with 8 GB, or a fast CPU | A big step up. The coder variant is strong on exact commands |
| `qwen2.5:14b` | About 10 to 12 GB | A GPU with 12 GB or more | Better reasoning about multi-step causes |
| `qwen2.5:32b`, `llama3.3:70b` | 20 GB and up | Data-centre GPUs | Close to hosted models for this work |

On a CPU, an investigation with a 7B model takes roughly 3 to 10 minutes, because the model reads a few thousand tokens of context in each round. Use a hosted model when speed matters, and Ollama for work that can wait, such as report notes.

### Settings

| Setting | Default | What it does |
|---|---|---|
| Ollama address | `RCC_OLLAMA_URL` | Where Ollama answers |
| Model | None | Choose after **Find models** |
| Model memory (tokens) | 8192 | Ollama's `num_ctx`. More fits more context but uses more RAM and is slower. Below 8192, context gets cut off |
| Small-model mode | Automatic | See [Small models](#small-models) |
| Let the model run queries | **Off** | Turn it on so the model can use tools. Without it, the model only reads the live context |
| Ask again if an answer is incomplete | Off | Asks once more when the answer lacks a root cause or evidence. Slow on a CPU |

### Small models

Models of about 8 billion parameters or fewer get a shorter prompt, the 4 most useful tools (`get_pod_logs`, `describe_object`, `query_logs`, and `run_instant_query`, from those that are connected), and the console's own diagnosis as the starting point. *Automatic* judges size from the model's name, such as `qwen2.5:3b` or `llama3.1:8b`. Choose *Always* for a small model whose name doesn't show its size.

## Gemini

1. Create a key in [Google AI Studio](https://aistudio.google.com/apikey).
2. In **Connections > AI model**, choose **Gemini**, paste the key, and select **Save key**. It's encrypted, and never shown again.
3. Choose a model from the list your key can use.
4. Tick **Allow sending prompts and live context, including host and service names, to Google**.
5. **Save AI settings**.

Each request carries the whole conversation, so the console doesn't rely on Google storing anything between requests. Google's API terms decide what Google keeps of what it receives, and they differ between the free and paid tiers. Read them before sending production data.

You can also set the key with `RCC_GEMINI_API_KEY` in `.env`. It then can't be changed in the app.

## Claude

1. Create a key in the [Claude Console](https://platform.claude.com). A claude.ai sign-in doesn't connect here, and API use is billed separately from Claude plans.
2. Choose **Claude**, paste the key, **Save key**, and choose a model.
3. Tick **Allow sending prompts and live context, including host and service names, to Anthropic**.
4. **Save AI settings**.

You can also set the key with `ANTHROPIC_API_KEY` in `.env`.

## Settings for every model

| Setting | Default | Where |
|---|---|---|
| Most model rounds per prompt | 6 | Connections, AI model |
| Longest answer (tokens) | 4096 for Claude, 8192 for Gemini | Connections, AI model |
| Monthly budget (US dollars) | None | Organization, Usage |
| Most tokens per investigation | None | Organization, Usage |
| Allow cloud AI models | On | Organization, Security policies (owners) |

## What is sent to the model

- The system prompt with the rules: read-only, cite evidence, say what's unknown.
- The live context: problems with diagnoses, alerts, targets, golden signals, changes, and log summaries.
- Matching runbooks, and related past investigations of your own or shared with the team.
- Your question, and the results of the tools the model calls.

Obvious secrets, such as tokens and passwords, are removed by pattern matching before anything is sent, but patterns can miss some. Logs, runbooks, and past work are marked as untrusted data, and the model is told never to follow instructions found in them.

> **Security:** Claude and Gemini stay off until an admin ticks the consent box, and an owner can block them for the whole organization with **Allow cloud AI models**. Prompt injection, a log line written to mislead the model, can't be fully prevented. Because the model has only read-only tools, the worst it can do is give a wrong answer, so read the evidence.

## Usage and cost

**Organization > Usage** shows this month's spend, a forecast, answers, average cost and time, and a table by model. Ollama shows $0, because it has no charge per use.
