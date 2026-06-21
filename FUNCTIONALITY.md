# Функционал системы (полный список)

Инвентаризация всех возможностей, которые гейтятся тарифом или ролью.
Единый источник лимитов — [`tiers.py`](tiers.py) (`TIER_LIMITS`). Меняем тарифы
только там — admin-api и бот подхватывают.

---

## 1. Числовые квоты

- **`max_channels`** — сколько каналов может завести клиент.
  Считается по «лучшему» тарифу среди его каналов.
  Применяется: создание канала ([admin-api/main.py:1037](admin-api/main.py#L1037)),
  кнопка «Add channel» на фронте.

- **`max_sources`** — сколько референс-источников можно подключить к каналу.
  Применяется: добавление источника ([admin-api/main.py:915](admin-api/main.py#L915)).

- **`max_posts_per_day`** — потолок частоты авто-расписания (режим `frequency`).
  Применяется: кламп в боте ([bot/scheduler.py:57](bot/scheduler.py#L57)),
  валидация PATCH профиля ([admin-api/main.py:847](admin-api/main.py#L847)).

---

## 2. Булевы фичи

- **`scheduling`** — авто-постинг по расписанию (`schedule_mode` = `frequency` | `times`).
  Без неё канал публикуется только вручную.
  Применяется: [admin-api/main.py:840](admin-api/main.py#L840), [bot/scheduler.py:51](bot/scheduler.py#L51).

- **`repost_mode`** — `content_mode = "repost"`: рерайт/репост чужих каналов
  вместо генерации постов из тем.
  Применяется: [admin-api/main.py:838](admin-api/main.py#L838), [admin-api/main.py:1040](admin-api/main.py#L1040).

- **`rag`** — заземление генерации. Гейтит сразу **два** подфлага профиля:
  - `use_rag` — опора на собственные прошлые посты канала;
  - `use_references` — подмешивание фактов из референс-каналов.
  Применяется: [admin-api/main.py:835](admin-api/main.py#L835), [context_builder.py:78](context_builder.py#L78).

- **`image_generation`** — картинки к постам. Без неё бот публикует только текст
  (передаётся флаг `allow_image`).
  Применяется: [admin-api/main.py:1121](admin-api/main.py#L1121) →
  [bot/internal_api.py:96](bot/internal_api.py#L96), генерация [orchestrator.py:93](orchestrator.py#L93).

- **`manual_publish`** — кнопка «опубликовать сейчас». Сейчас включена на всех тарифах.

---

## 3. Режим аналитики

- **`analytics`** — `basic` | `full`.
  - `basic` — окно ≤ 7 дней (`BASIC_ANALYTICS_MAX_DAYS`), без разбивки по темам.
  - `full` — без ограничения окна, с разбивкой.
  Применяется: [admin-api/main.py:635](admin-api/main.py#L635).
  Возможен третий уровень `advanced` (требует доработки бэка).

---

## 4. Роль (не тариф)

- **super-admin** (`is_super`) — не ограничен тарифом вообще:
  - «Post to all» (публикация во все активные каналы разом);
  - Administration (смена `subscription_tier` и владельца канала);
  - создание каналов без лимита `max_channels`.

---

## Текущая матрица тарифов

| Возможность | starter | pro | premium |
|---|---|---|---|
| `max_channels` | 1 | 3 | 10 |
| `max_sources` | 1 | 5 | 15 |
| `max_posts_per_day` | 1 | 5 | 10 |
| `scheduling` | ✗ | ✓ | ✓ |
| `repost_mode` | ✗ | ✓ | ✓ |
| `rag` | ✗ | ✓ | ✓ |
| `image_generation` | ✗ | ✓ | ✓ |
| `manual_publish` | ✓ | ✓ | ✓ |
| `analytics` | basic | full | full |

> ⚠️ Открытый вопрос: pro и premium сейчас отличаются только квотами.
> Кандидаты на premium-only рычаг: развести `rag` на `use_rag`/`use_references`
> (референсы → premium), либо `advanced`-аналитика.
