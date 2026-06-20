# Content AI — API документация

## Админка клиентов (Frontend Admin Dashboard)

### Аутентификация
Все эндпоинты требуют админ-токена в заголовке:
```
Authorization: Bearer <ADMIN_TOKEN>
```

---

## API Эндпоинты

### 1. Список каналов клиента

**GET** `/api/admin/tenants`

Получить все каналы (или каналы конкретного клиента).

**Query параметры:**
- `client_id` (optional) — фильтр по клиенту (если multi-tenant)

**Ответ:**
```json
{
  "tenants": [
    {
      "tenant_id": "uuid",
      "chat_id": "@channel_name",
      "channel_name": "Channel Display Name",
      "active": true,
      "tone": "enthusiastic and encouraging",
      "language": "uz",
      "writing_style": "...",
      "audience": "...",
      "topics": "topic1, topic2, ...",
      "creativity_level": 0.5,
      "factual_strictness": 0.7,
      "use_rag": false,
      "use_references": true,
      "avg_post_length": 500,
      "created_at": "2026-06-01T10:00:00Z"
    }
  ]
}
```

---

### 2. Профиль канала (детально)

**GET** `/api/admin/tenants/{tenant_id}/profile`

Получить полный профиль конкретного канала.

**Ответ:**
```json
{
  "profile": {
    "tenant_id": "uuid",
    "chat_id": "@channel_name",
    "channel_name": "Channel Name",
    "tone": "...",
    "language": "uz",
    "writing_style": "...",
    "audience": "...",
    "topics": ["topic1", "topic2"],
    "creativity_level": 0.5,
    "factual_strictness": 0.7,
    "use_rag": false,
    "use_references": true,
    "cta": null,
    "post_template": null,
    "image_style": null,
    "avg_post_length": 500,
    "active": true,
    "schedule_mode": "frequency",  // "off", "frequency", "times"
    "posts_per_day": 2,
    "schedule_times": ["09:00", "17:00"],
    "created_at": "2026-06-01T10:00:00Z"
  }
}
```

---

### 3. Статистика по постам

**GET** `/api/admin/tenants/{tenant_id}/stats`

Получить метрики постов: просмотры, репосты, реакции.

**Query параметры:**
- `days` (optional, default=30) — последние N дней
- `limit` (optional, default=20) — максимум записей

**Ответ:**
```json
{
  "summary": {
    "total_published": 45,
    "total_views": 12500,
    "total_forwards": 450,
    "total_reactions": 890,
    "avg_views_per_post": 278,
    "avg_forwards_per_post": 10,
    "avg_reactions_per_post": 20
  },
  "recent_posts": [
    {
      "post_id": 123,
      "message_id": 456,
      "topic": "investment and funding",
      "views": 450,
      "forwards": 25,
      "reactions": 45,
      "posted_at": "2026-06-17T10:00:00Z",
      "captured_at": "2026-06-18T10:00:00Z"
    }
  ],
  "by_topic": [
    {
      "topic": "investment and funding",
      "post_count": 12,
      "avg_views": 350,
      "avg_forwards": 15,
      "avg_reactions": 25
    }
  ]
}
```

---

### 4. История постов (опубликованные)

**GET** `/api/admin/tenants/{tenant_id}/posts`

Получить историю опубликованных постов.

**Query параметры:**
- `limit` (optional, default=20)
- `offset` (optional, default=0) — для пагинации
- `topic` (optional) — фильтр по теме
- `from_date` (optional, ISO8601)
- `to_date` (optional, ISO8601)

**Ответ:**
```json
{
  "total": 150,
  "posts": [
    {
      "id": 1,
      "tenant_id": "uuid",
      "topic": "investment and funding",
      "content": "Post text...",
      "image_path": "/path/to/image.png",
      "posted": true,
      "message_id": 456,
      "created_at": "2026-06-17T10:00:00Z",
      "metrics": {
        "views": 450,
        "forwards": 25,
        "reactions": 45,
        "captured_at": "2026-06-18T10:00:00Z"
      }
    }
  ]
}
```

---

### 5. Источники (Reference Channels)

**GET** `/api/admin/tenants/{tenant_id}/sources`

Получить список всех источников (референс-каналов).

**Ответ:**
```json
{
  "sources": [
    {
      "id": 1,
      "source_chat_id": "@itparkventures",
      "posts_indexed": 82,
      "created_at": "2026-06-01T10:00:00Z",
      "last_indexed_at": "2026-06-18T10:00:00Z"
    }
  ]
}
```

---

### 6. Правила (Rules)

**GET** `/api/admin/tenants/{tenant_id}/rules`

Получить все правила канала.

**Ответ:**
```json
{
  "rules": [
    {
      "id": 1,
      "rule_type": "forbidden_topic",  // "forbidden_topic", "required_hashtag", "formatting", "length_limit", "stylistic"
      "rule_value": "политика, война",
      "created_at": "2026-06-01T10:00:00Z"
    }
  ]
}
```

---

### 7. Расписание постинга

**GET** `/api/admin/tenants/{tenant_id}/schedule`

Получить расписание постинга.

**Ответ:**
```json
{
  "schedule": {
    "mode": "frequency",  // "off", "frequency", "times"
    "active": true,
    "posts_per_day": 2,
    "schedule_times": ["09:00", "17:00"],
    "next_post_at": "2026-06-18T09:00:00Z"
  }
}
```

---

### 8. RAG & References статус

**GET** `/api/admin/tenants/{tenant_id}/rag-status`

Получить статус RAG индексирования и источников.

**Ответ:**
```json
{
  "rag_enabled": false,
  "references_enabled": true,
  "sources_count": 10,
  "total_posts_indexed": 1247,
  "last_reindex_at": "2026-06-18T05:00:00Z",
  "rag_health": {
    "qdrant_connection": "ok",
    "ollama_embeddings": "ok",
    "avg_search_latency_ms": 45
  }
}
```

---

### 9. Обновление профиля

**PATCH** `/api/admin/tenants/{tenant_id}/profile`

Обновить поля профиля.

**Request body:**
```json
{
  "tone": "friendly",
  "creativity_level": 0.6,
  "factual_strictness": 0.8,
  "topics": "topic1, topic2",
  "use_references": true,
  "active": true
}
```

**Ответ:**
```json
{
  "success": true,
  "message": "Profile updated"
}
```

---

### 10. Добавить/удалить источник

**POST** `/api/admin/tenants/{tenant_id}/sources`

Добавить новый источник (референс-канал).

**Request body:**
```json
{
  "source_chat_id": "@newchannel"
}
```

**Ответ:**
```json
{
  "success": true,
  "source_id": 11,
  "posts_indexed": 125
}
```

---

**DELETE** `/api/admin/tenants/{tenant_id}/sources/{source_id}`

Удалить источник.

---

## Метрики для Админки (Dashboard)

### Основной KPI (на главной):
1. **Всего опубликовано** — count(posts where posted=true)
2. **Среднее просмотров** — avg(views) по всем постам
3. **Среднее репостов** — avg(forwards)
4. **Среднее реакций** — avg(reactions)
5. **Активные каналы** — count(tenants where active=true)

### По каналу:
1. **Статистика постинга** — posted/total, success rate
2. **Топ топики** — какие темы дают лучший engagement
3. **Timeline метрик** — график views/forwards/reactions за период
4. **RAG health** — успешность индексации, coverage
5. **Topic ротация** — распределение постов по темам

### Для отладки:
1. **Последние ошибки** — failed generations, API errors
2. **RAG покрытие** — какие топики имеют достаточно источников
3. **Creativity vs Quality** — коррелирует ли уровень творчества с engagement

---

## WebSocket для Real-time

**WS** `/ws/admin/tenants/{tenant_id}/live`

Live-поток событий (генерирование, публикация, метрики).

```json
{
  "event": "generation_started",
  "topic": "investment and funding",
  "timestamp": "2026-06-18T10:00:00Z"
}
```

```json
{
  "event": "post_published",
  "post_id": 123,
  "message_id": 456,
  "topic": "investment and funding",
  "timestamp": "2026-06-18T10:00:00Z"
}
```

```json
{
  "event": "metrics_updated",
  "post_id": 123,
  "views": 450,
  "forwards": 25,
  "reactions": 45,
  "timestamp": "2026-06-18T10:00:00Z"
}
```

---

---

## Запуск Admin API

### Локально:
```bash
python3 admin_api.py
# или через uvicorn
uvicorn admin_api:app --host 0.0.0.0 --port 8001 --reload
```

API будет доступна на http://localhost:8001

Swagger документация: http://localhost:8001/docs

### В Docker:
```bash
# Добавить в docker-compose.yml:
admin-api:
  build: .
  command: uvicorn admin_api:app --host 0.0.0.0 --port 8001
  ports:
    - "8001:8001" 
  environment:
    - DATABASE_URL=postgresql://postgres:password@db:5432/content_ai
  depends_on:
    - db
```

### Переменные окружения:
```
ADMIN_TOKEN=your-secret-token-here  # Установить в коде или .env
DATABASE_URL=postgresql://...
```

---

## Примеры использования (Frontend)

### React Hook для получения статистики:
```javascript
const useChannelStats = (tenantId) => {
  const [stats, setStats] = useState(null);
  useEffect(() => {
    fetch(`/api/admin/tenants/${tenantId}/stats?days=30`, {
      headers: { Authorization: `Bearer ${token}` }
    })
      .then(r => r.json())
      .then(setStats);
  }, [tenantId]);
  return stats;
};
```

### Получение списка каналов:
```javascript
fetch('/api/admin/tenants', {
  headers: { Authorization: `Bearer ${token}` }
})
  .then(r => r.json())
  .then(data => setTenants(data.tenants));
```

### Custom hook для работы с API:
```typescript
const useAdminAPI = (endpoint: string, token: string) => {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    fetch(`/api/admin${endpoint}`, {
      headers: { Authorization: `Bearer ${token}` }
    })
      .then(r => {
        if (!r.ok) throw new Error(`${r.status}`);
        return r.json();
      })
      .then(setData)
      .catch(setError)
      .finally(() => setLoading(false));
  }, [endpoint, token]);

  return { data, loading, error };
};

// Использование:
const { data: stats } = useAdminAPI(
  '/tenants/uuid/stats?days=30',
  adminToken
);
```

### Пример компонента Dashboard:
```typescript
interface ChannelDashboard {
  tenantId: string;
}

export const ChannelDashboard: React.FC<ChannelDashboard> = ({ tenantId }) => {
  const { data: profile } = useAdminAPI(`/tenants/${tenantId}/profile`, token);
  const { data: stats } = useAdminAPI(`/tenants/${tenantId}/stats?days=30`, token);
  const { data: sources } = useAdminAPI(`/tenants/${tenantId}/sources`, token);

  if (!stats) return <div>Loading...</div>;

  return (
    <div className="dashboard">
      <h1>{profile?.channel_name}</h1>
      
      <div className="metrics">
        <MetricCard 
          label="Всего опубликовано" 
          value={stats.summary.total_published} 
        />
        <MetricCard 
          label="Среднее просмотров" 
          value={stats.summary.avg_views_per_post.toFixed(0)} 
        />
        <MetricCard 
          label="Среднее репостов" 
          value={stats.summary.avg_forwards_per_post.toFixed(0)} 
        />
      </div>

      <SourcesList sources={sources?.sources} />
      <PostsList tenantId={tenantId} />
    </div>
  );
};
```

---

## Заметки для разработчиков

### Что нужно доделать в admin_api.py:

1. **Метрики постов** — `/stats` сейчас возвращает mock-данные. Нужно:
   - Запросить post_metrics из БД за период
   - Сгруппировать по топикам
   - Посчитать средние значения

   ```python
   # В database.py добавить:
   def get_post_metrics(tenant_id: str, days: int = 30):
       # SELECT * FROM post_metrics WHERE tenant_id = ? AND captured_at > NOW() - interval(days)
   ```

2. **Сохранение источников** — в `add_source` нужно вызвать `add_tenant_source()`:
   ```python
   from database import add_tenant_source
   source = await asyncio.to_thread(
       add_tenant_source, tenant_id, req.source_chat_id
   )
   return SourceAddResponse(success=True, source_id=source.id, ...)
   ```

3. **WebSocket live-events** — для real-time обновления метрик:
   - Добавить в `main.py` WebSocket обработчик
   - При каждом генерировании/публикации посылать события в WS
   - Frontend подписывается на `/ws/admin/tenants/{tenant_id}/live`

4. **Аутентификация** — сейчас простая проверка токена. Для production:
   - Использовать JWT или OAuth2
   - Привязать к конкретному клиенту (допуск только к своим каналам)

5. **Rate limiting** — добавить ограничение на количество запросов
   - Использовать `slowapi` или `ratelimit`

### Метрики для отслеживания:

| Метрика | Способ расчёта | Смысл |
|---------|----------------|-------|
| CTR (Click Through Rate) | forwards / views | Сколько % людей переформатировали |
| Engagement Rate | (forwards + reactions) / views | Общая активность |
| Avg Post Lifespan | дни между постингом и последним изменением метрик | Как долго пост "живой" |
| Topic Performance | views/forwards/reactions ПО теме | Какие темы работают лучше |
| Generation Success Rate | успешные / все попытки | Стабильность генерации |

### SQL для аналитики:

```sql
-- Топ-5 постов по просмотрам за 30 дней
SELECT ph.id, ph.topic, pm.views, pm.forwards, pm.reactions
FROM posts_history ph
LEFT JOIN post_metrics pm ON ph.id = pm.post_id
WHERE ph.tenant_id = ? AND ph.created_at > NOW() - interval '30 days'
ORDER BY pm.views DESC
LIMIT 5;

-- Распределение постов по темам
SELECT topic, COUNT(*) as count, AVG(pm.views) as avg_views
FROM posts_history ph
LEFT JOIN post_metrics pm ON ph.id = pm.post_id
WHERE ph.tenant_id = ? AND ph.posted = true
GROUP BY topic
ORDER BY count DESC;

-- Последний успешный пост
SELECT * FROM posts_history
WHERE tenant_id = ? AND posted = true
ORDER BY created_at DESC
LIMIT 1;
```

### Безопасность:

⚠️ **TODO:** Перед использованием в production:
- [ ] Переместить `ADMIN_TOKEN` в переменную окружения
- [ ] Добавить проверку прав доступа (client_id может видеть только свои каналы)
- [ ] Использовать HTTPS для всех запросов
- [ ] Логировать все изменения через админ-панель
- [ ] Rate limiting
- [ ] Input validation (уже частично есть через Pydantic)

---

# 🔁 Repost rejimi (пересборка новостей) — V1 + V2

Раздел описывает второй режим контента, добавленный после первой версии. Здесь —
поведение бота и модель данных (реализовано), а также что нужно дотянуть в
admin-api, чтобы дашборд мог им управлять.

## Два режима канала

Каждый канал работает в одном из режимов — поле `tenant_profiles.content_mode`:

| Режим | Значение | Что делает |
|-------|----------|------------|
| Topic (оригинальный) | `topic` (по умолчанию) | Генерация оригинальных постов на темы канала (`topics`) с ротацией — как в первой версии. |
| Repost (новостной) | `repost` | Берёт посты из source-каналов (`tenant_sources`), отбирает, переводит/адаптирует под стиль и публикует. |

Переключение режима: в боте **⚙️ Sozlamalar → 🔀 Rejim (topik/repost)**.
> ⚠️ В admin-api поле `content_mode` пока **не сериализуется и не принимается** —
> см. «TODO для admin-api» ниже.

В repost-режиме `tenant_sources` — это **новостная лента** (что репостить), а в
topic-режиме те же каналы используются лишь как RAG-контекст (факты).

## Поток repost (V2)

```
source-каналы → scrape свежих → чистка (форварды/пусто/реклама)
  → точный дедуп (covered-keys: выбранные посты + члены прошлых историй)
  → эмбеддинги (RAG POST /embed) → кластеризация по событию (cosine)
  → семантический дедуп кластеров (не повторять освещённую историю)
  → LLM выбирает лучший кластер
  → канонизация (объединение фактов всех источников об одном событии,
     перевод/адаптация, языковой замок: вывод 100% на языке канала)
  → картинка → preview/approve → publish → запись RepostStory
```

Кластер из одного поста → обычный rewrite (без объединения).
**Graceful degradation:** если RAG `/embed` недоступен — фолбэк на поведение V1
(точный дедуп, один пост, без кластеризации); репост не падает.

## Модель данных (новое)

**`tenant_profiles`** (новое поле):
- `content_mode: str` — `"topic"` | `"repost"` (default `"topic"`).

**`posts_history`** (новые поля, заполняются только repost-режимом):
- `source_chat_id: str | null` — исходный канал пересобранного поста;
- `source_message_id: int | null` — id исходного сообщения (для точного дедупа).

**`repost_stories`** (новая таблица, V2 — для семантического дедупа):
```
id              int
tenant_id       str (index)
headline        str        # первая строка канонического поста (лог/дебаг)
centroid_json   str        # JSON list[float] — усреднённый эмбеддинг кластера
member_keys_json str       # JSON list["source_chat_id:message_id"] всех членов
published_at    datetime (index)
```

## Дедуп (два слоя)
1. **Точный** — `source_chat_id:message_id`. Покрывает не только опубликованный
   пост, но и **все** сообщения кластера (через `member_keys` истории).
2. **Семантический** — cosine центроида нового кластера против центроидов
   опубликованных историй за `REPOST_DEDUP_DAYS`. Ловит ту же новость,
   перефразированную с другого источника.

## Зависимость: RAG-сервис `POST /embed`

Новый эндпоинт RAG-сервиса (`~/Desktop/RAG`), используется repost-режимом для
кластеризации/дедупа:

**POST** `/embed`
```json
// запрос
{ "texts": ["...", "..."] }
// ответ
{ "vectors": [[0.01, ...], [0.02, ...]] }   // nomic-embed-text, 768d
```

## Переменные окружения (repost)

| Переменная | Дефолт | Назначение |
|------------|--------|------------|
| `REPOST_MODEL` | = `GROQ_MODEL` | Модель ТОЛЬКО для переписывания/канонизации (крупнее = надёжнее перевод). |
| `REPOST_TEMPERATURE` | `0.3` | Температура переписывания (ниже = ближе к фактам). |
| `REPOST_GENERATE_IMAGE` | `true` | `true` — генерить свою картинку; `false` — брать оригинал поста. |
| `REPOST_FETCH_LIMIT` | `30` | Сколько свежих постов тянуть с каждого источника. |
| `REPOST_MAX_CANDIDATES` | `60` | Максимум кандидатов в эмбеддинги/LLM-отбор. |
| `REPOST_CLUSTER_THRESHOLD` | `0.86` | Порог кластеризации (cosine). Выше = дробнее (меньше риск склейки разных новостей). |
| `REPOST_DEDUP_THRESHOLD` | `0.83` | Порог семантического дедупа. Ниже = агрессивнее давит повторы. |
| `REPOST_DEDUP_DAYS` | `14` | За сколько дней смотреть прошлые истории. |

Калибровка порогов (nomic-embed-text, реальные посты): одно событие ≈ 0.85,
разные события ≈ 0.74–0.78.

## TODO для admin-api (чтобы дашборд управлял режимом)

Сейчас admin-api про repost не знает. Минимум для интеграции с дашбордом:
- `TenantProfileSchema` += `content_mode: str` (и в сериализаторах `list_tenants`
  / `get_profile`).
- `ProfileUpdateRequest` += `content_mode: Optional[str]` (PATCH профиля).
- Эндпоинт списка источников `GET /sources` уже есть — он же управляет
  новостной лентой repost-режима (отдельный UI не нужен).
- (Опц.) `GET /api/admin/tenants/{tenant_id}/repost-stories` — просмотр
  опубликованных историй (headline, члены, дата) для дебага дедупа.
