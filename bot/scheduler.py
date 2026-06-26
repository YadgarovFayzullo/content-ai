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
from bot.scraper import scrape_source
from database import (
    TenantProfile,
    add_tenant_source,
    claim_schedule_slot,
    get_active_tenants,
    get_all_tenants,
    get_schedule_plan,
    get_tenant_sources,
    materialize_weekly_plan,
    purge_schedule_slots_before,
    release_schedule_slot,
)
from publisher import send_to_telegram
from repost import produce_content
from tiers import allows, is_unlimited, limit_of

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
    """Список времён "HH:MM", когда канал должен постить сегодня.

    Уважает тариф (tiers.py): авто-расписание только на тарифах со `scheduling`,
    а частота в режиме frequency ограничена потолком `max_posts_per_day`.
    """
    tier = getattr(profile, "subscription_tier", None)
    if not allows(tier, "scheduling"):
        return []

    mode = profile.schedule_mode or "off"

    if mode == "frequency" and profile.posts_per_day > 0:
        max_ppd = limit_of(tier, "max_posts_per_day")
        ppd = profile.posts_per_day if is_unlimited(max_ppd) else min(profile.posts_per_day, max_ppd)
        n = min(ppd, 24)
        if n == 1:
            mins = [(POST_WINDOW_START + POST_WINDOW_END) // 2]
        else:
            step = (POST_WINDOW_END - POST_WINDOW_START) / (n - 1)
            mins = [round(POST_WINDOW_START + step * i) for i in range(n)]
        return [f"{m // 60:02d}:{m % 60:02d}" for m in mins]

    if mode == "times" and profile.post_times:
        times = [t.strip() for t in profile.post_times.split(",") if t.strip()]
        max_ppd = limit_of(tier, "max_posts_per_day")
        return times if is_unlimited(max_ppd) else times[:max_ppd]

    return []


def tenant_due_slots(profile: TenantProfile, now_hhmm: str, weekday: int):
    """Слоты канала, которые должны опубликоваться прямо сейчас (now_hhmm).

    Возвращает список (time, content_type), где content_type — явный тип слота
    из недельной сетки ("topic"/"repost") или None (= использовать profile.content_mode).

    weekday — день недели по TZ расписания (0=Пн … 6=Вс).

    schedule_mode="weekly" → читаем недельную сетку (SchedulePlanSlot) для текущего
    дня недели; иначе — легаси frequency/times через tenant_post_times (тип = None).
    Тариф уважается: max_posts_per_day режет число слотов за день.
    """
    tier = getattr(profile, "subscription_tier", None)
    if not allows(tier, "scheduling"):
        return []

    mode = profile.schedule_mode or "off"

    # Легаси-режимы (frequency/times) бесшовно мигрируем в недельную сетку:
    # те же времена на все 7 дней, тип = profile.content_mode. После этого канал
    # становится "weekly", и единственный живой источник расписания — сетка.
    if mode in ("frequency", "times"):
        ltimes = tenant_post_times(profile)
        if ltimes:
            materialize_weekly_plan(
                profile.tenant_id, ltimes, profile.content_mode or "topic"
            )
            profile.schedule_mode = "weekly"
            mode = "weekly"
        else:
            return []

    if mode == "weekly":
        slots = get_schedule_plan(profile.tenant_id)
        day_slots = [
            s for s in slots if s.weekday == weekday and s.enabled
        ]
        # Лимит постов в день по тарифу (по порядку времени).
        max_ppd = limit_of(tier, "max_posts_per_day")
        day_slots.sort(key=lambda s: s.time)
        if not is_unlimited(max_ppd):
            day_slots = day_slots[:max_ppd]
        return [(s.time, s.content_type) for s in day_slots if s.time == now_hhmm]

    return []


async def _publish_due(
    bot: Bot, profile: TenantProfile, slot: str, content_type: str | None = None
) -> None:
    """Сгенерировать и опубликовать один запланированный пост (фоновая задача).

    Вынесено из тика, чтобы тяжёлая генерация (LLM, десятки секунд) не блокировала
    минутный `schedule_tick` — иначе APScheduler пропускает следующие минуты как
    misfire, и запланированные посты «теряются». Слот уже застолблён в БД вызывающим;
    при ошибке снимаем отметку, чтобы дать ретрай на следующей минуте.

    content_type — тип слота недельной сетки ("topic"/"repost"); None → по content_mode.
    """
    try:
        content = await produce_content(profile, content_type=content_type)
    except Exception as e:
        await asyncio.to_thread(release_schedule_slot, slot)
        logging.error(f"Jadval generatsiya xatosi ({profile.chat_id}): {e}")
        return
    ok, detail = await send_to_telegram(bot, content, profile.chat_id)
    if not ok:
        await asyncio.to_thread(release_schedule_slot, slot)
    logging.info(f"Jadval post ({profile.chat_id}) {slot}: ok={ok} {detail}")


async def schedule_tick(bot: Bot) -> None:
    """Раз в минуту: запускает публикацию в каналы, у которых сейчас запланирован
    пост. Сам тик мгновенный — генерация/отправка уходят в фоновые задачи.

    Дедуп персистентный (таблица schedule_slots): слот «застолбляется» в БД, так
    что один и тот же (канал, дата, время) не публикуется дважды даже при рестарте
    бота или совпадении тиков.
    """
    now = datetime.now(TZ)
    now_hhmm = now.strftime("%H:%M")
    today = now.strftime("%Y-%m-%d")
    if now_hhmm == "00:00":
        # Раз в сутки чистим слоты прошлых дней.
        await asyncio.to_thread(purge_schedule_slots_before, today + " ")

    weekday = now.weekday()  # 0=Пн … 6=Вс
    tenants = await asyncio.to_thread(get_active_tenants)
    for profile in tenants:
        if (profile.schedule_mode or "off") == "off":
            continue
        due = await asyncio.to_thread(tenant_due_slots, profile, now_hhmm, weekday)
        for _time, ctype in due:
            # Тип в ключе слота: два разнотипных слота в одну минуту не «съедают»
            # дедуп друг друга.
            slot = f"{today} {now_hhmm} {profile.chat_id} {ctype or 'def'}"
            claimed = await asyncio.to_thread(claim_schedule_slot, slot)
            if not claimed:
                continue  # слот уже застолблён — не дублируем
            asyncio.create_task(_publish_due(bot, profile, slot, ctype))


async def index_source(
    tenant_id: str, src: str, limit: int = SCRAPE_HISTORY_LIMIT
) -> int:
    """Скрейпит ОДИН источник (канал или сайт) и индексирует его в RAG.

    Возвращает число проиндексированных чанков (0 — пусто или ошибка). При успехе
    обновляет `posts_indexed` источника в БД. Id поста = `f"{src}:{post_id}"`, а
    точка RAG = uuid5(tenant:post_id) → повторная индексация апсертит (без дублей)
    и добавляет новое. Используется и ночным reindex_references, и фоновой
    индексацией при добавлении источника через admin-api.
    """
    posts = await scrape_source(src, limit=limit)
    if not posts:
        logging.warning(f"Skreyp bo'sh ({src} / {tenant_id})")
        return 0
    for p in posts:
        p["id"] = f"{src}:{p['id']}"
    indexed = await rag_client.index_posts(tenant_id, posts, is_reference=True)
    if indexed:
        await asyncio.to_thread(add_tenant_source, tenant_id, src, indexed)
    return indexed


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
            indexed = await index_source(profile.tenant_id, s.source_chat_id)
            if indexed:
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
