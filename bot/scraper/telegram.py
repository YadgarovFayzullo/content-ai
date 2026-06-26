"""Скрейпинг Telegram-канала через Telethon (MTProto): история постов,
подписчики, официальная статистика, имя канала, фото поста.

Стартовый тариф: бот скрейпит только каналы, где он админ (проверяется на
онбординге через Bot API). Pro: ещё и любые публичные каналы по @username —
user-сессия читает их историю, даже если бот не админ.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Optional

from telethon.tl.types import MessageMediaPhoto
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.stats import GetBroadcastStatsRequest

from bot.scraper.client import _creds_ready, _get_client, _peer


async def get_subscriber_count(chat_id: Any) -> Optional[int]:
    """Число подписчиков канала через MTProto (Bot API его не отдаёт). None при
    недоступности (нет сессии / приватный канал без доступа / ошибка)."""
    if not _creds_ready():
        return None
    try:
        client = await _get_client()
        entity = await client.get_entity(_peer(chat_id))
        full = await client(GetFullChannelRequest(channel=entity))
        return int(getattr(full.full_chat, "participants_count", 0) or 0)
    except Exception as e:
        logging.warning("Obunachilar sonini olishda xato (%s): %s", chat_id, e)
        return None


def _abs_current(value: Any) -> Optional[float]:
    """Текущее значение из StatsAbsValueAndPrev (.current). None при отсутствии."""
    cur = getattr(value, "current", None)
    return float(cur) if cur is not None else None


def _percent(value: Any) -> Optional[float]:
    """% из StatsPercentValue (part/total*100). None, если total == 0/нет."""
    part = getattr(value, "part", None)
    total = getattr(value, "total", None)
    if not total:
        return None
    return round(float(part) / float(total) * 100, 1)


async def get_broadcast_stats(chat_id: Any) -> Optional[dict]:
    """Официальная статистика канала (stats.getBroadcastStats) через MTProto.

    Доступна только админу канала и только для каналов от ~50 подписчиков —
    для маленьких/недоступных каналов Telegram бросает ошибку, тогда возвращаем
    None (аналитика откатывается на наши собственные post_metrics).

    Берём ТОЛЬКО стабильные скалярные поля: % включённых уведомлений и средние
    views/shares/reactions на пост. Графики (top_hours и пр.) приходят как
    async-токены, требуют второго запроса и хрупкого парсинга — активные часы
    мы и так выводим из наших post_metrics, поэтому их здесь не трогаем."""
    if not _creds_ready():
        return None
    try:
        client = await _get_client()
        entity = await client.get_entity(_peer(chat_id))
        res = await client(GetBroadcastStatsRequest(channel=entity))
        return {
            "enabled_notifications_pct": _percent(
                getattr(res, "enabled_notifications", None)
            ),
            "views_per_post": _abs_current(getattr(res, "views_per_post", None)),
            "shares_per_post": _abs_current(getattr(res, "shares_per_post", None)),
            "reactions_per_post": _abs_current(
                getattr(res, "reactions_per_post", None)
            ),
        }
    except Exception as e:
        # Маленький канал / не админ / STATS_MIGRATE и пр. — статистики просто нет.
        logging.info("Broadcast stats mavjud emas (%s): %s", chat_id, e)
        return None


async def scrape_channel_history(
    chat_id: str, limit: int = 200
) -> list[dict[str, Any]]:
    """Возвращает последние посты канала:
    [{"id", "text", "date", "is_forward", "has_image"}].

    Текстовые посты (включая подписи к медиа). `is_forward`/`has_image` нужны
    repost-режиму (форварды отсеиваются, фото переиспользуется). Если Telethon
    не настроен — возвращает пустой список и пишет предупреждение в лог
    (онбординг не падает).
    """
    if not _creds_ready():
        logging.warning(
            "Telethon sozlanmagan (TELEGRAM_API_ID/HASH/TELETHON_SESSION yo'q) — "
            "kanal tarixi skreyp qilinmadi."
        )
        return []

    try:
        client = await _get_client()
        entity = await client.get_entity(_peer(chat_id))
        posts: list[dict[str, Any]] = []
        async for msg in client.iter_messages(entity, limit=limit):
            text = (msg.message or "").strip()
            if not text:
                continue
            posts.append(
                {
                    "id": msg.id,
                    "text": text,
                    "date": msg.date.isoformat() if msg.date else None,
                    "is_forward": msg.fwd_from is not None,
                    "has_image": isinstance(msg.media, MessageMediaPhoto),
                }
            )
        logging.info(f"Skreyp qilindi: {len(posts)} ta post ({chat_id})")
        return posts
    except Exception as e:
        logging.error(f"Telethon skreyp xatosi ({chat_id}): {e}")
        return []


_title_cache: dict[str, str] = {}


async def get_channel_title(chat_id: str) -> Optional[str]:
    """Отображаемое имя канала (для кредита «Photo: …» на карточке). Кэшируется на
    процесс. None — если Telethon не настроен или канал не резолвится."""
    key = str(chat_id)
    if key in _title_cache:
        return _title_cache[key] or None
    if not _creds_ready():
        return None
    try:
        client = await _get_client()
        entity = await client.get_entity(_peer(chat_id))
        title = getattr(entity, "title", None) or getattr(entity, "username", None)
        _title_cache[key] = title or ""
        return title
    except Exception as e:
        logging.warning(f"Kanal nomini olishda xato ({chat_id}): {e}")
        _title_cache[key] = ""
        return None


async def download_post_image(
    chat_id: str, message_id: int, out_dir: str = "gen_images"
) -> Optional[str]:
    """Скачивает фото конкретного поста источника. Путь к файлу или None.

    Используется repost-режимом: если у исходной новости есть картинка, её
    переиспользуем вместо генерации. При любой ошибке возвращаем None (публикуем
    пост без изображения, а не валим его)."""
    if not _creds_ready():
        return None
    try:
        client = await _get_client()
        entity = await client.get_entity(_peer(chat_id))
        msg = await client.get_messages(entity, ids=message_id)
        if msg is None or not isinstance(msg.media, MessageMediaPhoto):
            return None
        Path(out_dir).mkdir(exist_ok=True)
        dest = Path(out_dir) / f"src_{abs(hash(chat_id)) % 10**6}_{message_id}_{int(time.time())}.jpg"
        path = await client.download_media(msg, file=str(dest))
        return str(path) if path else None
    except Exception as e:
        logging.error(f"Telethon rasm yuklab olish xatosi ({chat_id}:{message_id}): {e}")
        return None
