"""Backend-слой сборки контекста.

Вся бизнес-логика живёт здесь: выборка профиля/правил/истории по tenant_id,
вызов RAG-ретривера и сборка единого `GenerationContext`. Движок генерации
(generator.py) НЕ обращается к БД и НЕ решает, что доставать — он лишь
потребляет уже собранный контекст.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from database import (
    TenantProfile,
    get_tenant_profile,
    get_tenant_rules,
    get_recent_posts,
    get_top_posts,
)
from rag import get_retriever


@dataclass
class RuleView:
    """Плоское представление правила (без привязки к ORM-сессии)."""

    rule_type: str
    rule_value: str


@dataclass
class GenerationContext:
    """Полностью собранный контекст для движка генерации.

    Содержит только данные одного арендатора. Движок не должен запрашивать
    ничего сверх того, что лежит здесь.
    """

    profile: TenantProfile
    topic: str
    rules: List[RuleView] = field(default_factory=list)
    recent_posts: List[str] = field(default_factory=list)
    # Лучшие по engagement посты канала — образцы «что заходит» (feedback loop).
    top_posts: List[str] = field(default_factory=list)
    rag_context: Optional[str] = None  # заполняется RAG-слоем, если подключён


def build_generation_context(
    tenant_id: str,
    topic: str,
    recent_limit: int = 5,
) -> Optional[GenerationContext]:
    """Собирает контекст для одного арендатора. Возвращает None, если профиль не найден.

    Синхронная функция — вызывайте через asyncio.to_thread из async-кода.
    """
    profile = get_tenant_profile(tenant_id)
    if profile is None:
        return None

    rules = [RuleView(r.rule_type, r.rule_value) for r in get_tenant_rules(tenant_id)]
    recent_posts = [p.content for p in get_recent_posts(tenant_id, limit=recent_limit)]
    top_posts = get_top_posts(tenant_id, limit=3)

    # RAG-слой: подмешиваем факты только если у канала включён use_rag.
    # Для каналов-агрегаторов анонсов RAG отключают, чтобы бот не воспроизводил
    # чужие события как свои.
    # Два НЕЗАВИСИМЫХ переключателя:
    #   use_rag        — заземлять на постах СВОЕГО канала
    #   use_references — подмешивать факты из РЕФЕРЕНС-каналов
    # Раньше рефералки гейтились через use_rag, и при выключенном RAG они молчали,
    # даже будучи включёнными. Теперь каждый источник управляется своим флагом.
    if profile.use_rag or profile.use_references:
        rag_context = get_retriever().retrieve(
            tenant_id,
            topic,
            include_own=profile.use_rag,
            include_references=profile.use_references,
        )
    else:
        rag_context = None

    return GenerationContext(
        profile=profile,
        topic=topic,
        rules=rules,
        recent_posts=recent_posts,
        top_posts=top_posts,
        rag_context=rag_context,
    )
