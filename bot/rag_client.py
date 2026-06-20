"""HTTP-клиент RAG-сервиса (~/Desktop/RAG).

Тонкая обёртка над двумя эндпоинтами: /index (загрузка истории постов канала в
векторное хранилище) и /search (выборка релевантных фактов под тему поста).
Все вызовы изолированы по tenant_id.
"""
from __future__ import annotations

import logging
import os
import random
from typing import Any, Optional

import httpx

from bot.config import RAG_URL


async def index_posts(
    tenant_id: str, posts: list[dict[str, Any]], is_reference: bool = False
) -> int:
    """Шлёт посты канала в RAG-сервис на индексацию. Возвращает число чанков.

    is_reference=True — посты референс-канала (отдельная квота в retrieval).
    """
    if not posts:
        return 0
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{RAG_URL}/index",
                json={
                    "tenant_id": tenant_id,
                    "posts": posts,
                    "is_reference": is_reference,
                },
            )
            resp.raise_for_status()
            return int(resp.json().get("indexed", 0))
    except Exception as e:
        logging.error(f"RAG /index xatosi ({tenant_id}): {e}")
        return 0


async def embed_texts(texts: list[str]) -> Optional[list[list[float]]]:
    """Возвращает эмбеддинги для списка текстов (RAG /embed) или None при сбое.

    Используется repost-режимом для семантической кластеризации/дедупа. None —
    сигнал вызывающему перейти на фолбэк (точный дедуп, без кластеризации), чтобы
    репост не падал из-за недоступного эмбеддинг-сервиса."""
    if not texts:
        return []
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(f"{RAG_URL}/embed", json={"texts": texts})
            resp.raise_for_status()
            vectors = resp.json().get("vectors")
            if not isinstance(vectors, list) or len(vectors) != len(texts):
                logging.error("RAG /embed: vektorlar soni mos kelmadi")
                return None
            return vectors
    except Exception as e:
        logging.error(f"RAG /embed xatosi: {e}")
        return None


async def delete_tenant(tenant_id: str) -> bool:
    """Удаляет вектора арендатора из RAG (при удалении канала). True при успехе."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{RAG_URL}/delete", json={"tenant_id": tenant_id}
            )
            resp.raise_for_status()
            return True
    except Exception as e:
        logging.error(f"RAG /delete xatosi ({tenant_id}): {e}")
        return False


# Отсев слабо-релевантных фактов. Серверный порог 0.5 (cosine) мягкий: под
# «networking» прилетали дата-центры/увольнения (score 0.53–0.56), которые модель
# впихивала не по теме. На РЕАЛЬНЫХ данных score'ы кучкуются в узкой полосе
# (напр. investment: 0.60–0.64), поэтому ОТНОСИТЕЛЬНОЕ окно почти ничего не режет.
# Работает АБСОЛЮТНЫЙ порог: по-настоящему релевантные факты дают ≥~0.60, а
# офтопик-хвост на «рыхлых» темах — ниже. Если под тему в корпусе нет ничего
# крепкого, пул честно пустеет → генерим из общих знаний, а не тащим мусор.
# Оба порога env-переопределяемы (разные корпуса → разные распределения).
REL_SCORE_FLOOR = float(os.getenv("RAG_SCORE_FLOOR", "0.57"))
REL_MARGIN = float(os.getenv("RAG_REL_MARGIN", "0.12"))


def _search(
    tenant_id: str, topic: str, limit: int, is_reference: Optional[bool]
) -> list[tuple[str, float]]:
    """Один запрос к /search. Возвращает список (content, score)."""
    try:
        resp = httpx.post(
            f"{RAG_URL}/search",
            json={
                "tenant_id": tenant_id,
                "query": topic,
                "limit": limit,
                "is_reference": is_reference,
            },
            timeout=15,
        )
        resp.raise_for_status()
        return [
            (c["content"], float(c.get("score", 0.0)))
            for c in resp.json().get("results", [])
            if c.get("content")
        ]
    except Exception as e:
        logging.error(f"RAG /search xatosi ({tenant_id}, ref={is_reference}): {e}")
        return []


def _relevant_pool(
    hits: list[tuple[str, float]], seen: set[str]
) -> list[str]:
    """Дедуп + отсечение слабо-релевантного хвоста.

    hits отсортированы по убыванию score. Оставляем факты со score не ниже
    max(REL_SCORE_FLOOR, лучший - REL_MARGIN): абсолютный пол режет офтопик на
    «рыхлых» темах, относительное окно — хвост при высоком топе. Дедуп по
    нормализованным первым 160 символам (канал репостит одно и то же)."""
    if not hits:
        return []
    top = hits[0][1]
    cutoff = max(REL_SCORE_FLOOR, top - REL_MARGIN)
    out: list[str] = []
    for content, score in hits:
        if score < cutoff:
            break  # дальше только менее релевантное (список отсортирован)
        key = " ".join(content.split())[:160].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(content)
    return out


def retrieve(tenant_id: str, topic: str, own_limit: int = 4, ref_limit: int = 3) -> Optional[str]:
    """Квотированная выборка фактов: свой канал + референс-каналы раздельно.

    Свой канал доминирует по similarity, поэтому референс-каналам выделяется
    отдельная квота (иначе они никогда не попадают в контекст). Результаты
    дедуплицируются по тексту (канал часто репостит одно и то же).

    Релевантность vs разнообразие: забираем ШИРОКИЙ пул (cap 20), отсекаем
    слабо-связанный хвост по относительному порогу (REL_MARGIN — чтобы под тему
    не лез нерелевантный факт), а из оставшихся ДЕЙСТВИТЕЛЬНО релевантных
    СЛУЧАЙНО сэмплим. Так на одну тему бот вытаскивает то один факт, то другой
    (разнообразие), но все — по теме (релевантность).

    Синхронная (вызывается из воркер-потока backend-слоя).
    """
    seen: set[str] = set()

    # Свой канал: широкий пул → релевантный отсев → случайная выборка own_limit.
    own_pool = (
        _relevant_pool(_search(tenant_id, topic, 20, is_reference=False), seen)
        if own_limit > 0
        else []
    )
    own_out = random.sample(own_pool, min(own_limit, len(own_pool))) if own_pool else []

    # Референс-каналы: то же самое отдельным пулом/квотой.
    ref_pool = (
        _relevant_pool(_search(tenant_id, topic, 20, is_reference=True), seen)
        if ref_limit > 0
        else []
    )
    ref_out = random.sample(ref_pool, min(ref_limit, len(ref_pool))) if ref_pool else []

    return "\n\n".join(own_out + ref_out) or None
