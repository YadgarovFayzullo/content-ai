# Admin API Microservice

REST API для управления каналами и получения метрик через админ-панель фронтенда.

## Запуск

### Локально
```bash
cd admin-api
pip install -r requirements.txt
DATABASE_URL=postgresql://postgres:postgres_pass@localhost:5432/content_ai python -m uvicorn main:app --port 8001
```

### Docker
```bash
docker-compose up admin-api
```

## Эндпоинты

- `GET /api/admin/health` — Проверка здоровья
- `GET /api/admin/tenants` — Список каналов
- `GET /api/admin/tenants/{tenant_id}/profile` — Профиль канала
- `GET /api/admin/tenants/{tenant_id}/stats` — Метрики постов
- `GET /api/admin/tenants/{tenant_id}/posts` — История постов
- `GET /api/admin/tenants/{tenant_id}/sources` — Источники
- `GET /api/admin/tenants/{tenant_id}/rules` — Правила
- `GET /api/admin/tenants/{tenant_id}/rag-status` — Статус RAG
- `PATCH /api/admin/tenants/{tenant_id}/profile` — Обновить профиль
- `POST /api/admin/tenants/{tenant_id}/sources` — Добавить источник
- `DELETE /api/admin/tenants/{tenant_id}/sources/{source_id}` — Удалить источник

## Документация

- Swagger UI: http://localhost:8001/docs
- ReDoc: http://localhost:8001/redoc

## Аутентификация

Все запросы требуют токена:
```bash
Authorization: Bearer <ADMIN_TOKEN>
```

Токен берётся из переменной окружения `ADMIN_TOKEN` (по умолчанию: `a12345678`).

## Зависимости

- FastAPI 0.137.1
- SQLModel 0.0.38
- PostgreSQL 16+
