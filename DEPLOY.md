# Deploy — content-ai + RAG + content-pilot

Единый публичный edge — **nginx**. Все внутренние сервисы (db, admin-api,
bot-api, rag, qdrant) живут в общей bridge-сети `content_ai_net`, ходят друг к
другу по именам и **не публикуют портов наружу**. Наружу торчит только nginx
(80/443).

```
INTERNET → nginx(80/443) → admin-api:8001 ─┬─ db:5432
                                           ├─ bot:8002 (internal API)
                                           └─ rag-api:8000 → qdrant:6333
```

## 0. Один раз на хосте

```bash
docker network create content_ai_net      # общая внешняя сеть для обоих compose
```

## 1. Конфиг (заполнить перед первым запуском)

**content-ai/.env**
```
DOMAIN=api.ВАШ-ДОМЕН                       # домен API-edge (A-запись → IP VPS)
CERTBOT_EMAIL=you@example.com              # для Let's Encrypt
FRONTEND_URL=https://app.ВАШ-ДОМЕН         # origin фронта → CORS admin-api
```
**Desktop/RAG/.env** — как раньше (ключи LLM/embeddings, QDRANT_URL уже задан в compose).

**content-pilot** (сборка фронта):
```
VITE_API_BASE_URL=https://api.ВАШ-ДОМЕН   # домен nginx, НЕ :8001
```

## 2. TLS (Let's Encrypt, автоматически)

Предусловие: `DOMAIN` (A-запись) указывает на IP этого VPS, в `.env` заданы
`DOMAIN` и `CERTBOT_EMAIL`. Затем один раз:

```bash
chmod +x init-letsencrypt.sh
./init-letsencrypt.sh
```

Скрипт ставит заглушку-сертификат, поднимает nginx, получает реальный сертификат
по webroot-проверке и перезагружает nginx. Сервис `certbot` затем продлевает его
автоматически (раз в 12ч), nginx перечитывает конфиг каждые 6ч. Конфиг nginx —
шаблон `nginx/nginx.conf.template` (домен подставляется из `DOMAIN`).

## 3. Запуск

Порядок важен только тем, что сеть `content_ai_net` должна существовать (шаг 0).

```bash
# RAG
cd ~/Desktop/RAG && docker compose up -d --build

# content-ai (db + bot + admin-api + nginx)
cd ~/content-ai && docker compose up -d --build

# Фронт (content-pilot) — собственный SSR-сервер / статик-хостинг
cd ~/Desktop/content-pilot && VITE_API_BASE_URL=https://api.ВАШ-ДОМЕН bun run build
```

## 4. Проверка после старта

```bash
docker network inspect content_ai_net      # в сети должны быть admin-api, bot, rag-api, db, nginx
curl -I http://localhost/                   # через nginx → admin-api (200/401/404, НЕ refused)
# Изнутри сети RAG доступен по имени, снаружи — нет:
docker exec content_ai_admin_api python -c "import urllib.request,os;print(urllib.request.urlopen(os.environ['RAG_URL']+'/health').status)" 2>/dev/null || echo "проверьте /health путь RAG"
curl -sS --max-time 3 http://localhost:8000/ ; echo "  ← должно быть connection refused (RAG не публичен)"
curl -sS --max-time 3 http://localhost:8001/ ; echo "  ← должно быть connection refused (admin-api не публичен)"
```

Внутренние порты (5432 / 8000 / 8001 / 8002) **не должны** отвечать с хоста —
только через nginx. Если отвечают — сервис где-то ещё публикует порт.
