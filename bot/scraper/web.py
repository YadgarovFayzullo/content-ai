"""Веб-сайт как источник фактов: скачивание HTML, RSS/Atom-ленты, сбор «постов».

Помимо Telegram-каналов источником фактов может быть веб-страница. Режимы:
  1. Новостная индекс-страница (напр. https://site.com/news/) — со страницы
     собираются ссылки на отдельные статьи, каждая статья скачивается и
     индексируется как отдельный «пост».
  2. RSS/Atom-лента — если на индекс-странице нашлась ссылка на фид (или фид
     лежит по типовому пути), статьи берутся из него. Это спасает сайты за
     Cloudflare: сам HTML под JS-challenge (403), а фид обычно открыт.
  3. Обычная страница — индексируется её собственный текст.

Получение HTML (`_fetch_html`) — двухступенчатое:
  • сначала обычный httpx-запрос с браузерными заголовками;
  • если ответ — Cloudflare JS-challenge (403 / cf-mitigated), страница
    дорендеривается headless-браузером (Playwright), который проходит challenge.
Если браузер недоступен/выключен (WEB_RENDER=0), для лент остаётся текст из
самого фида (заголовок + краткое описание) как запасной вариант.

Дедуп статей:
  • в рамках одного скрейпа — по URL и по хэшу содержимого;
  • между скрейпами — id поста = путь статьи (стабилен), а RAG апсертит точку
    по uuid5(tenant:post_id), поэтому одна и та же статья не дублируется.
Текст не чанкуется здесь: RAG-сервис сам режет посты >1500 символов на чанки.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urldefrag, urljoin, urlparse

import httpx

from bot.config import WEB_MAX_ARTICLES
from bot.scraper.browser import _fetch_rendered
from bot.scraper.text import (
    _BROWSER_HEADERS,
    _HREF_RE,
    _html_to_text,
    _readable_text,
)


_MIN_ARTICLE_LINKS = 3  # меньше — считаем страницу обычной (не лентой)


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
