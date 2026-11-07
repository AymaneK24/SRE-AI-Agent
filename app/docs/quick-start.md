# Quick start

This gets the console running on your own computer in about ten minutes, so you can try it before installing it for a team. For every detail, see [Deploy on localhost](/docs/deploy-localhost).

## What you need

- Docker with Docker Compose. On Windows, Docker Desktop with WSL 2.
- Git, and the project files.
- Something to watch: a Prometheus, a Loki, or a Kubernetes cluster. Docker Desktop's built-in Kubernetes is enough.
- An AI model: Ollama on your computer, or an API key for Gemini or Claude.

## 1. Write the settings file

From the project folder:

```bash
sh deploy/setup-env.sh
```

Answer the questions:

| Question | Answer for a local trial |
|---|---|
| Address people will use | `localhost` |
| Which HTTPS certificate | `3`, made by this server |
| HTTPS port | `443`, or `8443` if something already uses 443 |
| Email server | Leave empty |

The script writes `.env` with new random secrets and prints your first password. It refuses to overwrite an existing `.env`, because a new `RCC_SECRET_KEY` would make saved credentials unreadable.

> **Security:** `.env` holds the key that encrypts every saved credential. Keep a copy somewhere safe, and never commit it to Git.

## 2. Start it

```bash
docker compose up -d --build
docker compose ps
```

Three containers start: `console` (the app), `postgres` (the database), and `caddy` (HTTPS). Only Caddy publishes ports.

## 3. Sign in

Open `https://localhost` (or `https://localhost:8443`). The browser warns about the certificate the first time, because Caddy made it itself. Accept it for this trial.

Sign in with the email and password the script printed. You must choose a new password, of at least 12 characters.

## 4. Connect something

Go to **Connections**:

1. **Kubernetes:** follow [Kubernetes on Docker Desktop](/docs/kubernetes#docker-desktop).
2. **Prometheus:** add a metric source with its address. See [Prometheus](/docs/prometheus).
3. **Loki:** enter its address. See [Loki](/docs/loki).
4. **AI model:** choose Ollama, Gemini, or Claude. See [AI models](/docs/ai-models).

Any one connection is enough to start. Within a minute, the **Overview** shows what's wrong.

## 5. Give it something to find

The project includes a test namespace in which several services fail in different, realistic ways:

```bash
kubectl apply -f deploy/examples/shop-simulation.yaml
kubectl -n shop get pods -w
```

After one to three minutes, the overview lists the failures. Go to **Investigate** and ask: *Why is payment crash-looping?* The right answer is that the `postgres` Service has no pods behind it. [AI accuracy benchmark](/docs/accuracy-benchmark) lists the cause of each failure.

Remove it with `kubectl delete namespace shop`.

## Next steps

- [Concepts](/docs/concepts): what problems, events, incidents, and projects are.
- [Investigations](/docs/investigations): how to ask good questions.
- [Deploy on an Azure VM](/docs/deploy-azure-vm): install it for your team.
