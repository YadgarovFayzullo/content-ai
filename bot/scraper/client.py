"""Единый Telethon-клиент (user-сессия MTProto) на весь процесс.

Bot API (aiogram) не может выгрузить старые посты канала — только публиковать и
видеть новые. Поэтому история читается user-сессией Telethon. Сессия одна на
всё приложение, переиспользуется между вызовами.
"""
from __future__ import annotations

import logging
from asyncio import Lock
from typing import Any, Optional

from telethon import TelegramClient
from telethon.sessions import StringSession

from bot.config import TELEGRAM_API_ID, TELEGRAM_API_HASH, TELETHON_SESSION


_client: Optional[TelegramClient] = None
_client_lock = Lock()


def _creds_ready() -> bool:
    return bool(TELEGRAM_API_ID and TELEGRAM_API_HASH and TELETHON_SESSION)


def _peer(chat_id: Any) -> Any:
    """Нормализует chat_id для Telethon.

    Telethon резолвит числовой id канала (-100…) ТОЛЬКО как int; строку «-100…»
    он трактует как username и не находит (ValueError). @username и прочие строки
    оставляем как есть."""
    s = str(chat_id).strip()
    if s.lstrip("-").isdigit():
        return int(s)
    return s


async def _get_client() -> TelegramClient:
    """Единый Telethon-клиент на процесс. Lock не даёт двум корутинам создать
    две сессии одновременно (start() сетевой и неатомарный)."""
    global _client

    async with _client_lock:
        if _client is not None:
            return _client

        _client = TelegramClient(
            StringSession(TELETHON_SESSION),
            int(TELEGRAM_API_ID),
            TELEGRAM_API_HASH,
        )
        await _client.start()
        logging.info("Telethon client initialized")
        return _client
