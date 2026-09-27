#!/usr/bin/env bash
# CASE10 stand on a fresh Ubuntu host: Docker, .env with generated secrets, build, start, optional TLS.
#
#   bash deploy/case10/bootstrap.sh                                   # HTTP on port 80 (and 3100/8080/8000 on localhost)
#   APP_DOMAIN=case10.example.ru LETSENCRYPT_EMAIL=ops@example.ru bash deploy/case10/bootstrap.sh   # + HTTPS
#   GPU=1 bash deploy/case10/bootstrap.sh                             # NVIDIA Container Toolkit must be installed
set -euo pipefail
cd "$(dirname "$0")/../.."

SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
if ! command -v docker >/dev/null 2>&1; then
  $SUDO apt-get update && $SUDO apt-get install -y curl ca-certificates
  curl -fsSL https://get.docker.com | $SUDO sh
  $SUDO systemctl enable --now docker
fi
DOCKER=(docker); docker ps >/dev/null 2>&1 || DOCKER=($SUDO docker)

[ -f .env ] || cp deploy/case10/case10.env.example .env
set_env() { if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else printf '%s=%s\n' "$1" "$2" >> .env; fi; }
secret() { openssl rand -hex 16 2>/dev/null || date +%s%N | sha256sum | cut -c1-32; }
grep -q '^CASE10_ADMIN_PASSWORD=Case10-Demo-2026$' .env && [ "${KEEP_DEMO_PASSWORD:-0}" != "1" ] && set_env CASE10_ADMIN_PASSWORD "$(secret)"
grep -q '^CASE10_ADMIN_API_KEY=$' .env && set_env CASE10_ADMIN_API_KEY "$(secret)"
[ -n "${APP_DOMAIN:-}" ] && set_env APP_DOMAIN "$APP_DOMAIN" && set_env REACT_FRONTEND_API_BASE "${SCHEME:-https}://$APP_DOMAIN"
mkdir -p packages

FILES=(-f docker-compose.case10.yml -f deploy/case10/docker-compose.stand.yml)
[ "${GPU:-0}" = "1" ] && FILES+=(-f docker-compose.case10.gpu.yml)
COMPOSE=("${DOCKER[@]}" compose "${FILES[@]}")

"${COMPOSE[@]}" config --quiet
"${COMPOSE[@]}" up -d --build

if [ -n "${APP_DOMAIN:-}" ] && [ -n "${LETSENCRYPT_EMAIL:-}" ]; then
  "${COMPOSE[@]}" --profile tls run --rm --entrypoint certbot certbot certonly --webroot -w /var/www/certbot \
    -d "$APP_DOMAIN" --email "$LETSENCRYPT_EMAIL" --agree-tos --no-eff-email
  CASE10_NGINX_TEMPLATE=nginx.https.conf.template "${COMPOSE[@]}" --profile tls up -d
fi

"${COMPOSE[@]}" ps
echo
echo "Stand is up:  UI http://${APP_DOMAIN:-<host>}/   API contract /openapi.json"
echo "Demo login:   $(grep '^CASE10_ADMIN_LOGIN=' .env | cut -d= -f2) / $(grep '^CASE10_ADMIN_PASSWORD=' .env | cut -d= -f2)"
