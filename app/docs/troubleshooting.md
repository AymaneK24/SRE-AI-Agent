# Troubleshooting

Find the message you see, then the fix. Most problems are network paths, certificates, or host names.

## Installing and starting

| Message or symptom | Cause | Fix |
|---|---|---|
| `.env already exists, so nothing was changed` | `setup-env.sh` won't replace your secrets | Edit `.env` by hand. A new `RCC_SECRET_KEY` would make saved credentials unreadable |
| `Refusing to start in production mode: ...` | A required setting is missing or unsafe | The message lists each one, such as `RCC_PUBLIC_URL must use https`. Fix `.env`, then `docker compose up -d` |
| `Run sh deploy/setup-env.sh first` | Compose can't find `.env` values | Run the script in the project folder |
| `bind: address already in use` on 80 or 443 | Another program uses the port | Set `RCC_HTTP_PORT` and `RCC_HTTPS_PORT`, and add the port to `RCC_PUBLIC_URL` |
| Opening `https://IP-ADDRESS` fails with *ERR_SSL_PROTOCOL_ERROR*, `tlsv1 alert internal error`, or `SEC_E_INTERNAL_ERROR`, while `https://localhost` works | Clients send no server name to an IP address, so Caddy doesn't know which certificate to use | Set `RCC_DEFAULT_SNI` to that IP address in `.env`, and include it in `RCC_DOMAIN`, then `docker compose up -d`. See [Let others on your network reach it](/docs/deploy-localhost#let-others-on-your-network-reach-it) |
| The browser warns about the certificate | The self-made certificate | Expected with that choice. See [Certificates](/docs/certificates#made-by-this-server) |
| `Open the console at the address in RCC_PUBLIC_URL, or add this name to RCC_ALLOWED_HOSTS` | You used another host name or IP than the configured one | Use the configured address, or add the name to `RCC_ALLOWED_HOSTS` and restart |
| Let's Encrypt over the internet fails in Caddy's log | The name doesn't point at the server, or port 80 is closed | `dig +short NAME`, and open port 80 from the internet. For a private server, use another certificate choice |
| Let's Encrypt through Azure DNS fails | The managed identity lacks **DNS Zone Contributor** on the public zone, the zone isn't delegated, or outbound access to Azure, Let's Encrypt, or public DNS is blocked | `dig NS rcc.company.com @1.1.1.1` must show Azure's name servers. Check the role assignment and `docker compose logs caddy` |
| `pull access denied for rcc-caddy-dns` | The custom Caddy image wasn't built | `docker build --pull -t rcc-caddy-dns:2 deploy/caddy-dns` |
| Pages look old after an update | The browser cached the previous files | Reload with Ctrl+Shift+R |
| Settings, accounts, and connections are gone | The volumes were deleted, for example by `docker compose down -v` | Restore from backup. Use `docker compose down` without `-v` |

## Signing in

| Message | Fix |
|---|---|
| `That email or password isn't correct.` | Check the email you were invited with. After repeated failures, that device pauses for 15 minutes |
| `Verify your email first.` | Open the link in the email, or ask an admin to resend the invitation |
| `Your organization requires single sign-on. Use Sign in with SSO.` | Use **Sign in with SSO**. Only the break-glass account can use a password |
| `Single sign-on isn't set up for this email domain.` | An owner adds the domain in **Organization > Single sign-on** |
| *Single sign-on didn't finish* | See [Single sign-on](/docs/single-sign-on#when-sign-in-fails) |
| No reset email arrives | Without an email server, reset links go to the console's log: `docker compose logs console | grep -i reset` |
| `This invitation is invalid or has expired.` | Invitations last 72 hours and work once. Ask for a new one |

## Connections

| Message | Cause | Fix |
|---|---|---|
| *A connection isn't answering* | A connection failed on its last read | The banner and **Connections** show the error. The overview keeps the last good data |
| `points to this server itself, which can't be used as a target` | You entered `localhost` or `127.0.0.1` | In Docker, use `host.docker.internal` or the host's IP |
| `isn't in this installation's list of allowed targets (RCC_TARGET_ALLOWLIST)` | The allowlist doesn't include it | Add the exact host name or a range containing its IP |
| `is a reserved address and can't be used as a target` | A link-local or metadata address | These are always refused, by design |
| Timeout or connection refused | No network path from the console's server | [Test the path](/docs/finding-connection-details#test-the-path-before-you-connect) from the server |
| `queries require at least one regexp or equality matcher that does not have an empty-compatible value` | A Loki selector with no matcher that selects on its own | See [The rule Loki enforces](/docs/loki#the-rule-loki-enforces). Recent versions fix the simple case themselves |
| Loki shows streams as `unknown` | Your log agent doesn't set a `job` label | Add one in the agent's config, or ignore it |
| A stream shows as *namespace (pods without an app label)* | Those pods have no `app` label | Add `app` labels to the workloads, or ignore it |
| `logging/loki` shows many error-like lines | Older versions counted Loki's own records of the console's queries | Update. The count drops as old lines leave the 5-minute window |
| Kubernetes: certificate verify failed, hostname mismatch | The address isn't on the API server's certificate | Set **TLS server name**, such as `kubernetes.docker.internal` |
| Kubernetes: `401` | The token expired | [Create a new token](/docs/kubernetes#2-get-a-token) |
| Kubernetes: `403 ... cannot list resource "services"` | The role lacks `services` | Re-apply `deploy/k8s/reader-rbac.yaml` |
| The Kubernetes panel disappeared after adding Prometheus | The overview shows one view at a time | Use the **Cluster / Metrics / Logs** switch |

## Investigations and AI models

| Symptom | Cause | Fix |
|---|---|---|
| *No models found* for Ollama | The console can't reach Ollama, or Ollama has no models | From the server: `curl http://OLLAMA:11434/api/tags`. Ollama must listen on `0.0.0.0` if it's on another machine. Then `ollama pull MODEL` |
| The model answers without checking anything | **Let the model run queries** is off (the default for Ollama) | Turn it on in **Connections > AI model** |
| The model asks you questions instead of investigating, or repeats the same call | A small model | Use a 7B or larger model, or a hosted one. See [AI models](/docs/ai-models#which-model) |
| The answer starts with *The AI model didn't give an answer* | The model used all its rounds without answering, so the console summarised what was found | Ask a narrower question, raise **Most model rounds per prompt**, or use a stronger model |
| An investigation takes many minutes | Ollama on a CPU reads thousands of tokens each round | Use a GPU or a hosted model, or turn off **Ask again if an answer is incomplete** |
| The answer ignores what's in the logs | Context cut off by a small **Model memory** | Keep `num_ctx` at 8192 or more |
| Claude or Gemini isn't offered | The consent box isn't ticked, the key is missing, or an owner turned off **Allow cloud AI models** | Check **Connections > AI model** and **Organization > Security policies** |
| New investigations are refused | The monthly budget is reached | An admin raises it in **Organization > Usage** |
| `Stop the running investigation first.` | One question at a time per investigation | Wait, or stop it |

## Reports, runbooks, and webhooks

| Message | Fix |
|---|---|
| *The PDF is ready when the notes are* | AI notes are being written. The download appears when they finish |
| *Notes failed* | Select **Try AI notes again**, or check the AI model. Notes taking over 20 minutes are marked failed |
| `This workflow hasn't been reviewed yet.` | An admin opens it and selects **Mark as reviewed** |
| `Someone is already following this workflow.` | Continue that run, or finish it first |
| `Unknown webhook address.` | The token is wrong, or the addresses were replaced. Copy the current ones |
| `Include a title for this change.` | Change events need a `title` field |
| `400` from a webhook sent from inside a cluster | The host name it used isn't allowed. Add it to `RCC_ALLOWED_HOSTS`. See [Reach the console from the cluster](/docs/deploy-localhost#reach-the-console-from-the-cluster) |

## The docs

| Symptom | Fix |
|---|---|
| *Sign in to read the documentation* | The docs need a sign-in. Sign in, then open `/docs` again |

## Collecting information for help

```bash
docker compose ps
docker compose logs --since 30m console > console.log
docker compose logs --since 30m caddy > caddy.log
```

> **Security:** Logs can contain host names, email addresses, and invitation or reset links. Remove those before you share them.
