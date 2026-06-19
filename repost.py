"""Repost-режим (оркестрация).

Пересборка новостей из чужих каналов в канал арендатора:
    source-каналы → скрейп свежих постов → чистка (форварды/пустые/реклама) →
    дедуп (уже репостнутые) → LLM выбирает лучший → LLM переводит+адаптирует под
    стиль канала → картинка (оригинал, иначе генерим) → GeneratedContent.

Сам вызов LLM (отбор/переписывание) живёт в generator.py, публикация — в
publisher.py. Здесь — только «что собрать и в каком порядке».

Модуль async: скрейп и скачивание медиа идут через Telethon (async), а
синхронные шаги (БД, LLM) вызываются через asyncio.to_thread.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import List, Optional

from bot.scraper import download_post_image, scrape_channel_history
from context_builder import GenerationContext, RuleView
from database import (
    PostHistory,
    get_reposted_source_keys,
    get_tenant_rules,
    get_tenant_sources,
)
from generator import generate_illustration, rewrite_source_post, select_best_posts
from orchestrator import GeneratedContent, generate_for_tenant

# Сколько последних постов тянуть с каждого источника (свежие новости, не вся
# история — это для RAG). И верхний предел кандидатов, скармливаемых в LLM-отбор
# (ограничивает стоимость токенов).
REPOST_FETCH_LIMIT = int(os.getenv("REPOST_FETCH_LIMIT", "30"))
MAX_CANDIDATES = int(os.getenv("REPOST_MAX_CANDIDATES", "60"))
MIN_POST_CHARS = int(os.getenv("REPOST_MIN_CHARS", "40"))

# Картинка для репоста:
#   True  (по умолчанию) — ВСЕГДА генерим свою иллюстрацию (оригинал не берём);
#   False — переиспользуем оригинальное фото поста (если есть), иначе текстом.
GENERATE_IMAGE = os.getenv("REPOST_GENERATE_IMAGE", "true").lower() == "true"

# Простые маркеры рекламы/спама (uz/ru). Без ML — грубый отсев очевидного мусора;
# тонкий отбор делает LLM на шаге select_best_posts.
_AD_MARKERS = re.compile(
    r"\b(reklama|chegirma|aksiya|promo[\s-]?kod|sotuvda|sotib oling|buyurtma\b|"
    r"скидк|реклам|акци|промокод|розыгрыш|giveaway|купить|заказать)\b",
    re.IGNORECASE,
)


def _is_ad(text: str) -> bool:
    return bool(_AD_MARKERS.search(text))


def clean_candidates(posts: List[dict]) -> List[dict]:
    """Минимальная чистка (V1, без ML): убрать форварды, пустые/слишком короткие
    и очевидную рекламу. Тонкий отбор «лучших» — на LLM."""
    out: List[dict] = []
    for p in posts:
        text = (p.get("text") or "").strip()
        if not text or len(text) < MIN_POST_CHARS:
            continue
        if p.get("is_forward"):
            continue
        if _is_ad(text):
            continue
        out.append(p)
    return out


def _subject(text: str) -> str:
    """Тема для генерации картинки — первая содержательная строка без HTML."""
    plain = re.sub(r"<[^>]+>", "", text).strip()
    first = next((ln for ln in plain.splitlines() if ln.strip()), plain)
    return first[:120] or "news"


async def prepare_repost(profile) -> Optional[GeneratedContent]:
    """Готовит ОДИН репост для канала. Возвращает None, если новых постов нет.

    Бросает RuntimeError, если у канала нет источников или сорвалась переписка.
    `entry` ещё не сохранён — его сохранит publisher после успешной публикации.
    """
    sources = await asyncio.to_thread(get_tenant_sources, profile.tenant_id)
    if not sources:
        raise RuntimeError(
            "Manba kanallar yo'q — repost rejimi uchun kamida bitta manba qo'shing."
        )

    seen = await asyncio.to_thread(get_reposted_source_keys, profile.tenant_id)

    candidates: List[dict] = []
    for s in sources:
        posts = await scrape_channel_history(s.source_chat_id, limit=REPOST_FETCH_LIMIT)
        for p in posts:
            p["source_chat_id"] = s.source_chat_id
        candidates.extend(posts)

    candidates = clean_candidates(candidates)
    candidates = [
        c for c in candidates
        if f"{c['source_chat_id']}:{c['id']}" not in seen
    ]
    if not candidates:
        return None
    candidates = candidates[:MAX_CANDIDATES]

    idxs = await asyncio.to_thread(select_best_posts, profile, candidates, 1)
    if not idxs:
        return None
    chosen = candidates[idxs[0]]

    rules = await asyncio.to_thread(get_tenant_rules, profile.tenant_id)
    rule_views = [RuleView(r.rule_type, r.rule_value) for r in rules]
    text = await asyncio.to_thread(
        rewrite_source_post, profile, chosen["text"], rule_views
    )

    # Картинка: по умолчанию генерим свою иллюстрацию (оригинал не берём). Если
    # генерация выключена (REPOST_GENERATE_IMAGE=false) — переиспользуем оригинал.
    image_path = ""
    if GENERATE_IMAGE:
        try:
            subject = _subject(text)
            ctx = GenerationContext(profile=profile, topic=subject)
            image_path = await asyncio.to_thread(generate_illustration, ctx, subject)
        except Exception as e:
            logging.warning(f"Repost rasm yaratilmadi ({profile.chat_id}): {e}")
            image_path = ""
    elif chosen.get("has_image"):
        image_path = (
            await download_post_image(chosen["source_chat_id"], chosen["id"]) or ""
        )

    entry = PostHistory(
        tenant_id=profile.tenant_id,
        topic="repost",
        content=text,
        image_path=image_path,
        posted=False,
        source_chat_id=chosen["source_chat_id"],
        source_message_id=chosen["id"],
    )
    return GeneratedContent(text=text, image_path=image_path, entry=entry)


async def produce_content(profile) -> GeneratedContent:
    """Единая точка генерации контента для канала — ветвится по content_mode.

    repost — пересборка чужой новости (prepare_repost);
    topic  — оригинальный пост на тему канала (generate_for_tenant).
    Бросает RuntimeError при сбое или отсутствии новых постов в repost-режиме.
    """
    if (getattr(profile, "content_mode", "topic") or "topic") == "repost":
        content = await prepare_repost(profile)
        if content is None:
            raise RuntimeError(
                "Yangi post topilmadi: manbalarda yangi (hali repost qilinmagan) "
                "postlar yo'q."
            )
        return content
    return await asyncio.to_thread(generate_for_tenant, profile)
