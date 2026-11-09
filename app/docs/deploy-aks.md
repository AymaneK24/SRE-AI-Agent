# Deploy on AKS

Run the console in Azure Kubernetes Service, with the files in `deploy/k8s`: the console, PostgreSQL, and Caddy for HTTPS. The console can then also watch the cluster it runs in, with no token to manage.

> **Security:** We recommend a **private** console: Caddy on an internal load balancer (the default in `caddy.yaml`), reached over your VPN or ExpressRoute, with a company certificate. A public load balancer is an option for consoles people must reach from anywhere, described in [A public console](#a-public-console).

> **Note:** The Kubernetes files were checked for validity, but haven't been run on a real AKS cluster yet. Do a trial install in a test cluster first.

## Architecture

```
VPN or VNet ──> internal Azure Load Balancer ──> caddy (443) ──> console (8000) ──> postgres (5432)
                                                                 │
                                                                 └──> Prometheus, Loki, the API server, AI model
```

- **One console replica.** Live investigations and monitoring are held in memory, so the Deployment stays at `replicas: 1` with the `Recreate` strategy. Give it more CPU and memory rather than more copies.
- **State lives in PostgreSQL.** The console's own volume is temporary, because everything lasting is in the database and `RCC_SECRET_KEY`.

## 1. Registry and images

```bash
RG=rg-rcc; AKS=aks-prod; ACR=rccCOMPANY
az acr create -g $RG -n $ACR --sku Standard
az aks update -g $RG -n $AKS --attach-acr $ACR

# Build the console's image in ACR from the project folder
az acr build -r $ACR -t root-cause-console:$(git rev-parse --short HEAD) .

# Copy the database and proxy images into your registry, so the cluster pulls only from it
az acr import -n $ACR --source docker.io/library/postgres:16-alpine --image postgres:16-alpine
az acr import -n $ACR --source docker.io/library/caddy:2-alpine --image caddy:2-alpine
```

In `deploy/k8s/*.yaml`, replace `ACR_NAME` with your registry name and `IMAGE_TAG` with the tag you built:

```bash
TAG=$(git rev-parse --short HEAD)
sed -i "s/ACR_NAME/$ACR/g; s/IMAGE_TAG/$TAG/g" deploy/k8s/*.yaml
```

## 2. Namespace, settings, and secrets

```bash
kubectl apply -f deploy/k8s/reader-rbac.yaml       # creates the rcc namespace and the read-only account

DOMAIN=rcc.company.com
kubectl -n rcc create configmap rcc-config \
  --from-literal=RCC_MODE=production \
  --from-literal=RCC_DOMAIN=$DOMAIN \
  --from-literal=RCC_PUBLIC_URL=https://$DOMAIN \
  --from-literal=RCC_ALLOWED_HOSTS=$DOMAIN \
  --from-literal=RCC_COMPANY_NAME="Company" \
  --from-literal=RCC_ADMIN_NAME="Your Name" \
  --from-literal=RCC_ADMIN_EMAIL=you@company.com

PGPASS=$(openssl rand -hex 32)
umask 077
cat > rcc-secrets.env <<EOF
RCC_SECRET_KEY=$(openssl rand -hex 32)
RCC_ADMIN_PASSWORD=$(openssl rand -hex 16)
POSTGRES_PASSWORD=$PGPASS
RCC_DATABASE_URL=postgresql://rcc:$PGPASS@postgres:5432/rcc
EOF
kubectl -n rcc create secret generic rcc-secrets --from-env-file=rcc-secrets.env
grep RCC_ADMIN_PASSWORD rcc-secrets.env     # your first password
```

Store `rcc-secrets.env` in your vault, then delete it from disk: `shred -u rcc-secrets.env`.

Choose the certificate (see [Certificates](/docs/certificates)). For a private console, use your company's certificate:

```bash
# 1. Your company's certificate (recommended). caddy.yaml mounts this Secret at /certs.
kubectl -n rcc create secret tls rcc-tls --cert=cert.pem --key=key.pem
kubectl -n rcc create configmap caddyfile --from-file=Caddyfile=deploy/Caddyfile.internal

# 2. Or a certificate Caddy makes itself, for a trial
kubectl -n rcc create configmap caddyfile --from-file=Caddyfile=deploy/Caddyfile.selfsigned
```

For a certificate every browser trusts on a private console, use [cert-manager with Azure DNS](#trusted-certificates-with-cert-manager), which writes the same `rcc-tls` Secret.

### Secrets from Azure Key Vault (recommended)

Rather than creating the Secret by hand, keep the values in Key Vault and sync them with the Secrets Store CSI driver:

```bash
az aks enable-addons -g $RG -n $AKS --addons azure-keyvault-secrets-provider
az keyvault secret set --vault-name VAULT -n rcc-secret-key --value "$(openssl rand -hex 32)"
```

Then a `SecretProviderClass` with `secretObjects` creates `rcc-secrets` from Key Vault. Follow Microsoft's guide for the Secrets Store CSI driver with workload identity.

## 3. Deploy

```bash
kubectl apply -f deploy/k8s/postgres.yaml
kubectl -n rcc rollout status statefulset/postgres
kubectl apply -f deploy/k8s/console.yaml
kubectl -n rcc rollout status deployment/console
kubectl apply -f deploy/k8s/caddy.yaml
kubectl -n rcc get svc caddy -w        # wait for EXTERNAL-IP
```

The `EXTERNAL-IP` is a private address in the cluster's VNet. Give it an internal DNS name, in an Azure Private DNS zone linked to the VNets your people come from, or in your company DNS:

```bash
az network private-dns record-set a add-record -g $RG -z company.internal -n rcc -a EXTERNAL_IP
```

Make sure that address is reachable from your VPN or ExpressRoute, and that the cluster subnet's NSG allows 443 from those ranges only.

### Trusted certificates with cert-manager

cert-manager can get a Let's Encrypt certificate through the **DNS-01** challenge in Azure DNS, so nothing on the internet connects to the cluster. It writes the certificate to the `rcc-tls` Secret, which Caddy reads with `deploy/Caddyfile.internal`.

1. Install cert-manager, and give it a workload identity with **DNS Zone Contributor** on a dedicated public zone, such as `rcc.company.com`, as in [the VM guide](/docs/deploy-azure-vm#let-s-encrypt-through-azure-dns). Follow cert-manager's *AzureDNS* guide for the workload identity setup.
2. Create a `ClusterIssuer` with the `azureDNS` solver for that zone, and a `Certificate` named `rcc-tls` in the `rcc` namespace for `rcc.company.com`.
3. Resolve `rcc.company.com` privately, with a private DNS zone of the same name pointing at Caddy's internal address.

Caddy reads the certificate files when it starts. cert-manager renews 30 days before expiry, so restart Caddy after renewals, for example weekly with a CronJob, or automatically with a tool such as Stakater Reloader: `kubectl -n rcc rollout restart deployment/caddy`.

### A public console

Only when people must reach it from anywhere:

1. Remove the internal annotation, so the load balancer gets a public IP:

   ```bash
   kubectl -n rcc annotate svc caddy service.beta.kubernetes.io/azure-load-balancer-internal-
   kubectl -n rcc get svc caddy -w
   ```

2. Point a public DNS name at it: `az network dns record-set a add-record -g DNS_RG -z company.com -n rcc -a EXTERNAL_IP`.
3. Use Let's Encrypt over the internet: `kubectl -n rcc create configmap caddyfile --from-file=Caddyfile=deploy/Caddyfile -o yaml --dry-run=client | kubectl apply -f -`, then `kubectl -n rcc rollout restart deployment/caddy`. Ports 80 and 443 must be open to the internet.
4. Turn on [single sign-on](/docs/single-sign-on) and **Require single sign-on for everyone**, and consider Azure Application Gateway with WAF or Azure Front Door in front.

## 4. Watch this cluster

The console's Deployment ships with `automountServiceAccountToken: false`. To let it watch the cluster it runs in, run it as the read-only account:

```bash
kubectl -n rcc patch deployment console --type merge -p \
  '{"spec":{"template":{"spec":{"serviceAccountName":"rcc-reader","automountServiceAccountToken":true}}}}'
```

Then in **Connections > Kubernetes**, tick **The console runs inside this cluster** and save. See [Kubernetes](/docs/kubernetes).

Prometheus and Loki in the same cluster are reached by their Service names, such as `http://prometheus-operated.monitoring.svc.cluster.local:9090`.

## 5. Restrict traffic

With a network plugin that enforces NetworkPolicy (`--network-policy azure`, `calico`, or `cilium`):

```bash
kubectl apply -f deploy/k8s/networkpolicy.yaml
```

Caddy then accepts 80 and 443 from anywhere, the console only accepts traffic from Caddy, and PostgreSQL only from the console.

## 6. PostgreSQL

The in-cluster PostgreSQL uses a 32 GB Azure disk (`managed-csi`). For important data, use **Azure Database for PostgreSQL Flexible Server** instead, with private access in the cluster's VNet, automatic backups, and high availability:

```bash
az postgres flexible-server create -g $RG -n rcc-db --version 16 --tier GeneralPurpose --sku-name Standard_D2ds_v5 \
  --vnet AKS_VNET --subnet DB_SUBNET --private-dns-zone rcc-db.private.postgres.database.azure.com \
  --admin-user rccadmin --admin-password "$(openssl rand -hex 24)"
```

Then create the console's own account and database, and set `RCC_DATABASE_URL` in the Secret, as described in [External database](/docs/configuration#external-database). Skip `postgres.yaml`.

## Operate it

```bash
kubectl -n rcc logs deploy/console -f                    # logs
kubectl -n rcc rollout restart deployment/console        # restart
kubectl -n rcc exec statefulset/postgres -- pg_dump -U rcc -d rcc | gzip > rcc-$(date +%F).sql.gz   # backup
```

To update, build a new tag, set it, and wait for the rollout:

```bash
az acr build -r $ACR -t root-cause-console:NEW_TAG .
kubectl -n rcc set image deployment/console console=$ACR.azurecr.io/root-cause-console:NEW_TAG
kubectl -n rcc rollout status deployment/console
```

## Security checklist for AKS

- The console runs as a non-root user (10001) with a read-only root filesystem, no Linux capabilities, and `RuntimeDefault` seccomp. Keep these settings.
- Pull images only from your ACR, and scan them: Microsoft Defender for Containers, or `trivy image`.
- Keep `RCC_SECRET_KEY` in Key Vault, not in Git or a pipeline file.
- Keep Caddy's load balancer internal, and use the NetworkPolicy file.
- Prefer a **private AKS cluster** (`--enable-private-cluster`), or at least `--api-server-authorized-ip-ranges`, so the Kubernetes API isn't open to the internet.
- Route the cluster's outbound traffic through Azure Firewall (`--outbound-type userDefinedRouting`) to allow only the destinations the console needs.
- Give the `rcc-reader` role only the resources listed in [Kubernetes](/docs/kubernetes#what-it-reads).
