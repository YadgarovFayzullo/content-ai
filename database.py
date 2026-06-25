"""Слой данных мульти-арендной системы.

ВАЖНО: все таблицы ОБЩИЕ и логически разделены по `tenant_id`, а не физически
(никаких отдельных таблиц на канал). Любой доступ обязан фильтроваться по
tenant_id — данные арендаторов не должны пересекаться.
"""
import json
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, List

from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel, Field, create_engine, Session, select
from dotenv import load_dotenv

load_dotenv()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_tenant_id() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# МОДЕЛИ
# ---------------------------------------------------------------------------


class TenantProfile(SQLModel, table=True):
    """Конфигурация одного арендатора (Telegram-канала)."""

    __tablename__ = "tenant_profiles"

    id: Optional[int] = Field(default=None, primary_key=True)
    # Стабильный идентификатор арендатора (не меняется, в отличие от @username).
    tenant_id: str = Field(default_factory=_new_tenant_id, unique=True, index=True)
    # Назначение в Telegram: @username или числовой -100...
    chat_id: str = Field(unique=True, index=True)

    # Telegram user_id клиента-владельца. None — ничей (управляет только
    # супер-админ). Клиент видит/управляет только своими тенантами.
    owner_id: Optional[str] = Field(default=None, index=True)

    channel_name: str = ""
    tone: str = "neutral"
    language: str = "uz"
    writing_style: str = ""
    audience: str = ""
    post_template: Optional[str] = None
    image_style: Optional[str] = None
    # Реальный призыв к действию / ссылка (напр. "Topshirish uchun: @admin" или
    # "Apply: site.uz/apply"). Вставляется в пост ДОСЛОВНО — это не выдумка LLM.
    cta: str = ""
    topics: str = ""  # темы через запятую — источник для авто-генерации

    # Режим контента:
    #   topic  — генерация оригинальных постов на темы из `topics` (с ротацией);
    #   repost — пересборка чужих новостей: посты из source-каналов отбираются,
    #            переводятся/адаптируются под стиль канала и публикуются.
    # В repost-режиме source-каналы (TenantSource) — это новостная лента, а не
    # просто RAG-контекст.
    content_mode: str = "topic"

    # Источник картинки в topic-режиме: "ai" — ИИ-иллюстрация (по умолчанию),
    # "stock" — чистое тематическое фото из интернета (Pexels), без надписей.
    image_mode: str = "ai"

    # Тариф арендатора: starter — только каналы где бот админ; pro — ещё и
    # любые публичные каналы по @username.
    subscription_tier: str = "starter"

    # Средняя длина постов канала (символы) — считается при скрейпе, задаёт
    # целевую длину генерации. 0 = не измерено (используем дефолтную структуру).
    avg_post_length: int = 0

    creativity_level: float = 0.5    # 0.0–1.0  -> temperature LLM
    factual_strictness: float = 0.7  # 0.0–1.0

    # Использовать ли RAG-факты при генерации. True — заземлять на контенте канала
    # (хорошо для фактологических каналов). False — общий вечнозелёный контент без
    # подмешивания постов (лучше для каналов-агрегаторов анонсов, чтобы бот не
    # воспроизводил чужие события как свои).
    use_rag: bool = True

    # Подмешивать ли факты из РЕФЕРЕНС-каналов (TenantSource) при генерации.
    # True — использовать и свой канал, и референсы. False — только свой канал
    # (клиент хочет, чтобы инфо бралась исключительно из его постов, без чужих).
    use_references: bool = True

    # Стилевые тумблеры контента. use_emoji — разрешать ли эмодзи в постах (умеренно);
    # use_hashtags — добавлять ли хэштеги в конце поста. Читаются генератором.
    use_emoji: bool = True
    use_hashtags: bool = False

    # Автопостинг по расписанию:
    #   off       — выключено
    #   frequency — posts_per_day раз в день, равномерно по окну 09:00–21:00
    #   times     — в явные времена из post_times ("09:00,14:00,20:00")
    schedule_mode: str = "off"
    posts_per_day: int = 0
    post_times: str = ""

    active: bool = True
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class TenantRule(SQLModel, table=True):
    """Правило/ограничение арендатора. Несколько строк на один tenant_id."""

    __tablename__ = "tenant_rules"

    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: str = Field(index=True)
    # forbidden_topic | required_hashtag | formatting | length_limit | stylistic
    rule_type: str
    rule_value: str
    # Системное правило (создаётся автоматически при подключении канала).
    # Применяется в генерации, но НЕ показывается клиенту в меню «Qoidalar».
    is_system: bool = False
    created_at: datetime = Field(default_factory=_utcnow)


class PostHistory(SQLModel, table=True):
    """История постов арендатора (память для уникальности/дедупликации)."""

    __tablename__ = "posts_history"

    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: str = Field(index=True)
    topic: Optional[str] = None
    content: str
    image_path: Optional[str] = None
    posted: bool = Field(default=False)
    # message_id в Telegram — нужен для последующего сбора метрик поста.
    message_id: Optional[int] = Field(default=None, index=True)
    # Repost-режим: исходный пост, из которого пересобран этот. Нужны для дедупа —
    # один и тот же чужой пост не репостим дважды. Для topic-режима = None.
    source_chat_id: Optional[str] = Field(default=None, index=True)
    source_message_id: Optional[int] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=_utcnow)


class TenantSource(SQLModel, table=True):
    """Референс-канал арендатора: сторонний канал, чьи посты индексируются в RAG
    под этим tenant_id для обогащения контекста (фактов/формулировок). Сам канал
    НЕ публикуется и НЕ меняет стиль профиля — только добавляет retrieval-контекст.
    """

    __tablename__ = "tenant_sources"

    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: str = Field(index=True)
    source_chat_id: str               # @username стороннего канала
    posts_indexed: int = 0
    # Квота/приоритет источника: чем БОЛЬШЕ — тем раньше из него берётся новость
    # в repost-режиме (при равной свежести). 0 — обычный приоритет.
    priority: int = 0
    created_at: datetime = Field(default_factory=_utcnow)


class PostMetric(SQLModel, table=True):
    """Замер метрик опубликованного поста (снимок на момент captured_at).

    Несколько строк на один пост — история роста просмотров/реакций во времени.
    """

    __tablename__ = "post_metrics"

    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: str = Field(index=True)
    post_id: int = Field(index=True)        # posts_history.id
    message_id: int                          # message_id в Telegram
    views: int = 0
    forwards: int = 0
    reactions: int = 0
    captured_at: datetime = Field(default_factory=_utcnow)


class ChannelStat(SQLModel, table=True):
    """Снимок числа подписчиков канала (на момент captured_at).

    Канал-уровневая метрика (в отличие от PostMetric — по посту). Несколько строк
    на канал = история роста аудитории во времени; дельту считаем по двум последним.
    """

    __tablename__ = "channel_stats"

    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: str = Field(index=True)
    subscribers: int = 0
    captured_at: datetime = Field(default_factory=_utcnow, index=True)


class RepostStory(SQLModel, table=True):
    """Опубликованная «история» repost-режима (V2): кластер постов-источников об
    одном событии, сведённый в один канонический пост.

    Нужна для семантического дедупа: храним усреднённый эмбеддинг кластера
    (centroid) и ключи ВСЕХ его членов, чтобы не репостить ту же новость снова —
    ни тем же сообщением, ни перефразированной с другого источника. Вектор хранится
    как JSON-строка (без pgvector — историй за окно мало, косинус считаем в Python).
    """

    __tablename__ = "repost_stories"

    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: str = Field(index=True)
    headline: str = ""               # первая строка канонического поста (для логов/дебага)
    centroid_json: str               # JSON list[float] — усреднённый эмбеддинг кластера
    member_keys_json: str            # JSON list[str] — "source_chat_id:message_id" членов
    published_at: datetime = Field(default_factory=_utcnow, index=True)


class AuthLogin(SQLModel, table=True):
    """Одноразовый login-handshake для входа в веб-панель через Telegram-бота.

    Веб создаёт запись (pending), отдаёт deep-link `?start=auth_<token>`. Бот по
    нажатию подтверждает (привязывает telegram_user_id). Веб поллит и обменивает
    подтверждённый токен на сессию. Токен одноразовый и живёт недолго.
    """

    __tablename__ = "auth_logins"

    token: str = Field(primary_key=True)
    telegram_user_id: Optional[str] = None
    # pending | confirmed | denied | consumed
    status: str = Field(default="pending")
    created_at: datetime = Field(default_factory=_utcnow)


class AuthSession(SQLModel, table=True):
    """Сессия веб-панели. owner_id = Telegram user_id владельца (или супер-админа)."""

    __tablename__ = "auth_sessions"

    token: str = Field(primary_key=True)
    owner_id: str = Field(index=True)
    created_at: datetime = Field(default_factory=_utcnow)
    expires_at: datetime


class ScheduleSlot(SQLModel, table=True):
    """Персистентный дедуп автопостинга по расписанию.

    Один ряд = «пост для (канал, дата, время) уже запущен». Уникальный PK `slot`
    не даёт опубликовать один и тот же запланированный слот дважды — переживает
    рестарт бота и совпадение тиков. Старые слоты подчищаются раз в сутки.
    """

    __tablename__ = "schedule_slots"

    slot: str = Field(primary_key=True)  # "YYYY-MM-DD HH:MM <chat_id>"
    created_at: datetime = Field(default_factory=_utcnow)


# ---------------------------------------------------------------------------
# ДВИЖОК БД
# ---------------------------------------------------------------------------

database_url = os.getenv("DATABASE_URL", "sqlite:///facts.db")

# На некоторых платформах (Render и т.п.) URL приходит как postgres:// — чиним.
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

# pool_pre_ping снимает "протухшие" соединения; пул рассчитан на ~100 арендаторов.
_engine_kwargs: dict[str, Any] = {"pool_pre_ping": True}
if database_url.startswith("postgresql"):
    _engine_kwargs.update(pool_size=10, max_overflow=20)

# expire_on_commit=False — объекты остаются пригодными после закрытия сессии,
# что удобно при возврате ORM-моделей из функций доступа.
engine = create_engine(database_url, **_engine_kwargs)

# Семантический-«лайт» дедуп истории постов опирается на pg_trgm (только Postgres).
# На SQLite (локальная разработка) функции дедупа деградируют до подстрочного
# фолбэка — продакшен всегда Postgres.
_IS_PG = database_url.startswith("postgresql")


# Колонки, добавленные после первого релиза. create_all() новые таблицы создаёт,
# но колонки в существующие НЕ досыпает — поэтому добавляем их вручную, идемпотентно.
# Тип задаётся в синтаксисе, общем для SQLite и PostgreSQL.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "tenant_profiles": {
        "subscription_tier": "VARCHAR DEFAULT 'starter'",
        "avg_post_length": "INTEGER DEFAULT 0",
        "owner_id": "VARCHAR",
        "schedule_mode": "VARCHAR DEFAULT 'off'",
        "posts_per_day": "INTEGER DEFAULT 0",
        "post_times": "VARCHAR DEFAULT ''",
        "cta": "VARCHAR DEFAULT ''",
        "use_rag": "BOOLEAN DEFAULT TRUE",
        "use_references": "BOOLEAN DEFAULT TRUE",
        "use_emoji": "BOOLEAN DEFAULT TRUE",
        "use_hashtags": "BOOLEAN DEFAULT FALSE",
        "content_mode": "VARCHAR DEFAULT 'topic'",
        "image_mode": "VARCHAR DEFAULT 'ai'",
    },
    "posts_history": {
        "message_id": "INTEGER",
        "source_chat_id": "VARCHAR",
        "source_message_id": "INTEGER",
    },
    "tenant_rules": {"is_system": "BOOLEAN DEFAULT FALSE"},
    "tenant_sources": {"priority": "INTEGER DEFAULT 0"},
}


def _run_migrations() -> None:
    """Досыпает недостающие колонки в уже существующие таблицы (in-place)."""
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            if table not in existing_tables:
                continue  # таблицы ещё нет — create_all создаст её сразу с колонками
            present = {c["name"] for c in inspector.get_columns(table)}
            for name, ddl in columns.items():
                if name not in present:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def _ensure_trgm_dedup() -> None:
    """Включает pg_trgm и триграммные GIN-индексы по теме/контенту постов.

    Нужно для дедупа перед генерацией (находим, что уже опубликовано). Идемпотентно;
    только Postgres. Индексы держат similarity()-поиск быстрым при росте истории.
    """
    if not _IS_PG:
        return
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_posts_history_topic_trgm "
                "ON posts_history USING gin (topic gin_trgm_ops)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_posts_history_content_trgm "
                "ON posts_history USING gin (content gin_trgm_ops)"
            )
        )


def create_db_and_tables():
    SQLModel.metadata.create_all(engine)  # создаёт недостающие таблицы (вкл. post_metrics)
    _run_migrations()                      # досыпает новые колонки в старые таблицы
    _ensure_trgm_dedup()                   # pg_trgm + индексы для дедупа перед генерацией


# ---------------------------------------------------------------------------
# ДЕДУП АВТОПОСТИНГА ПО РАСПИСАНИЮ
# ---------------------------------------------------------------------------


def claim_schedule_slot(slot: str) -> bool:
    """Атомарно «застолбить» слот расписания.

    True  — слот наш, можно публиковать.
    False — слот уже застолблён (дубль) — публиковать НЕ нужно.

    Атомарность держится на уникальном PK: конкурентная вставка того же slot
    падает с IntegrityError, который мы ловим.
    """
    with Session(engine) as session:
        session.add(ScheduleSlot(slot=slot))
        try:
            session.commit()
            return True
        except IntegrityError:
            session.rollback()
            return False


def release_schedule_slot(slot: str) -> None:
    """Снять отметку слота — при ошибке генерации/отправки, чтобы дать ретрай
    на следующей минуте."""
    with Session(engine) as session:
        obj = session.get(ScheduleSlot, slot)
        if obj:
            session.delete(obj)
            session.commit()


def purge_schedule_slots_before(today_prefix: str) -> None:
    """Удалить слоты прошлых дней (today_prefix = 'YYYY-MM-DD '), чтобы таблица
    не росла бесконечно."""
    with Session(engine) as session:
        rows = session.exec(select(ScheduleSlot)).all()
        for r in rows:
            if not r.slot.startswith(today_prefix):
                session.delete(r)
        session.commit()


# ---------------------------------------------------------------------------
# ДОСТУП К АРЕНДАТОРАМ — каждая функция ОБЯЗАТЕЛЬНО ограничена tenant_id/chat_id.
# ---------------------------------------------------------------------------


def create_tenant(chat_id: str, **profile_fields: object) -> Optional[TenantProfile]:
    """Создаёт арендатора. Возвращает None, если chat_id уже существует."""
    with Session(engine, expire_on_commit=False) as session:
        exists = session.exec(
            select(TenantProfile).where(TenantProfile.chat_id == chat_id)
        ).first()
        if exists:
            return None
        profile = TenantProfile(chat_id=chat_id, **profile_fields)
        session.add(profile)
        session.commit()
        session.refresh(profile)
        return profile


def get_tenant_profile(tenant_id: str) -> Optional[TenantProfile]:
    with Session(engine, expire_on_commit=False) as session:
        return session.exec(
            select(TenantProfile).where(TenantProfile.tenant_id == tenant_id)
        ).first()


def get_tenant_by_chat_id(chat_id: str) -> Optional[TenantProfile]:
    with Session(engine, expire_on_commit=False) as session:
        return session.exec(
            select(TenantProfile).where(TenantProfile.chat_id == chat_id)
        ).first()


def get_active_tenants() -> List[TenantProfile]:
    with Session(engine, expire_on_commit=False) as session:
        return list(
            session.exec(
                select(TenantProfile).where(TenantProfile.active == True)  # noqa: E712
            ).all()
        )


def get_all_tenants() -> List[TenantProfile]:
    """Все арендаторы, включая приостановленных (для админ-управления)."""
    with Session(engine, expire_on_commit=False) as session:
        return list(session.exec(select(TenantProfile)).all())


# --- Владение тенантами (мульти-клиентский доступ) ---------------------------


def get_tenants_for_owner(owner_id: str) -> List[TenantProfile]:
    """Все тенанты клиента (по его Telegram user_id)."""
    with Session(engine, expire_on_commit=False) as session:
        return list(
            session.exec(
                select(TenantProfile).where(TenantProfile.owner_id == owner_id)
            ).all()
        )


def get_active_tenants_for_owner(owner_id: str) -> List[TenantProfile]:
    with Session(engine, expire_on_commit=False) as session:
        return list(
            session.exec(
                select(TenantProfile)
                .where(TenantProfile.owner_id == owner_id)
                .where(TenantProfile.active == True)  # noqa: E712
            ).all()
        )


def is_tenant_owner(tenant_id: str, owner_id: str) -> bool:
    with Session(engine, expire_on_commit=False) as session:
        profile = session.exec(
            select(TenantProfile).where(TenantProfile.tenant_id == tenant_id)
        ).first()
        return bool(profile and profile.owner_id == owner_id)


def count_tenants_for_owner(owner_id: str) -> int:
    """Сколько каналов у клиента — для квоты max_channels (см. tiers.py)."""
    with Session(engine, expire_on_commit=False) as session:
        return len(
            session.exec(
                select(TenantProfile.id).where(TenantProfile.owner_id == owner_id)
            ).all()
        )


def get_owner_tiers(owner_id: str) -> List[str]:
    """Тарифы всех каналов клиента — чтобы вычислить его «лучший» (tiers.best_tier)."""
    with Session(engine, expire_on_commit=False) as session:
        rows = session.exec(
            select(TenantProfile.subscription_tier).where(
                TenantProfile.owner_id == owner_id
            )
        ).all()
        return [r for r in rows if r]


def assign_tenant_owner(tenant_id: str, owner_id: Optional[str]) -> Optional[TenantProfile]:
    """Назначает (или снимает, если owner_id=None) владельца тенанта."""
    return update_tenant_profile(tenant_id, owner_id=owner_id)


# --- Авторизация в веб-панели через Telegram-бота ----------------------------

# Login-токен действителен 5 минут — этого хватает на handshake, дальше истекает.
LOGIN_TOKEN_TTL = timedelta(minutes=5)


def create_login_request(token: Optional[str] = None) -> AuthLogin:
    """Создаёт одноразовый login-handshake (pending). Веб отдаёт deep-link боту."""
    token = token or secrets.token_urlsafe(24)
    with Session(engine, expire_on_commit=False) as session:
        login = AuthLogin(token=token, status="pending")
        session.add(login)
        session.commit()
        session.refresh(login)
        return login


def get_login_request(token: str) -> Optional[AuthLogin]:
    with Session(engine, expire_on_commit=False) as session:
        return session.exec(
            select(AuthLogin).where(AuthLogin.token == token)
        ).first()


def confirm_login_request(
    token: str, telegram_user_id: str, denied: bool = False
) -> bool:
    """Подтверждает/отклоняет login (вызывается ботом).

    Проходит только если запись существует, всё ещё `pending` и не старше 5 минут.
    Возвращает True при успешной смене статуса.
    """
    with Session(engine, expire_on_commit=False) as session:
        login = session.exec(
            select(AuthLogin).where(AuthLogin.token == token)
        ).first()
        if not login or login.status != "pending":
            return False
        created = login.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if _utcnow() - created > LOGIN_TOKEN_TTL:
            return False
        login.telegram_user_id = telegram_user_id
        login.status = "denied" if denied else "confirmed"
        session.add(login)
        session.commit()
        return True


def mark_login_consumed(token: str) -> None:
    """Помечает login использованным после выдачи сессии (одноразовость)."""
    with Session(engine, expire_on_commit=False) as session:
        login = session.exec(
            select(AuthLogin).where(AuthLogin.token == token)
        ).first()
        if login:
            login.status = "consumed"
            session.add(login)
            session.commit()


def create_auth_session(owner_id: str, ttl_days: int = 30) -> str:
    """Создаёт долгоживущую сессию веб-панели, возвращает session-токен."""
    token = secrets.token_urlsafe(32)
    now = _utcnow()
    with Session(engine, expire_on_commit=False) as session:
        auth = AuthSession(
            token=token,
            owner_id=owner_id,
            created_at=now,
            expires_at=now + timedelta(days=ttl_days),
        )
        session.add(auth)
        session.commit()
        return token


def get_session_owner(token: str) -> Optional[str]:
    """owner_id активной сессии или None (если нет/истекла)."""
    with Session(engine, expire_on_commit=False) as session:
        auth = session.exec(
            select(AuthSession).where(AuthSession.token == token)
        ).first()
        if not auth:
            return None
        expires = auth.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if _utcnow() > expires:
            return None
        return auth.owner_id


def delete_auth_session(token: str) -> bool:
    with Session(engine, expire_on_commit=False) as session:
        auth = session.exec(
            select(AuthSession).where(AuthSession.token == token)
        ).first()
        if not auth:
            return False
        session.delete(auth)
        session.commit()
        return True


def remove_tenant(chat_id: str) -> bool:
    """Удаляет арендатора вместе с его правилами (история сохраняется для аудита)."""
    with Session(engine, expire_on_commit=False) as session:
        profile = session.exec(
            select(TenantProfile).where(TenantProfile.chat_id == chat_id)
        ).first()
        if not profile:
            return False
        for rule in session.exec(
            select(TenantRule).where(TenantRule.tenant_id == profile.tenant_id)
        ).all():
            session.delete(rule)
        session.delete(profile)
        session.commit()
        return True


def update_tenant_profile(tenant_id: str, **fields: object) -> Optional[TenantProfile]:
    """Точечное обновление полей профиля (для будущего админ-CRUD)."""
    with Session(engine, expire_on_commit=False) as session:
        profile = session.exec(
            select(TenantProfile).where(TenantProfile.tenant_id == tenant_id)
        ).first()
        if not profile:
            return None
        for key, value in fields.items():
            if hasattr(profile, key):
                setattr(profile, key, value)
        profile.updated_at = _utcnow()
        session.add(profile)
        session.commit()
        session.refresh(profile)
        return profile


# --- Правила -----------------------------------------------------------------


def get_tenant_rules(tenant_id: str, include_system: bool = True) -> List[TenantRule]:
    """Правила арендатора. include_system=False — только пользовательские
    (для показа в UI); по умолчанию все (для генерации)."""
    with Session(engine, expire_on_commit=False) as session:
        stmt = select(TenantRule).where(TenantRule.tenant_id == tenant_id)
        if not include_system:
            stmt = stmt.where(TenantRule.is_system == False)  # noqa: E712
        return list(session.exec(stmt).all())


def add_tenant_rule(
    tenant_id: str, rule_type: str, rule_value: str, is_system: bool = False
) -> TenantRule:
    with Session(engine, expire_on_commit=False) as session:
        rule = TenantRule(
            tenant_id=tenant_id,
            rule_type=rule_type,
            rule_value=rule_value,
            is_system=is_system,
        )
        session.add(rule)
        session.commit()
        session.refresh(rule)
        return rule


def remove_tenant_rule(rule_id: int) -> bool:
    with Session(engine, expire_on_commit=False) as session:
        rule = session.get(TenantRule, rule_id)
        if not rule:
            return False
        session.delete(rule)
        session.commit()
        return True


# --- История постов ----------------------------------------------------------


def get_recent_posts(tenant_id: str, limit: int = 5) -> List[PostHistory]:
    with Session(engine, expire_on_commit=False) as session:
        return list(
            session.exec(
                select(PostHistory)
                .where(PostHistory.tenant_id == tenant_id)
                .order_by(PostHistory.created_at.desc())
                .limit(limit)
            ).all()
        )


# --- Дедуп перед генерацией (что уже опубликовано) ----------------------------
# Цель: до генерации пройтись по постам тенанта, понять, что уже освещено, и не
# повторяться. Слой similarity намеренно изолирован в find_similar_posts(): сейчас
# это pg_trgm (бесплатно, без модели), позже без смены вызывающего кода заменяется
# на векторный поиск (pgvector + эмбеддинги).


def _norm_topic(t: Optional[str]) -> str:
    """Нормализует тему для сравнения: схлопывает пробелы и приводит к нижнему регистру."""
    return " ".join((t or "").split()).lower()


def get_recent_post_topics(tenant_id: str, limit: int = 20) -> List[str]:
    """Нормализованные темы последних постов тенанта (новые→старые, с повторами).

    Порядок сохраняется: вызывающий по индексу определяет «давность» темы для
    выбора наименее недавно использованной (LRU-ротация тем)."""
    with Session(engine, expire_on_commit=False) as session:
        rows = session.exec(
            select(PostHistory.topic)
            .where(PostHistory.tenant_id == tenant_id)
            .order_by(PostHistory.created_at.desc())
            .limit(limit)
        ).all()
    return [_norm_topic(t) for t in rows if t]


def find_similar_posts(
    tenant_id: str, query: str, limit: int = 3, min_sim: float = 0.45
) -> List[dict]:
    """Прошлые посты тенанта, наиболее похожие на `query` (обычно — тему).

    Возвращает [{"content","topic","score"}] по убыванию score (0..1). На Postgres —
    триграммная схожесть pg_trgm по теме (основной сигнал) и началу контента; на
    SQLite — грубый фолбэк по совпадению нормализованной темы. Пусто, если ничего
    не превышает min_sim. Это «что уже опубликовано» для анти-повтора в промпте."""
    q = (query or "").strip()
    if not q:
        return []

    if _IS_PG:
        sql = text(
            """
            SELECT content, topic,
                   GREATEST(
                       similarity(coalesce(topic, ''), :q),
                       similarity(left(content, 400), :q)
                   ) AS score
            FROM posts_history
            WHERE tenant_id = :tid
            ORDER BY score DESC
            LIMIT :lim
            """
        )
        with engine.begin() as conn:
            rows = conn.execute(
                sql, {"q": q, "tid": tenant_id, "lim": limit}
            ).all()
        return [
            {"content": r[0], "topic": r[1], "score": float(r[2])}
            for r in rows
            if r[2] is not None and float(r[2]) >= min_sim
        ]

    # SQLite-фолбэк: совпадение по нормализованной теме (без триграмм).
    nq = _norm_topic(q)
    with Session(engine, expire_on_commit=False) as session:
        recent = session.exec(
            select(PostHistory)
            .where(PostHistory.tenant_id == tenant_id)
            .order_by(PostHistory.created_at.desc())
            .limit(50)
        ).all()
    out = [
        {"content": p.content, "topic": p.topic, "score": 1.0}
        for p in recent
        if _norm_topic(p.topic) == nq
    ]
    return out[:limit]


def save_post(post: PostHistory) -> PostHistory:
    with Session(engine, expire_on_commit=False) as session:
        session.add(post)
        session.commit()
        session.refresh(post)
        return post


def get_published_posts_since(
    tenant_id: str,
    since: datetime,
    exclude_topics: Optional[List[str]] = None,
) -> List[PostHistory]:
    """Опубликованные посты арендатора с message_id, созданные не раньше `since`,
    по возрастанию даты. Для еженедельного «обзора недели»: только реальные посты
    канала (есть message_id). exclude_topics — темы, исключаемые из выборки
    (напр. сами обзоры недели), чтобы дайджест не ссылался сам на себя.
    """
    with Session(engine, expire_on_commit=False) as session:
        stmt = (
            select(PostHistory)
            .where(PostHistory.tenant_id == tenant_id)
            .where(PostHistory.posted == True)  # noqa: E712
            .where(PostHistory.message_id.is_not(None))
            .where(PostHistory.created_at >= since)
            .order_by(PostHistory.created_at)
        )
        if exclude_topics:
            stmt = stmt.where(PostHistory.topic.not_in(exclude_topics))
        return list(session.exec(stmt).all())


def get_reposted_source_keys(tenant_id: str) -> set[str]:
    """Ключи "source_chat_id:source_message_id" уже опубликованных репостов канала.

    Используется в repost-режиме для дедупа: исключаем из кандидатов те посты
    источников, что уже были пересобраны и опубликованы (posted=True).
    """
    with Session(engine, expire_on_commit=False) as session:
        rows = session.exec(
            select(PostHistory.source_chat_id, PostHistory.source_message_id)
            .where(PostHistory.tenant_id == tenant_id)
            .where(PostHistory.posted == True)  # noqa: E712
            .where(PostHistory.source_message_id.is_not(None))
        ).all()
        return {f"{chat}:{mid}" for chat, mid in rows if chat and mid is not None}


# --- Repost-истории (V2: семантический дедуп/кластеризация) -------------------


def save_repost_story(
    tenant_id: str,
    centroid: List[float],
    member_keys: List[str],
    headline: str = "",
) -> RepostStory:
    """Сохраняет опубликованную историю (centroid кластера + ключи всех членов)."""
    with Session(engine, expire_on_commit=False) as session:
        story = RepostStory(
            tenant_id=tenant_id,
            headline=(headline or "")[:200],
            centroid_json=json.dumps(centroid),
            member_keys_json=json.dumps(member_keys),
        )
        session.add(story)
        session.commit()
        session.refresh(story)
        return story


def get_recent_repost_centroids(tenant_id: str, days: int = 14) -> List[List[float]]:
    """Центроиды историй, опубликованных за последние `days` дней (для дедупа)."""
    since = _utcnow() - timedelta(days=days)
    with Session(engine, expire_on_commit=False) as session:
        rows = session.exec(
            select(RepostStory.centroid_json)
            .where(RepostStory.tenant_id == tenant_id)
            .where(RepostStory.published_at >= since)
        ).all()
    out: List[List[float]] = []
    for r in rows:
        try:
            out.append(json.loads(r))
        except (TypeError, ValueError):
            continue
    return out


def get_covered_source_keys(tenant_id: str, days: int = 30) -> set[str]:
    """Все «закрытые» ключи источников: выбранные посты (PostHistory) + ВСЕ члены
    опубликованных историй. Так точный дедуп покрывает не только опубликованный
    пост кластера, но и остальные сообщения о том же событии."""
    keys = get_reposted_source_keys(tenant_id)
    since = _utcnow() - timedelta(days=days)
    with Session(engine, expire_on_commit=False) as session:
        rows = session.exec(
            select(RepostStory.member_keys_json)
            .where(RepostStory.tenant_id == tenant_id)
            .where(RepostStory.published_at >= since)
        ).all()
    for r in rows:
        try:
            keys.update(json.loads(r))
        except (TypeError, ValueError):
            continue
    return keys


# --- Метрики -----------------------------------------------------------------


def get_posts_for_metrics(since: datetime) -> List[PostHistory]:
    """Опубликованные посты с message_id, созданные не раньше `since`.

    Свежие посты ещё набирают просмотры — их и переснимаем периодически.

    Посты удалённых арендаторов отбрасываем на уровне запроса: их профиль удалён
    (remove_tenant хранит историю для аудита), снять метрику по ним всё равно
    нельзя — иначе collect_metrics шумит предупреждениями на каждый осиротевший
    пост при каждом запуске.
    """
    with Session(engine, expire_on_commit=False) as session:
        return list(
            session.exec(
                select(PostHistory)
                .where(PostHistory.posted == True)  # noqa: E712
                .where(PostHistory.message_id.is_not(None))
                .where(PostHistory.created_at >= since)
                .where(
                    PostHistory.tenant_id.in_(select(TenantProfile.tenant_id))
                )
            ).all()
        )


# --- Референс-каналы ---------------------------------------------------------


def add_tenant_source(
    tenant_id: str, source_chat_id: str, posts_indexed: int
) -> TenantSource:
    """Добавляет/обновляет привязку референс-канала к арендатору."""
    with Session(engine, expire_on_commit=False) as session:
        existing = session.exec(
            select(TenantSource)
            .where(TenantSource.tenant_id == tenant_id)
            .where(TenantSource.source_chat_id == source_chat_id)
        ).first()
        if existing:
            existing.posts_indexed = posts_indexed
            session.add(existing)
            session.commit()
            session.refresh(existing)
            return existing
        src = TenantSource(
            tenant_id=tenant_id,
            source_chat_id=source_chat_id,
            posts_indexed=posts_indexed,
        )
        session.add(src)
        session.commit()
        session.refresh(src)
        return src


def get_tenant_sources(tenant_id: str) -> List[TenantSource]:
    """Источники арендатора, отсортированные по приоритету (квоте) — сначала
    самые приоритетные, при равенстве — старые (по id) первыми."""
    with Session(engine, expire_on_commit=False) as session:
        return list(
            session.exec(
                select(TenantSource)
                .where(TenantSource.tenant_id == tenant_id)
                .order_by(TenantSource.priority.desc(), TenantSource.id)
            ).all()
        )


def set_tenant_source_priority(source_id: int, priority: int) -> bool:
    """Задаёт квоту/приоритет источнику. Больше — раньше берётся новость."""
    with Session(engine, expire_on_commit=False) as session:
        src = session.get(TenantSource, source_id)
        if not src:
            return False
        src.priority = priority
        session.add(src)
        session.commit()
        return True


def remove_tenant_source(source_id: int) -> bool:
    with Session(engine, expire_on_commit=False) as session:
        src = session.get(TenantSource, source_id)
        if not src:
            return False
        session.delete(src)
        session.commit()
        return True


def get_top_posts(tenant_id: str, limit: int = 3) -> List[str]:
    """Тексты самых «зашедших» постов канала — образцы для few-shot генерации.

    Reward = views + 3·reactions + 5·forwards (репост/реакция — более сильный
    сигнал, чем просмотр). Берётся последний замер по каждому посту. Если метрик
    ещё нет (cron не отработал / Telethon не настроен) — возвращает [].
    """
    with Session(engine, expire_on_commit=False) as session:
        metrics = session.exec(
            select(PostMetric).where(PostMetric.tenant_id == tenant_id)
        ).all()
        if not metrics:
            return []

        # Последний замер по каждому post_id (метрики растут во времени).
        latest: dict[int, PostMetric] = {}
        for m in metrics:
            cur = latest.get(m.post_id)
            if cur is None or m.captured_at > cur.captured_at:
                latest[m.post_id] = m

        scored = sorted(
            (
                (m.views + 3 * m.reactions + 5 * m.forwards, pid)
                for pid, m in latest.items()
            ),
            reverse=True,
        )
        top_ids = [pid for score, pid in scored[:limit] if score > 0]
        if not top_ids:
            return []

        posts = session.exec(
            select(PostHistory).where(PostHistory.id.in_(top_ids))
        ).all()
        by_id = {p.id: p for p in posts}
        return [by_id[pid].content for pid in top_ids if pid in by_id]


def get_tenant_stats(tenant_id: str, days: int = 30, limit: int = 20) -> dict:
    """Агрегированная статистика канала за период `days`.

    Считает по опубликованным постам, созданным за период. По каждому посту
    берётся ПОСЛЕДНИЙ замер метрик (метрики растут во времени — иначе двойной
    счёт). Если метрик ещё нет, пост учитывается с нулевыми просмотрами.
    """
    since = _utcnow() - timedelta(days=days)
    subs = get_channel_subscribers(tenant_id)  # последний снимок подписчиков (или None)
    subs_summary = {
        "subscribers": subs["subscribers"] if subs else None,
        "subscribers_at": subs["captured_at"] if subs else None,
        "subscribers_delta": subs["delta"] if subs else None,
        "subscribers_series": get_channel_subscriber_series(tenant_id),
    }
    empty = {
        "summary": {
            "total_published": 0,
            "total_views": 0,
            "total_forwards": 0,
            "total_reactions": 0,
            "avg_views_per_post": 0,
            "avg_forwards_per_post": 0,
            "avg_reactions_per_post": 0,
            **subs_summary,
        },
        "recent_posts": [],
        "by_topic": [],
    }

    with Session(engine, expire_on_commit=False) as session:
        posts = list(
            session.exec(
                select(PostHistory)
                .where(PostHistory.tenant_id == tenant_id)
                .where(PostHistory.posted == True)  # noqa: E712
                .where(PostHistory.created_at >= since)
                .order_by(PostHistory.created_at.desc())
            ).all()
        )
        if not posts:
            return empty

        post_ids = [p.id for p in posts]
        metrics = session.exec(
            select(PostMetric)
            .where(PostMetric.tenant_id == tenant_id)
            .where(PostMetric.post_id.in_(post_ids))
        ).all()

        # Последний замер по каждому post_id.
        latest: dict[int, PostMetric] = {}
        for m in metrics:
            cur = latest.get(m.post_id)
            if cur is None or m.captured_at > cur.captured_at:
                latest[m.post_id] = m

        total_published = len(posts)
        total_views = sum(m.views for m in latest.values())
        total_forwards = sum(m.forwards for m in latest.values())
        total_reactions = sum(m.reactions for m in latest.values())
        n = total_published or 1

        recent_posts = []
        for p in posts[:limit]:
            m = latest.get(p.id)
            recent_posts.append(
                {
                    "post_id": p.id,
                    "message_id": p.message_id or 0,
                    "topic": p.topic or "—",
                    "views": m.views if m else 0,
                    "forwards": m.forwards if m else 0,
                    "reactions": m.reactions if m else 0,
                    "posted_at": p.created_at.isoformat() if p.created_at else None,
                    "captured_at": m.captured_at.isoformat() if m else None,
                }
            )

        # Разбивка по темам (avg на пост), сортировка по суммарным просмотрам.
        topics: dict[str, dict] = {}
        for p in posts:
            m = latest.get(p.id)
            acc = topics.setdefault(
                p.topic or "—", {"count": 0, "views": 0, "forwards": 0, "reactions": 0}
            )
            acc["count"] += 1
            if m:
                acc["views"] += m.views
                acc["forwards"] += m.forwards
                acc["reactions"] += m.reactions
        by_topic = [
            {
                "topic": t,
                "post_count": a["count"],
                "avg_views": round(a["views"] / a["count"]),
                "avg_forwards": round(a["forwards"] / a["count"]),
                "avg_reactions": round(a["reactions"] / a["count"]),
            }
            for t, a in sorted(
                topics.items(), key=lambda kv: kv[1]["views"], reverse=True
            )
        ]

        return {
            "summary": {
                "total_published": total_published,
                "total_views": total_views,
                "total_forwards": total_forwards,
                "total_reactions": total_reactions,
                "avg_views_per_post": round(total_views / n),
                "avg_forwards_per_post": round(total_forwards / n),
                "avg_reactions_per_post": round(total_reactions / n),
                **subs_summary,
            },
            "recent_posts": recent_posts,
            "by_topic": by_topic,
        }


def save_metric(
    tenant_id: str,
    post_id: int,
    message_id: int,
    views: int,
    forwards: int,
    reactions: int,
) -> PostMetric:
    with Session(engine, expire_on_commit=False) as session:
        metric = PostMetric(
            tenant_id=tenant_id,
            post_id=post_id,
            message_id=message_id,
            views=views,
            forwards=forwards,
            reactions=reactions,
        )
        session.add(metric)
        session.commit()
        session.refresh(metric)
        return metric


def save_channel_stat(tenant_id: str, subscribers: int) -> ChannelStat:
    """Сохраняет снимок числа подписчиков канала."""
    with Session(engine, expire_on_commit=False) as session:
        stat = ChannelStat(tenant_id=tenant_id, subscribers=subscribers)
        session.add(stat)
        session.commit()
        session.refresh(stat)
        return stat


def get_channel_subscribers(tenant_id: str) -> Optional[dict]:
    """Последний снимок подписчиков + дельта к предыдущему. None, если замеров нет.

    Возвращает {"subscribers", "captured_at", "delta"} — delta = разница с
    предыдущим снимком (None, если он один)."""
    with Session(engine, expire_on_commit=False) as session:
        rows = session.exec(
            select(ChannelStat)
            .where(ChannelStat.tenant_id == tenant_id)
            .order_by(ChannelStat.captured_at.desc())
            .limit(2)
        ).all()
        if not rows:
            return None
        latest = rows[0]
        delta = latest.subscribers - rows[1].subscribers if len(rows) > 1 else None
        return {
            "subscribers": latest.subscribers,
            "captured_at": latest.captured_at.isoformat() if latest.captured_at else None,
            "delta": delta,
        }


def get_channel_subscriber_series(tenant_id: str, limit: int = 30) -> List[int]:
    """Последние `limit` снимков подписчиков (oldest→newest) — для спарклайна роста."""
    with Session(engine, expire_on_commit=False) as session:
        rows = session.exec(
            select(ChannelStat)
            .where(ChannelStat.tenant_id == tenant_id)
            .order_by(ChannelStat.captured_at.desc())
            .limit(limit)
        ).all()
        return [r.subscribers for r in reversed(rows)]
