# Deploy on an Azure VM

One Linux VM runs the console, PostgreSQL, and Caddy with Docker Compose. It's the simplest setup for a team, and easy to back up and update.

> **Security:** We recommend a **private** server: no public IP address, reached only over your VPN, ExpressRoute, or office network. The console holds read access to your monitoring and a map of your systems, so it has no reason to be on the internet. Use a public server only if people must reach it from anywhere, and then require single sign-on.

## Private or public

| | Private (recommended) | Public |
|---|---|---|
| Public IP on the VM | None | Yes |
| Who can connect | Your VPN, ExpressRoute, and office ranges | Anyone on the internet, limited by NSG rules |
| DNS name | Internal, in an Azure Private DNS zone or your company DNS | Public |
| Certificate | Your company CA, made by this server, or Let's Encrypt through Azure DNS | Let's Encrypt over the internet |
| Admin access to the VM | Azure Bastion | Azure Bastion (recommended), or SSH from one address |
| Outbound internet | Through a NAT gateway or Azure Firewall, for updates and any cloud AI | Direct |

Private doesn't mean offline. The VM can still reach out (for OS updates, container images, a cloud AI model, Slack, or your SSO provider) while nothing on the internet can reach in.

## What you need

| Item | Recommended |
|---|---|
| VM size | `Standard_B2ms` (2 vCPU, 8 GB) or larger. 2 vCPU and 4 GB is the minimum |
| Image | Ubuntu Server 24.04 LTS |
| Disk | 64 GB Premium SSD. Managed disks are encrypted at rest by default |
| Network | A subnet in a VNet that can reach your Prometheus, Loki, and Kubernetes API, and that your VPN reaches |
| Address | An internal DNS name, such as `rcc.company.internal` or `rcc.company.com` resolved privately |
| Certificate | See [step 7](#7-choose-the-certificate) |

## Private setup (recommended)

The commands use these names. Change them to yours, and use your existing hub-and-spoke VNet if you have one.

```bash
RG=rg-rcc
LOC=westeurope
VNET=rcc-vnet
SUBNET=rcc
VPN_CIDR=10.8.0.0/16          # your VPN or office ranges
az group create -n $RG -l $LOC
```

### 1. Network and outbound access

```bash
az network vnet create -g $RG -n $VNET --address-prefixes 10.50.0.0/16 \
  --subnet-name $SUBNET --subnet-prefixes 10.50.1.0/24

# Outbound internet without inbound: a NAT gateway with its own public IP
az network public-ip create -g $RG -n rcc-nat-ip --sku Standard
az network nat gateway create -g $RG -n rcc-nat --public-ip-addresses rcc-nat-ip --idle-timeout 10
az network vnet subnet update -g $RG --vnet-name $VNET -n $SUBNET --nat-gateway rcc-nat
az network public-ip show -g $RG -n rcc-nat-ip --query ipAddress -o tsv   # the address your VM goes out from
```

Connect the VNet to your network: peer it with your hub VNet, or attach it to your VPN gateway. Your VPN users must be able to reach `10.50.1.0/24` on port 443.

To allow only certain outbound destinations, route the subnet through **Azure Firewall** instead of the NAT gateway, with application rules for what the console needs: `download.docker.com`, `*.ubuntu.com`, `registry-1.docker.io`, `production.cloudflare.docker.com`, `github.com`, and, if you use them, `generativelanguage.googleapis.com`, `api.anthropic.com`, `hooks.slack.com`, and your SSO provider.

### 2. Create the VM without a public IP

```bash
az vm create -g $RG -n rcc-vm \
  --image Ubuntu2404 --size Standard_B2ms --os-disk-size-gb 64 \
  --vnet-name $VNET --subnet $SUBNET --public-ip-address "" --nsg-rule NONE \
  --admin-username azureuser --ssh-key-values ~/.ssh/id_ed25519.pub \
  --assign-identity

PRIVATE_IP=$(az vm show -d -g $RG -n rcc-vm --query privateIps -o tsv); echo $PRIVATE_IP
```

`--assign-identity` gives the VM a managed identity, used later for backups and, if you choose it, Let's Encrypt through Azure DNS. No password or key is stored for either.

### 3. Allow only your network in

```bash
NSG=rcc-vmNSG
az network nsg rule create -g $RG --nsg-name $NSG -n https-from-vpn --priority 100 \
  --access Allow --protocol Tcp --destination-port-ranges 443 --source-address-prefixes $VPN_CIDR
az network nsg rule create -g $RG --nsg-name $NSG -n ssh-from-bastion --priority 110 \
  --access Allow --protocol Tcp --destination-port-ranges 22 --source-address-prefixes 10.50.255.0/26
```

Nothing allows traffic from the internet, so Azure's default rules block it. Port 80 isn't needed: Caddy only uses it to redirect to HTTPS.

### 4. An internal DNS name

With an Azure Private DNS zone linked to the VNet:

```bash
az network private-dns zone create -g $RG -n company.internal
az network private-dns link vnet create -g $RG -z company.internal -n rcc-link -v $VNET -e false
az network private-dns record-set a add-record -g $RG -z company.internal -n rcc -a $PRIVATE_IP
```

People on the VPN or in the office resolve this name through your DNS. For on-premises DNS to see a private zone, use the **Azure DNS Private Resolver** with a conditional forwarder for `company.internal`, or add the record to your company DNS directly.

If you choose Let's Encrypt through Azure DNS in step 7, the name must be under a public domain you own, such as `rcc.company.com`. Use that zone name here instead: see [Let's Encrypt through Azure DNS](#let-s-encrypt-through-azure-dns).

### 5. Connect to the VM with Azure Bastion

```bash
az network vnet subnet create -g $RG --vnet-name $VNET -n AzureBastionSubnet --address-prefixes 10.50.255.0/26
az network public-ip create -g $RG -n rcc-bastion-ip --sku Standard
az network bastion create -g $RG -n rcc-bastion --vnet-name $VNET --public-ip-address rcc-bastion-ip \
  --sku Standard --enable-tunneling true

az network bastion ssh -g $RG -n rcc-bastion --auth-type ssh-key --username azureuser --ssh-key ~/.ssh/id_ed25519 \
  --target-resource-id $(az vm show -g $RG -n rcc-vm --query id -o tsv)
```

Bastion's public IP belongs to Azure's managed service, not to your VM. Sign-in goes through Microsoft Entra ID and Azure RBAC, and every session is logged. If you already reach the VNet over VPN, plain `ssh azureuser@$PRIVATE_IP` works too.

### 6. Prepare the VM

```bash
sudo apt-get update && sudo apt-get -y upgrade
sudo apt-get install -y git unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades     # automatic security updates
```

Get the project files. For a private repository, use a read-only deploy key:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/rcc_deploy -N ''
cat ~/.ssh/rcc_deploy.pub     # add it in GitHub: repository Settings > Deploy keys, read-only
sudo mkdir -p /opt/root-cause-console && sudo chown $USER /opt/root-cause-console
GIT_SSH_COMMAND='ssh -i ~/.ssh/rcc_deploy' git clone git@github.com:OWNER/REPO.git /opt/root-cause-console
cd /opt/root-cause-console
sh deploy/install-docker.sh
```

Log out and back in, so your user can run `docker`.

> **Security:** Membership of the `docker` group is equivalent to root on the VM. Give access to the VM only to the people who operate it.

### 7. Choose the certificate

On a private server, choose one of the first three. `deploy/setup-env.sh` asks in the same order:

| # | Certificate | Browsers trust it | Needs |
|---|---|---|---|
| 1 | **Your company's certificate** (recommended) | Yes, on company devices | A certificate for the name from your PKI team. See [Certificates](/docs/certificates#your-company-s-certificate) |
| 2 | **Made by this server** | Only after you install its root on each device | Nothing. Good for a trial or a small team |
| 3 | **Let's Encrypt through Azure DNS** | Yes, everywhere | A public DNS zone in Azure, and a custom Caddy image. See below |
| 4 | Let's Encrypt over the internet | Yes, everywhere | A public server. See [Public setup](#public-setup) |

### Let's Encrypt through Azure DNS

Let's Encrypt normally connects to your server to check that you control the name. With the **DNS-01** challenge, it checks a temporary TXT record in public DNS instead, so nothing connects to your server. You get a certificate every browser trusts, on a server with no inbound access from the internet.

The name must be under a domain you own publicly, such as `rcc.company.com`. Two zones with the same name keep it private:

- A **public** Azure DNS zone `rcc.company.com`. It holds only the temporary `_acme-challenge` records, never the server's address.
- A **private** DNS zone `rcc.company.com`, linked to the VNet, with the server's private address. Your network resolves the name; the internet doesn't.

```bash
DNS_RG=rg-dns
# A dedicated public zone, so the VM's identity can't change anything else in your domain
az network dns zone create -g $DNS_RG -n rcc.company.com
az network dns zone show -g $DNS_RG -n rcc.company.com --query nameServers -o tsv
# Delegate it from the parent zone (here in Azure; elsewhere, add NS records at your DNS provider)
az network dns record-set ns create -g PARENT_RG -z company.com -n rcc
for ns in $(az network dns zone show -g $DNS_RG -n rcc.company.com --query nameServers -o tsv); do
  az network dns record-set ns add-record -g PARENT_RG -z company.com -n rcc -d $ns
done

# Let the VM's managed identity write records in that zone only
az role assignment create --role "DNS Zone Contributor" \
  --assignee $(az vm show -g $RG -n rcc-vm --query identity.principalId -o tsv) \
  --scope $(az network dns zone show -g $DNS_RG -n rcc.company.com --query id -o tsv)

# The private zone people resolve the name from
az network private-dns zone create -g $RG -n rcc.company.com
az network private-dns link vnet create -g $RG -z rcc.company.com -n rcc-link -v $VNET -e false
az network private-dns record-set a add-record -g $RG -z rcc.company.com -n @ -a $PRIVATE_IP
```

On the VM, build Caddy with the Azure DNS plugin once:

```bash
cd /opt/root-cause-console
docker build --pull -t rcc-caddy-dns:2 deploy/caddy-dns
```

Then choose `3` in `setup-env.sh`, and enter the subscription ID and the resource group of the **public** zone (`rg-dns`). It sets `RCC_CADDYFILE=./deploy/Caddyfile.azure-dns` and `RCC_CADDY_IMAGE=rcc-caddy-dns:2`. Caddy signs in to Azure with the VM's managed identity, so no secret is stored.

The VM needs outbound access to `acme-v02.api.letsencrypt.org`, `management.azure.com`, and public DNS resolvers (`1.1.1.1`, `8.8.8.8` on port 53). Caddy asks public resolvers to confirm the challenge record, because the private zone of the same name would hide it.

> **Note:** Every certificate Let's Encrypt issues is published in public Certificate Transparency logs, so the name `rcc.company.com` becomes publicly known, though nobody outside your network can resolve or reach it. If even the name must stay secret, use your company's certificate.

> **Note:** The Azure DNS image and Caddyfile follow the plugin's documented configuration but haven't been tested against real Azure DNS yet. Try them on a test name first, and watch `docker compose logs caddy`.

### 8. Settings and start

```bash
cd /opt/root-cause-console
sh deploy/setup-env.sh
```

| Question | Answer |
|---|---|
| Address | The internal name, such as `rcc.company.internal` or `rcc.company.com` |
| Certificate | `1`, `2`, or `3`, as chosen in step 7 |
| Email server | Your company relay, or `smtp.gmail.com` with an app password |

```bash
chmod 600 .env
docker compose up -d --build
docker compose ps
docker compose logs caddy | grep -iE 'certificate|error'
```

From a computer on the VPN, open `https://rcc.company.internal`, sign in with the printed email and password, and set your own password.

**Copy `.env` to your vault now.** Without `RCC_SECRET_KEY`, a restored backup can't read its saved credentials. See [Finding connection details](/docs/finding-connection-details#storing-secrets).

## Public setup

Use this only when people must reach the console from anywhere. The differences from the private setup:

1. **Create the VM with a public IP and a DNS label:**

   ```bash
   az vm create -g $RG -n rcc-vm --image Ubuntu2404 --size Standard_B2ms --os-disk-size-gb 64 \
     --public-ip-sku Standard --public-ip-address-dns-name rcc-COMPANY --nsg-rule NONE \
     --admin-username azureuser --ssh-key-values ~/.ssh/id_ed25519.pub --assign-identity
   ```

   The VM gets a name such as `rcc-COMPANY.westeurope.cloudapp.azure.com`, or point your own public DNS name at its IP.

2. **Open 443 as narrowly as you can, and 80 for Let's Encrypt:**

   ```bash
   az network nsg rule create -g $RG --nsg-name rcc-vmNSG -n https --priority 100 \
     --access Allow --protocol Tcp --destination-port-ranges 443 --source-address-prefixes Internet
   az network nsg rule create -g $RG --nsg-name rcc-vmNSG -n http-acme --priority 110 \
     --access Allow --protocol Tcp --destination-port-ranges 80 --source-address-prefixes Internet
   ```

   Replace `Internet` on the 443 rule with known ranges whenever you can. Use Azure Bastion for SSH rather than opening port 22.

3. **Choose certificate `4`** (Let's Encrypt over the internet) in `setup-env.sh`.

4. **Turn on [single sign-on](/docs/single-sign-on)** with your provider's two-factor sign-in, then tick **Require single sign-on for everyone**. On the internet, password guessing is constant.

5. **Consider a web application firewall**, such as Azure Application Gateway with WAF or Azure Front Door, in front of the VM. If you add one, it should pass `X-Forwarded-*` headers and must not buffer responses, because live updates stream.

## Reach your systems

From the VM, check that the console can reach each thing you'll connect. See [Test the path](/docs/finding-connection-details#test-the-path-before-you-connect).

- **Another VNet:** peer it with the console's VNet, and allow the VM's private IP in the target's NSG.
- **An AKS cluster with authorized IP ranges:** add the address the VM goes out from (the NAT gateway's IP on a private setup): `az aks update -g RG -n CLUSTER --api-server-authorized-ip-ranges EXISTING,NAT_IP/32`.
- **A private AKS cluster:** the VM's VNet must resolve and reach the private API server, usually by linking the cluster's private DNS zone to it.

Limit where the console may connect, in `.env`, then run `docker compose up -d`:

```bash
RCC_TARGET_ALLOWLIST=10.20.0.0/16,loki.company.internal,hooks.slack.com
```

## Backups

```bash
# Daily database dump at 02:15, kept 14 days on the VM
( crontab -l 2>/dev/null; echo '15 2 * * * cd /opt/root-cause-console && sh deploy/backup.sh >> backups/backup.log 2>&1' ) | crontab -
```

Copy backups off the VM to a storage account that only your VNet can reach, written by the VM's managed identity:

```bash
SA=rccbackupsCOMPANY
az network vnet subnet update -g $RG --vnet-name $VNET -n $SUBNET --service-endpoints Microsoft.Storage
az storage account create -g $RG -n $SA --sku Standard_ZRS --min-tls-version TLS1_2 \
  --allow-blob-public-access false --allow-shared-key-access false --default-action Deny
az storage account network-rule add -g $RG --account-name $SA --vnet-name $VNET --subnet $SUBNET
az role assignment create --role "Storage Blob Data Contributor" \
  --assignee $(az vm show -g $RG -n rcc-vm --query identity.principalId -o tsv) \
  --scope $(az storage account show -g $RG -n $SA --query id -o tsv)
# Create the container with an identity allowed on the account, then on the VM, after installing the Azure CLI:
az login --identity
az storage blob upload-batch --account-name $SA -d backups -s /opt/root-cause-console/backups --auth-mode login --overwrite false
```

Also turn on Azure Backup for the VM, for a whole-machine restore. See [Operations](/docs/operations) for restoring.

## Updates

```bash
cd /opt/root-cause-console
sh deploy/backup.sh
GIT_SSH_COMMAND='ssh -i ~/.ssh/rcc_deploy' git pull
docker compose build --pull console
docker compose up -d
```

With the Azure DNS certificate, rebuild Caddy too: `docker build --pull -t rcc-caddy-dns:2 deploy/caddy-dns`. The database is upgraded on start. Rebuild at least monthly, even without code changes, to pick up security fixes in the base images.
