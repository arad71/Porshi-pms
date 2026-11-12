#!/usr/bin/env bash
# Porshi PMS - one-shot install on a fresh Ubuntu 24.04 VPS
# (Contabo or any other host).
#
# What it does:
#   1. updates the server, adds a firewall (SSH, HTTP, HTTPS only)
#   2. runs the repo's installer (Kamra's), building from arad71/Porshi-pms
#      (the app is bound to 127.0.0.1:8080 so it is NOT reachable from the
#      internet without HTTPS)
#   3. puts Caddy in front for HTTPS with an automatic Let's Encrypt certificate
#
# Run as root:   bash deploy-porshi.sh
# Unattended:    SITE_NAME=... ADMIN_EMAIL=... ADMIN_PASSWORD=... bash deploy-porshi.sh
#                (no questions; used by .github/workflows/deploy-contabo.yml)
# Run again:     updates the app (the admin password is only needed the first time)
# Pick a branch: PORSHI_BRANCH=feat/maintenance-module bash deploy-porshi.sh
#                (default: develop)

set -euo pipefail

PORSHI_REPO="${PORSHI_REPO:-https://github.com/arad71/Porshi-pms}"
PORSHI_BRANCH="${PORSHI_BRANCH:-develop}"
RAW="https://raw.githubusercontent.com/arad71/Porshi-pms/${PORSHI_BRANCH}/deploy/install.sh"

green() { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
die() { printf '\033[31mError: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo -i first)"
. /etc/os-release
[ "${ID:-}" = ubuntu ] || yellow "Not Ubuntu (${PRETTY_NAME:-unknown}). Tested on Ubuntu 24.04 - continuing."

INSTALLED=0
[ -x /opt/kamra/install.sh ] && INSTALLED=1
UNATTENDED=0
[ -n "${SITE_NAME:-}" ] && UNATTENDED=1

# --- questions (skipped when SITE_NAME etc. come from the environment) ---------
echo
green "Porshi PMS installer  (branch: ${PORSHI_BRANCH}, $([ "$INSTALLED" = 1 ] && echo update || echo first install))"
echo
if [ "$UNATTENDED" = 0 ]; then
  exec < /dev/tty
  read -r -p "Domain for the PMS (e.g. pms.yourhotel.com): " SITE_NAME
  if [ "$INSTALLED" = 0 ]; then
    read -r -p "Admin email: " ADMIN_EMAIL
    while true; do
      read -r -s -p "Admin password (min 10 characters): " ADMIN_PASSWORD; echo
      read -r -s -p "Confirm admin password: " ADMIN_PASSWORD_CONFIRM; echo
      [ "${#ADMIN_PASSWORD}" -ge 10 ] || { yellow "Too short."; continue; }
      [ "$ADMIN_PASSWORD" = "$ADMIN_PASSWORD_CONFIRM" ] && break
      yellow "Passwords don't match."
    done
  fi
fi
[[ "$SITE_NAME" == *.* ]] || die "the domain must look like pms.yourhotel.com"
if [ "$INSTALLED" = 0 ]; then
  [[ "${ADMIN_EMAIL:-}" == *@* ]] || die "ADMIN_EMAIL must be an email address"
  ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"
  [ "${#ADMIN_PASSWORD}" -ge 10 ] || die "ADMIN_PASSWORD must be at least 10 characters"
fi

# --- DNS check (HTTPS fails without it) ----------------------------------------
apt-get update -qq
apt-get install -y -qq curl dnsutils ufw ca-certificates >/dev/null
MY_IP="$(curl -fsS4 https://api.ipify.org || true)"
DNS_IP="$(dig +short A "$SITE_NAME" | tail -1 || true)"
if [ -n "$MY_IP" ] && [ "$DNS_IP" != "$MY_IP" ]; then
  yellow "DNS: ${SITE_NAME} points to '${DNS_IP:-nothing}', this server is ${MY_IP}."
  yellow "Add an A record ${SITE_NAME} -> ${MY_IP} at your DNS provider."
  if [ "$UNATTENDED" = 0 ]; then
    read -r -p "Continue anyway? HTTPS will start working once DNS is right. [y/N] " ok
    [[ "$ok" =~ ^[Yy]$ ]] || exit 1
  else
    yellow "Continuing - HTTPS starts working once DNS is right."
  fi
fi

# --- server basics ------------------------------------------------------------
green "Updating the server..."
DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get upgrade -y -qq >/dev/null
timedatectl set-ntp true 2>/dev/null || true

green "Firewall: allowing SSH, HTTP and HTTPS only..."
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

# --- Porshi -------------------------------------------------------------
export KAMRA_GIT_URL="$PORSHI_REPO" KAMRA_BRANCH="$PORSHI_BRANCH"
# only reachable from this machine; Caddy serves it over HTTPS
export HTTP_PUBLISH_PORT="127.0.0.1:8080"
if [ "$INSTALLED" = 1 ]; then
  green "Updating (backup, rebuild from ${PORSHI_BRANCH}, restart, migrate). 20-45 minutes."
  bash /opt/kamra/install.sh update
else
  green "Installing. The first build takes 20-45 minutes - leave this running."
  curl -fsSL "$RAW" -o /root/porshi-install.sh
  export SITE_NAME ADMIN_EMAIL ADMIN_PASSWORD
  bash /root/porshi-install.sh install
fi
unset ADMIN_PASSWORD ADMIN_PASSWORD_CONFIRM

# --- HTTPS ----------------------------------------------------------------------
green "Setting up HTTPS (Caddy + Let's Encrypt)..."
apt-get install -y -qq caddy >/dev/null
cat > /etc/caddy/Caddyfile <<EOF
${SITE_NAME} {
	encode gzip
	reverse_proxy 127.0.0.1:8080
}
EOF
systemctl enable caddy >/dev/null 2>&1
systemctl restart caddy

# --- check ----------------------------------------------------------------------
sleep 5
if curl -fsS -o /dev/null --max-time 20 "http://127.0.0.1:8080/api/method/ping" \
     -H "Host: ${SITE_NAME}"; then
  green "The app answers locally."
else
  yellow "The app didn't answer yet - give it a minute, then: docker compose -p kamra ps"
fi
if ss -ltn | grep -qE '0\.0\.0\.0:8080|\[::\]:8080'; then
  yellow "Warning: port 8080 is open to the internet. Tell Claude - it should be 127.0.0.1 only."
fi

echo
green "Done."
green "  Open:      https://${SITE_NAME}/kamra"
green "  Sign in:   Administrator, with the password you just set"
green "  Then:      finish /kamra/setup to create your property"
green "  Update:    sudo /opt/kamra/install.sh update"
green "  Backups:   docker compose -p kamra exec backend ls sites/*/private/backups"
echo
yellow "If the HTTPS page doesn't load, DNS is usually not pointing here yet."
yellow "Caddy retries the certificate on its own; check with: journalctl -u caddy -n 50"
