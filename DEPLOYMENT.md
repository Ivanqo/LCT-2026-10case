# Production deployment for docaibuild.ru

Эта схема поднимает:

- `https://landing.docaibuild.ru/` -> лендинг;
- `https://app.docaibuild.ru/` -> основное приложение;
- `https://app.docaibuild.ru/api/*` -> backend API; для CASE10-внешнего контракта используйте Node gateway (`node_gateway`, порт `8080`) и его `/openapi.json`;
- `https://landing.docaibuild.ru/api/lead` -> форма заявки лендинга;
- `https://docaibuild.ru/` -> редирект на лендинг;
- Nginx reverse proxy + Certbot для SSL;
- автообновление сертификата через контейнер `certbot`;
- GitHub Actions CD: pull на VPS и `docker compose up --build -d`.

## 1. DNS

В панели домена создай A-записи:

```text
docaibuild.ru -> IP_ТВОЕГО_VPS
landing.docaibuild.ru -> IP_ТВОЕГО_VPS
app.docaibuild.ru -> IP_ТВОЕГО_VPS
```

Перед выпуском SSL проверь, что домены уже резолвятся на сервер:

```bash
dig +short docaibuild.ru
dig +short landing.docaibuild.ru
dig +short app.docaibuild.ru
```

## 2. Первый запуск на Ubuntu VPS

Подключись к серверу:

```bash
ssh administrator@IP_ТВОЕГО_VPS
```

Установи git, если его нет:

```bash
sudo apt-get update
sudo apt-get install -y git
```

Склонируй проект:

```bash
mkdir -p /home/administrator
cd /home/administrator
git clone <URL_ТВОЕГО_GIT_РЕПОЗИТОРИЯ> pd_rd
cd pd_rd
```

Запусти bootstrap. Он установит Docker, если Docker ещё не установлен, создаст `.env`, сгенерирует секреты, поднимет временный HTTP Nginx, выпустит Let's Encrypt сертификат и переключит проект на HTTPS:

```bash
LETSENCRYPT_EMAIL=admin@docaibuild.ru bash deploy/bootstrap-ubuntu.sh
```

После успешного запуска будут доступны:

```text
https://docaibuild.ru/
https://landing.docaibuild.ru/
https://app.docaibuild.ru/
```

## 3. Настройка `.env`

Bootstrap создаёт `.env` из `.env.example`. Перед реальной эксплуатацией проверь:

```bash
nano .env
```

Особенно важны:

```text
ROOT_DOMAIN=docaibuild.ru
LANDING_DOMAIN=landing.docaibuild.ru
APP_DOMAIN=app.docaibuild.ru
CERT_NAME=docaibuild.ru
LETSENCRYPT_EMAIL=admin@docaibuild.ru
DEFAULT_ADMIN_LOGIN=admin
DEFAULT_ADMIN_PASSWORD=...
DEFAULT_ADMIN_API_KEY=...
POSTGRES_PASSWORD=...
CORS_ORIGINS=https://app.docaibuild.ru
QWEN_PROXY_BASE_URL=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

Если `.env` менялся после запуска:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up --build -d --remove-orphans
```

## 4. Полезные команды на сервере

Статус:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
```

Логи всех сервисов:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f --tail=200
```

Логи конкретного сервиса:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f api
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f nginx
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f certbot
```

Ручное продление SSL:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm --entrypoint certbot certbot renew --webroot -w /var/www/certbot
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec nginx nginx -s reload
```

## 5. GitHub Actions CI/CD

В репозитории добавлены workflow:

- `.github/workflows/ci.yml` проверяет compose-конфиг и Python-синтаксис;
- `.github/workflows/deploy.yml` деплоит `master` на VPS по SSH.

В GitHub открой `Settings -> Secrets and variables -> Actions`.

Добавь secret:

```text
DEPLOY_SSH_KEY
```

Это приватный SSH-ключ, которым GitHub Actions сможет зайти на VPS.

Добавь variables:

```text
DEPLOY_HOST=IP_ТВОЕГО_VPS
DEPLOY_PORT=22
DEPLOY_USER=administrator
DEPLOY_PATH=/home/administrator/pd_rd
DEPLOY_BRANCH=master
```

На VPS добавь публичный ключ в `authorized_keys` пользователя `administrator`:

```bash
mkdir -p ~/.ssh
nano ~/.ssh/authorized_keys
chmod 700 ~/.ssh
chmod 600 ~/.ssh/authorized_keys
```

После этого каждый push в `master` выполнит:

```bash
git pull --ff-only origin master
docker compose -f docker-compose.yml -f docker-compose.prod.yml up --build -d --remove-orphans
```

## 6. Firewall

Открой только SSH, HTTP и HTTPS:

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status
```

Внутренние порты API/RAG/IFC/Postgres/Redis в compose привязаны к `127.0.0.1`, поэтому снаружи они не публикуются.
