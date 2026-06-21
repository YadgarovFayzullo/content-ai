"""Оркестрация (backend).

Связывает сборку контекста, движок генерации и историю. Здесь принимаются
решения «что генерировать» (выбор темы), но НЕ происходит сам вызов LLM-логики
(это в generator.py) и НЕ публикация (это в publisher.py).

Все функции синхронные — вызываются из async-кода через asyncio.to_thread.
"""
import logging
import random
from collections import deque
from typing import TypedDict

from database import TenantProfile, PostHistory, get_tenant_profile
from context_builder import build_generation_context
from generator import generate_post, generate_illustration, image_subject
from image_search import fetch_stock_photo_sync

DEFAULT_TOPIC = "psixologiya, fan yoki texnologiya haqida qiziqarli mini-fakt"

# Память недавно выбранных тем по tenant_id (в процессе). Чистый random.choice
# кластеризуется — одна тема выпадает подряд по много раз. Здесь храним последние
# выборы и не повторяем их, пока не пройдём по всем темам (равномерная ротация).
# Покрывает и preview (не сохраняется в историю), и автопостинг в рамках сессии.
_RECENT_TOPICS: dict[str, list[str]] = {}

# Недавно сгенерированные тексты по tenant_id (в процессе). Нужны для анти-повтора
# превью, которые не попадают в историю БД. maxlen задаётся при создании deque.
_RECENT_GENERATED: dict[str, deque] = {}


class GeneratedContent(TypedDict, total=False):
    """Полезная нагрузка, передаваемая в publisher.send_to_telegram.

    text/image_path/entry — обязательны. story_vec/story_keys заполняет только
    repost-режим V2 (centroid кластера и ключи всех его членов): publisher после
    успешной публикации сохраняет из них RepostStory для семантического дедупа.
    В topic-режиме их нет.
    """

    text: str
    image_path: str
    entry: PostHistory
    story_vec: list[float]
    story_keys: list[str]


def pick_topic(profile: TenantProfile) -> str:
    """Тему из `topics` арендатора с ротацией: не повторяет недавние, пока не
    переберёт все. Так посты равномерно покрывают все темы, а не зацикливаются."""
    topics = [t.strip() for t in (profile.topics or "").split(",") if t.strip()]
    if not topics:
        return DEFAULT_TOPIC
    if len(topics) == 1:
        return topics[0]

    recent = _RECENT_TOPICS.get(profile.tenant_id, [])
    fresh = [t for t in topics if t not in recent]
    choice = random.choice(fresh or topics)

    # Окно = все темы кроме одной → тема не повторится, пока не используем остальные.
    window = max(1, len(topics) - 1)
    _RECENT_TOPICS[profile.tenant_id] = (recent + [choice])[-window:]
    return choice


def generate_for_tenant(profile: TenantProfile) -> GeneratedContent:
    """Готовит контент для одного арендатора.

    Бросает RuntimeError при сбое генерации. `entry` ещё не сохранён — его
    сохранит publisher после успешной публикации.
    """
    topic = pick_topic(profile)

    ctx = build_generation_context(profile.tenant_id, topic)
    if ctx is None:
        raise RuntimeError("Tenant profili topilmadi")

    # Превью не пишутся в историю, поэтому правило «не повторяй RECENT POSTS» их не
    # видит — два превью подряд выходили идентичными. Подмешиваем недавно
    # сгенерированные (в т.ч. preview) тексты в recent_posts как анти-повтор.
    # reversed: deque хранит старые слева, новые справа, а промпт показывает только
    # первые N постов — без разворота модель видела самые СТАРЫЕ превью и никогда
    # только что сгенерированные, поэтому повторялась дословно.
    buf = _RECENT_GENERATED.setdefault(profile.tenant_id, deque(maxlen=12))
    ctx.recent_posts = list(reversed(buf)) + ctx.recent_posts

    post_text = generate_post(ctx)
    buf.append(post_text)

    # Источник картинки задаётся профилем (image_mode):
    #   "stock" — чистое тематическое фото из интернета (Pexels), БЕЗ надписей;
    #   "ai" (по умолчанию) — ИИ-иллюстрация.
    # Картинка необязательна: при сбое публикуем текстовый пост, а не валим его.
    image_mode = (getattr(profile, "image_mode", "ai") or "ai").lower()
    image_path = ""
    if image_mode == "stock":
        # Визуальный subject (англ.) из текста поста — лучший запрос к Pexels,
        # чем сырая тема (для «Турции» даст конкретную сцену).
        try:
            subject = image_subject(post_text) or topic
            found = fetch_stock_photo_sync(subject)
            if found:
                image_path = found[0]
        except Exception as e:
            logging.warning(f"Stock-rasm topilmadi ({profile.chat_id}): {e}")
        # фото не нашлось → фолбэк на ИИ-иллюстрацию ниже
    if not image_path:
        try:
            image_path = generate_illustration(ctx, topic)
        except RuntimeError as e:
            logging.warning(f"Rasm yaratilmadi ({profile.chat_id}), matnli post: {e}")
            image_path = ""

    entry = PostHistory(
        tenant_id=profile.tenant_id,
        topic=topic,
        content=post_text,
        image_path=image_path,
        posted=False,
    )
    return GeneratedContent(text=post_text, image_path=image_path, entry=entry)


def generate_preview(
    tenant_id: str,
    topic: str | None = None,
    extra_context: str | None = None,
) -> dict:
    """Генерирует ТЕКСТ поста по запросу (для admin-панели). Не публикует и не
    пишет в историю. Картинку не генерирует — только текст.

    topic пустой → выбирается из тем профиля (ротация, как у автопостинга).
    extra_context (опц.) — доп. факты/тезисы от оператора, подмешиваются в контекст.

    Возвращает {"text", "topic"}. Бросает RuntimeError, если профиль не найден.
    """
    profile = get_tenant_profile(tenant_id)
    if profile is None:
        raise RuntimeError("Tenant profili topilmadi")

    chosen_topic = (topic or "").strip() or pick_topic(profile)

    ctx = build_generation_context(tenant_id, chosen_topic)
    if ctx is None:
        raise RuntimeError("Tenant profili topilmadi")

    # Доп. контекст оператора подаём как RAG-факты (движок их учитывает).
    extra = (extra_context or "").strip()
    if extra:
        ctx.rag_context = f"{ctx.rag_context}\n\n{extra}" if ctx.rag_context else extra

    # Тот же анти-повтор, что и в превью бота: подмешиваем недавно сгенерированные.
    buf = _RECENT_GENERATED.setdefault(tenant_id, deque(maxlen=12))
    ctx.recent_posts = list(reversed(buf)) + ctx.recent_posts

    post_text = generate_post(ctx)
    buf.append(post_text)

    return {"text": post_text, "topic": chosen_topic}
