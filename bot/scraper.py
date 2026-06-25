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
from telethon.tl.functions.stats import GetBroadcastStatsRequest

from bot.config import (
    TELEGRAM_API_ID,
    TELEGRAM_API_HASH,
    TELETHON_SESSION,
    WEB_MAX_ARTICLES,
    WEB_RENDER,
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


# --- Веб-сайты как источник ---------------------------------------------------
#
# Помимо Telegram-каналов источником фактов может быть веб-страница. Режимы:
#   1. Новостная индекс-страница (напр. https://site.com/news/) — со страницы
#      собираются ссылки на отдельные статьи, каждая статья скачивается и
#      индексируется как отдельный «пост».
#   2. RSS/Atom-лента — если на индекс-странице нашлась ссылка на фид (или фид
#      лежит по типовому пути), статьи берутся из него. Это спасает сайты за
#      Cloudflare: сам HTML под JS-challenge (403), а фид обычно открыт.
#   3. Обычная страница — индексируется её собственный текст.
#
# Получение HTML (`_fetch_html`) — двухступенчатое:
#   • сначала обычный httpx-запрос с браузерными заголовками;
#   • если ответ — Cloudflare JS-challenge (403 / cf-mitigated), страница
#     дорендеривается headless-браузером (Playwright), который проходит challenge.
# Если браузер недоступен/выключен (WEB_RENDER=0), для лент остаётся текст из
# самого фида (заголовок + краткое описание) как запасной вариант.
#
# Дедуп статей:
#   • в рамках одного скрейпа — по URL и по хэшу содержимого;
#   • между скрейпами — id поста = путь статьи (стабилен), а RAG апсертит точку
#     по uuid5(tenant:post_id), поэтому одна и та же статья не дублируется.
# Текст не чанкуется здесь: RAG-сервис сам режет посты >1500 символов на чанки.


_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)
_MIN_ARTICLE_LINKS = 3  # меньше — считаем страницу обычной (не лентой)

# Браузерные заголовки: многие сайты отдают 403 на «ботские» User-Agent.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}


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


def _html_to_text(html: str) -> str:
    """HTML → текст без фильтра по длине строк (для коротких описаний из фида)."""
    parser = _TextExtractor()
    parser.feed(html)
    return parser.text().strip()


# --- Headless-браузер (Playwright) для прохождения JS-challenge -----------------
#
# Один браузер на процесс (как и Telethon-клиент): запуск chromium дорогой.
# Импорт ленивый — если playwright не установлен, веб-скрейп всё равно работает
# (просто без рендера). `_browser_unavailable` запоминает неудачу, чтобы не
# пытаться поднять браузер на каждой странице.

_browser: Any = None
_playwright: Any = None
_browser_lock = Lock()
_browser_unavailable = False


async def _get_browser() -> Any:
    """Единый headless-chromium на процесс. None, если рендер выключен
    (WEB_RENDER=0), playwright не установлен или браузер не поднялся."""
    global _browser, _playwright, _browser_unavailable

    if not WEB_RENDER or _browser_unavailable:
        return None

    async with _browser_lock:
        if _browser is not None:
            return _browser
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            _browser_unavailable = True
            logging.warning(
                "Playwright o'rnatilmagan — JS-challenge sahifalar render "
                "qilinmaydi (faqat RSS/oddiy sahifalar)."
            )
            return None
        try:
            _playwright = await async_playwright().start()
            _browser = await _playwright.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            logging.info("Playwright chromium ishga tushdi")
            return _browser
        except Exception as e:
            _browser_unavailable = True
            logging.warning("Playwright ishga tushmadi: %s", e)
            return None


async def _fetch_rendered(url: str) -> Optional[str]:
    """Рендерит страницу headless-браузером и возвращает её HTML после того, как
    JS (в т.ч. Cloudflare challenge) отработал. None при недоступности/ошибке."""
    browser = await _get_browser()
    if browser is None:
        return None
    context = None
    try:
        context = await browser.new_context(
            user_agent=_BROWSER_HEADERS["User-Agent"], locale="en-US"
        )
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        # Дать Cloudflare-challenge время отработать и подгрузить контент.
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        return await page.content()
    except Exception as e:
        logging.warning("Render qilishda xato (%s): %s", url, e)
        return None
    finally:
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass


def _looks_challenged(resp: httpx.Response) -> bool:
    """Похоже ли, что ответ — заглушка анти-бота (Cloudflare и т.п.), а не
    настоящая страница. Такие ответы стоит дорендерить браузером."""
    if resp.headers.get("cf-mitigated") == "challenge":
        return True
    server = resp.headers.get("server", "").lower()
    return resp.status_code in (403, 429, 503) and "cloudflare" in server


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
        if u.path.lower().endswith((".xml", ".rss", ".atom", ".json")):
            continue  # фид/ассет, не статья
        if href in seen:
            continue
        seen.add(href)
        out.append(href)
        if len(out) >= limit:
            break
    return out


async def _fetch_html(http: httpx.AsyncClient, url: str) -> Optional[str]:
    """GET страницы с двухступенчатой стратегией:
      1) обычный httpx-запрос с браузерными заголовками;
      2) если ответ — анти-бот challenge (см. _looks_challenged), страница
         дорендеривается headless-браузером.
    None — если страница недоступна или это не HTML/текст/XML."""
    resp: Optional[httpx.Response] = None
    try:
        resp = await http.get(url)
    except Exception as e:
        logging.warning("Sahifa ochilmadi (%s): %s", url, e)

    if resp is not None and resp.status_code < 400 and not _looks_challenged(resp):
        ctype = resp.headers.get("content-type", "")
        if "html" in ctype or "text" in ctype or "xml" in ctype:
            return resp.text
        return None  # бинарь (картинка/pdf) — не наш случай

    # Заблокировано или challenge — пробуем браузер.
    if resp is not None and _looks_challenged(resp):
        logging.info("JS-challenge, render orqali urinish: %s", url)
    rendered = await _fetch_rendered(url)
    if rendered is None and resp is not None:
        logging.warning("Sahifa ochilmadi (%s): HTTP %s", url, resp.status_code)
    return rendered


def _make_post(url: str, text: str, date_iso: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Собирает «пост» в общем формате. id = путь URL (стабилен между скрейпами →
    RAG апсертит, без дублей). None, если содержательного текста нет."""
    text = text.strip()
    if len(text) < 80:
        return None
    return {
        "id": urlparse(url).path or url,
        "text": text,
        "date": date_iso or datetime.now(timezone.utc).isoformat(),
        "is_forward": False,
        "has_image": False,
    }


def _article_post(url: str, html: str) -> Optional[dict[str, Any]]:
    """«Пост» из полного HTML статьи."""
    return _make_post(url, _readable_text(html))


# --- RSS/Atom-ленты ------------------------------------------------------------

_FEED_LINK_RE = re.compile(
    r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*>', re.I
)


async def _feed_from_urls(
    http: httpx.AsyncClient, urls: list[str]
) -> Optional[tuple[str, list[dict[str, Any]]]]:
    """Берёт первый рабочий фид из `urls` и парсит его. Возвращает
    (feed_url, entries) или None. entries: [{"link","title","summary","date"}]."""
    seen: set[str] = set()
    for feed_url in urls:
        if feed_url in seen:
            continue
        seen.add(feed_url)
        xml = await _fetch_html(http, feed_url)
        if not xml or ("<rss" not in xml[:600].lower() and "<feed" not in xml[:600].lower()):
            continue
        entries = _parse_feed(xml)
        if entries:
            return feed_url, entries
    return None


def _common_feed_urls(index_url: str) -> list[str]:
    """Типовые пути к фиду относительно индекс-пути и корня сайта."""
    base = urlparse(index_url)
    path = base.path.rstrip("/")
    return [
        urljoin(index_url, c)
        for c in (
            f"{path}/rss.xml", f"{path}/feed", f"{path}/feed.xml", f"{path}/atom.xml",
            "/rss.xml", "/feed", "/feed.xml", "/atom.xml", "/index.xml",
        )
    ]


def _discover_feed_explicit(index_url: str, html: Optional[str]) -> Optional[str]:
    """URL фида из <link rel=alternate type=application/rss+xml> в HTML."""
    if not html:
        return None
    for tag in _FEED_LINK_RE.findall(html):
        m = _HREF_RE.search(tag)
        if m:
            return urljoin(index_url, m.group(1).strip())
    return None


def _parse_feed(xml: str) -> list[dict[str, Any]]:
    """Парсит RSS/Atom через feedparser → список записей с link/title/summary/date.
    Если feedparser не установлен — пустой список (фид-режим просто отключается)."""
    try:
        import feedparser
    except ImportError:
        logging.warning("feedparser o'rnatilmagan — RSS-rejim o'chirilgan.")
        return []
    parsed = feedparser.parse(xml)
    out: list[dict[str, Any]] = []
    for e in parsed.entries:
        link = (e.get("link") or "").strip()
        if not link:
            continue
        body = ""
        if e.get("content"):
            body = e["content"][0].get("value", "")
        body = body or e.get("summary", "") or e.get("description", "")
        date_iso = None
        if e.get("published_parsed"):
            try:
                date_iso = datetime(*e["published_parsed"][:6], tzinfo=timezone.utc).isoformat()
            except Exception:
                pass
        out.append({
            "link": link,
            "title": (e.get("title") or "").strip(),
            "summary": _html_to_text(body),
            "date": date_iso,
        })
    return out


def _dedup_append(
    post: dict[str, Any], posts: list[dict[str, Any]], seen_hashes: set[str]
) -> None:
    """Добавляет пост, отсекая дубли по хэшу содержимого (одна новость под
    разными URL)."""
    h = hashlib.sha1(post["text"][:500].encode("utf-8")).hexdigest()
    if h in seen_hashes:
        return
    seen_hashes.add(h)
    posts.append(post)


async def scrape_website(url: str, limit: int = WEB_MAX_ARTICLES) -> list[dict[str, Any]]:
    """Скрейпит сайт как источник фактов. Возвращает «посты» в том же формате,
    что и scrape_channel_history: [{"id","text","date","is_forward","has_image"}].

    Порядок выбора режима:
      1) Явно объявленный фид (<link rel=alternate>) — самый чистый список статей;
      2) HTML-лента: ссылки на статьи, вложенные в индекс-путь;
      3) Фид по типовым путям (/rss.xml, /feed …) — спасает сайты под Cloudflare,
         когда сам HTML под JS-challenge;
      4) обычная страница как один пост.
    Для фид-режима текст каждой статьи берётся полным рендером, а если он
    недоступен (нет браузера) — заголовок+описание из самого фида.
    `limit` ограничивает число статей. При любой ошибке — пустой список и лог."""
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=20.0, headers=_BROWSER_HEADERS
        ) as http:
            index_html = await _fetch_html(http, url)

            # 0. Сам URL уже является фидом (например …/feed/, …/rss.xml).
            #    Тогда HTML-эвристики не нужны — парсим его напрямую.
            if index_html and (
                "<rss" in index_html[:600].lower()
                or "<feed" in index_html[:600].lower()
            ):
                entries = _parse_feed(index_html)
                if entries:
                    return await _scrape_feed(http, url, url, entries, limit)

            # 1. Явно объявленный фид имеет приоритет над парсингом HTML-ссылок:
            #    это канонический список статей (с датами), без навигационного мусора.
            explicit = _discover_feed_explicit(url, index_html)
            if explicit:
                feed = await _feed_from_urls(http, [explicit])
                if feed is not None:
                    return await _scrape_feed(http, url, *feed, limit)

            # 2. HTML-лента: ссылки на статьи, вложенные в индекс-путь.
            links = _article_links(url, index_html, limit) if index_html else []
            if len(links) >= _MIN_ARTICLE_LINKS:
                return await _scrape_article_links(http, url, links, {})

            # 3. Фид по типовым путям (когда явного <link> нет / HTML под challenge).
            feed = await _feed_from_urls(http, _common_feed_urls(url))
            if feed is not None:
                return await _scrape_feed(http, url, *feed, limit)

            # 4. Обычная страница как один пост.
            if index_html:
                post = _article_post(url, index_html)
                if post:
                    logging.info("Sayt skreyp qilindi: 1 ta sahifa (%s)", url)
                    return [post]
                logging.warning("Saytda matn topilmadi: %s", url)
                return []

            logging.warning("Sayt o'qilmadi: %s", url)
            return []
    except Exception as e:
        logging.error("Sayt skreyp xatosi (%s): %s", url, e)
        return []


async def _scrape_feed(
    http: httpx.AsyncClient,
    index_url: str,
    feed_url: str,
    entries: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    """Собирает посты из записей фида (полный рендер статьи, иначе — заголовок+
    описание из фида) и логирует итог."""
    entries = entries[:limit]
    summaries = {e["link"]: e for e in entries}
    posts = await _scrape_article_links(
        http, index_url, [e["link"] for e in entries], summaries
    )
    logging.info(
        "Sayt skreyp qilindi (RSS): %d ta post / %d ta yozuv (%s)",
        len(posts), len(entries), feed_url,
    )
    return posts


async def _scrape_article_links(
    http: httpx.AsyncClient,
    index_url: str,
    links: list[str],
    summaries: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Качает каждую статью из `links` и собирает посты. Если полный текст статьи
    недоступен (challenge без рендера), но для ссылки есть запись фида —
    используется заголовок+описание из фида (`summaries`: link → запись фида)."""
    posts: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    for link in links:
        feed_date = summaries.get(link, {}).get("date")
        html = await _fetch_html(http, link)
        # Полный текст статьи, но дату публикации берём из фида (надёжнее, чем
        # «сейчас»), если она там есть.
        post = _make_post(link, _readable_text(html), feed_date) if html else None
        if post is None and link in summaries:
            e = summaries[link]
            fallback = f"{e['title']}\n{e['summary']}".strip()
            post = _make_post(link, fallback, e.get("date"))
        if post is None:
            continue
        _dedup_append(post, posts, seen_hashes)
        await asyncio.sleep(0.3)  # вежливость к сайту

    if not summaries:  # путь 1 (HTML-лента) логируем здесь; RSS — у вызывающего
        logging.info(
            "Sayt skreyp qilindi: %d ta maqola / %d ta havola (%s)",
            len(posts), len(links), index_url,
        )
    return posts


async def scrape_source(source: str, limit: int = WEB_MAX_ARTICLES) -> list[dict[str, Any]]:
    """Единая точка скрейпа источника: веб-сайт (http/https URL) или
    Telegram-канал (@username / -100…). Формат постов одинаковый для обоих,
    поэтому вызывающий код (индексация в RAG) не зависит от типа источника.

    Для каналов лимит = число сообщений (SCRAPE_HISTORY_LIMIT, сотни); для сайта
    это число статей, поэтому там применяется отдельный потолок WEB_MAX_ARTICLES."""
    if _is_url(source):
        return await scrape_website(source, limit=min(limit, WEB_MAX_ARTICLES))
    return await scrape_channel_history(source, limit=limit)
