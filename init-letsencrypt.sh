#!/bin/sh
# Первичная выдача Let's Encrypt сертификата для nginx-edge. Запускать ОДИН раз
# на сервере после того, как DOMAIN указывает на этот VPS (A-запись) и заданы
# DOMAIN/CERTBOT_EMAIL в .env.
#
# Решает «курицу и яйцо»: nginx с 443-блоком не стартует без сертификата, а
# certbot (webroot) не выдаст сертификат без работающего nginx на :80. Поэтому:
#   1) кладём самоподписанный заглушку-сертификат,
#   2) поднимаем nginx,
#   3) заменяем заглушку реальным сертификатом через webroot-проверку,
#   4) перезагружаем nginx.
set -e

# Подхватываем DOMAIN/CERTBOT_EMAIL из .env
if [ -f .env ]; then
  export $(grep -E '^(DOMAIN|CERTBOT_EMAIL)=' .env | xargs)
fi
: "${DOMAIN:?Задайте DOMAIN в .env (напр. publix-api.duckdns.org)}"
: "${CERTBOT_EMAIL:?Задайте CERTBOT_EMAIL в .env}"

LE=./nginx/letsencrypt
mkdir -p "$LE/live/$DOMAIN" ./nginx/www

# 1. Заглушка-сертификат (чтобы nginx поднялся с 443-блоком)
if [ ! -s "$LE/live/$DOMAIN/fullchain.pem" ]; then
  echo "### Создаю самоподписанный заглушку-сертификат для $DOMAIN ..."
  openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
    -keyout "$LE/live/$DOMAIN/privkey.pem" \
    -out    "$LE/live/$DOMAIN/fullchain.pem" \
    -subj "/CN=$DOMAIN"
fi

echo "### Поднимаю nginx ..."
docker compose up -d --force-recreate nginx

echo "### Удаляю заглушку и запрашиваю реальный сертификат ..."
rm -rf "$LE/live/$DOMAIN" "$LE/archive/$DOMAIN" "$LE/renewal/$DOMAIN.conf"
docker compose run --rm --entrypoint certbot certbot certonly \
  --webroot -w /var/www/certbot \
  --email "$CERTBOT_EMAIL" -d "$DOMAIN" \
  --rsa-key-size 4096 --agree-tos --no-eff-email --force-renewal

echo "### Перезагружаю nginx с реальным сертификатом ..."
docker compose exec nginx nginx -s reload

echo "### Поднимаю сервис авто-продления (certbot) ..."
docker compose up -d certbot

echo "### Готово. Проверьте: https://$DOMAIN/"
