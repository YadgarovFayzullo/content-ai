"""Скрейпинг источников фактов: Telegram-каналы (Telethon/MTProto) и веб-сайты.

`scrape_source` — единая точка входа, по виду источника выбирает Telegram- или
веб-режим. Внутренние модули:
  • client   — единый Telethon-клиент на процесс (+ _creds_ready/_peer);
  • telegram — история канала, подписчики, статистика, имя, фото поста;
  • text     — HTML→текст и браузерные заголовки;
  • browser  — headless-Playwright для JS-challenge;
  • web      — скачивание сайтов, RSS/Atom, scrape_website.

Публичные имена ре-экспортируются здесь, поэтому вызывающий код продолжает
импортировать из `bot.scraper`, не зная о разбиении на модули.
"""
from __future__ import annotations

from typing import Any

from bot.config import WEB_MAX_ARTICLES
from bot.scraper.client import _creds_ready, _get_client, _peer
from bot.scraper.telegram import (
    download_post_image,
    get_broadcast_stats,
    get_channel_title,
    get_subscriber_count,
    scrape_channel_engagement,
    scrape_channel_history,
)
from bot.scraper.text import _is_url
from bot.scraper.web import scrape_website

__all__ = [
    "scrape_source",
    "scrape_channel_history",
    "scrape_website",
    "download_post_image",
    "get_channel_title",
    "get_subscriber_count",
    "get_broadcast_stats",
    "scrape_channel_engagement",
    "_creds_ready",
    "_get_client",
    "_peer",
]


async def scrape_source(source: str, limit: int = WEB_MAX_ARTICLES) -> list[dict[str, Any]]:
    """Единая точка скрейпа источника: веб-сайт (http/https URL) или
    Telegram-канал (@username / -100…). Формат постов одинаковый для обоих,
    поэтому вызывающий код (индексация в RAG) не зависит от типа источника.

    Для каналов лимит = число сообщений (SCRAPE_HISTORY_LIMIT, сотни); для сайта
    это число статей, поэтому там применяется отдельный потолок WEB_MAX_ARTICLES."""
    if _is_url(source):
        return await scrape_website(source, limit=min(limit, WEB_MAX_ARTICLES))
    return await scrape_channel_history(source, limit=limit)
