"""Admin API для фронтенд-панели управления клиентами.

Предоставляет REST эндпоинты для:
- Профилей каналов
- Статистики постов (views, forwards, reactions)
- Истории постов
- Источников (reference channels)
- Правил и расписания
"""
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Tuple
from fastapi import FastAPI, HTTPException, Depends, Query, Header, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncio
import hashlib
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
from database import (
    get_all_tenants,
    get_tenant_profile,
    get_tenant_rules,
    get_tenant_sources,
    get_recent_posts,
    get_tenant_stats,
    get_tenants_for_owner,
    is_tenant_owner,
    update_tenant_profile,
    create_db_and_tables,
    create_login_request,
    get_login_request,
    confirm_login_request,
    mark_login_consumed,
    create_auth_session,
    get_session_owner,
    delete_auth_session,
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


async def verify_admin(authorization: Optional[str] = Header(None)):
    """Проверить админ-токен."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized: missing or invalid Authorization header")
    try:
        token = authorization.split(" ")[1].strip()
    except IndexError:
        raise HTTPException(status_code=401, detail="Unauthorized: malformed Authorization header")

    if token != ADMIN_TOKEN:
        raise HTTPException(
            status_code=403,
            detail=f"Forbidden: token mismatch. Expected: {ADMIN_TOKEN}, Got: {token}"
        )
    return token


async def verify_admin_flexible(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Как verify_admin, но допускает токен в query-параметре ?token=.

    Нужно для <img src=...>, который не умеет слать заголовок Authorization.
    Заголовок имеет приоритет над query.
    """
    candidate: Optional[str] = None
    if authorization and authorization.startswith("Bearer "):
        candidate = authorization.split(" ", 1)[1].strip()
    elif token:
        candidate = token.strip()

    if not candidate:
        raise HTTPException(status_code=401, detail="Unauthorized: missing token")
    if candidate != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden: token mismatch")
    return candidate


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
    creativity_level: float
    factual_strictness: float
    use_rag: bool
    use_references: bool
    avg_post_length: Optional[int]
    content_mode: str = "topic"
    active: bool
    created_at: str


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


class ScheduleSchema(BaseModel):
    mode: str  # "off", "frequency", "times"
    active: bool
    posts_per_day: Optional[int] = None
    schedule_times: Optional[List[str]] = None
    next_post_at: Optional[str] = None


class ScheduleResponse(BaseModel):
    schedule: ScheduleSchema


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
    content_mode: Optional[str] = None
    active: Optional[bool] = None


class SourceAddRequest(BaseModel):
    source_chat_id: str


class SourceAddResponse(BaseModel):
    success: bool
    source_id: int
    posts_indexed: int


class GenerateRequest(BaseModel):
    topic: Optional[str] = None
    context: Optional[str] = None


class GenerateResponse(BaseModel):
    text: str
    topic: str


# ============================================================================
# Endpoints
# ============================================================================

@app.get("/api/admin/tenants", response_model=TenantListResponse, tags=["Tenants"])
async def list_tenants(
    token: str = Depends(verify_admin),
) -> TenantListResponse:
    """Получить список всех каналов."""
    tenants = await asyncio.to_thread(get_all_tenants)
    result = [
        TenantProfileSchema(
            tenant_id=t.tenant_id,
            chat_id=t.chat_id,
            channel_name=t.channel_name or "—",
            tone=t.tone,
            language=t.language,
            writing_style=t.writing_style,
            audience=t.audience,
            topics=t.topics,
            creativity_level=t.creativity_level,
            factual_strictness=t.factual_strictness,
            use_rag=t.use_rag,
            use_references=t.use_references,
            avg_post_length=t.avg_post_length,
            content_mode=getattr(t, "content_mode", None) or "topic",
            active=t.active,
            created_at=t.created_at.isoformat() if t.created_at else None,
        )
        for t in tenants
    ]
    return TenantListResponse(tenants=result)


@app.get("/api/admin/tenants/{tenant_id}/profile", response_model=TenantProfileSchema, tags=["Tenants"])
async def get_profile(
    tenant_id: str,
    token: str = Depends(verify_admin),
) -> TenantProfileSchema:
    """Получить полный профиль канала."""
    profile = await asyncio.to_thread(get_tenant_profile, tenant_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return TenantProfileSchema(
        tenant_id=profile.tenant_id,
        chat_id=profile.chat_id,
        channel_name=profile.channel_name or "—",
        tone=profile.tone,
        language=profile.language,
        writing_style=profile.writing_style,
        audience=profile.audience,
        topics=profile.topics,
        creativity_level=profile.creativity_level,
        factual_strictness=profile.factual_strictness,
        use_rag=profile.use_rag,
        use_references=profile.use_references,
        avg_post_length=profile.avg_post_length,
        content_mode=getattr(profile, "content_mode", None) or "topic",
        active=profile.active,
        created_at=profile.created_at.isoformat() if profile.created_at else None,
    )


@app.get("/api/admin/tenants/{tenant_id}/avatar", tags=["Tenants"])
async def get_tenant_avatar(
    tenant_id: str,
    request: Request,
    _: str = Depends(verify_admin_flexible),
):
    """Отдать аватарку канала (фото из Telegram), проксируя байты с кешем.

    404 — если канал не найден / у него нет доступного фото; фронт рисует инициалы.
    """
    profile = await asyncio.to_thread(get_tenant_profile, tenant_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Tenant not found")

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
    token: str = Depends(verify_admin),
):
    """Получить метрики постов за период из post_metrics / posts_history."""
    return await asyncio.to_thread(get_tenant_stats, tenant_id, days, limit)


@app.get("/api/admin/tenants/{tenant_id}/posts", response_model=PostsListResponse, tags=["Posts"])
async def get_posts(
    tenant_id: str,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    topic: Optional[str] = None,
    token: str = Depends(verify_admin),
) -> PostsListResponse:
    """Получить историю опубликованных постов."""
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
    token: str = Depends(verify_admin),
) -> SourcesListResponse:
    """Получить список источников (reference channels)."""
    sources = await asyncio.to_thread(get_tenant_sources, tenant_id)
    if not sources:
        return SourcesListResponse(sources=[])

    result = [
        SourceSchema(
            id=s.id,
            source_chat_id=s.source_chat_id,
            posts_indexed=s.posts_indexed,
            created_at=s.created_at.isoformat() if s.created_at else None,
        )
        for s in sources
    ]
    return SourcesListResponse(sources=result)


@app.get("/api/admin/tenants/{tenant_id}/rules", response_model=RulesListResponse, tags=["Rules"])
async def get_rules(
    tenant_id: str,
    token: str = Depends(verify_admin),
) -> RulesListResponse:
    """Получить все правила канала."""
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
    token: str = Depends(verify_admin),
) -> RAGStatusResponse:
    """Получить статус RAG и индексирования."""
    profile = await asyncio.to_thread(get_tenant_profile, tenant_id)
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


@app.patch("/api/admin/tenants/{tenant_id}/profile", tags=["Tenants"])
async def update_profile(
    tenant_id: str,
    update: ProfileUpdateRequest,
    token: str = Depends(verify_admin),
):
    """Обновить профиль канала."""
    profile = await asyncio.to_thread(get_tenant_profile, tenant_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Tenant not found")

    update_data = update.dict(exclude_unset=True)
    if "content_mode" in update_data and update_data["content_mode"] not in ("topic", "repost"):
        raise HTTPException(status_code=422, detail="content_mode must be 'topic' or 'repost'")
    await asyncio.to_thread(
        lambda: update_tenant_profile(tenant_id, **update_data)
    )

    return {"success": True, "message": "Profile updated"}


@app.post("/api/admin/tenants/{tenant_id}/generate", response_model=GenerateResponse, tags=["Generate"])
async def generate_post_preview(
    tenant_id: str,
    req: GenerateRequest,
    token: str = Depends(verify_admin),
) -> GenerateResponse:
    """Сгенерировать ТЕКСТ поста под стиль канала (превью). Не публикует."""
    from orchestrator import generate_preview

    profile = await asyncio.to_thread(get_tenant_profile, tenant_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Tenant not found")

    try:
        result = await asyncio.to_thread(
            generate_preview, tenant_id, req.topic, req.context
        )
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return GenerateResponse(text=result["text"], topic=result["topic"])


@app.post("/api/admin/tenants/{tenant_id}/sources", response_model=SourceAddResponse, tags=["Sources"])
async def add_source(
    tenant_id: str,
    req: SourceAddRequest,
    token: str = Depends(verify_admin),
) -> SourceAddResponse:
    """Добавить новый источник (индексирование происходит в фоне)."""
    from database import add_tenant_source

    profile = await asyncio.to_thread(get_tenant_profile, tenant_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Tenant not found")

    # Сохраняем источник в БД
    source = await asyncio.to_thread(
        add_tenant_source, tenant_id, req.source_chat_id, 0
    )

    # TODO: Запустить фоновую задачу на индексирование через Celery/APScheduler

    return SourceAddResponse(
        success=True,
        source_id=source.id or 0,
        posts_indexed=0,  # Будет обновлено после индексирования
    )


# Health check
@app.get("/api/admin/health")
async def health():
    """Проверка здоровья API."""
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
