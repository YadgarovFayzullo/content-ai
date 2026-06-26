"""Repost-режим (оркестрация).

V2-поток: пересборка новостей из чужих каналов в канал арендатора с семантической
кластеризацией и дедупом.

    source-каналы → скрейп свежих → чистка (форварды/пустые/реклама)
      → точный дедуп (covered-keys: выбранные посты + члены прошлых историй)
      → эмбеддинги (RAG /embed) → кластеризация по событию
      → семантический дедуп кластеров (не повторять освещённую историю)
      → LLM выбирает лучший кластер → канонизация (объединение фактов источников,
        перевод/адаптация под стиль) → картинка → GeneratedContent (+ story).

Если эмбеддинг-сервис недоступен — graceful-фолбэк на поведение V1 (точный дедуп,
один пост, без кластеризации). Сам вызов LLM и канонизация — в generator.py,
публикация — в publisher.py.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import List, Optional

from bot.rag_client import embed_texts
from bot.scraper import download_post_image, scrape_source
from clustering import centroid, cluster_indices, is_duplicate_story
from context_builder import GenerationContext, RuleView
from news_card import render_news_card
from database import (
    PostHistory,
    get_covered_source_keys,
    get_recent_repost_centroids,
    get_tenant_rules,
    get_tenant_sources,
)
from generator import (
    canonicalize_cluster,
    generate_illustration,
    image_subject,
    news_headline,
    rewrite_source_post,
    select_best_posts,
)
from image_search import fetch_stock_photo
from orchestrator import GeneratedContent, generate_for_tenant

# Сколько последних постов тянуть с каждого источника (свежие новости, не вся
# история — это для RAG). И верхний предел кандидатов, скармливаемых в LLM-отбор
# и эмбеддинги (ограничивает стоимость токенов).
REPOST_FETCH_LIMIT = int(os.getenv("REPOST_FETCH_LIMIT", "30"))
MAX_CANDIDATES = int(os.getenv("REPOST_MAX_CANDIDATES", "60"))
MIN_POST_CHARS = int(os.getenv("REPOST_MIN_CHARS", "40"))

# Пороги V2 (откалиброваны на реальных постах + nomic-embed-text: одно событие
# ≈0.85, разные события ≈0.74–0.78). Кластеризация ВЫСОКАЯ (0.86): склеиваем
# только явно одно событие — false-merge разных новостей хуже, чем недо-склейка
# (несклеенный дубль всё равно поймает семантический дедуп). Дедуп НИЖЕ (0.83):
# агрессивно давим повторы — двойной постинг хуже редкого пропуска похожей новости.
CLUSTER_THRESHOLD = float(os.getenv("REPOST_CLUSTER_THRESHOLD", "0.86"))
DEDUP_THRESHOLD = float(os.getenv("REPOST_DEDUP_THRESHOLD", "0.83"))
DEDUP_DAYS = int(os.getenv("REPOST_DEDUP_DAYS", "14"))

# Картинка для репоста — REPOST_IMAGE_MODE:
#   "card"     (по умолчанию) — чистое тематическое фото из интернета (Pexels) по
#              смыслу новости + заголовок на градиенте; нет фото — фолбэк на
#              AI-иллюстрацию. Фото источника НЕ берём (там часто вшит текст);
#   "photo"    — переиспользуем оригинальное фото источника как есть (без накладки);
#   "generate" — ВСЕГДА генерим свою AI-иллюстрацию.
# Back-compat: старый REPOST_GENERATE_IMAGE учитывается, только если REPOST_IMAGE_MODE
# не задан (true→generate, false→photo).
_legacy_gen = os.getenv("REPOST_GENERATE_IMAGE")
if os.getenv("REPOST_IMAGE_MODE") is None and _legacy_gen is not None:
    _default_mode = "generate" if _legacy_gen.lower() == "true" else "photo"
else:
    _default_mode = "card"
REPOST_IMAGE_MODE = (os.getenv("REPOST_IMAGE_MODE") or _default_mode).lower()

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



def _key(c: dict) -> str:
    return f"{c['source_chat_id']}:{c['id']}"


async def _attach_image(profile, primary: dict, text: str) -> str:
    """Картинка поста по REPOST_IMAGE_MODE. Сбой → пустая строка (пост без фото).

    card  — чистое тематическое фото из интернета (Pexels) + заголовок на градиенте
            (нет фото → AI-иллюстрация);
    photo — оригинальное фото источника как есть;
    generate — AI-иллюстрация всегда.
    """
    mode = REPOST_IMAGE_MODE

    # photo: переиспользуем оригинал источника как есть.
    if mode == "photo":
        if primary.get("has_image"):
            return await download_post_image(
                primary["source_chat_id"], primary["id"]
            ) or ""
        return ""

    # Осмысленный английский subject (визуальная метафора сути новости) — и как
    # поисковый запрос к Pexels, и как промпт для AI-иллюстрации.
    subject = await asyncio.to_thread(image_subject, text)

    if mode == "card":
        # Чистое фото из интернета по смыслу новости (не из канала-источника).
        found = await fetch_stock_photo(subject)
        if found:
            photo_path, author = found
            try:
                headline = await asyncio.to_thread(news_headline, profile, text)
                carded = await asyncio.to_thread(
                    render_news_card, photo_path, headline, author
                )
                return carded or photo_path
            except Exception as e:
                logging.warning(f"News-card render qilinmadi ({profile.chat_id}): {e}")
                return photo_path  # без накладки, но пост не теряем
        # фото не нашлось → падаем в AI-иллюстрацию ниже

    # generate, либо card-фолбэк когда фото не нашлось.
    try:
        ctx = GenerationContext(profile=profile, topic=subject)
        return await asyncio.to_thread(generate_illustration, ctx, subject)
    except Exception as e:
        logging.warning(f"Repost rasm yaratilmadi ({profile.chat_id}): {e}")
        return ""


async def _gather_candidates(profile) -> List[dict]:
    """Скрейп свежих постов всех источников + чистка + точный дедуп.

    Кандидаты со ВСЕХ источников сортируются по КВОТЕ источника (priority desc),
    а внутри равного приоритета — по свежести (новейшие первыми), и лишь затем
    обрезаются до MAX_CANDIDATES. Так посты из приоритетных источников берутся
    первыми, но при равной квоте свежесть решает, а не порядок БД.
    """
    sources = await asyncio.to_thread(get_tenant_sources, profile.tenant_id)
    if not sources:
        raise RuntimeError(
            "Manba kanallar yo'q — repost rejimi uchun kamida bitta manba qo'shing."
        )

    candidates: List[dict] = []
    for s in sources:
        posts = await scrape_source(s.source_chat_id, limit=REPOST_FETCH_LIMIT)
        for p in posts:
            p["source_chat_id"] = s.source_chat_id
            p["priority"] = s.priority
        candidates.extend(posts)

    candidates = clean_candidates(candidates)
    seen = await asyncio.to_thread(get_covered_source_keys, profile.tenant_id)
    candidates = [c for c in candidates if _key(c) not in seen]

    # Квота источника превыше всего, затем свежесть: даты Telethon — UTC ISO,
    # поэтому лексикографической сортировки достаточно; посты без даты — в конец.
    candidates.sort(
        key=lambda c: (c.get("priority", 0), c.get("date") or ""), reverse=True
    )
    return candidates[:MAX_CANDIDATES]


async def prepare_repost(profile) -> Optional[GeneratedContent]:
    """Готовит ОДИН репост. None — новых постов/историй нет. RuntimeError — нет
    источников или сорвалась генерация. `entry` сохранит publisher после публикации.
    """
    candidates = await _gather_candidates(profile)
    if not candidates:
        return None

    rules = await asyncio.to_thread(get_tenant_rules, profile.tenant_id)
    rule_views = [RuleView(r.rule_type, r.rule_value) for r in rules]

    vectors = await embed_texts([c["text"] for c in candidates])

    if vectors:
        content = await _build_clustered(profile, candidates, vectors, rule_views)
    else:
        # Фолбэк V1: без эмбеддингов — один лучший пост, точный дедуп.
        logging.warning(
            "Repost: embeddinglar yo'q (RAG/embed ishlamayapti) — V1 fallback (klastersiz)."
        )
        content = await _build_single(profile, candidates, rule_views)
    return content


async def _build_clustered(
    profile, candidates: List[dict], vectors: List[List[float]], rule_views
) -> Optional[GeneratedContent]:
    """V2: кластеризация → семантический дедуп → выбор кластера → канонизация."""
    clusters = cluster_indices(vectors, CLUSTER_THRESHOLD)
    recent = await asyncio.to_thread(
        get_recent_repost_centroids, profile.tenant_id, DEDUP_DAYS
    )

    fresh: List[tuple[List[int], List[float]]] = []
    for idxs in clusters:
        c_centroid = centroid([vectors[i] for i in idxs])
        if is_duplicate_story(c_centroid, recent, DEDUP_THRESHOLD):
            continue
        fresh.append((idxs, c_centroid))
    if not fresh:
        return None

    # Представитель кластера для отбора — самый длинный (полный) пост группы.
    reps = [
        {"text": candidates[max(idxs, key=lambda i: len(candidates[i]["text"]))]["text"]}
        for idxs, _ in fresh
    ]
    sel = await asyncio.to_thread(select_best_posts, profile, reps, 1)
    chosen_idxs, chosen_centroid = fresh[sel[0] if sel else 0]

    member_texts = [candidates[i]["text"] for i in chosen_idxs]
    member_keys = [_key(candidates[i]) for i in chosen_idxs]
    primary = candidates[max(chosen_idxs, key=lambda i: len(candidates[i]["text"]))]

    text = await asyncio.to_thread(
        canonicalize_cluster, profile, member_texts, rule_views
    )
    image_path = await _attach_image(profile, primary, text)

    entry = _make_entry(profile, text, image_path, primary)
    content: GeneratedContent = {
        "text": text,
        "image_path": image_path,
        "entry": entry,
        "story_vec": chosen_centroid,
        "story_keys": member_keys,
    }
    logging.info(
        "Repost (klaster): %d ta manba post birlashtirildi (%s)",
        len(member_keys), profile.chat_id,
    )
    return content


async def _build_single(profile, candidates: List[dict], rule_views) -> Optional[GeneratedContent]:
    """Фолбэк V1: выбрать один лучший пост и переписать (без кластеризации)."""
    sel = await asyncio.to_thread(select_best_posts, profile, candidates, 1)
    if not sel:
        return None
    primary = candidates[sel[0]]
    text = await asyncio.to_thread(
        rewrite_source_post, profile, primary["text"], rule_views
    )
    image_path = await _attach_image(profile, primary, text)
    entry = _make_entry(profile, text, image_path, primary)
    # story_* не заполняем: без эмбеддинга нет centroid; дедуп держится на
    # PostHistory (exact) как в V1.
    return {"text": text, "image_path": image_path, "entry": entry}


def _make_entry(profile, text: str, image_path: str, primary: dict) -> PostHistory:
    return PostHistory(
        tenant_id=profile.tenant_id,
        topic="repost",
        content=text,
        image_path=image_path,
        posted=False,
        source_chat_id=primary["source_chat_id"],
        source_message_id=primary["id"],
    )


async def produce_content(profile, with_image: bool = True) -> GeneratedContent:
    """Единая точка генерации контента для канала — ветвится по content_mode.

    with_image — пост уйдёт с картинкой (тариф/публикация её разрешают). В topic-режиме
    влияет на длину текста (под лимит подписи к фото) и на то, генерировать ли картинку.

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
    return await asyncio.to_thread(generate_for_tenant, profile, with_image)
