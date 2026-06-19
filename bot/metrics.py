"""Сбор метрик опубликованных постов через Telethon.

Bot API не отдаёт просмотры/пересылки/реакции постов канала — это доступно
только MTProto. Поэтому метрики снимаются той же user-сессией, что и скрейпинг.

Снимаем посты за последние METRICS_WINDOW_DAYS дней: свежие ещё набирают охват,
поэтому делаем периодические снимки (history в post_metrics).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from database import get_posts_for_metrics, get_tenant_profile, save_metric
from bot.scraper import _creds_ready, _get_client

# Окно сбора: посты старше уже стабилизировались, их не переснимаем.
METRICS_WINDOW_DAYS = 7


def _count_reactions(msg) -> int:
    reactions = getattr(msg, "reactions", None)
    if not reactions or not getattr(reactions, "results", None):
        return 0
    return sum(r.count for r in reactions.results)


async def collect_metrics() -> int:
    """Снимает метрики свежих постов всех арендаторов. Возвращает число замеров."""
    if not _creds_ready():
        logging.warning("Telethon sozlanmagan — metrikalar yig'ilmadi.")
        return 0

    since = datetime.now(timezone.utc) - timedelta(days=METRICS_WINDOW_DAYS)
    posts = await asyncio.to_thread(get_posts_for_metrics, since)
    if not posts:
        logging.info("Metrika uchun mos post yo'q (oxirgi %d kun).", METRICS_WINDOW_DAYS)
        return 0

    logging.info("Metrika uchun %d ta post topildi.", len(posts))
    client = await _get_client()
    saved = 0
    skipped = 0
    for post in posts:
        try:
            # chat_id арендатора нужен, чтобы достать сообщение из нужного канала.
            profile = await asyncio.to_thread(get_tenant_profile, post.tenant_id)
            if not profile:
                skipped += 1
                logging.warning(
                    "Metrika o'tkazib yuborildi (post %s): tenant %s profili topilmadi.",
                    post.id, post.tenant_id,
                )
                continue

            entity = await client.get_entity(profile.chat_id)
            msg = await client.get_messages(entity, ids=post.message_id)
            if not msg:
                skipped += 1
                logging.warning(
                    "Metrika o'tkazib yuborildi (post %s): %s kanalida %s xabar topilmadi "
                    "(o'chirilgan yoki message_id noto'g'ri).",
                    post.id, profile.chat_id, post.message_id,
                )
                continue

            await asyncio.to_thread(
                save_metric,
                post.tenant_id,
                post.id,
                post.message_id,
                int(getattr(msg, "views", 0) or 0),
                int(getattr(msg, "forwards", 0) or 0),
                _count_reactions(msg),
            )
            saved += 1
        except Exception as e:
            skipped += 1
            logging.error("Metrika xatosi (post %s): %s", post.id, e)

    logging.info("Metrikalar yig'ildi: %d ta saqlandi, %d ta o'tkazib yuborildi.", saved, skipped)
    return saved
