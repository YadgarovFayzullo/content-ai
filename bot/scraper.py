"""Скрейпинг истории Telegram-канала через Telethon (MTProto).

Bot API (aiogram) не может выгрузить старые посты канала — только публиковать и
видеть новые. Поэтому история читается user-сессией Telethon. Сессия одна на
всё приложение, переиспользуется между вызовами.

Стартовый тариф: бот скрейпит только каналы, где он админ (проверяется на
онбординге через Bot API). Pro: ещё и любые публичные каналы по @username —
user-сессия читает их историю, даже если бот не админ.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urldefrag, urljoin, urlparse
from asyncio import Lock

import httpx
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaPhoto
from telethon.tl.functions.channels import GetFullChannelRequest

from bot.config import (
    TELEGRAM_API_ID,
    TELEGRAM_API_HASH,
    TELETHON_SESSION,
    WEB_MAX_ARTICLES,
)


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


# --- Веб-сайты как источник ---------------------------------------------------
#
# Помимо Telegram-каналов источником фактов может быть веб-страница. Два режима:
#   1. Новостная индекс-страница (напр. https://site.com/news/) — со страницы
#      собираются ссылки на отдельные статьи, каждая статья скачивается и
#      индексируется как отдельный «пост».
#   2. Обычная страница — индексируется её собственный текст.
# Режим выбирается автоматически: если на странице нашлось ≥3 ссылок-статей
# (тот же домен, путь глубже индекс-пути) — это новостная лента.
#
# Дедуп статей:
#   • в рамках одного скрейпа — по URL и по хэшу содержимого;
#   • между скрейпами — id поста = путь статьи (стабилен), а RAG апсертит точку
#     по uuid5(tenant:post_id), поэтому одна и та же статья не дублируется.
# Текст не чанкуется здесь: RAG-сервис сам режет посты >1500 символов на чанки.


_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)
_MIN_ARTICLE_LINKS = 3  # меньше — считаем страницу обычной (не лентой)


class _TextExtractor(HTMLParser):
    """Минимальный HTML→текст: собирает видимый текст, пропуская script/style и
    прочий нетекстовый мусор. Блочные теги дают перенос строки, чтобы абзацы не
    слипались в одну строку."""

    _SKIP = {"script", "style", "noscript", "template", "svg", "head"}
    _BLOCK = {
        "p", "br", "div", "section", "article", "li", "tr", "header", "footer",
        "h1", "h2", "h3", "h4", "h5", "h6",
    }

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        if tag in self._BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        if tag in self._BLOCK:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        lines = (re.sub(r"[ \t]+", " ", ln).strip() for ln in raw.splitlines())
        return "\n".join(ln for ln in lines if ln)


def _is_url(source: str) -> bool:
    return source.startswith("http://") or source.startswith("https://")


def _readable_text(html: str) -> str:
    """HTML → читаемый текст: короткие строки (навигация, кнопки, копирайты)
    отсекаются как шум, остаётся содержательный текст абзацами."""
    parser = _TextExtractor()
    parser.feed(html)
    lines = [ln for ln in parser.text().split("\n") if len(ln) >= 30]
    return "\n".join(lines)


def _article_links(index_url: str, html: str, limit: int) -> list[str]:
    """Ссылки на статьи с новостной индекс-страницы: тот же домен, путь глубже
    индекс-пути (так отсекаются /about, /pricing, внешние ссылки и пагинация на
    сам индекс). Дедуп по URL, порядок сохраняется, не более `limit`."""
    base = urlparse(index_url)
    base_path = (base.path.rstrip("/") or "") + "/"  # "/news" и "/news/" → "/news/"
    seen: set[str] = set()
    out: list[str] = []
    for raw in _HREF_RE.findall(html):
        href, _ = urldefrag(urljoin(index_url, raw.strip()))
        u = urlparse(href)
        if u.scheme not in ("http", "https") or u.netloc != base.netloc:
            continue
        if not u.path.startswith(base_path):
            continue  # не вложено в индекс-путь — это не статья ленты
        if u.path.rstrip("/") == base.path.rstrip("/"):
            continue  # сам индекс / якорь на него
        if href in seen:
            continue
        seen.add(href)
        out.append(href)
        if len(out) >= limit:
            break
    return out


async def _fetch_html(http: httpx.AsyncClient, url: str) -> Optional[str]:
    """GET страницы. None, если недоступна или ответ не HTML/текст."""
    try:
        resp = await http.get(url)
        resp.raise_for_status()
    except Exception as e:
        logging.warning("Sahifa ochilmadi (%s): %s", url, e)
        return None
    ctype = resp.headers.get("content-type", "")
    if "html" not in ctype and "text" not in ctype:
        return None
    return resp.text


def _article_post(url: str, html: str) -> Optional[dict[str, Any]]:
    """Строит «пост» из статьи. id = путь статьи (стабилен между скрейпами →
    RAG апсертит, без дублей). None, если содержательного текста нет."""
    text = _readable_text(html)
    if len(text) < 80:
        return None
    return {
        "id": urlparse(url).path or url,
        "text": text,
        "date": datetime.now(timezone.utc).isoformat(),
        "is_forward": False,
        "has_image": False,
    }


async def scrape_website(url: str, limit: int = WEB_MAX_ARTICLES) -> list[dict[str, Any]]:
    """Скрейпит сайт как источник фактов. Возвращает «посты» в том же формате,
    что и scrape_channel_history: [{"id","text","date","is_forward","has_image"}].

    Новостная индекс-страница → по посту на статью (см. _article_links); обычная
    страница → один пост с её текстом. `limit` ограничивает число статей.
    Telethon не нужен. При любой ошибке — пустой список и предупреждение в лог."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; ContentAI/1.0; +https://content-ai.local)"
        )
    }
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=20.0, headers=headers
        ) as http:
            index_html = await _fetch_html(http, url)
            if index_html is None:
                logging.warning("Sayt o'qilmadi yoki HTML emas: %s", url)
                return []

            links = _article_links(url, index_html, limit)
            if len(links) < _MIN_ARTICLE_LINKS:
                # Не лента — индексируем саму страницу как один пост.
                post = _article_post(url, index_html)
                if post:
                    logging.info("Sayt skreyp qilindi: 1 ta sahifa (%s)", url)
                    return [post]
                logging.warning("Saytda matn topilmadi: %s", url)
                return []

            posts: list[dict[str, Any]] = []
            seen_hashes: set[str] = set()
            for link in links:
                html = await _fetch_html(http, link)
                if html is None:
                    continue
                post = _article_post(link, html)
                if post is None:
                    continue
                # Дедуп по содержимому: одна и та же новость под разными URL.
                h = hashlib.sha1(post["text"][:500].encode("utf-8")).hexdigest()
                if h in seen_hashes:
                    continue
                seen_hashes.add(h)
                posts.append(post)
                await asyncio.sleep(0.3)  # вежливость к сайту

            logging.info(
                "Sayt skreyp qilindi: %d ta maqola / %d ta havola (%s)",
                len(posts), len(links), url,
            )
            return posts
    except Exception as e:
        logging.error("Sayt skreyp xatosi (%s): %s", url, e)
        return []


async def scrape_source(source: str, limit: int = WEB_MAX_ARTICLES) -> list[dict[str, Any]]:
    """Единая точка скрейпа источника: веб-сайт (http/https URL) или
    Telegram-канал (@username / -100…). Формат постов одинаковый для обоих,
    поэтому вызывающий код (индексация в RAG) не зависит от типа источника.

    Для каналов лимит = число сообщений (SCRAPE_HISTORY_LIMIT, сотни); для сайта
    это число статей, поэтому там применяется отдельный потолок WEB_MAX_ARTICLES."""
    if _is_url(source):
        return await scrape_website(source, limit=min(limit, WEB_MAX_ARTICLES))
    return await scrape_channel_history(source, limit=limit)
