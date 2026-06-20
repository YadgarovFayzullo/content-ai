"""Семантическая кластеризация и дедуп новостей (V2, repost-режим).

Без тяжёлых ML-библиотек: косинус на чистом Python. Кандидатов немного (≤ ~60),
поэтому жадная O(n²) кластеризация дёшева. Эмбеддинги приходят из RAG /embed
(см. bot.rag_client.embed_texts) — здесь только математика над готовыми векторами.
"""
from __future__ import annotations

import math
from typing import List, Sequence

Vector = Sequence[float]


def cosine(a: Vector, b: Vector) -> float:
    """Косинусная близость двух векторов (0..1 для эмбеддингов). 0 при нулевом."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def centroid(vectors: List[Vector]) -> List[float]:
    """Усреднённый вектор (центроид кластера). Пустой список → []."""
    n = len(vectors)
    if n == 0:
        return []
    dim = len(vectors[0])
    acc = [0.0] * dim
    for v in vectors:
        for i, x in enumerate(v):
            acc[i] += x
    return [x / n for x in acc]


def cluster_indices(vectors: List[Vector], threshold: float) -> List[List[int]]:
    """Жадная кластеризация по косинусу. Каждый вектор кладём в кластер с
    максимальной близостью к его членам, если она ≥ threshold; иначе — новый
    кластер. Возвращает списки индексов (по порядку входа).

    threshold — главный регулятор «склейки»: выше → дробнее (меньше риск
    объединить несвязанные новости), ниже → крупнее группы."""
    clusters: List[List[int]] = []
    for i, v in enumerate(vectors):
        best_c: List[int] | None = None
        best_s = threshold
        for c in clusters:
            s = max(cosine(v, vectors[j]) for j in c)
            if s >= best_s:
                best_s = s
                best_c = c
        if best_c is None:
            clusters.append([i])
        else:
            best_c.append(i)
    return clusters


def is_duplicate_story(
    cluster_centroid: Vector,
    recent_centroids: List[Vector],
    threshold: float,
) -> bool:
    """True, если центроид кластера семантически совпадает с одной из уже
    опубликованных историй (≥ threshold). Порог дедупа держим ВЫШЕ порога
    кластеризации — консервативно, чтобы не выкинуть реально новую новость."""
    if not cluster_centroid:
        return False
    return any(
        r and cosine(cluster_centroid, r) >= threshold for r in recent_centroids
    )
