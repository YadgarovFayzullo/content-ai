"""Скрейпинг истории Telegram-канала через Telethon (MTProto).

Bot API (aiogram) не может выгрузить старые посты канала — только публиковать и
видеть новые. Поэтому история читается user-сессией Telethon. Сессия одна на
всё приложение, переиспользуется между вызовами.

Стартовый тариф: бот скрейпит только каналы, где он админ (проверяется на
онбординге через Bot API). Pro: ещё и любые публичные каналы по @username —
user-сессия читает их историю, даже если бот не админ.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Optional

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaPhoto

from bot.config import TELEGRAM_API_ID, TELEGRAM_API_HASH, TELETHON_SESSION

_client: Optional[TelegramClient] = None


def _creds_ready() -> bool:
    return bool(TELEGRAM_API_ID and TELEGRAM_API_HASH and TELETHON_SESSION)


async def _get_client() -> TelegramClient:
    global _client
    if _client is None:
        _client = TelegramClient(
            StringSession(TELETHON_SESSION), int(TELEGRAM_API_ID), TELEGRAM_API_HASH
        )
    if not _client.is_connected():
        await _client.connect()
    return _client


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
        entity = await client.get_entity(chat_id)
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
        entity = await client.get_entity(chat_id)
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
