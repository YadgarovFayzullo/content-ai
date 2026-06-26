"""Admin API для фронтенд-панели управления клиентами.

Предоставляет REST эндпоинты для:
- Профилей каналов
- Статистики постов (views, forwards, reactions)
- Истории постов
- Источников (reference channels)
- Правил и расписания
"""
from datetime import datetime, timedelta, timezone, date
from typing import Optional, List, Tuple
from zoneinfo import ZoneInfo
from fastapi import FastAPI, HTTPException, Depends, Query, Header, Request, Response, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncio
import hashlib
import logging
import sys
import os
import time
import httpx

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (
    API_TITLE,
    API_VERSION,
    ADMIN_TOKEN,
    ADMIN_ID,
    CORS_CONFIG,
    RAG_URL,
)

# Токен бота читаем напрямую из окружения (имя переменной в .env —
# TELEGRAM_BOT_TOKEN; BOT_TOKEN оставлен как запасной вариант). Так не зависим
# от того, как именно назван токен в config.py конкретного окружения.
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN", "")

# @username бота — нужен, чтобы построить deep-link авторизации клиента в панель
# (https://t.me/<username>?start=auth_<token>). Если не задан — отдаём только токен.
BOT_USERNAME = os.getenv("BOT_USERNAME", "").lstrip("@")

# Внутренний API бота (publish / publish-all / collect-metrics). Бот в той же
# bridge-сети content_ai_net — ходим по имени сервиса `bot:8002`. Общий секрет —
# ADMIN_TOKEN из .env (заголовок X-Internal-Token).
INTERNAL_BOT_URL = os.getenv("INTERNAL_BOT_URL", "http://bot:8002")
from database import (
    TenantProfile,
    get_all_tenants,
    get_tenant_profile,
    get_tenant_rules,
    add_tenant_rule,
    remove_tenant_rule,
    get_tenant_sources,
    get_recent_posts,
    get_published_posts_since,
    get_schedule_plan,
    replace_schedule_plan,
    get_last_post_times,
    get_tenant_stats,
    get_tenants_for_owner,
    count_tenants_for_owner,
    get_owner_tiers,
    is_tenant_owner,
    assign_tenant_owner,
    update_tenant_profile,
    create_tenant,
    remove_tenant,
    create_db_and_tables,
    create_login_request,
    get_login_request,
    confirm_login_request,
    mark_login_consumed,
    create_auth_session,
    get_session_owner,
    delete_auth_session,
    create_chat_session,
    list_chat_sessions,
    get_chat_session,
    get_chat_messages,
    add_chat_message,
    rename_chat_session,
    delete_chat_session,
)
from tiers import (
    TIER_LIMITS,
    BASIC_ANALYTICS_MAX_DAYS,
    allows,
    best_tier,
    limit_of,
    limits_for,
    normalize_tier,
    required_tier_for,
    within_limit,
)

app = FastAPI(title=API_TITLE, version=API_VERSION)

# Initialize database on startup
@app.on_event("startup")
async def startup_event():
    """Create database tables if they don't exist."""
    await asyncio.to_thread(create_db_and_tables)


@app.on_event("shutdown")
async def shutdown_event():
    """Закрыть общий httpx-клиент, если он был открыт."""
    global _avatar_client
    if _avatar_client is not None:
        await _avatar_client.aclose()
        _avatar_client = None

# Enable CORS
app.add_middleware(CORSMiddleware, **CORS_CONFIG)


# ============================================================================
# Channel avatars (proxy + cache)
# ============================================================================
# Аватарки каналов берутся из Telegram через бота и проксируются клиенту, чтобы
# BOT_TOKEN не покидал сервер, а временный file_path из getFile не утекал во фронт.

# Telegram Bot API дёргаем напрямую по HTTP через httpx — так не тащим aiogram
# в образ admin-api. Один общий клиент на процесс (создавать на запрос = дорого).
_TG_API = "https://api.telegram.org"
_avatar_client: Optional[httpx.AsyncClient] = None

# chat_id -> (expires_at_epoch, image_bytes | None, etag). None = у канала нет
# доступного фото (приватный / без аватара) — кешируем, чтобы не дёргать Telegram.
_avatar_cache: dict[str, Tuple[float, Optional[bytes], str]] = {}
_AVATAR_TTL_SECONDS = 24 * 60 * 60  # аватарки меняются крайне редко


def _get_avatar_client() -> httpx.AsyncClient:
    global _avatar_client
    if _avatar_client is None:
        _avatar_client = httpx.AsyncClient(timeout=10.0)
    return _avatar_client


async def _fetch_avatar_bytes(chat_id: str) -> Tuple[Optional[bytes], str]:
    """Скачать фото канала через Telegram Bot API. Возвращает (bytes | None, etag)."""
    if not BOT_TOKEN:
        return None, ""
    client = _get_avatar_client()
    try:
        r = await client.get(f"{_TG_API}/bot{BOT_TOKEN}/getChat", params={"chat_id": chat_id})
        r.raise_for_status()
        photo = (r.json().get("result") or {}).get("photo")
        file_id = photo.get("big_file_id") if photo else None
        if not file_id:
            return None, ""

        rf = await client.get(f"{_TG_API}/bot{BOT_TOKEN}/getFile", params={"file_id": file_id})
        rf.raise_for_status()
        file_path = (rf.json().get("result") or {}).get("file_path")
        if not file_path:
            return None, ""

        rb = await client.get(f"{_TG_API}/file/bot{BOT_TOKEN}/{file_path}")
        rb.raise_for_status()
        data = rb.content
    except Exception:
        return None, ""
    if not data:
        return None, ""
    etag = '"' + hashlib.sha1(data).hexdigest() + '"'
    return data, etag


# ============================================================================
# Аутентификация и доступ (restrict mode)
# ============================================================================
# Два типа «принципала» (кто делает запрос):
#   • супер-админ — статический ADMIN_TOKEN ИЛИ сессия владельца с owner_id==ADMIN_ID.
#     Видит и меняет ВСЁ, тарифные лимиты на него не распространяются.
#   • клиент — сессия веб-панели (выдаётся после Telegram-handshake). Видит и меняет
#     ТОЛЬКО свои каналы (owner_id), и в пределах лимитов тарифа канала (tiers.py).
# Токен (статический админский или сессионный) передаётся как `Authorization: Bearer`.


class Principal:
    """Кто выполняет запрос: супер-админ (видит всё) или клиент (только свои каналы)."""

    def __init__(self, owner_id: Optional[str], is_super: bool):
        self.owner_id = owner_id  # Telegram user_id клиента; None — статический супер-токен
        self.is_super = is_super


def _principal_for_token(token: str, owner_id: Optional[str]) -> Principal:
    """owner_id уже резолвнут из сессии (или None для статического токена)."""
    is_super = token == ADMIN_TOKEN or (owner_id is not None and owner_id == ADMIN_ID)
    return Principal(owner_id=None if token == ADMIN_TOKEN else owner_id, is_super=is_super)


async def _resolve_principal(candidate: Optional[str]) -> Principal:
    """Превращает Bearer-токен в Principal: статический супер-токен или сессию клиента."""
    if not candidate:
        raise HTTPException(status_code=401, detail="Unauthorized: missing token")
    if candidate == ADMIN_TOKEN:
        return Principal(owner_id=None, is_super=True)
    owner_id = await asyncio.to_thread(get_session_owner, candidate)
    if not owner_id:
        raise HTTPException(status_code=401, detail="Unauthorized: invalid or expired session")
    return _principal_for_token(candidate, owner_id)


async def get_principal(authorization: Optional[str] = Header(None)) -> Principal:
    """Зависимость авторизации для всех защищённых эндпоинтов."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized: missing or invalid Authorization header")
    return await _resolve_principal(authorization.split(" ", 1)[1].strip())


async def get_principal_flexible(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
) -> Principal:
    """Как get_principal, но допускает токен в ?token= (для <img src=...>, который
    не умеет слать заголовок Authorization). Заголовок имеет приоритет над query.
    """
    candidate: Optional[str] = None
    if authorization and authorization.startswith("Bearer "):
        candidate = authorization.split(" ", 1)[1].strip()
    elif token:
        candidate = token.strip()
    return await _resolve_principal(candidate)


async def _require_tenant(principal: Principal, tenant_id: str) -> TenantProfile:
    """Возвращает профиль канала, проверив доступ: 404 если нет, 403 если чужой."""
    profile = await asyncio.to_thread(get_tenant_profile, tenant_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Tenant not found")
    if not principal.is_super and profile.owner_id != principal.owner_id:
        raise HTTPException(status_code=403, detail="Forbidden: not your channel")
    return profile


def _require_super(principal: Principal) -> None:
    """403, если принципал не супер-админ (для глобальных/админских операций)."""
    if not principal.is_super:
        raise HTTPException(status_code=403, detail="Forbidden: super-admin only")


def _tier_feature_error(feature: str, tier: str) -> None:
    """403 «фича недоступна на тарифе» со структурой для апселла на фронте."""
    raise HTTPException(
        status_code=403,
        detail={
            "error": "tier_restricted",
            "feature": feature,
            "current_tier": normalize_tier(tier),
            "required_tier": required_tier_for(feature),
            "message": f"Feature '{feature}' is not available on the '{normalize_tier(tier)}' plan",
        },
    )


def _tier_quota_error(limit_key: str, current: int, maximum: int, tier: str) -> None:
    """403 «квота тарифа исчерпана» со структурой для апселла на фронте."""
    raise HTTPException(
        status_code=403,
        detail={
            "error": "tier_quota_exceeded",
            "limit": limit_key,
            "max": maximum,
            "current": current,
            "current_tier": normalize_tier(tier),
            "required_tier": required_tier_for(limit_key),
            "message": f"'{limit_key}' limit reached for the '{normalize_tier(tier)}' plan",
        },
    )


# ============================================================================
# Schemas (Pydantic models)
# ============================================================================

class TenantProfileSchema(BaseModel):
    tenant_id: str
    chat_id: str
    channel_name: str
    tone: Optional[str]
    language: str
    writing_style: Optional[str]
    audience: Optional[str]
    topics: Optional[str]
    post_template: Optional[str] = None
    cta: Optional[str] = None
    creativity_level: float
    factual_strictness: float
    use_rag: bool
    use_references: bool
    use_emoji: bool = True
    use_hashtags: bool = False
    avg_post_length: Optional[int]
    content_mode: str = "topic"
    image_mode: str = "ai"
    active: bool
    created_at: str
    # Время последнего опубликованного (неудалённого) поста — для колонки
    # «Последний пост». None, если канал ещё ничего не публиковал. Не путать с
    # created_at (дата подключения канала).
    last_post_at: Optional[str] = None
    # Владелец (Telegram user_id) и тариф — для restrict mode. capabilities —
    # развёрнутые лимиты тарифа (tiers.py), чтобы фронт сразу знал, что показывать.
    owner_id: Optional[str] = None
    subscription_tier: str = "starter"
    schedule_mode: str = "off"
    posts_per_day: int = 0
    # Фронт ожидает массив schedule_times (а PATCH принимает post_times строкой).
    schedule_times: List[str] = []
    capabilities: dict = {}


def _tenant_schema(
    profile: TenantProfile, last_post_at: Optional[datetime] = None
) -> TenantProfileSchema:
    """Собирает TenantProfileSchema из ORM-профиля (одно место — не дублировать).

    last_post_at — время последнего поста (вычисляется вызывающим батчем для
    списка, чтобы не делать N+1); None → канал ещё не публиковал."""
    tier = normalize_tier(getattr(profile, "subscription_tier", None))
    return TenantProfileSchema(
        tenant_id=profile.tenant_id,
        chat_id=profile.chat_id,
        channel_name=profile.channel_name or "—",
        tone=profile.tone,
        language=profile.language,
        writing_style=profile.writing_style,
        audience=profile.audience,
        topics=profile.topics,
        post_template=getattr(profile, "post_template", None),
        cta=getattr(profile, "cta", None),
        creativity_level=profile.creativity_level,
        factual_strictness=profile.factual_strictness,
        use_rag=profile.use_rag,
        use_references=profile.use_references,
        use_emoji=getattr(profile, "use_emoji", True),
        use_hashtags=getattr(profile, "use_hashtags", False),
        avg_post_length=profile.avg_post_length,
        content_mode=getattr(profile, "content_mode", None) or "topic",
        image_mode=getattr(profile, "image_mode", None) or "ai",
        active=profile.active,
        created_at=profile.created_at.isoformat() if profile.created_at else None,
        last_post_at=last_post_at.isoformat() if last_post_at else None,
        owner_id=profile.owner_id,
        subscription_tier=tier,
        schedule_mode=getattr(profile, "schedule_mode", None) or "off",
        posts_per_day=getattr(profile, "posts_per_day", 0) or 0,
        schedule_times=[
            t.strip()
            for t in (getattr(profile, "post_times", None) or "").split(",")
            if t.strip()
        ],
        capabilities=limits_for(tier),
    )


class TenantListResponse(BaseModel):
    tenants: List[TenantProfileSchema]


class PostMetricsSchema(BaseModel):
    post_id: int
    message_id: int
    topic: str
    views: int
    forwards: int
    reactions: int
    posted_at: str
    captured_at: str


class StatsSummarySchema(BaseModel):
    total_published: int
    total_views: int
    total_forwards: int
    total_reactions: int
    avg_views_per_post: float
    avg_forwards_per_post: float
    avg_reactions_per_post: float


class PostDetailSchema(BaseModel):
    id: int
    tenant_id: str
    topic: str
    content: str
    image_path: Optional[str]
    posted: bool
    message_id: Optional[int]
    created_at: str
    metrics: Optional[PostMetricsSchema] = None


class PostsListResponse(BaseModel):
    total: int
    posts: List[PostDetailSchema]


class SourceSchema(BaseModel):
    id: int
    source_chat_id: str
    posts_indexed: int
    # Квота/приоритет источника: чем больше — тем раньше из него берётся новость
    # в repost-режиме (при равной свежести). 0 — обычный приоритет.
    priority: int
    created_at: str
    last_indexed_at: Optional[str] = None


class SourcesListResponse(BaseModel):
    sources: List[SourceSchema]


class RuleSchema(BaseModel):
    id: int
    rule_type: str
    rule_value: str
    created_at: str


class RulesListResponse(BaseModel):
    rules: List[RuleSchema]


class RuleCreateRequest(BaseModel):
    # forbidden_topic | required_hashtag | formatting | length_limit | stylistic
    rule_type: str
    rule_value: str


class ScheduleSchema(BaseModel):
    mode: str  # "off", "frequency", "times"
    active: bool
    posts_per_day: Optional[int] = None
    schedule_times: Optional[List[str]] = None
    next_post_at: Optional[str] = None


class ScheduleResponse(BaseModel):
    schedule: ScheduleSchema


# --- Недельная сетка расписания (weekly grid) -------------------------------

class WeekdaySlotSchema(BaseModel):
    weekday: int            # 0=Пн … 6=Вс
    time: str               # "HH:MM"
    content_type: str       # "topic" | "repost"
    enabled: bool = True


class SchedulePlanResponse(BaseModel):
    slots: List[WeekdaySlotSchema]


class SchedulePlanUpdateRequest(BaseModel):
    slots: List[WeekdaySlotSchema]


# --- Календарь (проекция плана + опубликованные посты) ----------------------

class CalendarPlannedSchema(BaseModel):
    time: str
    content_type: str       # "topic" | "repost"


class CalendarPublishedSchema(BaseModel):
    time: str
    content_type: str       # "topic" | "repost"
    post_id: Optional[int] = None
    title: str


class CalendarDaySchema(BaseModel):
    date: str               # "YYYY-MM-DD"
    planned: List[CalendarPlannedSchema] = []
    published: List[CalendarPublishedSchema] = []


class CalendarResponse(BaseModel):
    days: List[CalendarDaySchema]


class CalendarChannelSchema(BaseModel):
    tenant_id: str
    channel_name: str
    chat_id: str
    days: List[CalendarDaySchema]


class GlobalCalendarResponse(BaseModel):
    channels: List[CalendarChannelSchema]


class RAGHealthSchema(BaseModel):
    qdrant_connection: str  # "ok", "error", "unknown"
    ollama_embeddings: str
    avg_search_latency_ms: Optional[int] = None


class RAGStatusResponse(BaseModel):
    rag_enabled: bool
    references_enabled: bool
    sources_count: int
    total_posts_indexed: int
    last_reindex_at: Optional[str] = None
    rag_health: RAGHealthSchema


class ProfileUpdateRequest(BaseModel):
    tone: Optional[str] = None
    creativity_level: Optional[float] = None
    factual_strictness: Optional[float] = None
    topics: Optional[str] = None
    use_references: Optional[bool] = None
    use_rag: Optional[bool] = None
    use_emoji: Optional[bool] = None
    use_hashtags: Optional[bool] = None
    content_mode: Optional[str] = None
    image_mode: Optional[str] = None  # "ai" | "stock" | "none"
    active: Optional[bool] = None
    # Макс. длина поста (символы). 0 = без жёсткого лимита (дефолтная структура).
    # Это НАСТРОЙКА владельца, не авто-замер: при онбординге канал кладёт сюда
    # медиану длины постов, но дальше значение редактируется только владельцем.
    avg_post_length: Optional[int] = None
    # Стиль/рубрика контента (topic-режим). post_template — шаблон/рубрика поста
    # (напр. «ТОП-5 фактов о стране»), движок следует ему дословно.
    writing_style: Optional[str] = None
    post_template: Optional[str] = None
    cta: Optional[str] = None
    language: Optional[str] = None
    audience: Optional[str] = None
    # Авто-расписание (гейтится тарифом: scheduling + max_posts_per_day).
    schedule_mode: Optional[str] = None  # "off" | "frequency" | "times"
    posts_per_day: Optional[int] = None
    post_times: Optional[str] = None


class SourceAddRequest(BaseModel):
    source_chat_id: str
    # Необязательная квота при добавлении (больше — раньше берётся новость).
    priority: int = 0


class SourceAddResponse(BaseModel):
    success: bool
    source_id: int
    posts_indexed: int


class SourcePriorityRequest(BaseModel):
    priority: int


class GenerateRequest(BaseModel):
    topic: Optional[str] = None
    context: Optional[str] = None


class GenerateResponse(BaseModel):
    text: str
    topic: str
    # Картинка превью как data-URI (base64) по image_mode канала, либо "" если её
    # не удалось получить (например, AI-режим без бюджета). Фронт показывает как <img src>.
    image: str = ""


class TenantCreateRequest(BaseModel):
    chat_id: str
    channel_name: Optional[str] = None
    language: Optional[str] = None
    tone: Optional[str] = None
    topics: Optional[str] = None
    content_mode: Optional[str] = None
    # Только супер-админ: назначить владельца и тариф при создании. У клиента эти
    # поля игнорируются (owner_id = он сам, tier = его «лучший» текущий тариф).
    owner_id: Optional[str] = None
    subscription_tier: Optional[str] = None


class PublishRequest(BaseModel):
    text: Optional[str] = None
    topic: Optional[str] = None
    context: Optional[str] = None


class TierUpdateRequest(BaseModel):
    subscription_tier: str  # starter | pro | premium


class OwnerAssignRequest(BaseModel):
    owner_id: Optional[str] = None  # None — снять владельца (станет «ничей»)


# ============================================================================
# Endpoints
# ============================================================================

# ----------------------------------------------------------------------------
# Авторизация клиента в веб-панель (Telegram deep-link handshake) + текущий юзер
# ----------------------------------------------------------------------------

@app.post("/api/admin/auth/telegram/start", tags=["Auth"])
async def auth_telegram_start():
    """Начать вход: создаём одноразовый login-токен и deep-link на бота.

    Фронт открывает deep_link (или показывает QR), пользователь жмёт Start в боте —
    тот подтверждает токен. Затем фронт поллит /auth/telegram/poll.
    """
    login = await asyncio.to_thread(create_login_request)
    deep_link = (
        f"https://t.me/{BOT_USERNAME}?start=auth_{login.token}" if BOT_USERNAME else None
    )
    return {"token": login.token, "deep_link": deep_link, "expires_in": 300}


@app.get("/api/admin/auth/telegram/poll", tags=["Auth"])
async def auth_telegram_poll(token: str = Query(...)):
    """Опрос статуса login-токена. confirmed → выдаём сессию (одноразово)."""
    login = await asyncio.to_thread(get_login_request, token)
    if not login:
        raise HTTPException(status_code=404, detail="login token not found")
    if login.status == "pending":
        return {"status": "pending"}
    if login.status == "denied":
        raise HTTPException(status_code=403, detail={"status": "denied"})
    if login.status == "consumed":
        raise HTTPException(status_code=409, detail={"status": "consumed"})

    owner_id = login.telegram_user_id or ""
    session_token = await asyncio.to_thread(create_auth_session, owner_id)
    await asyncio.to_thread(mark_login_consumed, token)
    return {
        "status": "confirmed",
        "session_token": session_token,
        "owner_id": owner_id,
        "is_super": owner_id == ADMIN_ID,
    }


@app.post("/api/admin/auth/logout", tags=["Auth"])
async def auth_logout(authorization: Optional[str] = Header(None)):
    """Завершить сессию (удаляет сессионный токен; статический супер-токен игнорим)."""
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ", 1)[1].strip()
        if token != ADMIN_TOKEN:
            await asyncio.to_thread(delete_auth_session, token)
    return {"success": True}


@app.get("/api/admin/me", tags=["Auth"])
async def get_me(principal: Principal = Depends(get_principal)):
    """Кто я: роль, владелец и полная матрица тарифов (для рендера ограничений)."""
    return {
        "owner_id": principal.owner_id,
        "is_super": principal.is_super,
        "tiers": TIER_LIMITS,
    }


@app.get("/api/admin/tenants", response_model=TenantListResponse, tags=["Tenants"])
async def list_tenants(
    principal: Principal = Depends(get_principal),
) -> TenantListResponse:
    """Список каналов: супер-админ — все, клиент — только свои (restrict)."""
    if principal.is_super:
        tenants = await asyncio.to_thread(get_all_tenants)
    else:
        tenants = await asyncio.to_thread(get_tenants_for_owner, principal.owner_id)
    # Время последнего поста — одним батч-запросом на все каналы (без N+1).
    last_posts = await asyncio.to_thread(
        get_last_post_times, [t.tenant_id for t in tenants]
    )
    return TenantListResponse(
        tenants=[_tenant_schema(t, last_posts.get(t.tenant_id)) for t in tenants]
    )


class ProfileResponse(BaseModel):
    profile: TenantProfileSchema


@app.get("/api/admin/tenants/{tenant_id}/profile", response_model=ProfileResponse, tags=["Tenants"])
async def get_profile(
    tenant_id: str,
    principal: Principal = Depends(get_principal),
) -> ProfileResponse:
    """Получить полный профиль канала (только свой/любой для супера).

    Обёртка {"profile": ...} — фронт ожидает именно её (ProfileResponse). Раньше
    отдавали профиль плоско → фронт читал .profile = undefined → форма канала не
    загружала данные с сервера («исчезают после перезагрузки»)."""
    profile = await _require_tenant(principal, tenant_id)
    last_posts = await asyncio.to_thread(get_last_post_times, [tenant_id])
    return ProfileResponse(
        profile=_tenant_schema(profile, last_posts.get(tenant_id))
    )


@app.get("/api/admin/tenants/{tenant_id}/avatar", tags=["Tenants"])
async def get_tenant_avatar(
    tenant_id: str,
    request: Request,
    principal: Principal = Depends(get_principal_flexible),
):
    """Отдать аватарку канала (фото из Telegram), проксируя байты с кешем.

    404 — если канал не найден / у него нет доступного фото; фронт рисует инициалы.
    """
    profile = await _require_tenant(principal, tenant_id)

    chat_id = profile.chat_id
    now = time.time()
    cached = _avatar_cache.get(chat_id)
    if cached and cached[0] > now:
        _, data, etag = cached
    else:
        data, etag = await _fetch_avatar_bytes(chat_id)
        _avatar_cache[chat_id] = (now + _AVATAR_TTL_SECONDS, data, etag)

    if not data:
        raise HTTPException(status_code=404, detail="No avatar")

    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})

    return Response(
        content=data,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "public, max-age=86400",
            "ETag": etag,
        },
    )


@app.get("/api/admin/tenants/{tenant_id}/stats", tags=["Metrics"])
async def get_stats(
    tenant_id: str,
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(20, ge=1, le=100),
    principal: Principal = Depends(get_principal),
):
    """Получить метрики постов за период из post_metrics / posts_history.

    На тарифах с базовой аналитикой (starter) окно ограничено BASIC_ANALYTICS_MAX_DAYS
    и не отдаётся разбивка по темам — это и есть restrict «basic analytics».
    """
    profile = await _require_tenant(principal, tenant_id)
    basic = (
        not principal.is_super
        and limits_for(profile.subscription_tier)["analytics"] == "basic"
    )
    if basic:
        days = min(days, BASIC_ANALYTICS_MAX_DAYS)
    stats = await asyncio.to_thread(get_tenant_stats, tenant_id, days, limit)
    if basic and isinstance(stats, dict):
        stats["by_topic"] = []
        stats["analytics_tier"] = "basic"
        stats["window_days"] = days
    return stats


@app.get("/api/admin/tenants/{tenant_id}/insights", tags=["Metrics"])
async def get_insights(
    tenant_id: str,
    days: int = Query(30, ge=1, le=365),
    tz: Optional[str] = Query(None, description="IANA-таймзона пользователя для пика активности"),
    principal: Principal = Depends(get_principal),
):
    """Детерминированные AI Insights канала (без LLM): оба прокси «% активных»,
    активные часы, контраст тем, прогноз охвата. На basic-аналитике окно урезано
    и тематические инсайты не отдаются — как в /stats. tz — для показа пика
    активности в локальном времени пользователя (иначе UTC)."""
    profile = await _require_tenant(principal, tenant_id)
    from analytics_agent import build_insights

    basic = (
        not principal.is_super
        and limits_for(profile.subscription_tier)["analytics"] == "basic"
    )
    if basic:
        days = min(days, BASIC_ANALYTICS_MAX_DAYS)
    insights = await asyncio.to_thread(build_insights, tenant_id, days, tz)
    if basic:
        insights["by_topic"] = []
        insights["topic_contrast"] = None
        insights["forecast"] = None
        insights["analytics_tier"] = "basic"
    return insights


class AiChatRequest(BaseModel):
    message: str
    # Прошлые реплики диалога [{role: "user"|"assistant", content}] — для контекста.
    history: Optional[List[dict]] = None
    days: Optional[int] = 30
    # IANA-таймзона пользователя из браузера (напр. "Asia/Tashkent") — в ней
    # показываем пик активности вместо UTC. None → UTC.
    tz: Optional[str] = None
    # Сессия беседы (uuid). Если задана — реплики (вопрос + ответ агента)
    # сохраняются в БД под этой беседой. None → разовый ответ без сохранения.
    session_id: Optional[str] = None


def _chat_owner_key(principal: Principal) -> str:
    """Ключ владельца для скоупинга бесед: owner_id клиента или '' для супер-токена."""
    return principal.owner_id or ""


def _chat_session_schema(chat) -> dict:
    return {
        "session_id": chat.session_id,
        "tenant_id": chat.tenant_id,
        "title": chat.title or "",
        "created_at": chat.created_at.isoformat() if chat.created_at else None,
        "updated_at": chat.updated_at.isoformat() if chat.updated_at else None,
    }


async def _require_chat_session(principal: Principal, tenant_id: str, session_id: str):
    """Беседа с проверкой принадлежности каналу и владельцу. 404 если нет/чужая."""
    chat = await asyncio.to_thread(get_chat_session, session_id)
    if (
        not chat
        or chat.tenant_id != tenant_id
        or (not principal.is_super and chat.owner_id != _chat_owner_key(principal))
    ):
        raise HTTPException(status_code=404, detail="Chat session not found")
    return chat


@app.post("/api/admin/tenants/{tenant_id}/ai-chat", tags=["Metrics"])
async def ai_chat_endpoint(
    tenant_id: str,
    req: AiChatRequest,
    principal: Principal = Depends(get_principal),
):
    """ИИ-агент-менеджер: отвечает на вопрос о канале поверх реальных метрик
    (build_insights → Groq). Числа считаются детерминированно, LLM только
    формулирует «что/почему/что делать». Окно урезается на basic-тарифе."""
    profile = await _require_tenant(principal, tenant_id)
    from analytics_agent import chat as analytics_chat

    message = (req.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message required")

    days = req.days or 30
    if (
        not principal.is_super
        and limits_for(profile.subscription_tier)["analytics"] == "basic"
    ):
        days = min(days, BASIC_ANALYTICS_MAX_DAYS)

    # Если задана сессия — она должна существовать и принадлежать клиенту.
    chat = None
    if req.session_id:
        chat = await _require_chat_session(principal, tenant_id, req.session_id)

    # history передаём как есть, но ограничиваем длину — защита от раздувания промпта.
    # Если сессия известна, а history не передан — берём её из БД (источник правды).
    if req.history is not None:
        history = req.history[-10:]
    elif chat is not None:
        stored = await asyncio.to_thread(get_chat_messages, chat.session_id)
        history = [{"role": m.role, "content": m.content} for m in stored][-10:]
    else:
        history = []

    try:
        result = await asyncio.to_thread(
            analytics_chat, tenant_id, message, history, days, req.tz
        )
    except Exception as e:
        logging.error("AI chat error (tenant %s): %s", tenant_id, e)
        raise HTTPException(status_code=502, detail=f"AI agent error: {e}")

    # Персистим обе реплики в беседу (вопрос пользователя + ответ агента).
    if chat is not None:
        reply = result.get("reply") if isinstance(result, dict) else None
        await asyncio.to_thread(add_chat_message, chat.session_id, "user", message)
        if reply:
            await asyncio.to_thread(add_chat_message, chat.session_id, "assistant", reply)
        result = {**result, "session_id": chat.session_id}
    return result


@app.get("/api/admin/tenants/{tenant_id}/chat-sessions", tags=["Metrics"])
async def list_chat_sessions_endpoint(
    tenant_id: str,
    principal: Principal = Depends(get_principal),
):
    """Список сохранённых бесед с ИИ-агентом канала (новые→старые), для сайдбара."""
    await _require_tenant(principal, tenant_id)
    sessions = await asyncio.to_thread(
        list_chat_sessions, tenant_id, _chat_owner_key(principal)
    )
    return {"sessions": [_chat_session_schema(s) for s in sessions]}


@app.post("/api/admin/tenants/{tenant_id}/chat-sessions", tags=["Metrics"])
async def create_chat_session_endpoint(
    tenant_id: str,
    principal: Principal = Depends(get_principal),
):
    """Создаёт новую (пустую) беседу и возвращает её."""
    await _require_tenant(principal, tenant_id)
    chat = await asyncio.to_thread(
        create_chat_session, tenant_id, _chat_owner_key(principal)
    )
    return _chat_session_schema(chat)


@app.get("/api/admin/tenants/{tenant_id}/chat-sessions/{session_id}", tags=["Metrics"])
async def get_chat_session_endpoint(
    tenant_id: str,
    session_id: str,
    principal: Principal = Depends(get_principal),
):
    """Беседа с её сообщениями (для открытия в чате)."""
    chat = await _require_chat_session(principal, tenant_id, session_id)
    messages = await asyncio.to_thread(get_chat_messages, session_id)
    return {
        **_chat_session_schema(chat),
        "messages": [{"role": m.role, "content": m.content} for m in messages],
    }


class ChatSessionRenameRequest(BaseModel):
    title: str


@app.patch("/api/admin/tenants/{tenant_id}/chat-sessions/{session_id}", tags=["Metrics"])
async def rename_chat_session_endpoint(
    tenant_id: str,
    session_id: str,
    req: ChatSessionRenameRequest,
    principal: Principal = Depends(get_principal),
):
    """Переименовать беседу."""
    await _require_chat_session(principal, tenant_id, session_id)
    chat = await asyncio.to_thread(rename_chat_session, session_id, req.title)
    return _chat_session_schema(chat)


@app.delete("/api/admin/tenants/{tenant_id}/chat-sessions/{session_id}", tags=["Metrics"])
async def delete_chat_session_endpoint(
    tenant_id: str,
    session_id: str,
    principal: Principal = Depends(get_principal),
):
    """Удалить беседу вместе с сообщениями."""
    await _require_chat_session(principal, tenant_id, session_id)
    ok = await asyncio.to_thread(delete_chat_session, session_id)
    return {"ok": ok}


@app.get("/api/admin/tenants/{tenant_id}/posts", response_model=PostsListResponse, tags=["Posts"])
async def get_posts(
    tenant_id: str,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    topic: Optional[str] = None,
    principal: Principal = Depends(get_principal),
) -> PostsListResponse:
    """Получить историю опубликованных постов."""
    await _require_tenant(principal, tenant_id)
    posts = await asyncio.to_thread(get_recent_posts, tenant_id, limit * 2)
    if not posts:
        return PostsListResponse(total=0, posts=[])

    result = [
        PostDetailSchema(
            id=p.id,
            tenant_id=p.tenant_id,
            topic=p.topic or "—",
            content=p.content[:200] + "..." if len(p.content) > 200 else p.content,
            image_path=p.image_path,
            posted=p.posted,
            message_id=p.message_id,
            created_at=p.created_at.isoformat() if p.created_at else None,
        )
        for p in posts[offset:offset + limit]
    ]
    return PostsListResponse(total=len(posts), posts=result)


@app.get("/api/admin/tenants/{tenant_id}/sources", response_model=SourcesListResponse, tags=["Sources"])
async def get_sources(
    tenant_id: str,
    principal: Principal = Depends(get_principal),
) -> SourcesListResponse:
    """Получить список источников (reference channels)."""
    await _require_tenant(principal, tenant_id)
    sources = await asyncio.to_thread(get_tenant_sources, tenant_id)
    if not sources:
        return SourcesListResponse(sources=[])

    result = [
        SourceSchema(
            id=s.id,
            source_chat_id=s.source_chat_id,
            posts_indexed=s.posts_indexed,
            priority=s.priority,
            created_at=s.created_at.isoformat() if s.created_at else None,
        )
        for s in sources
    ]
    return SourcesListResponse(sources=result)


@app.get("/api/admin/tenants/{tenant_id}/rules", response_model=RulesListResponse, tags=["Rules"])
async def get_rules(
    tenant_id: str,
    principal: Principal = Depends(get_principal),
) -> RulesListResponse:
    """Получить все правила канала."""
    await _require_tenant(principal, tenant_id)
    rules = await asyncio.to_thread(get_tenant_rules, tenant_id, False)
    if not rules:
        return RulesListResponse(rules=[])

    result = [
        RuleSchema(
            id=r.id,
            rule_type=r.rule_type,
            rule_value=r.rule_value,
            created_at=r.created_at.isoformat() if r.created_at else None,
        )
        for r in rules
    ]
    return RulesListResponse(rules=result)


# Допустимые типы правил (см. TenantRule в database.py). Системные правила
# (is_system) клиент не создаёт и не удаляет — это служебные правила канала.
_RULE_TYPES = {
    "forbidden_topic",
    "required_hashtag",
    "formatting",
    "length_limit",
    "stylistic",
}


@app.post("/api/admin/tenants/{tenant_id}/rules", response_model=RuleSchema, tags=["Rules"])
async def create_rule(
    tenant_id: str,
    req: RuleCreateRequest,
    principal: Principal = Depends(get_principal),
) -> RuleSchema:
    """Добавить пользовательское правило канала (не системное)."""
    await _require_tenant(principal, tenant_id)

    if req.rule_type not in _RULE_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"rule_type must be one of {sorted(_RULE_TYPES)}",
        )
    if not req.rule_value.strip():
        raise HTTPException(status_code=422, detail="rule_value must not be empty")

    rule = await asyncio.to_thread(
        add_tenant_rule, tenant_id, req.rule_type, req.rule_value.strip(), False
    )
    return RuleSchema(
        id=rule.id,
        rule_type=rule.rule_type,
        rule_value=rule.rule_value,
        created_at=rule.created_at.isoformat() if rule.created_at else None,
    )


@app.delete("/api/admin/tenants/{tenant_id}/rules/{rule_id}", tags=["Rules"])
async def delete_rule(
    tenant_id: str,
    rule_id: int,
    principal: Principal = Depends(get_principal),
):
    """Удалить пользовательское правило канала.

    Проверяем, что правило принадлежит этому каналу и не системное — иначе по
    rule_id можно было бы трогать чужие/служебные правила.
    """
    await _require_tenant(principal, tenant_id)

    rules = await asyncio.to_thread(get_tenant_rules, tenant_id, False)
    if not any(r.id == rule_id for r in rules):
        raise HTTPException(status_code=404, detail="Rule not found for this tenant")

    ok = await asyncio.to_thread(remove_tenant_rule, rule_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Rule not found")
    return {"success": True, "message": "Rule removed"}


# ============================================================================
# Расписание (недельная сетка) + календарь
# ============================================================================

# TZ расписания — та же, что у бота-планировщика (по умолчанию Ташкент). Нужна,
# чтобы «сегодня» и время опубликованных постов совпадали с автопостингом.
try:
    _SCHED_TZ = ZoneInfo(os.getenv("TZ_NAME", "Asia/Tashkent"))
except Exception:
    _SCHED_TZ = timezone(timedelta(hours=5))

_CONTENT_TYPES = {"topic", "repost"}
_DATE_FMT = "%Y-%m-%d"


def _parse_range(date_from: str, date_to: str) -> tuple[date, date]:
    """Парсит from/to (YYYY-MM-DD); валидирует диапазон (≤ 92 дня — квартал)."""
    try:
        d0 = datetime.strptime(date_from, _DATE_FMT).date()
        d1 = datetime.strptime(date_to, _DATE_FMT).date()
    except ValueError:
        raise HTTPException(status_code=422, detail="from/to must be YYYY-MM-DD")
    if d1 < d0:
        raise HTTPException(status_code=422, detail="'to' must be >= 'from'")
    if (d1 - d0).days > 92:
        raise HTTPException(status_code=422, detail="range too large (max 92 days)")
    return d0, d1


def _valid_hhmm(t: str) -> bool:
    try:
        h, m = t.split(":")
        return 0 <= int(h) <= 23 and 0 <= int(m) <= 59 and len(m) == 2
    except (ValueError, AttributeError):
        return False


@app.get("/api/admin/tenants/{tenant_id}/schedule", response_model=SchedulePlanResponse, tags=["Schedule"])
async def get_schedule(
    tenant_id: str,
    principal: Principal = Depends(get_principal),
) -> SchedulePlanResponse:
    """Недельная сетка слотов канала (weekly grid)."""
    await _require_tenant(principal, tenant_id)
    slots = await asyncio.to_thread(get_schedule_plan, tenant_id)
    return SchedulePlanResponse(
        slots=[
            WeekdaySlotSchema(
                weekday=s.weekday, time=s.time,
                content_type=s.content_type, enabled=s.enabled,
            )
            for s in slots
        ]
    )


@app.put("/api/admin/tenants/{tenant_id}/schedule", response_model=SchedulePlanResponse, tags=["Schedule"])
async def update_schedule(
    tenant_id: str,
    req: SchedulePlanUpdateRequest,
    principal: Principal = Depends(get_principal),
) -> SchedulePlanResponse:
    """Заменить недельную сетку канала. Гейтится тарифом: scheduling, repost_mode
    (для repost-слотов) и max_posts_per_day на каждый день недели."""
    profile = await _require_tenant(principal, tenant_id)
    tier = getattr(profile, "subscription_tier", None)

    # Валидация формата слотов.
    for s in req.slots:
        if not 0 <= s.weekday <= 6:
            raise HTTPException(status_code=422, detail="weekday must be 0..6 (Mon..Sun)")
        if not _valid_hhmm(s.time):
            raise HTTPException(status_code=422, detail=f"invalid time '{s.time}' (HH:MM)")
        if s.content_type not in _CONTENT_TYPES:
            raise HTTPException(status_code=422, detail="content_type must be 'topic' or 'repost'")

    # Тариф (супер-админ не ограничен).
    if not principal.is_super and req.slots:
        if not allows(tier, "scheduling"):
            _tier_feature_error("scheduling", tier)
        if any(s.content_type == "repost" for s in req.slots) and not allows(tier, "repost_mode"):
            _tier_feature_error("repost_mode", tier)
        maxp = limit_of(tier, "max_posts_per_day")
        per_day: dict[int, int] = {}
        for s in req.slots:
            if s.enabled:
                per_day[s.weekday] = per_day.get(s.weekday, 0) + 1
        worst = max(per_day.values(), default=0)
        if not within_limit(worst, maxp):
            _tier_quota_error("max_posts_per_day", worst, maxp, tier)

    saved = await asyncio.to_thread(
        replace_schedule_plan,
        tenant_id,
        [s.dict() for s in req.slots],
    )
    # Непустая сетка → schedule_mode="weekly"; пустая → "off" (выключить автопостинг).
    await asyncio.to_thread(
        update_tenant_profile, tenant_id,
        schedule_mode="weekly" if req.slots else "off",
    )
    return SchedulePlanResponse(
        slots=[
            WeekdaySlotSchema(
                weekday=s.weekday, time=s.time,
                content_type=s.content_type, enabled=s.enabled,
            )
            for s in saved
        ]
    )


def _project_calendar(
    profile: TenantProfile, slots, posts, d0: date, d1: date, tier: str, is_super: bool
) -> List[CalendarDaySchema]:
    """Собирает дни календаря: план (проекция недельной сетки на будущие даты) +
    опубликованные посты из истории. Возвращает только непустые дни."""
    today = datetime.now(_SCHED_TZ).date()

    # max_posts_per_day режет план так же, как сделает планировщик.
    maxp = limit_of(tier, "max_posts_per_day")
    by_weekday: dict[int, list] = {}
    for s in slots:
        if s.enabled:
            by_weekday.setdefault(s.weekday, []).append(s)
    for wd in by_weekday:
        by_weekday[wd].sort(key=lambda s: s.time)
        if not is_super and not is_unlimited(maxp):
            by_weekday[wd] = by_weekday[wd][:maxp]

    days: dict[str, CalendarDaySchema] = {}

    # План — только сегодня и будущее (прошлый план уже либо опубликован, либо нет).
    weekly_on = (profile.schedule_mode or "off") == "weekly"
    if weekly_on:
        cur = max(d0, today)
        while cur <= d1:
            wd_slots = by_weekday.get(cur.weekday(), [])
            if wd_slots:
                key = cur.strftime(_DATE_FMT)
                days[key] = CalendarDaySchema(
                    date=key,
                    planned=[
                        CalendarPlannedSchema(time=s.time, content_type=s.content_type)
                        for s in wd_slots
                    ],
                )
            cur += timedelta(days=1)

    # Опубликованные — из истории в диапазоне.
    for p in posts:
        if not p.created_at:
            continue
        local = p.created_at.astimezone(_SCHED_TZ) if p.created_at.tzinfo else p.created_at.replace(tzinfo=timezone.utc).astimezone(_SCHED_TZ)
        pd = local.date()
        if pd < d0 or pd > d1:
            continue
        key = pd.strftime(_DATE_FMT)
        day = days.get(key) or CalendarDaySchema(date=key)
        ctype = "repost" if (p.topic or "") == "repost" else "topic"
        title = (p.topic if (p.topic and p.topic != "repost") else (p.content or "")[:48]) or "—"
        day.published.append(
            CalendarPublishedSchema(
                time=local.strftime("%H:%M"),
                content_type=ctype,
                post_id=p.id,
                title=title,
            )
        )
        days[key] = day

    return [days[k] for k in sorted(days)]


@app.get("/api/admin/tenants/{tenant_id}/calendar", response_model=CalendarResponse, tags=["Schedule"])
async def get_tenant_calendar(
    tenant_id: str,
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
    principal: Principal = Depends(get_principal),
) -> CalendarResponse:
    """Календарь канала: запланированные слоты (проекция) + опубликованные посты."""
    profile = await _require_tenant(principal, tenant_id)
    d0, d1 = _parse_range(date_from, date_to)
    slots = await asyncio.to_thread(get_schedule_plan, tenant_id)
    since = datetime.combine(d0, datetime.min.time(), tzinfo=_SCHED_TZ)
    posts = await asyncio.to_thread(get_published_posts_since, tenant_id, since)
    days = _project_calendar(
        profile, slots, posts, d0, d1,
        getattr(profile, "subscription_tier", None), principal.is_super,
    )
    return CalendarResponse(days=days)


@app.get("/api/admin/calendar", response_model=GlobalCalendarResponse, tags=["Schedule"])
async def get_global_calendar(
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
    principal: Principal = Depends(get_principal),
) -> GlobalCalendarResponse:
    """Календарь всех каналов принципала (по каждому каналу — свои дни)."""
    d0, d1 = _parse_range(date_from, date_to)
    if principal.is_super:
        tenants = await asyncio.to_thread(get_all_tenants)
    else:
        tenants = await asyncio.to_thread(get_tenants_for_owner, principal.owner_id)

    since = datetime.combine(d0, datetime.min.time(), tzinfo=_SCHED_TZ)
    channels: List[CalendarChannelSchema] = []
    for profile in tenants:
        slots = await asyncio.to_thread(get_schedule_plan, profile.tenant_id)
        posts = await asyncio.to_thread(get_published_posts_since, profile.tenant_id, since)
        days = _project_calendar(
            profile, slots, posts, d0, d1,
            getattr(profile, "subscription_tier", None), principal.is_super,
        )
        if days:
            channels.append(
                CalendarChannelSchema(
                    tenant_id=profile.tenant_id,
                    channel_name=profile.channel_name or profile.chat_id,
                    chat_id=profile.chat_id,
                    days=days,
                )
            )
    return GlobalCalendarResponse(channels=channels)


def _probe_rag_ready() -> str:
    """Опрашивает RAG-сервис (отдельный контейнер) на готовность Qdrant.

    Возвращает "ok" если /ready отвечает 200, иначе "error".
    """
    import urllib.request

    try:
        with urllib.request.urlopen(f"{RAG_URL}/ready", timeout=3) as resp:
            return "ok" if resp.status == 200 else "error"
    except Exception:
        return "error"


@app.get("/api/admin/tenants/{tenant_id}/rag-status", response_model=RAGStatusResponse, tags=["RAG"])
async def get_rag_status(
    tenant_id: str,
    principal: Principal = Depends(get_principal),
) -> RAGStatusResponse:
    """Получить статус RAG и индексирования."""
    profile = await _require_tenant(principal, tenant_id)
    sources = await asyncio.to_thread(get_tenant_sources, tenant_id)

    total_indexed = sum(s.posts_indexed for s in sources) if sources else 0

    # Реальная проверка RAG-сервиса (отдельный контейнер по RAG_URL).
    qdrant_status = await asyncio.to_thread(_probe_rag_ready)

    return RAGStatusResponse(
        rag_enabled=profile.use_rag if profile else False,
        references_enabled=profile.use_references if profile else True,
        sources_count=len(sources) if sources else 0,
        total_posts_indexed=total_indexed,
        last_reindex_at=None,  # Добавить в БД если нужно
        rag_health=RAGHealthSchema(
            qdrant_connection=qdrant_status,
            ollama_embeddings="ok" if qdrant_status == "ok" else "unknown",
        ),
    )


def _enforce_profile_tier(principal: Principal, tier: str, data: dict) -> None:
    """Проверяет, что запрошенные изменения профиля разрешены тарифом (для клиента).

    Супер-админ не ограничен. Гейтятся: RAG (use_rag/use_references), repost-режим,
    авто-расписание и его частота (max_posts_per_day).
    """
    if principal.is_super:
        return
    if data.get("use_rag") or data.get("use_references"):
        if not allows(tier, "rag"):
            _tier_feature_error("rag", tier)
    if data.get("content_mode") in ("repost", "both") and not allows(tier, "repost_mode"):
        _tier_feature_error("repost_mode", tier)
    if data.get("schedule_mode") and data["schedule_mode"] != "off":
        if not allows(tier, "scheduling"):
            _tier_feature_error("scheduling", tier)
    ppd = data.get("posts_per_day")
    if ppd is not None and ppd > 0:
        if not allows(tier, "scheduling"):
            _tier_feature_error("scheduling", tier)
        maxp = limit_of(tier, "max_posts_per_day")
        if not within_limit(ppd, maxp):
            _tier_quota_error("max_posts_per_day", ppd, maxp, tier)


@app.patch("/api/admin/tenants/{tenant_id}/profile", tags=["Tenants"])
async def update_profile(
    tenant_id: str,
    update: ProfileUpdateRequest,
    principal: Principal = Depends(get_principal),
):
    """Обновить профиль канала (в пределах тарифа канала, см. _enforce_profile_tier)."""
    profile = await _require_tenant(principal, tenant_id)

    update_data = update.dict(exclude_unset=True)
    if "content_mode" in update_data and update_data["content_mode"] not in ("topic", "repost", "both"):
        raise HTTPException(status_code=422, detail="content_mode must be 'topic', 'repost' or 'both'")
    if "schedule_mode" in update_data and update_data["schedule_mode"] not in ("off", "frequency", "times", "weekly"):
        raise HTTPException(status_code=422, detail="schedule_mode must be 'off', 'frequency', 'times' or 'weekly'")
    if "image_mode" in update_data and update_data["image_mode"] not in ("ai", "stock", "none"):
        raise HTTPException(status_code=422, detail="image_mode must be 'ai', 'stock' or 'none'")
    if "avg_post_length" in update_data and update_data["avg_post_length"] is not None:
        # Telegram-пост ≤ 4096 символов; 0 = «без лимита». Зажимаем в диапазон,
        # чтобы случайные/мусорные значения не ломали генерацию.
        update_data["avg_post_length"] = max(0, min(int(update_data["avg_post_length"]), 4096))

    _enforce_profile_tier(principal, profile.subscription_tier, update_data)

    await asyncio.to_thread(
        lambda: update_tenant_profile(tenant_id, **update_data)
    )

    return {"success": True, "message": "Profile updated"}


@app.post("/api/admin/tenants/{tenant_id}/generate", response_model=GenerateResponse, tags=["Generate"])
async def generate_post_preview(
    tenant_id: str,
    req: GenerateRequest,
    principal: Principal = Depends(get_principal),
) -> GenerateResponse:
    """Сгенерировать ТЕКСТ поста под стиль канала (превью). Не публикует."""
    from orchestrator import generate_preview

    await _require_tenant(principal, tenant_id)

    try:
        result = await asyncio.to_thread(
            generate_preview, tenant_id, req.topic, req.context
        )
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return GenerateResponse(
        text=result["text"],
        topic=result["topic"],
        image=result.get("image", ""),
    )


class SuggestTopicsRequest(BaseModel):
    # Текущие (возможно ещё не сохранённые) значения формы — подбор идёт по ним.
    post_template: Optional[str] = None
    writing_style: Optional[str] = None
    audience: Optional[str] = None
    tone: Optional[str] = None
    language: Optional[str] = None
    content_mode: Optional[str] = None
    count: Optional[int] = 12


class SuggestTopicsResponse(BaseModel):
    topics: List[str]


@app.post(
    "/api/admin/tenants/{tenant_id}/suggest-topics",
    response_model=SuggestTopicsResponse,
    tags=["Generate"],
)
async def suggest_topics_endpoint(
    tenant_id: str,
    req: SuggestTopicsRequest,
    principal: Principal = Depends(get_principal),
) -> SuggestTopicsResponse:
    """Подобрать темы (topics) через ИИ по тому, что владелец уже вписал в
    контент-поля (шаблон/стиль/аудитория). Не сохраняет — фронт сам подставляет."""
    from generator import suggest_topics

    await _require_tenant(principal, tenant_id)
    try:
        topics = await asyncio.to_thread(
            suggest_topics,
            post_template=req.post_template or "",
            writing_style=req.writing_style or "",
            audience=req.audience or "",
            tone=req.tone or "",
            language=req.language or "",
            content_mode=req.content_mode or "topic",
            count=min(max(req.count or 12, 1), 30),
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    return SuggestTopicsResponse(topics=topics)


@app.post("/api/admin/tenants/{tenant_id}/sources", response_model=SourceAddResponse, tags=["Sources"])
async def add_source(
    tenant_id: str,
    req: SourceAddRequest,
    background: BackgroundTasks,
    principal: Principal = Depends(get_principal),
) -> SourceAddResponse:
    """Добавить новый источник (индексирование происходит в фоне).

    Число источников на канал ограничено тарифом (max_sources). Уже добавленный
    источник (по source_chat_id) не считается новым — апсерт под квоту не подпадает.
    """
    from database import add_tenant_source, set_tenant_source_priority

    profile = await _require_tenant(principal, tenant_id)

    existing = await asyncio.to_thread(get_tenant_sources, tenant_id)
    already = any(s.source_chat_id == req.source_chat_id for s in existing)
    if not principal.is_super and not already:
        maxs = limit_of(profile.subscription_tier, "max_sources")
        if not within_limit(len(existing) + 1, maxs):
            _tier_quota_error("max_sources", len(existing), maxs, profile.subscription_tier)

    # Сохраняем источник в БД
    source = await asyncio.to_thread(
        add_tenant_source, tenant_id, req.source_chat_id, 0
    )

    # Квота задаётся отдельно (add_tenant_source её не принимает) — только если
    # запросили ненулевой приоритет, чтобы не трогать дефолт без нужды.
    if req.priority and source.id:
        await asyncio.to_thread(set_tenant_source_priority, source.id, req.priority)

    # Запускаем индексацию в фоне (после ответа): бот скрейпит источник и
    # апсертит его в RAG, проставляя posts_indexed. Источник уже в БД, так что
    # даже если бот сейчас недоступен — ночной reindex_references его подхватит.
    background.add_task(_index_source_bg, tenant_id, req.source_chat_id)

    return SourceAddResponse(
        success=True,
        source_id=source.id or 0,
        posts_indexed=0,  # Обновится в фоне; фронт может перезапросить список источников
    )


@app.patch("/api/admin/tenants/{tenant_id}/sources/{source_id}/priority", tags=["Sources"])
async def update_source_priority(
    tenant_id: str,
    source_id: int,
    req: SourcePriorityRequest,
    principal: Principal = Depends(get_principal),
):
    """Задать квоту/приоритет источнику (больше — раньше берётся новость)."""
    from database import set_tenant_source_priority

    await _require_tenant(principal, tenant_id)
    # Проверяем, что источник действительно принадлежит этому каналу — иначе
    # по source_id можно было бы менять чужие источники.
    sources = await asyncio.to_thread(get_tenant_sources, tenant_id)
    if not any(s.id == source_id for s in sources):
        raise HTTPException(status_code=404, detail="Source not found for this tenant")

    ok = await asyncio.to_thread(set_tenant_source_priority, source_id, req.priority)
    if not ok:
        raise HTTPException(status_code=404, detail="Source not found")
    return {"success": True, "priority": req.priority}


@app.delete("/api/admin/tenants/{tenant_id}/sources/{source_id}", tags=["Sources"])
async def delete_source(
    tenant_id: str,
    source_id: int,
    principal: Principal = Depends(get_principal),
):
    """Удалить источник (reference channel) из канала."""
    from database import remove_tenant_source

    await _require_tenant(principal, tenant_id)
    sources = await asyncio.to_thread(get_tenant_sources, tenant_id)
    if not any(s.id == source_id for s in sources):
        raise HTTPException(status_code=404, detail="Source not found for this tenant")

    ok = await asyncio.to_thread(remove_tenant_source, source_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Source not found")
    return {"success": True, "message": "Source removed"}


async def _proxy_to_bot(method: str, path: str, json_body: Optional[dict] = None) -> dict:
    """Делегирует action-запрос внутреннему API бота (единственный владелец
    aiogram-Bot и Telethon). Публикация/генерация может идти долго — таймаут щедрый.
    """
    url = f"{INTERNAL_BOT_URL}{path}"
    headers = {"X-Internal-Token": ADMIN_TOKEN}
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            r = await client.request(method, url, json=json_body, headers=headers)
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=503,
            detail=f"Bot internal API unreachable: {e}",
        )
    if r.status_code >= 400:
        # Пробрасываем тело ошибки бота как есть (там осмысленный message).
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        raise HTTPException(status_code=r.status_code, detail=detail)
    return r.json()


async def _index_source_bg(tenant_id: str, source: str) -> None:
    """Фоновая индексация источника: делегируем боту (он владеет Telethon-сессией
    и RAG-клиентом). Ошибки не пробрасываем — это фон, ночной reindex_references
    всё равно подхватит источник. Просто логируем."""
    try:
        await _proxy_to_bot(
            "POST",
            f"/internal/tenants/{tenant_id}/index-source",
            {"source": source},
        )
    except Exception as e:
        logging.warning("Manba fon indeksatsiyasi xatosi (%s / %s): %s", source, tenant_id, e)


@app.post("/api/admin/tenants", tags=["Tenants"])
async def create_tenant_endpoint(
    req: TenantCreateRequest,
    principal: Principal = Depends(get_principal),
):
    """Создать канал (профиль). Возвращает 409, если он уже есть.

    Клиент: владельцем становится он сам, тариф = его «лучший» текущий, и действует
    квота max_channels (по этому тарифу). Супер-админ: может задать owner_id и тариф.
    """
    # Юзернеймы в Telegram регистронезависимы — нормализуем, как делает бот, чтобы
    # @Chan и @chan не порождали дубль (chat_id уникален и регистрозависим в БД).
    chat_id = req.chat_id.strip()
    if chat_id.startswith("@"):
        chat_id = chat_id.lower()

    fields = req.dict(exclude_unset=True, exclude={"chat_id", "owner_id", "subscription_tier"})
    if "content_mode" in fields and fields["content_mode"] not in ("topic", "repost", "both"):
        raise HTTPException(status_code=422, detail="content_mode must be 'topic', 'repost' or 'both'")

    if principal.is_super:
        # Супер задаёт владельца и тариф явно (или дефолты).
        if req.owner_id is not None:
            fields["owner_id"] = req.owner_id
        fields["subscription_tier"] = normalize_tier(req.subscription_tier)
    else:
        # Клиент: владелец = он сам; тариф наследуется от лучшего из его каналов
        # (новые каналы того же уровня), но не выше квоты на число каналов.
        owner_id = principal.owner_id
        owner_tiers = await asyncio.to_thread(get_owner_tiers, owner_id)
        owner_tier = best_tier(owner_tiers)
        count = await asyncio.to_thread(count_tenants_for_owner, owner_id)
        maxc = limit_of(owner_tier, "max_channels")
        if not within_limit(count + 1, maxc):
            _tier_quota_error("max_channels", count, maxc, owner_tier)
        if fields.get("content_mode") in ("repost", "both") and not allows(owner_tier, "repost_mode"):
            _tier_feature_error("repost_mode", owner_tier)
        fields["owner_id"] = owner_id
        fields["subscription_tier"] = owner_tier

    profile = await asyncio.to_thread(create_tenant, chat_id, **fields)
    if not profile:
        raise HTTPException(status_code=409, detail="Tenant already exists")

    return {"success": True, "tenant": _tenant_schema(profile)}


@app.patch("/api/admin/tenants/{tenant_id}/tier", tags=["Tenants"])
async def update_tenant_tier(
    tenant_id: str,
    req: TierUpdateRequest,
    principal: Principal = Depends(get_principal),
):
    """Сменить тариф канала. Только супер-админ (это биллинговое решение)."""
    _require_super(principal)
    if req.subscription_tier not in TIER_LIMITS:
        raise HTTPException(status_code=422, detail=f"tier must be one of {list(TIER_LIMITS)}")
    profile = await asyncio.to_thread(get_tenant_profile, tenant_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Tenant not found")
    updated = await asyncio.to_thread(
        lambda: update_tenant_profile(tenant_id, subscription_tier=req.subscription_tier)
    )
    return {"success": True, "tenant": _tenant_schema(updated)}


@app.patch("/api/admin/tenants/{tenant_id}/owner", tags=["Tenants"])
async def assign_owner(
    tenant_id: str,
    req: OwnerAssignRequest,
    principal: Principal = Depends(get_principal),
):
    """Назначить/снять владельца канала (привязка к клиенту). Только супер-админ."""
    _require_super(principal)
    profile = await asyncio.to_thread(get_tenant_profile, tenant_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Tenant not found")
    updated = await asyncio.to_thread(assign_tenant_owner, tenant_id, req.owner_id)
    return {"success": True, "tenant": _tenant_schema(updated)}


@app.delete("/api/admin/tenants/{tenant_id}", tags=["Tenants"])
async def delete_tenant_endpoint(
    tenant_id: str,
    principal: Principal = Depends(get_principal),
):
    """Удалить канал (профиль + его RAG-индекс). Клиент — только свой."""
    profile = await _require_tenant(principal, tenant_id)

    removed = await asyncio.to_thread(remove_tenant, profile.chat_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Tenant not found")

    # Чистим RAG-индекс канала (иначе осиротевшие вектора) — best-effort, как в боте.
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            await client.post(f"{RAG_URL}/delete", json={"tenant_id": tenant_id})
    except Exception:
        pass

    return {"success": True}


@app.post("/api/admin/tenants/{tenant_id}/publish", tags=["Publish"])
async def publish_endpoint(
    tenant_id: str,
    req: PublishRequest,
    principal: Principal = Depends(get_principal),
):
    """Опубликовать пост в канал. Делегируется внутреннему API бота.

    Картинки к посту генерятся только если тариф канала это разрешает
    (image_generation) — иначе боту передаём allow_image=false, и он постит без фото.
    """
    profile = await _require_tenant(principal, tenant_id)
    body = req.dict(exclude_unset=True)
    body["allow_image"] = principal.is_super or allows(
        profile.subscription_tier, "image_generation"
    )
    return await _proxy_to_bot(
        "POST", f"/internal/tenants/{tenant_id}/publish", body
    )


@app.post("/api/admin/publish-all", tags=["Publish"])
async def publish_all_endpoint(
    principal: Principal = Depends(get_principal),
):
    """Опубликовать во все активные каналы. Только супер-админ (глобальная операция)."""
    _require_super(principal)
    return await _proxy_to_bot("POST", "/internal/publish-all")


@app.post("/api/admin/tenants/{tenant_id}/collect-metrics", tags=["Metrics"])
async def collect_metrics_endpoint(
    tenant_id: str,
    principal: Principal = Depends(get_principal),
):
    """Запустить сбор метрик (Telethon у бота). Делегируется внутреннему API бота."""
    await _require_tenant(principal, tenant_id)
    return await _proxy_to_bot(
        "POST", f"/internal/tenants/{tenant_id}/collect-metrics"
    )


@app.post("/api/admin/collect-metrics", tags=["Metrics"])
async def collect_metrics_all_endpoint(
    principal: Principal = Depends(get_principal),
):
    """Глобальный сбор метрик по всем каналам — как кнопка в боте. Только супер-админ."""
    _require_super(principal)
    return await _proxy_to_bot("POST", "/internal/collect-metrics")


@app.get("/api/admin/system-status", tags=["System"])
async def system_status_endpoint(
    principal: Principal = Depends(get_principal),
):
    """Снимок здоровья сервера (контейнеры/ресурсы/логи/инциденты). Только
    супер-админ. Сбор делает бот (у него примонтирован docker.sock)."""
    _require_super(principal)
    return await _proxy_to_bot("GET", "/internal/system-status")


# Health check
@app.get("/api/admin/health")
async def health():
    """Проверка здоровья API."""
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
