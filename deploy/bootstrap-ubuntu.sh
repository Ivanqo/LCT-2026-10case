#!/usr/bin/env bash
set -euo pipefail

ROOT_DOMAIN="${ROOT_DOMAIN:-${DOMAIN:-docaibuild.ru}}"
LANDING_DOMAIN="${LANDING_DOMAIN:-landing.${ROOT_DOMAIN}}"
APP_DOMAIN="${APP_DOMAIN:-app.${ROOT_DOMAIN}}"
CERT_NAME="${CERT_NAME:-${ROOT_DOMAIN}}"
PROJECT_DIR="${PROJECT_DIR:-$(pwd)}"
LETSENCRYPT_EMAIL="${LETSENCRYPT_EMAIL:-}"
STAGING="${STAGING:-0}"

if [ -z "$LETSENCRYPT_EMAIL" ]; then
  echo "Set LETSENCRYPT_EMAIL before running, for example:"
  echo "  LETSENCRYPT_EMAIL=admin@docaibuild.ru bash deploy/bootstrap-ubuntu.sh"
  exit 1
fi

cd "$PROJECT_DIR"

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  SUDO="sudo"
fi

if ! command -v docker >/dev/null 2>&1; then
  if ! command -v curl >/dev/null 2>&1; then
    $SUDO apt-get update
    $SUDO apt-get install -y curl ca-certificates
  fi
  curl -fsSL https://get.docker.com | $SUDO sh
  $SUDO systemctl enable --now docker
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose plugin is not available. Reinstall Docker using https://get.docker.com."
  exit 1
fi

DOCKER=(docker)
if ! docker ps >/dev/null 2>&1; then
  if [ -n "$SUDO" ] && $SUDO docker ps >/dev/null 2>&1; then
    echo "Current user cannot access /var/run/docker.sock yet; using sudo docker for this bootstrap run."
    echo "For future deploys, add the user to the docker group and re-login:"
    echo "  sudo usermod -aG docker $(whoami)"
    DOCKER=($SUDO docker)
  else
    echo "Cannot access Docker daemon. Try re-login after adding this user to the docker group:"
    echo "  sudo usermod -aG docker $(whoami)"
    exit 1
  fi
fi

if [ ! -f .env ]; then
  cp .env.example .env
fi

set_env() {
  local key="$1"
  local value="$2"
  if grep -q "^${key}=" .env; then
    sed -i "s|^${key}=.*|${key}=${value}|" .env
  else
    printf '%s=%s\n' "$key" "$value" >> .env
  fi
}

secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24
  else
    date +%s%N | sha256sum | awk '{print $1}'
  fi
}

ensure_secret() {
  local key="$1"
  local current
  current="$(grep "^${key}=" .env 2>/dev/null | tail -n1 | cut -d= -f2- || true)"
  if [ -z "$current" ] || printf '%s' "$current" | grep -q "CHANGE_ME"; then
    set_env "$key" "$(secret)"
  fi
}

set_env ROOT_DOMAIN "$ROOT_DOMAIN"
set_env LANDING_DOMAIN "$LANDING_DOMAIN"
set_env APP_DOMAIN "$APP_DOMAIN"
set_env CERT_NAME "$CERT_NAME"
set_env LETSENCRYPT_EMAIL "$LETSENCRYPT_EMAIL"
set_env CORS_ORIGINS "https://${APP_DOMAIN}"
ensure_secret DEFAULT_ADMIN_PASSWORD
ensure_secret DEFAULT_ADMIN_API_KEY
ensure_secret POSTGRES_PASSWORD

COMPOSE=("${DOCKER[@]}" compose -f docker-compose.yml -f docker-compose.prod.yml)

"${COMPOSE[@]}" config --quiet
"${COMPOSE[@]}" up --build -d postgres redis rag ifc ifc_worker api frontend landing nginx_bootstrap

"${COMPOSE[@]}" run --rm --entrypoint /bin/sh certbot \
  -c "mkdir -p /var/www/certbot/.well-known/acme-challenge && printf ok > /var/www/certbot/.well-known/acme-challenge/bootstrap-healthcheck"

if ! curl -fsS "http://127.0.0.1/.well-known/acme-challenge/bootstrap-healthcheck" >/dev/null; then
  echo "Nginx bootstrap is not serving ACME challenge files on local port 80."
  echo "Check:"
  echo "  ${COMPOSE[*]} ps nginx_bootstrap"
  echo "  ${COMPOSE[*]} logs --tail=100 nginx_bootstrap"
  exit 1
fi

echo "Local ACME challenge check passed. Make sure port 80 is open from the internet before Certbot runs."

CERTBOT_FLAGS=()
if [ "$STAGING" = "1" ]; then
  CERTBOT_FLAGS+=(--staging)
fi

CERTBOT_DOMAINS=(-d "$LANDING_DOMAIN" -d "$APP_DOMAIN")
if [ "${INCLUDE_ROOT_DOMAIN:-1}" = "1" ]; then
  CERTBOT_DOMAINS+=(-d "$ROOT_DOMAIN")
fi

"${COMPOSE[@]}" run --rm --entrypoint certbot certbot \
  certonly \
  --webroot \
  -w /var/www/certbot \
  --cert-name "$CERT_NAME" \
  "${CERTBOT_DOMAINS[@]}" \
  --email "$LETSENCRYPT_EMAIL" \
  --agree-tos \
  --no-eff-email \
  "${CERTBOT_FLAGS[@]}"

"${COMPOSE[@]}" stop nginx_bootstrap
"${COMPOSE[@]}" rm -f nginx_bootstrap
"${COMPOSE[@]}" up --build -d --remove-orphans
"${COMPOSE[@]}" ps

echo
echo "Deployment is ready:"
echo "  https://${LANDING_DOMAIN}/"
echo "  https://${APP_DOMAIN}/"
