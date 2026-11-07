# Deploy on an Azure VM

One Linux VM runs the console, PostgreSQL, and Caddy with Docker Compose. This is the simplest setup for a team, and it's easy to back up and update.

## What you need

| Item | Recommended |
|---|---|
| VM size | `Standard_B2ms` (2 vCPU, 8 GB) or larger. 2 vCPU and 4 GB is the minimum |
| Image | Ubuntu Server 24.04 LTS |
| Disk | 64 GB Premium SSD, encrypted (the default for managed disks) |
| Network | A VNet that can reach your Prometheus, Loki, and Kubernetes API |
| Address | A DNS name. For a public certificate, a public IP with ports 80 and 443 open, or a company certificate for an internal name |

## 1. Create the VM

```bash
RG=rg-rcc
LOC=westeurope
az group create -n $RG -l $LOC

az vm create -g $RG -n rcc-vm \
  --image Ubuntu2404 --size Standard_B2ms \
  --admin-username azureuser --ssh-key-values ~/.ssh/id_ed25519.pub \
  --public-ip-sku Standard --public-ip-address-dns-name rcc-COMPANY \
  --os-disk-size-gb 64 --nsg-rule NONE

az vm list-ip-addresses -g $RG -n rcc-vm -o table
```

`--public-ip-address-dns-name` gives the VM a name such as `rcc-COMPANY.westeurope.cloudapp.azure.com`, which Let's Encrypt can certify. For an internal-only console, leave out the public IP (`--public-ip-address ""`), and use a private DNS name with a company certificate.

## 2. Open only what's needed

```bash
NSG=rcc-vmNSG
# HTTPS from your company's addresses only
az network nsg rule create -g $RG --nsg-name $NSG -n https --priority 100 \
  --access Allow --protocol Tcp --destination-port-ranges 443 --source-address-prefixes OFFICE_CIDR VPN_CIDR
# Port 80 only if you use Let's Encrypt, which checks the domain over it
az network nsg rule create -g $RG --nsg-name $NSG -n http-acme --priority 110 \
  --access Allow --protocol Tcp --destination-port-ranges 80 --source-address-prefixes Internet
# SSH from your own address only, or use Azure Bastion instead
az network nsg rule create -g $RG --nsg-name $NSG -n ssh --priority 120 \
  --access Allow --protocol Tcp --destination-port-ranges 22 --source-address-prefixes YOUR_IP/32
```

> **Security:** Don't open 443 to the whole internet unless people sign in from anywhere. If they do, turn on [single sign-on](/docs/single-sign-on) with your provider's two-factor sign-in. Never open 5432 (PostgreSQL) or 8000 (the console's internal port): neither is published by the Compose file, so keep it that way.

## 3. Prepare the VM

```bash
ssh azureuser@rcc-COMPANY.westeurope.cloudapp.azure.com
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

> **Security:** Membership of the `docker` group is equivalent to root on the VM. Give SSH access to the VM only to the people who operate it, and prefer Azure Bastion over an open SSH port.

## 4. Settings and start

```bash
cd /opt/root-cause-console
sh deploy/setup-env.sh
```

| Question | Answer |
|---|---|
| Address | `rcc-COMPANY.westeurope.cloudapp.azure.com`, or your own DNS name |
| Certificate | `1` (Let's Encrypt) for a public name, `2` for a company certificate. See [Certificates](/docs/certificates) |
| Email server | Your company relay, or `smtp.gmail.com` with an app password |

```bash
chmod 600 .env
docker compose up -d --build
docker compose ps
docker compose logs caddy | grep -i certificate      # Let's Encrypt: "certificate obtained successfully"
```

Open `https://YOUR-ADDRESS`, sign in with the printed email and password, and set your own password.

**Copy `.env` to your password manager or Key Vault now.** Without `RCC_SECRET_KEY`, a restored backup can't read its saved credentials.

## 5. Reach your systems

From the VM, check the console can reach each thing you'll connect. See [Test the path](/docs/finding-connection-details#test-the-path-before-you-connect).

- **Another VNet:** peer it with the VM's VNet, and allow the VM's private IP in the target's NSG.
- **An AKS cluster with authorized IP ranges:** add the VM's public IP: `az aks update -g RG -n CLUSTER --api-server-authorized-ip-ranges EXISTING,VM_IP/32`.
- **A private AKS cluster:** the VM must be in a VNet that can resolve and reach the private API server.

Limit where the console may connect, in `.env`, then run `docker compose up -d`:

```bash
RCC_TARGET_ALLOWLIST=10.20.0.0/16,loki.company.internal,hooks.slack.com
```

## 6. Backups

```bash
# Daily database dump at 02:15, kept 14 days on the VM
( crontab -l 2>/dev/null; echo '15 2 * * * cd /opt/root-cause-console && sh deploy/backup.sh >> backups/backup.log 2>&1' ) | crontab -
```

Copy backups off the VM, for example to a storage account the VM writes to with its managed identity:

```bash
az vm identity assign -g $RG -n rcc-vm
az storage account create -g $RG -n rccbackupsCOMPANY --sku Standard_ZRS --min-tls-version TLS1_2 --allow-blob-public-access false
az storage container create --account-name rccbackupsCOMPANY -n backups --auth-mode login
az role assignment create --assignee $(az vm show -g $RG -n rcc-vm --query identity.principalId -o tsv) \
  --role "Storage Blob Data Contributor" \
  --scope $(az storage account show -g $RG -n rccbackupsCOMPANY --query id -o tsv)
# On the VM, after installing the Azure CLI:
az login --identity
az storage blob upload-batch --account-name rccbackupsCOMPANY -d backups -s /opt/root-cause-console/backups --auth-mode login --overwrite false
```

Also turn on Azure Backup for the VM, for a whole-machine restore. See [Operations](/docs/operations) for restoring.

## 7. Updates

```bash
cd /opt/root-cause-console
sh deploy/backup.sh
GIT_SSH_COMMAND='ssh -i ~/.ssh/rcc_deploy' git pull
docker compose build --pull console
docker compose up -d
```

The database is upgraded on start. Rebuild at least monthly, even without code changes, to pick up security fixes in the base image.
