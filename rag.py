"""RAG-СЕАМ (точка интеграции, без реализации).

Здесь нет векторного хранилища, эмбеддингов и логики поиска — только контракт.
Ваша внешняя RAG-система реализует протокол `RagRetriever` и подключается одним
вызовом `set_retriever(...)` на старте приложения.

Поток данных:
    backend (context_builder) -> get_retriever().retrieve(tenant_id, topic)
                              -> кладёт результат в GenerationContext.rag_context
                              -> движок генерации использует его как
                                 приоритетный источник фактов.

Пока ретривер не подключён, активен NullRagRetriever (возвращает None), и движок
работает на общих знаниях LLM. Никаких изменений в остальных слоях при интеграции
не требуется — только реализовать протокол и вызвать set_retriever().
"""
from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class RagRetriever(Protocol):
    """Контракт RAG-ретривера. Реализуйте его в своей системе.

    Должен быть потокобезопасным и быстрым (вызывается синхронно из воркер-потока
    backend-слоя). Может вернуть None, если релевантного контекста нет.
    """

    def retrieve(
        self,
        tenant_id: str,
        topic: str,
        include_own: bool = True,
        include_references: bool = True,
    ) -> Optional[str]:
        ...


class NullRagRetriever:
    """Заглушка по умолчанию: RAG ещё не подключён."""

    def retrieve(
        self,
        tenant_id: str,
        topic: str,
        include_own: bool = True,
        include_references: bool = True,
    ) -> Optional[str]:
        return None


_retriever: RagRetriever = NullRagRetriever()


def get_retriever() -> RagRetriever:
    """Возвращает активный ретривер (по умолчанию — NullRagRetriever)."""
    return _retriever


def set_retriever(retriever: RagRetriever) -> None:
    """Подключает вашу RAG-реализацию. Вызвать один раз при старте приложения.

    Пример:
        from rag import set_retriever
        set_retriever(MyVectorRetriever(...))
    """
    global _retriever
    _retriever = retriever
