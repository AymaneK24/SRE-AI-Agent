#!/bin/sh
# Asks a few questions and writes .env with new secrets. Run it once, from the project folder.
set -eu
if [ -e .env ]; then
  echo ".env already exists, so nothing was changed. Replacing it would make saved data unreadable."
  exit 1
fi
printf 'Company name: '
read -r company
printf 'Your name: '
read -r admin_name
printf 'Your email, which you will sign in with: '
read -r admin_email
printf 'Address people will use, without https:// (a host name or IP address): '
read -r domain
echo 'Which HTTPS certificate?'
echo '  1) Let'"'"'s Encrypt: a public host name, with ports 80 and 443 open to the internet'
echo '  2) Your company'"'"'s certificate: put cert.pem and key.pem in deploy/certs'
echo '  3) Made by this server: works with an IP address, but browsers warn on first visit'
printf 'Choose 1, 2, or 3 [3]: '
read -r choice
printf 'HTTPS port [443]: '
read -r https_port
printf 'Email server (leave empty for none, or enter smtp.gmail.com): '
read -r smtp_host
smtp_user=""; smtp_password=""; smtp_from=""
if [ -n "$smtp_host" ]; then
  printf 'Email address that sends mail: '
  read -r smtp_from
  printf 'Email username (usually the same address): '
  read -r smtp_user
  printf 'Email password or Gmail app password: '
  stty -echo 2>/dev/null || true
  read -r smtp_password
  stty echo 2>/dev/null || true
  echo
  if [ "$smtp_host" = "smtp.gmail.com" ]; then smtp_password="$(printf '%s' "$smtp_password" | tr -d ' ')"; fi
fi
if [ -z "$company" ] || [ -z "$admin_name" ]; then echo "Enter the company name and your name."; exit 1; fi
case "$admin_email" in *@*.*) ;; *) echo "Enter a real email address."; exit 1 ;; esac
case "$domain" in *://*|*/*|'') echo "Enter only the host name or IP address, such as rcc.company.internal."; exit 1 ;; esac
case "$choice" in
  1) caddyfile=./deploy/Caddyfile ;;
  2) caddyfile=./deploy/Caddyfile.internal ;;
  3|'') caddyfile=./deploy/Caddyfile.selfsigned ;;
  *) echo "Choose 1, 2, or 3."; exit 1 ;;
esac
https_port="${https_port:-443}"
if [ "$https_port" = "443" ]; then public_url="https://$domain"; else public_url="https://$domain:$https_port"; fi

admin_password="$(openssl rand -hex 16)"
umask 077
cat > .env <<ENV
RCC_DOMAIN=$domain
RCC_PUBLIC_URL=$public_url
RCC_ALLOWED_HOSTS=$domain
RCC_CADDYFILE=$caddyfile
RCC_HTTPS_PORT=$https_port
RCC_COMPANY_NAME=$company
RCC_SECRET_KEY=$(openssl rand -hex 32)
POSTGRES_PASSWORD=$(openssl rand -hex 32)
RCC_ADMIN_NAME=$admin_name
RCC_ADMIN_EMAIL=$admin_email
RCC_ADMIN_PASSWORD=$admin_password
RCC_OLLAMA_URL=http://host.docker.internal:11434
RCC_SMTP_HOST=$smtp_host
RCC_SMTP_PORT=587
RCC_SMTP_USERNAME=$smtp_user
RCC_SMTP_PASSWORD=$smtp_password
RCC_SMTP_FROM=$smtp_from
RCC_SMTP_TLS=starttls
RCC_OPERATOR_NAME=Aymane KENBOUCH
RCC_LEGAL_REVIEWED=false
ENV
echo
echo "Wrote .env. Save a copy somewhere safe, off this server."
echo "Address:           $public_url"
echo "Sign in as:        $admin_email"
echo "First password:    $admin_password   (you must change it at first sign-in)"
echo "Next, run:         docker compose up -d --build"
