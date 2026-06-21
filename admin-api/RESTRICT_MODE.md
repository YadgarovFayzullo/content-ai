# Restrict Mode — гайд для фронтенда

Документ описывает, как админ-панель работает **для клиентов** (а не только супер-админа)
и как backend ограничивает действия по **тарифу** (starter / pro / premium).

Restrict mode состоит из двух независимых слоёв:

1. **Роль (кто ты)** — супер-админ видит все каналы; клиент — только свои.
2. **Тариф (что можно)** — у каждого канала тариф, который гейтит фичи и квоты.

Всё это enforced на бэкенде (и в БД, и в API, и в боте). Фронт **обязан**
скрывать/блокировать недоступное проактивно, но не должен полагаться на это как на
защиту — backend всё равно вернёт `403`.

---

## 1. Аутентификация

Раньше был один статический `ADMIN_TOKEN` (= супер-админ). Теперь поддерживаются
**два вида Bearer-токена**, оба идут в одном заголовке:

```
Authorization: Bearer <token>
```

| Токен | Кто это | Что видит |
|---|---|---|
| `ADMIN_TOKEN` (из .env) | супер-админ | все каналы, без тарифных лимитов |
| session token (выдаётся при логине) | клиент | только свои каналы, в пределах тарифа |

Сессия клиента так же может быть супер-админской, если `owner_id` входящего
пользователя совпадает с супер-админом — тогда `is_super: true`.

### 1.1 Вход клиента (Telegram deep-link handshake)

Клиент логинится через Telegram-бота (у нас нет паролей). Поток:

```
1. POST /api/admin/auth/telegram/start
   → { token, deep_link, expires_in: 300 }

2. Фронт открывает deep_link (или рисует QR) — пользователь жмёт Start в боте,
   бот привязывает его telegram user_id к токену.

3. Фронт поллит:  GET /api/admin/auth/telegram/poll?token=<token>
     • { status: "pending" }                      → ждём, повторяем (раз в ~2 сек)
     • 403 { detail: { status: "denied" } }       → пользователь отклонил
     • 200 { status: "confirmed", session_token,
             owner_id, is_super }                  → ГОТОВО

4. Сохраняем session_token, дальше шлём его как `Authorization: Bearer <session_token>`.
```

`deep_link` будет `null`, если на сервере не задан `BOT_USERNAME` — тогда постройте
ссылку сами из известного юзернейма бота: `https://t.me/<bot>?start=auth_<token>`.

Login-токен живёт **5 минут** и **одноразовый** (после обмена на сессию — `consumed`,
повторный poll вернёт `409`). Сессия живёт **30 дней**.

### 1.2 Выход

```
POST /api/admin/auth/logout      (с Authorization: Bearer <session_token>)
→ { success: true }
```

### 1.3 Кто я

```
GET /api/admin/me
→ {
    owner_id: "12345678" | null,   // null для статического супер-токена
    is_super: true | false,
    tiers: { ...полная матрица тарифов... }   // см. ниже, для рендера фич
  }
```

Дёргайте `/me` сразу после логина: по `is_super` решаете, показывать ли админские
кнопки (смена тарифа, назначение владельца, publish-all), а `tiers` — словарь лимитов
для построения UI.

---

## 2. Тарифы и матрица лимитов

Тариф привязан **к каналу** (`tenant.subscription_tier`). Значение приходит в каждом
профиле канала вместе с развёрнутыми возможностями:

```jsonc
// фрагмент ответа GET /api/admin/tenants/{id}/profile  и элементов /tenants
{
  "tenant_id": "…",
  "subscription_tier": "pro",
  "owner_id": "12345678",
  "schedule_mode": "frequency",
  "posts_per_day": 3,
  "post_times": "",
  "capabilities": {
    "max_channels": 3,
    "max_sources": 5,
    "max_posts_per_day": 5,
    "scheduling": true,
    "repost_mode": true,
    "rag": true,
    "image_generation": true,
    "manual_publish": true,
    "analytics": "full"
  }
}
```

Используйте `capabilities` канала напрямую — не хардкодьте матрицу на фронте.
`-1` в числовом лимите означает **без ограничения** (unlimited).

### Матрица (источник правды — `tiers.py` на бэкенде)

| Возможность | Starter | Pro | Premium |
|---|---|---|---|
| `max_channels` (на клиента) | 1 | 3 | ∞ (-1) |
| `max_sources` (на канал) | 1 | 5 | ∞ (-1) |
| `max_posts_per_day` | 1 | 5 | ∞ (-1) |
| `scheduling` (авто-расписание) | ✗ | ✓ | ✓ |
| `repost_mode` (`content_mode="repost"`) | ✗ | ✓ | ✓ |
| `rag` (`use_rag` / `use_references`) | ✗ | ✓ | ✓ |
| `image_generation` (картинки к постам) | ✗ | ✓ | ✓ |
| `manual_publish` (кнопка «опубликовать») | ✓ | ✓ | ✓ |
| `analytics` | basic | full | full |

- **`max_channels`** считается по «лучшему» тарифу среди каналов клиента. Применяется
  при создании канала клиентом.
- **`analytics: "basic"`** (starter): окно статистики ограничено **7 днями**, и не
  отдаётся разбивка по темам (`by_topic: []`). В ответе появятся `analytics_tier:"basic"`
  и `window_days`.
- **`image_generation`**: на starter пост публикуется **без картинки** (backend сам
  выкидывает фото), даже если в пайплайне оно сгенерировалось бы.
- **`rag`**: на тарифах без RAG факты не подмешиваются **при самой генерации**, даже
  если в профиле остались `use_rag`/`use_references = true` (напр. дефолт на starter).
  Тариф — финальный гейт; флаги в профиле лишь «пожелание».

> Супер-админ **не ограничен** тарифами — для него все гейты выключены.

---

## 3. Что меняется по эндпоинтам

Все `/api/admin/tenants/{id}/...` теперь проверяют владение: чужой канал → `403`,
несуществующий → `404`.

| Эндпоинт | Доступ | Тарифный гейт |
|---|---|---|
| `GET /tenants` | клиент видит только свои | — |
| `GET /tenants/{id}/profile` | владелец/супер | — |
| `GET /tenants/{id}/stats` | владелец/супер | starter: окно ≤ 7 дн, без `by_topic` |
| `GET /tenants/{id}/posts` `…/sources` `…/rules` `…/rag-status` `…/avatar` | владелец/супер | — |
| `POST /tenants/{id}/rules`, `DELETE …/rules/{rule_id}` | владелец/супер | — |
| `PATCH /tenants/{id}/profile` | владелец/супер | `use_rag`/`use_references`→`rag`; `content_mode="repost"`→`repost_mode`; `schedule_mode≠off`/`posts_per_day`→`scheduling` + `max_posts_per_day` |
| `POST /tenants/{id}/sources` | владелец/супер | `max_sources` (новый источник сверх лимита → `403`) |
| `PATCH …/sources/{sid}/priority`, `DELETE …/sources/{sid}` | владелец/супер | — |
| `POST /tenants/{id}/generate` | владелец/супер | — (текстовое превью доступно всем) |
| `POST /tenants/{id}/publish` | владелец/супер | картинка только если `image_generation` |
| `POST /tenants/{id}/collect-metrics` | владелец/супер | — |
| `POST /tenants` (создать канал) | клиент/супер | клиент: `max_channels`; `repost`→`repost_mode` |
| `DELETE /tenants/{id}` | владелец/супер | — |
| `POST /publish-all` | **только супер** | — |
| `PATCH /tenants/{id}/tier` | **только супер** | смена тарифа канала |
| `PATCH /tenants/{id}/owner` | **только супер** | привязка канала к клиенту |

### Создание канала клиентом

`POST /api/admin/tenants` от клиента:
- `owner_id` и `subscription_tier` в теле **игнорируются** — владельцем становится сам
  клиент, тариф наследуется от его лучшего канала (по умолчанию starter).
- Если клиент исчерпал `max_channels` — `403 tier_quota_exceeded`.

Супер-админ может передать `owner_id` и `subscription_tier` явно.

### Расписание (новые поля в PATCH profile)

`PATCH /api/admin/tenants/{id}/profile` теперь принимает:
- `schedule_mode`: `"off"` | `"frequency"` | `"times"`
- `posts_per_day`: число (для `frequency`)
- `post_times`: `"09:00,14:00,20:00"` (для `times`)

На starter любая попытка включить расписание → `403 tier_restricted (scheduling)`.
На pro `posts_per_day > 5` → `403 tier_quota_exceeded (max_posts_per_day)`.

### Правила канала (Rules)

`GET /tenants/{id}/rules` отдаёт только **пользовательские** правила (системные,
создаваемые при подключении канала, скрыты). Создание/удаление — тоже только
пользовательские:

```
POST /api/admin/tenants/{id}/rules
  body { "rule_type": "...", "rule_value": "..." }
  → 200 { id, rule_type, rule_value, created_at }

DELETE /api/admin/tenants/{id}/rules/{rule_id}
  → 200 { success: true }
```

`rule_type` — одно из: `forbidden_topic`, `required_hashtag`, `formatting`,
`length_limit`, `stylistic` (иначе `422`). Пустой `rule_value` → `422`.
Удаление чужого/несуществующего/системного `rule_id` → `404`. Тарифного гейта на
правила нет.

---

## 4. Форматы ошибок restrict mode

Все — HTTP `403` с телом `{ "detail": { ... } }`.

**Фича недоступна на тарифе:**
```json
{
  "detail": {
    "error": "tier_restricted",
    "feature": "scheduling",
    "current_tier": "starter",
    "required_tier": "pro",
    "message": "Feature 'scheduling' is not available on the 'starter' plan"
  }
}
```

**Квота исчерпана:**
```json
{
  "detail": {
    "error": "tier_quota_exceeded",
    "limit": "max_sources",
    "max": 1,
    "current": 1,
    "current_tier": "starter",
    "required_tier": "pro",
    "message": "'max_sources' limit reached for the 'starter' plan"
  }
}
```

**Доступ запрещён (чужой канал / только-супер):**
```json
{ "detail": "Forbidden: not your channel" }
{ "detail": "Forbidden: super-admin only" }
```

**Не авторизован / сессия истекла:**
```json
{ "detail": "Unauthorized: invalid or expired session" }   // 401
```

### Рекомендация по UX
- На `tier_restricted` / `tier_quota_exceeded` показывайте апселл на `required_tier`.
- На `401` от session-токена — разлогинивайте и ведите на экран входа.
- Кнопки недоступных фич рисуйте задизейбленными c подсказкой тарифа (из `capabilities`),
  а не ждите `403` — он лишь страховка.

---

## 5. Чеклист интеграции фронта

- [ ] Логин-флоу: `start` → poll → хранить `session_token`.
- [ ] Все запросы шлют `Authorization: Bearer <session_token | ADMIN_TOKEN>`.
- [ ] После логина: `GET /me`, ветвление UI по `is_super`.
- [ ] Возможности фич/квот берём из `capabilities` каждого канала.
- [ ] Обработка `403` с `error: tier_*` → апселл; `401` → релогин.
- [ ] Админские экраны (смена тарифа, владелец, publish-all) — только при `is_super`.
