# Kubernetes

Connect a cluster in **Connections > Logs and Kubernetes > Kubernetes**. The console then finds workload problems, diagnoses known failures, reads crash logs, and gives the model `get_kubernetes_state`, `describe_object`, and `get_pod_logs`.

The console only reads. It never reads Secrets or ConfigMap contents, and it has no code that writes to the cluster.

## What it reads

| Resource | Why |
|---|---|
| `pods` | Status, restarts, exit reasons, readiness |
| `pods/log` | The last lines of a crashed container, once per restart |
| `events` | Warning events, such as failed probes and scheduling failures |
| `deployments` | Rollouts and available replicas |
| `services` | Whether a Service reaches any ready pod, which explains most *connection refused* errors, and which workload calls which, for the [likely cause](/docs/alerts-and-webhooks#likely-cause-one-root-many-symptoms) of a group of problems. Only host names are read from pod environments; secret-looking variables are skipped and no value is kept |
| `nodes` | Node readiness and pressure |

StatefulSets, DaemonSets, and Jobs are found through their pods' owners, so they need no extra permission.

## What it finds

Problems are counted per workload, worst first, and each gets a diagnosis written by code, not by AI:

| Problem | Typical cause the diagnosis points to |
|---|---|
| Crash loop | The app exits on start: the last log lines, exit code, and the error |
| Out of memory | Killed at its memory limit |
| Liveness probe kills | The probe path or port is wrong, or the app is too slow to answer |
| Configuration error | A missing Secret or ConfigMap, or a missing key in one |
| Image pull failure | A wrong tag, or registry credentials |
| Unschedulable | Asks for more CPU or memory than any node has, or no node matches |
| Never ready | The readiness probe fails |
| Rollout stuck | A new version that never becomes available |
| Node not ready, node pressure | Node trouble, such as memory or disk pressure |

## 1. Create a read-only account

Apply this once. It creates a ServiceAccount that can only `get` and `list` what's above:

```yaml
apiVersion: v1
kind: Namespace
metadata: { name: rcc }
---
apiVersion: v1
kind: ServiceAccount
metadata: { name: rcc-reader, namespace: rcc }
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata: { name: rcc-reader }
rules:
  - apiGroups: [""]
    resources: [pods, pods/log, events, nodes, services]
    verbs: [get, list]
  - apiGroups: [apps]
    resources: [deployments]
    verbs: [get, list]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata: { name: rcc-reader }
roleRef: { apiGroup: rbac.authorization.k8s.io, kind: ClusterRole, name: rcc-reader }
subjects: [{ kind: ServiceAccount, name: rcc-reader, namespace: rcc }]
```

The same file is in the project as `deploy/k8s/reader-rbac.yaml`:

```bash
kubectl apply -f deploy/k8s/reader-rbac.yaml
kubectl auth can-i list pods --as=system:serviceaccount:rcc:rcc-reader -A        # yes
kubectl auth can-i get secrets --as=system:serviceaccount:rcc:rcc-reader -A      # no
kubectl auth can-i delete pods --as=system:serviceaccount:rcc:rcc-reader -A      # no
```

Leaving out `nodes` is fine: node problems then aren't watched, and the console says so. Leaving out `pods/log` means no crash logs in diagnoses or investigations.

### Only some namespaces

To limit the console to certain namespaces, bind the same ClusterRole with a **RoleBinding** in each namespace instead of the ClusterRoleBinding, and enter one namespace in the form's **Namespace** field:

```bash
kubectl -n shop create rolebinding rcc-reader --clusterrole=rcc-reader --serviceaccount=rcc:rcc-reader
```

`nodes` are cluster-wide, so a RoleBinding can't grant them. Node problems aren't watched in this setup.

## 2. Get a token

```bash
kubectl -n rcc create token rcc-reader --duration=720h
```

This makes a token that expires after 30 days. Some clusters cap the duration lower, and when the token expires the connection stops answering until you paste a new one. Put a reminder in your calendar.

If you need a token that doesn't expire, create a token Secret. It's valid until you delete the Secret, so store it as carefully as a password and rotate it on a schedule:

```bash
kubectl apply -f - <<'EOF'
apiVersion: v1
kind: Secret
metadata:
  name: rcc-reader-token
  namespace: rcc
  annotations: { kubernetes.io/service-account.name: rcc-reader }
type: kubernetes.io/service-account-token
EOF
kubectl -n rcc get secret rcc-reader-token -o jsonpath='{.data.token}' | base64 -d
```

> **Security:** When the console runs inside the cluster, skip tokens altogether: use [the in-cluster option](#console-inside-the-cluster). Kubernetes then rotates the token for you.

## 3. Get the address and CA certificate

```bash
# The API server address of your current context
kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}'; echo

# Its CA certificate, in PEM form
kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' | base64 -d > cluster-ca.crt
openssl x509 -in cluster-ca.crt -noout -subject -enddate
```

If your kubeconfig has `certificate-authority` (a file path) instead of `certificate-authority-data`, open that file.

## 4. Fill in the form

| Field | What to enter |
|---|---|
| Address | The API server address, such as `https://10.0.0.1:6443` or `https://NAME-dns-abc123.hcp.westeurope.azmk8s.io:443` |
| Namespace | Optional. Empty means every namespace the token can read |
| Read-only token | The token from step 2 |
| CA certificate | The PEM from step 3 |
| TLS server name | Only when you connect through another name than the one on the certificate. See [Docker Desktop](#docker-desktop) |
| Verify the certificate | Keep it on. Turn it off only on a test cluster |

**Test connection**, then **Save**. After saving, the CA certificate shows as one line, such as *CA certificate saved: kubernetes, expires 2035-01-01*, with **Replace** and **Remove**. Leaving the box empty when you save again keeps it.

## Console inside the cluster

When the console itself runs in the cluster (see [Deploy on AKS](/docs/deploy-aks)), tick **The console runs inside this cluster**. It then uses its own mounted ServiceAccount token and the cluster's CA, with no address or token to enter.

The console's Deployment must run as the `rcc-reader` ServiceAccount, with the token mounted:

```bash
kubectl -n rcc patch deployment console --type merge -p \
  '{"spec":{"template":{"spec":{"serviceAccountName":"rcc-reader","automountServiceAccountToken":true}}}}'
```

## Docker Desktop

Docker Desktop's API server certificate names `kubernetes.docker.internal`, but a container reaches your computer as `host.docker.internal`. So:

| Field | Value |
|---|---|
| Address | `https://host.docker.internal:6443` |
| CA certificate | From step 3, with the `docker-desktop` context selected |
| TLS server name | `kubernetes.docker.internal` |

```bash
kubectl config use-context docker-desktop
kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}'; echo   # https://kubernetes.docker.internal:6443
```

## AKS

```bash
az aks get-credentials -g RESOURCE_GROUP -n CLUSTER
az aks show -g RESOURCE_GROUP -n CLUSTER --query fqdn -o tsv          # address: https://FQDN:443
az aks show -g RESOURCE_GROUP -n CLUSTER --query apiServerAccessProfile
```

- **Authorized IP ranges:** if the API server only accepts certain addresses, add the console server's outgoing IP address.
- **Private cluster:** the API server has only a private address, so the console must run in a network that can reach it (the same VNet, a peered VNet, or inside the cluster).
- **Entra ID sign-in on the cluster** doesn't affect ServiceAccount tokens, which keep working.

## Amazon EKS and Google GKE

```bash
# EKS
aws eks describe-cluster --name CLUSTER --query 'cluster.[endpoint,certificateAuthority.data]' --output text
# GKE
gcloud container clusters describe CLUSTER --region REGION --format 'value(endpoint,masterAuth.clusterCaCertificate)'
```

The CA comes base64-encoded: decode it with `base64 -d`. The address is `https://ENDPOINT`. ServiceAccount tokens work the same way on both.

## Projects and namespaces

A [project](/docs/projects-and-reports) can include namespaces, such as `shop` and `shop-jobs`. Only problems in those namespaces count towards the project.

## Common errors

| Error | Cause and fix |
|---|---|
| Certificate verify failed, hostname mismatch | The address isn't on the certificate. Set **TLS server name** to a name that is, such as `kubernetes.docker.internal` |
| Certificate verify failed, unknown CA | Paste the cluster's CA certificate |
| `401 Unauthorized` | The token expired or is wrong. Create a new one |
| `403 Forbidden ... cannot list resource "nodes"` | The role lacks `nodes`. Add it, or accept that node problems aren't watched |
| `403 ... "services"` | Add `services` to the ClusterRole. Older setups didn't include it |
| Timeout | The console's server can't reach the API server: firewall, authorized IP ranges, or a private cluster |
