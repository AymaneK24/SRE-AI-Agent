# Deploy on localhost

Run the full stack on your own computer, to try the console or develop against it. This page covers Linux, macOS, and Windows with Docker Desktop and WSL 2.

> **Warning:** A local install is for trials. It uses a certificate your browser doesn't trust, and your computer sleeps. For a team, use [an Azure VM](/docs/deploy-azure-vm) or [AKS](/docs/deploy-aks).

## 1. Prerequisites

- Docker Desktop (Windows, macOS) or Docker Engine with the Compose plugin (Linux).
- On Windows: WSL 2, and the project files inside WSL or on a Windows drive, such as `/mnt/c/Users/YOU/Desktop/root-cause-console`.
- Optional: Docker Desktop's Kubernetes (Settings > Kubernetes > Enable), and Ollama.

```bash
docker version
docker compose version
```

## 2. Settings and start

```bash
cd root-cause-console
sh deploy/setup-env.sh          # address: localhost, certificate: 2, port: 443 or 8443
docker login dhi.io             # once: free Docker account, for the hardened base image
docker compose up -d --build
docker compose ps
docker compose logs -f console  # Ctrl+C to stop following
```

If port 443 or 80 is taken (by IIS, Skype, or another proxy), set other ports in `.env`, then run `docker compose up -d` again:

```bash
RCC_HTTPS_PORT=8443
RCC_HTTP_PORT=8080
RCC_PUBLIC_URL=https://localhost:8443
```

Open `https://localhost` (or `https://localhost:8443`), accept the certificate warning, and sign in with the email and password the script printed.

### Trust the certificate (optional)

Caddy made its own certificate authority. To stop the warning on **your own computer only**, export it and add it to your trusted roots:

```bash
docker compose exec caddy cat /data/caddy/pki/authorities/local/root.crt > caddy-local-root.crt
```

- **Windows:** double-click the file, **Install Certificate**, **Local Machine**, **Trusted Root Certification Authorities**.
- **macOS:** `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain caddy-local-root.crt`
- **Ubuntu:** `sudo cp caddy-local-root.crt /usr/local/share/ca-certificates/ && sudo update-ca-certificates`

> **Security:** A trusted root can sign a certificate for any website. Do this only for a trial on your own computer, remove it afterwards, and never on a shared machine.

## Let others on your network reach it

To let a colleague on the same Wi-Fi or office network try your console, by your computer's IP address:

**1. Find your IP.** In Windows, `ipconfig`, under your Wi-Fi or Ethernet adapter, such as `10.50.55.177`. Not the `172.x` address of WSL, which other computers can't reach.

**2. Tell the console about it** in `.env`, keeping `localhost` for yourself:

```bash
RCC_DOMAIN=10.50.55.177, localhost
RCC_DEFAULT_SNI=10.50.55.177
RCC_PUBLIC_URL=https://10.50.55.177
RCC_ALLOWED_HOSTS=10.50.55.177,localhost
```

`RCC_DEFAULT_SNI` matters: browsers and Windows send no server name when connecting to an IP address, and without it Caddy can't pick a certificate, so the connection fails with a TLS error. Then run `docker compose up -d`.

**3. Allow it through Windows Firewall**, in PowerShell **as administrator** (Win + X, Terminal (Admin)):

```powershell
Get-NetConnectionProfile                       # NetworkCategory should be Private on a network you trust
Set-NetConnectionProfile -InterfaceAlias "Wi-Fi" -NetworkCategory Private

New-NetFirewallRule -DisplayName "Root Cause Console (HTTPS)" -Direction Inbound -Protocol TCP `
  -LocalPort 443 -Action Allow -Profile Private -RemoteAddress LocalSubnet
```

If your laptop's policy doesn't let you change the network to Private, use `-Profile Public,Private` instead. `LocalSubnet` still limits it to devices on your network. A **Block** rule always wins over an Allow rule, so check none blocks Docker:

```powershell
Get-NetFirewallRule | Where-Object { $_.DisplayName -match 'docker|vpnkit' } | Select-Object DisplayName, Action, Profile, Enabled
```

**4. Check, from Windows** (not from WSL, whose own network may not reach your Wi-Fi address):

```powershell
curl.exe -vk https://10.50.55.177/healthz      # ends with {"status":"ok"}
```

Then from your colleague's computer: `Test-NetConnection 10.50.55.177 -Port 443` should say `True`.

| If it still fails | Fix |
|---|---|
| `TcpTestSucceeded : False` from the other computer | A VPN client such as NordVPN blocks LAN access while connected, or the router isolates Wi-Fi devices ("AP isolation" or "client isolation"). Disconnect the VPN, or turn isolation off |
| A TLS error | `RCC_DEFAULT_SNI` isn't set to the IP, or `docker compose up -d` wasn't run after changing `.env` |
| *Open the console at the address in RCC_PUBLIC_URL* | Add the IP to `RCC_ALLOWED_HOSTS` |

Your colleague's browser warns about the certificate, because Caddy made it. Traffic is still encrypted. Invite them in **Organization > Members** first. When you're done, remove the rule: `Remove-NetFirewallRule -DisplayName "Root Cause Console (HTTPS)"`.

> **Security:** Only do this on a network you trust, such as your home or office, never on public Wi-Fi. Your IP can change when you reconnect, so update `.env` if it does.

## 3. Connect Docker Desktop's Kubernetes

Follow [Kubernetes on Docker Desktop](/docs/kubernetes#docker-desktop): address `https://host.docker.internal:6443`, the CA from `kubectl config view`, and **TLS server name** `kubernetes.docker.internal`.

## 4. Connect Prometheus and Loki in that cluster

Services inside Docker Desktop's cluster aren't reachable from the console's container until you publish them. The simplest way is a LoadBalancer Service, which Docker Desktop publishes on your computer:

```bash
kubectl -n monitoring patch svc PROMETHEUS_SERVICE -p '{"spec":{"type":"LoadBalancer"}}'
kubectl -n monitoring patch svc LOKI_SERVICE -p '{"spec":{"type":"LoadBalancer"}}'
kubectl -n monitoring get svc        # EXTERNAL-IP shows localhost
```

Then connect them as `http://host.docker.internal:9090` and `http://host.docker.internal:3100`.

## 5. Connect Ollama

| Where Ollama runs | Address in the console |
|---|---|
| On Windows or macOS directly | `http://host.docker.internal:11434` |
| Inside WSL | `http://WSL_IP:11434`, with `WSL_IP` from `hostname -I` in WSL. Ollama must listen on `0.0.0.0` |
| In a container on the same Compose network | `http://ollama:11434` |

## Reach the console from the cluster

Alertmanager and other tools in the cluster send webhooks to the console. Locally, the console's HTTPS address is `localhost`, which pods can't use. Publish the console's own port on your computer's loopback address only, with `docker-compose.override.yml` next to `docker-compose.yml`:

```yaml
services:
  console:
    ports:
      - "127.0.0.1:8000:8000"
```

The console only answers host names it knows, so add the name pods will use to `RCC_ALLOWED_HOSTS` in `.env`:

```bash
RCC_ALLOWED_HOSTS=localhost,host.docker.internal
```

```bash
docker compose up -d
curl -fsS http://localhost:8000/healthz
```

From pods, the console is now `http://host.docker.internal:8000`. Use that in place of the address's start, for example `http://host.docker.internal:8000/api/hooks/alertmanager/TOKEN`.

> **Security:** This port is plain HTTP and skips Caddy. Binding it to `127.0.0.1` keeps it off your network. Never use this override on a server.

## 6. Something to investigate

```bash
kubectl apply -f deploy/examples/shop-simulation.yaml
```

See [AI accuracy benchmark](/docs/accuracy-benchmark#the-test-scenarios) for what each failure is.

## Update, stop, and reset

```bash
git pull
docker compose up -d --build        # rebuilds the image from your code, keeps data
docker compose down                 # stops, keeps data
docker compose down -v              # stops and DELETES the database, certificates, and all saved settings
```

After an update, reload the browser with Ctrl+Shift+R so it loads the new pages. The database upgrades itself on start.

## Run without Docker, for development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
RCC_ALLOW_LOOPBACK_TARGETS=true python -m app
```

It serves plain HTTP on `http://localhost:8000` with SQLite in `./data`. Sign in as `admin@rootcause.local` with password `admin`, then set your own email and password. This default account exists only in development mode. Production mode refuses to start without your own.
