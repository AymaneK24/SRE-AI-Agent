# Finding connection details

Commands to find the addresses, ports, certificates, tokens, and secrets the console asks for, and to test them before you connect.

## Test the path before you connect

The address must work **from the console's server**, not from your laptop. The console's image has no shell, so test from a throwaway container on the same Docker network:

```bash
docker network ls | grep default            # usually FOLDER_default, such as root-cause-console_default
docker run --rm --network root-cause-console_default curlimages/curl:8.10.1 \
  -sS -m 5 -o /dev/null -w '%{http_code}\n' http://PROMETHEUS:9090/-/ready
```

`200` means it's reachable. A timeout means a firewall, a wrong address, or no route. `Could not resolve host` means a DNS name the server doesn't know.

On Kubernetes, the same from inside the console's namespace:

```bash
kubectl -n rcc run nettest --rm -it --restart=Never --image=curlimages/curl:8.10.1 -- \
  curl -sS -m 5 http://prometheus-operated.monitoring.svc.cluster.local:9090/-/ready
```

## Host names and IP addresses

| What you need | Command |
|---|---|
| This Linux server's IP addresses | `hostname -I` or `ip -brief addr` |
| What a name resolves to | `getent hosts NAME` or `dig +short NAME` |
| Which ports are listening here | `sudo ss -tlnp` |
| A Kubernetes Service's cluster address | `kubectl -n NS get svc NAME -o wide` |
| Its in-cluster DNS name | `NAME.NAMESPACE.svc.cluster.local` |
| A LoadBalancer's address | `kubectl -n NS get svc NAME -o jsonpath='{.status.loadBalancer.ingress[0].ip}'` |
| Ingress host names | `kubectl get ingress -A` |
| An Azure VM's public and private IP | `az vm list-ip-addresses -g RG -n VM -o table` |
| An AKS API server | `az aks show -g RG -n CLUSTER --query fqdn -o tsv` |
| An EC2 instance's private IP | `aws ec2 describe-instances --instance-ids ID --query 'Reservations[].Instances[].PrivateIpAddress'` |
| Something on the Docker host, from a container | `host.docker.internal` |

## Ports

| Service | Usual port |
|---|---|
| Prometheus | 9090 |
| Alertmanager | 9093 |
| Loki | 3100, or 80 on `loki-gateway` |
| Kubernetes API | 6443 on self-managed and Docker Desktop, 443 on AKS, EKS, and GKE |
| Ollama | 11434 |
| The console itself | 8000 inside, 443 through Caddy |

## Certificates

```bash
# What certificate does a server present, and for which names?
openssl s_client -connect HOST:443 -servername HOST </dev/null 2>/dev/null \
  | openssl x509 -noout -subject -issuer -enddate -ext subjectAltName

# Save the whole chain it sends
openssl s_client -connect HOST:443 -servername HOST -showcerts </dev/null 2>/dev/null \
  | awk '/BEGIN CERT/,/END CERT/' > chain.pem

# Read a PEM file
openssl x509 -in cert.pem -noout -text | less

# Does a certificate match its key?
diff <(openssl x509 -in cert.pem -noout -pubkey) <(openssl pkey -in key.pem -pubout) && echo match

# A Kubernetes cluster's CA
kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' | base64 -d

# The CA that Caddy made for the console, when using the self-signed choice
docker compose exec caddy cat /data/caddy/pki/authorities/local/root.crt
```

**Hostname mismatch** means the address you entered isn't in the certificate's `subjectAltName`. Connect with a name that is, or for Kubernetes set **TLS server name** to it.

## Tokens and keys

| Credential | How to get it |
|---|---|
| Kubernetes ServiceAccount token | `kubectl -n rcc create token rcc-reader --duration=720h`. See [Kubernetes](/docs/kubernetes#2-get-a-token) |
| Azure client ID and secret | `az ad sp create-for-rbac --name NAME --role ROLE --scopes SCOPE` |
| AWS access key | `aws iam create-access-key --user-name NAME`, or better an IAM role |
| Google service account key | `gcloud iam service-accounts keys create key.json --iam-account SA_EMAIL` |
| Gemini API key | [Google AI Studio](https://aistudio.google.com/apikey) |
| Claude API key | [Claude Console](https://platform.claude.com) |
| Slack webhook | Slack app settings, Incoming Webhooks |
| Gmail app password | Google Account, Security, App passwords (needs two-step verification) |

## The console's own secrets

```bash
openssl rand -hex 32        # RCC_SECRET_KEY, POSTGRES_PASSWORD, RCC_METRICS_TOKEN
openssl rand -hex 16        # a first admin password
```

`deploy/setup-env.sh` makes these for you. To read a value from an existing `.env` without printing the whole file:

```bash
grep '^RCC_PUBLIC_URL=' .env
```

> **Security:** Never paste `.env`, tokens, or keys into tickets, chat, or AI tools, including the console's own investigations. Credentials belong in the connection forms, where they're encrypted, or in a secret store.

## Storing secrets

| Where it runs | Keep secrets in |
|---|---|
| Docker Compose on a VM | `.env`, readable only by root (`chmod 600 .env`), with a copy in your password manager or vault |
| Kubernetes | A Secret, created from a file and never committed: `kubectl -n rcc create secret generic rcc-secrets --from-env-file=secrets.env` |
| AKS | Azure Key Vault with the Secrets Store CSI driver, synced to the `rcc-secrets` Secret |

```bash
# Azure Key Vault
az keyvault secret set --vault-name VAULT -n rcc-secret-key --value "$(openssl rand -hex 32)"
az keyvault secret show --vault-name VAULT -n rcc-secret-key --query value -o tsv
```

> **Warning:** If you lose `RCC_SECRET_KEY`, every saved credential becomes unreadable and must be entered again. Back it up separately from the database backups, so one stolen backup doesn't hold both.
