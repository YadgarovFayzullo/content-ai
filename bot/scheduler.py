"""Автопостинг по расписанию (per-tenant).

Архитектура — «минутный тик»: раз в минуту проверяем, у каких активных каналов
текущее время (в TZ) совпадает с их запланированным временем поста, и публикуем.
Два режима на канал (TenantProfile.schedule_mode):
  frequency — posts_per_day раз в день, равномерно по окну POST_WINDOW;
  times     — явные времена из post_times.
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import List, Tuple
from zoneinfo import ZoneInfo

from aiogram import Bot

from bot import rag_client
from bot.config import SCRAPE_HISTORY_LIMIT
from bot.scraper import scrape_channel_history
from database import (
    TenantProfile,
    add_tenant_source,
    get_active_tenants,
    get_all_tenants,
    get_tenant_sources,
)
from publisher import send_to_telegram
from repost import produce_content

# Часовой пояс расписания (по умолчанию Ташкент). Fallback на UTC+5, если в образе
# нет базы tzdata.
try:
    TZ = ZoneInfo(os.getenv("TZ_NAME", "Asia/Tashkent"))
except Exception:
    TZ = timezone(timedelta(hours=5))

# Окно для режима frequency: посты равномерно с 09:00 до 21:00.
POST_WINDOW_START = 9 * 60
POST_WINDOW_END = 21 * 60


def tenant_post_times(profile: TenantProfile) -> List[str]:
    """Список времён "HH:MM", когда канал должен постить сегодня."""
    mode = profile.schedule_mode or "off"

    if mode == "frequency" and profile.posts_per_day > 0:
        n = min(profile.posts_per_day, 24)
        if n == 1:
            mins = [(POST_WINDOW_START + POST_WINDOW_END) // 2]
        else:
            step = (POST_WINDOW_END - POST_WINDOW_START) / (n - 1)
            mins = [round(POST_WINDOW_START + step * i) for i in range(n)]
        return [f"{m // 60:02d}:{m % 60:02d}" for m in mins]

    if mode == "times" and profile.post_times:
        return [t.strip() for t in profile.post_times.split(",") if t.strip()]

    return []


async def schedule_tick(bot: Bot) -> None:
    """Раз в минуту: публикует в каналы, у которых сейчас запланирован пост."""
    now_hhmm = datetime.now(TZ).strftime("%H:%M")
    tenants = await asyncio.to_thread(get_active_tenants)
    for profile in tenants:
        if (profile.schedule_mode or "off") == "off":
            continue
        if now_hhmm not in tenant_post_times(profile):
            continue
        try:
            content = await produce_content(profile)
        except Exception as e:
            logging.error(f"Jadval generatsiya xatosi ({profile.chat_id}): {e}")
            continue
        ok, detail = await send_to_telegram(bot, content, profile.chat_id)
        logging.info(f"Jadval post ({profile.chat_id}) {now_hhmm}: ok={ok} {detail}")
        await asyncio.sleep(2)


async def reindex_references() -> None:
    """Раз в сутки заново скрейпит все референс-каналы всех арендаторов и
    переиндексирует их в RAG. Точка-id = uuid5(tenant:post_id) — повторная
    индексация апсертит существующие посты (без дублей) и добавляет новые,
    появившиеся со дня прошлого скрейпа. Так пул фактов растёт сам, а не
    «застывает» на снимке момента добавления канала.
    """
    tenants = await asyncio.to_thread(get_all_tenants)
    total_sources = total_indexed = 0
    for profile in tenants:
        sources = await asyncio.to_thread(get_tenant_sources, profile.tenant_id)
        for s in sources:
            src = s.source_chat_id
            posts = await scrape_channel_history(src, limit=SCRAPE_HISTORY_LIMIT)
            if not posts:
                logging.warning(f"Re-skreyp bo'sh ({src} / {profile.chat_id})")
                continue
            for p in posts:
                p["id"] = f"{src}:{p['id']}"
            indexed = await rag_client.index_posts(
                profile.tenant_id, posts, is_reference=True
            )
            if indexed:
                await asyncio.to_thread(
                    add_tenant_source, profile.tenant_id, src, indexed
                )
                total_sources += 1
                total_indexed += indexed
            await asyncio.sleep(2)  # бережём rate-limit Telethon
    logging.info(
        f"Referenslar yangilandi: {total_sources} ta manba, {total_indexed} ta post"
    )


async def scheduled_job(bot: Bot) -> List[Tuple[str, bool, str]]:
    """Разовая публикация во все активные каналы (ручной триггер/совместимость)."""
    results: List[Tuple[str, bool, str]] = []
    tenants = await asyncio.to_thread(get_active_tenants)
    for profile in tenants:
        chat_id = profile.chat_id
        try:
            content = await produce_content(profile)
        except Exception as e:
            logging.error(f"Generatsiya xatosi ({chat_id}): {e}")
            results.append((chat_id, False, f"Generatsiya: {e}"))
            continue
        ok, detail = await send_to_telegram(bot, content, chat_id)
        results.append((chat_id, ok, detail))
        await asyncio.sleep(5)
    return results
