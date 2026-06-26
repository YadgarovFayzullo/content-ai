"""Внутренний HTTP-API бота для admin-api (action-эндпоинты).

admin-api — тонкий сервис без aiogram/Telethon, поэтому реальные действия
(публикация, массовая публикация, сбор метрик) он делегирует сюда: бот остаётся
единственным владельцем aiogram-Bot и Telethon-сессии (нет дубля сессии и
конфликтов блокировок SQLite).

Сервер поднимается в том же процессе/loop, что и polling (см. main.py), на
порту INTERNAL_API_PORT. Бот на общей bridge-сети content_ai_net; порт 8002
НЕ публикуется наружу — admin-api достукивается до него по имени `bot:8002`
внутри сети.

Аутентификация — общий секрет ADMIN_TOKEN из .env (заголовок X-Internal-Token).
Эндпоинты не предназначены для внешнего доступа.
"""
import asyncio
import logging
import os

from aiogram import Bot
from aiohttp import web

from database import (
    PostHistory,
    get_tenant_profile,
)
from orchestrator import generate_preview
from publisher import send_to_telegram
from repost import produce_content
from bot.scheduler import index_source, scheduled_job
from bot.metrics import collect_metrics
from monitoring import collect_status

INTERNAL_API_PORT = int(os.getenv("INTERNAL_API_PORT", "8002"))
# В контейнере биндим 0.0.0.0 (compose задаёт INTERNAL_API_HOST=0.0.0.0): порт
# 8002 не публикуется наружу, поэтому достижим лишь по имени `bot:8002` внутри
# content_ai_net. Дефолт 127.0.0.1 — безопасный для локального запуска без Docker.
INTERNAL_API_HOST = os.getenv("INTERNAL_API_HOST", "127.0.0.1")
_INTERNAL_TOKEN = os.getenv("ADMIN_TOKEN", "a12345678")


def _check_auth(request: web.Request) -> None:
    """Бросает 401, если общий секрет не совпал."""
    if request.headers.get("X-Internal-Token") != _INTERNAL_TOKEN:
        raise web.HTTPUnauthorized(reason="bad internal token")


async def _publish_text(bot: Bot, profile, text: str, topic: str):
    """Публикует уже готовый текст (без картинки) и возвращает (ok, detail, msg_id)."""
    entry = PostHistory(
        tenant_id=profile.tenant_id,
        topic=topic or "",
        content=text,
        image_path="",
        posted=False,
    )
    content = {"text": text, "image_path": "", "entry": entry}
    ok, detail = await send_to_telegram(bot, content, profile.chat_id)
    return ok, detail, (entry.message_id if ok else None)


async def handle_publish(request: web.Request) -> web.Response:
    """POST /internal/tenants/{tenant_id}/publish

    Body {text?, topic?, context?}:
      - text       → публикуем как есть (без картинки);
      - topic/context (без text) → generate_preview → публикуем текст (без картинки);
      - ничего     → produce_content (полный пайплайн: картинка, repost-режим).
    """
    _check_auth(request)
    tenant_id = request.match_info["tenant_id"]
    body = await request.json() if request.can_read_body else {}
    bot: Bot = request.app["bot"]

    profile = await asyncio.to_thread(get_tenant_profile, tenant_id)
    if not profile:
        raise web.HTTPNotFound(reason="tenant not found")

    text = (body.get("text") or "").strip()
    topic = (body.get("topic") or "").strip()
    context = (body.get("context") or "").strip()
    # Тариф канала может запрещать картинки — admin-api проставляет allow_image.
    # По умолчанию True (обратная совместимость / прямые вызовы).
    allow_image = body.get("allow_image", True)

    try:
        if text:
            ok, detail, msg_id = await _publish_text(bot, profile, text, topic)
        elif topic or context:
            preview = await asyncio.to_thread(
                generate_preview, tenant_id, topic or None, context or None
            )
            ok, detail, msg_id = await _publish_text(
                bot, profile, preview["text"], preview["topic"]
            )
        else:
            # allow_image влияет уже на генерацию: без картинки текст пишется полной
            # длины (до 4096), а не урезается под лимит подписи к фото.
            content = await produce_content(profile, with_image=allow_image)
            if not allow_image:
                # Подстраховка: тариф без image_generation — публикуем только текст.
                content["image_path"] = ""
            ok, detail = await send_to_telegram(bot, content, profile.chat_id)
            msg_id = content["entry"].message_id if ok else None
    except RuntimeError as e:
        return web.json_response({"success": False, "message": str(e)}, status=502)

    return web.json_response(
        {
            "success": ok,
            "channel": profile.chat_id,
            "message_id": msg_id,
            "message": detail,
        }
    )


async def handle_publish_all(request: web.Request) -> web.Response:
    """POST /internal/publish-all → массовая публикация во все активные каналы."""
    _check_auth(request)
    bot: Bot = request.app["bot"]
    results = await scheduled_job(bot)
    return web.json_response(
        {
            "results": [
                {"chat_id": chat_id, "ok": ok, "detail": detail}
                for chat_id, ok, detail in results
            ]
        }
    )


async def handle_collect_metrics(request: web.Request) -> web.Response:
    """POST /internal/tenants/{tenant_id}/collect-metrics → сбор метрик.

    collect_metrics() работает по всем арендаторам сразу (Telethon), отдельного
    per-tenant сбора нет; tenant_id в пути принимаем для совместимости с фронтом.
    """
    _check_auth(request)
    saved = await collect_metrics()
    return web.json_response({"success": True, "saved": saved})


async def handle_channel_posts(request: web.Request) -> web.Response:
    """GET /internal/tenants/{tenant_id}/channel-posts?limit=10

    Последние посты САМОГО канала (живой скрейп через Telethon). Нужно
    аналитическому агенту: даже если наш сервис в этом канале ещё ничего не
    публиковал, агенту есть что «прочитать» — реальные посты канала. Форварды
    отсеиваем (это чужой контент), берём только посты с текстом.
    """
    _check_auth(request)
    tenant_id = request.match_info["tenant_id"]
    try:
        limit = max(1, min(int(request.query.get("limit", "10")), 50))
    except (TypeError, ValueError):
        limit = 10

    profile = await asyncio.to_thread(get_tenant_profile, tenant_id)
    if not profile:
        raise web.HTTPNotFound(reason="tenant not found")

    from bot.scraper import scrape_channel_history

    # Сканируем с запасом (пропускаем форварды/пустые), затем берём limit постов.
    raw = await scrape_channel_history(profile.chat_id, limit=max(limit * 3, 30))
    posts = [
        {"id": p.get("id"), "text": p.get("text"), "date": p.get("date")}
        for p in raw
        if (p.get("text") or "").strip() and not p.get("is_forward")
    ][:limit]
    return web.json_response({"posts": posts})


async def handle_index_source(request: web.Request) -> web.Response:
    """POST /internal/tenants/{tenant_id}/index-source  Body {source}

    Скрейпит и индексирует ОДИН источник (канал или сайт) в RAG. Бот —
    единственный владелец Telethon-сессии и RAG-клиента, поэтому admin-api
    делегирует индексацию сюда (фоном, чтобы добавление источника не ждало
    сетевого скрейпа). Идемпотентно: повторный вызов апсертит без дублей.
    """
    _check_auth(request)
    tenant_id = request.match_info["tenant_id"]
    body = await request.json() if request.can_read_body else {}
    src = (body.get("source") or "").strip()
    if not src:
        raise web.HTTPBadRequest(reason="source required")
    indexed = await index_source(tenant_id, src)
    return web.json_response({"success": True, "indexed": indexed})


async def handle_system_status(request: web.Request) -> web.Response:
    """GET /internal/system-status → снимок здоровья сервера (для админ-панели).

    Бот — единственный контейнер с примонтированным docker.sock, поэтому сбор
    статуса живёт здесь, а admin-api проксирует.
    """
    _check_auth(request)
    snap = await collect_status(with_logs=True)
    return web.json_response(snap)


def create_internal_app(bot: Bot) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app.router.add_post(
        "/internal/tenants/{tenant_id}/publish", handle_publish
    )
    app.router.add_post("/internal/publish-all", handle_publish_all)
    app.router.add_post(
        "/internal/tenants/{tenant_id}/index-source", handle_index_source
    )
    # Живой скрейп последних постов канала — для «чтения» аналитическим агентом.
    app.router.add_get(
        "/internal/tenants/{tenant_id}/channel-posts", handle_channel_posts
    )
    app.router.add_post(
        "/internal/tenants/{tenant_id}/collect-metrics", handle_collect_metrics
    )
    # Глобальный сбор метрик (по всем каналам) — как кнопка в боте. collect_metrics()
    # и так собирает по всем арендаторам, поэтому переиспользуем тот же хэндлер.
    app.router.add_post("/internal/collect-metrics", handle_collect_metrics)
    # Мониторинг сервера (контейнеры/ресурсы/логи) — рендерится в админ-панели.
    app.router.add_get("/internal/system-status", handle_system_status)
    return app


async def start_internal_api(bot: Bot) -> web.AppRunner:
    """Поднимает внутренний HTTP-сервер рядом с polling (не блокирует loop)."""
    app = create_internal_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=INTERNAL_API_HOST, port=INTERNAL_API_PORT)
    await site.start()
    logging.info("Internal API ishga tushdi: %s:%d", INTERNAL_API_HOST, INTERNAL_API_PORT)
    return runner
