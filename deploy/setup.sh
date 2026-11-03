#!/usr/bin/env bash
# One-time setup of the website on your VPS (Hostinger KVM 2, Ubuntu). From this folder run:
#     bash setup.sh                         first time: asks for the address and your login
#     bash setup.sh --domain aq.example.com later: move to your own address (DNS A record first)
#     bash setup.sh --add-user priya            give a team member their own login
#     bash setup.sh --remove-user priya         take a login away
# It installs Docker if needed, creates all passwords, and starts everything with HTTPS.
# Run it again any time to update/restart.
set -euo pipefail
cd "$(dirname "$0")"

start=yes
new_domain=""
add_user=""
remove_user=""
while [ $# -gt 0 ]; do
  case "$1" in
    --no-start) start=no ;;
    --domain) new_domain="${2:-}"; shift ;;
    --add-user) add_user="${2:-}"; shift ;;
    --remove-user) remove_user="${2:-}"; shift ;;
    *) echo "unknown option: $1"; exit 2 ;;
  esac
  shift
done

if ! command -v docker >/dev/null 2>&1; then
  echo "Installing Docker (one time, about a minute)..."
  curl -fsSL https://get.docker.com | sh
fi

if [ ! -f .env ]; then
  ip=$(hostname -I 2>/dev/null | awk '{print $1}')
  suggestion="${ip//./-}.sslip.io"
  echo
  echo "Web address for the website."
  echo "  - Your own subdomain (e.g. aq.yourcompany.com): first add a DNS 'A' record pointing it to ${ip:-the server IP}."
  echo "  - No domain? Press Enter to use ${suggestion}, a free address that already points here."
  read -r -p "Address [${suggestion}]: " domain
  domain=${domain:-$suggestion}
  domain=${domain#https://}; domain=${domain#http://}; domain=${domain%%/*}
  read -r -p "Your login name [admin]: " user
  user=${user:-admin}
  password=$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | cut -c1-16)
  umask 077
  cat > .env <<EOF
DOMAIN=${domain}
POSTGRES_PASSWORD=$(openssl rand -hex 24)
AGENT_TOKEN=$(openssl rand -hex 24)
DASHBOARD_USERS=${user}:${password}
ALERT_WEBHOOK_URL=
REVIEW_CONFIDENCE=0.6
EOF
  echo "Settings saved in $(pwd)/.env (keep it private)."
fi

if [ -n "$new_domain" ]; then
  new_domain=${new_domain#https://}; new_domain=${new_domain#http://}; new_domain=${new_domain%%/*}
  sed -i "s|^DOMAIN=.*|DOMAIN=${new_domain}|" .env
  echo "Address changed to ${new_domain}."
fi

current_users() { grep '^DASHBOARD_USERS=' .env | cut -d= -f2-; }
if [ -n "$add_user" ]; then
  if ! [[ "$add_user" =~ ^[A-Za-z0-9._-]{1,40}$ ]]; then echo "Login names may use letters, digits, . _ - only."; exit 2; fi
  if current_users | tr ',' '\n' | cut -d: -f1 | grep -qx "$add_user"; then echo "There is already a login called $add_user."; exit 2; fi
  new_pw=$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | cut -c1-16)
  sed -i "s|^DASHBOARD_USERS=.*|DASHBOARD_USERS=$(current_users),${add_user}:${new_pw}|" .env
  echo "Added login:  ${add_user}  /  ${new_pw}   (send it to them privately)"
fi
if [ -n "$remove_user" ]; then
  kept=$(current_users | tr ',' '\n' | grep -v "^${remove_user}:" | paste -sd, -)
  if [ -z "$kept" ]; then echo "Can't remove the last login."; exit 2; fi
  sed -i "s|^DASHBOARD_USERS=.*|DASHBOARD_USERS=${kept}|" .env
  echo "Removed login: ${remove_user}"
fi

if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow 80/tcp >/dev/null && ufw allow 443/tcp >/dev/null && ufw allow 443/udp >/dev/null
fi

if [ "$start" = yes ]; then
  echo "Starting the website (the first time this builds everything, a few minutes)..."
  docker compose up -d --build
fi

domain=$(grep '^DOMAIN=' .env | cut -d= -f2-)
users=$(current_users)
first=${users%%,*}
echo
echo "Done. Open:   https://${domain}"
echo "Your login:   ${first%%:*}  /  ${first#*:}"
echo "All logins:   $(echo "$users" | tr ',' '\n' | cut -d: -f1 | paste -sd, - | sed 's/,/, /g')"
echo "(The HTTPS certificate is fetched on the first visit; give it up to a minute.)"
echo "Capture program token (for agent config.yaml): $(grep '^AGENT_TOKEN=' .env | cut -d= -f2-)"
