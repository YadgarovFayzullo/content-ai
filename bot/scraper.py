from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Optional
from asyncio import Lock

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaPhoto

from bot.config import TELEGRAM_API_ID, TELEGRAM_API_HASH, TELETHON_SESSION


_client: Optional[TelegramClient] = None
_client_lock = Lock()


def _creds_ready() -> bool:
    return bool(TELEGRAM_API_ID and TELEGRAM_API_HASH and TELETHON_SESSION)


def _peer(chat_id: Any) -> Any:
    s = str(chat_id).strip()
    if s.lstrip("-").isdigit():
        return int(s)
    return s


async def _get_client() -> TelegramClient:
    global _client

    async with _client_lock:
        if _client is not None:
            return _client

        _client = TelegramClient(
            StringSession(TELETHON_SESSION),
            int(TELEGRAM_API_ID),
            TELEGRAM_API_HASH
        )

        await _client.start()
        logging.info("Telethon client initialized")
        return _client