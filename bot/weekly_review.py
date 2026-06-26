"""Еженедельный «обзор недели» для repost-режима.

Раз в неделю в каждый канал на repost-режиме публикуется дайджест: пронумерованный
список заголовков постов, вышедших за последние REVIEW_DAYS дней, с кликабельными
ссылками на сами посты. Заголовок — первая строка поста (без HTML). Сам обзор тоже
пишется в историю (topic=WEEKLY_REVIEW_TOPIC), но в следующий дайджест не попадает
(исключается по теме) и в обзор сам себя не включает.

topic-режим не трогаем — обзор только для каналов-агрегаторов (repost).
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.types import LinkPreviewOptions

from database import (
    PostHistory,
    get_active_tenants,
    get_published_posts_since,
    save_post,
)

# Тема-маркер для самих обзоров — чтобы дайджест не ссылался сам на себя.
WEEKLY_REVIEW_TOPIC = "weekly_review"

# Окно обзора и защита от лимита Telegram (4096 символов на сообщение).
REVIEW_DAYS = 7
MAX_ITEMS = 40
TITLE_MAX_CHARS = 90

# Заголовок дайджеста по языку профиля (uz — дефолт).
_HEADERS = {
    "uz": "📰 <b>Hafta sharhi</b> — o'tgan haftada chiqqan postlar:",
    "ru": "📰 <b>Обзор недели</b> — посты за прошедшую неделю:",
    "en": "📰 <b>Weekly review</b> — posts from the past week:",
}


def _extract_title(content: str) -> str:
    """Заголовок поста для строки дайджеста: первая непустая строка без HTML,
    обрезанная до TITLE_MAX_CHARS."""
    plain = re.sub(r"<[^>]+>", " ", content or "")
    # Остаточный markdown (если первая строка не успела стать HTML): **bold**, # заголовок.
    plain = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", r"\1\2", plain)
    plain = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", plain)
    for line in plain.splitlines():
        line = re.sub(r"\s+", " ", line).strip().strip("\"'").rstrip(".").strip()
        if line:
            if len(line) > TITLE_MAX_CHARS:
                line = line[: TITLE_MAX_CHARS - 1].rstrip() + "…"
            return line
    return "Post"


def _post_link(chat, message_id: int) -> str | None:
    """Ссылка на пост канала. Публичный канал → t.me/<username>/<id>;
    приватный (-100…) → t.me/c/<short>/<id>. Иначе None (строку дадим без ссылки)."""
    if getattr(chat, "username", None):
        return f"https://t.me/{chat.username}/{message_id}"
    cid = str(getattr(chat, "id", ""))
    if cid.startswith("-100"):
        return f"https://t.me/c/{cid[4:]}/{message_id}"
    return None


def _build_digest(profile, posts, chat) -> str | None:
    """Текст обзора: заголовок + нумерованный список постов-ссылок. None — если
    ни одной строки не получилось."""
    lines: list[str] = []
    for p in posts[:MAX_ITEMS]:
        title = html.escape(_extract_title(p.content))
        link = _post_link(chat, p.message_id)
        n = len(lines) + 1
        lines.append(f'{n}. <a href="{link}">{title}</a>' if link else f"{n}. {title}")
    if not lines:
        return None
    header = _HEADERS.get((profile.language or "uz").lower(), _HEADERS["uz"])
    return header + "\n\n" + "\n".join(lines)


async def post_weekly_reviews(bot: Bot) -> int:
    """Публикует обзор недели во все активные repost-каналы. Возвращает число
    опубликованных обзоров.

    Каналы без постов за неделю пропускаются (пустой обзор не шлём). Ошибка по
    одному каналу не валит остальные.
    """
    since = datetime.now(timezone.utc) - timedelta(days=REVIEW_DAYS)
    tenants = await asyncio.to_thread(get_active_tenants)
    published = 0
    for profile in tenants:
        if (getattr(profile, "content_mode", "topic") or "topic") not in ("repost", "both"):
            continue

        posts = await asyncio.to_thread(
            get_published_posts_since, profile.tenant_id, since, [WEEKLY_REVIEW_TOPIC]
        )
        if not posts:
            logging.info(
                "Hafta sharhi o'tkazib yuborildi (%s): hafta ichida post yo'q.",
                profile.chat_id,
            )
            continue

        try:
            chat = await bot.get_chat(profile.chat_id)
            text = _build_digest(profile, posts, chat)
            if not text:
                continue
            msg = await bot.send_message(
                chat_id=profile.chat_id,
                text=text,
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            entry = PostHistory(
                tenant_id=profile.tenant_id,
                topic=WEEKLY_REVIEW_TOPIC,
                content=text,
                posted=True,
                message_id=msg.message_id,
            )
            await asyncio.to_thread(save_post, entry)
            published += 1
            logging.info(
                "Hafta sharhi joylandi (%s): %d ta post.", profile.chat_id, len(posts)
            )
        except Exception as e:
            logging.error("Hafta sharhi xatosi (%s): %s", profile.chat_id, e)

        await asyncio.sleep(2)  # бережём rate-limit между каналами

    logging.info("Hafta sharhi yakunlandi: %d ta kanalga joylandi.", published)
    return published
