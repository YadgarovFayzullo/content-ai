"""Оркестрация (backend).

Связывает сборку контекста, движок генерации и историю. Здесь принимаются
решения «что генерировать» (выбор темы), но НЕ происходит сам вызов LLM-логики
(это в generator.py) и НЕ публикация (это в publisher.py).

Все функции синхронные — вызываются из async-кода через asyncio.to_thread.
"""
import base64
import logging
import random
from collections import deque
from pathlib import Path
from typing import TypedDict

from database import (
    TenantProfile,
    PostHistory,
    get_tenant_profile,
    get_recent_post_topics,
    get_topic_keys,
    save_topic_keys,
    find_similar_posts,
)
from context_builder import build_generation_context
from generator import (
    generate_post,
    generate_illustration,
    image_subject_for_topic,
    canonical_topics,
)
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


def _norm(t: str) -> str:
    return " ".join((t or "").split()).lower()


def _canonical_keys(strings: list[str]) -> dict[str, str]:
    """Канонический (язык-независимый) ключ для каждой строки темы.

    Дедуп ротации должен видеть «Сингапур» и «Singapore» как одно и то же, иначе
    один и тот же город выходит дважды на разных языках. Ключи считаем ИИ один раз
    на новую тему и кэшируем в БД (topic_aliases); при недоступности модели
    откатываемся на нормализованную строку — тогда дедуп деградирует до прежнего
    поведения (в пределах одного языка), но ничего не ломает."""
    norm_of = {s: _norm(s) for s in strings if s and s.strip()}
    uniq = sorted({n for n in norm_of.values() if n})
    if not uniq:
        return {}

    cache = get_topic_keys(uniq)  # {norm: key} из кэша
    missing = [n for n in uniq if n not in cache]
    if missing:
        learned = canonical_topics(missing)  # {norm: key}, best-effort
        resolved = {n: (learned.get(n) or n) for n in missing}
        save_topic_keys(resolved)
        cache.update(resolved)
    return {s: cache.get(norm_of[s], norm_of[s]) for s in norm_of}


def pick_topic(profile: TenantProfile) -> tuple[str, bool]:
    """Выбирает тему с дедупом по БД. Возвращает (topic, forced_repeat).

    «Repick, иначе regenerate»: берём тему, которую тенант ДАВНО не публиковал
    (свежую). Если все темы уже недавно освещены — берём наименее недавнюю и
    помечаем forced_repeat=True (вызывающий подаст прошлые посты как «уже
    опубликовано», чтобы движок дал другой угол).

    Дедуп опирается на историю в БД (переживает рестарт и общий для бота/админки),
    а in-process `_RECENT_TOPICS` лишь сглаживает выбор внутри одной сессии.
    Сравнение идёт по КАНОНИЧЕСКОМУ ключу темы (см. _canonical_keys), поэтому
    «Дубай» и «Dubai» считаются одной темой и не повторяются на разных языках."""
    topics = [t.strip() for t in (profile.topics or "").split(",") if t.strip()]
    if not topics:
        return DEFAULT_TOPIC, False
    if len(topics) == 1:
        # Одна тема — ротация невозможна: всегда «обнови угол» относительно прошлых.
        return topics[0], True

    # Темы последних постов из БД (новые→старые). Покрытыми считаем те, что попали
    # в окно последнего «круга» (≈ числа тем) — за круг каждая тема должна выйти раз.
    recent_db = get_recent_post_topics(profile.tenant_id, limit=max(len(topics), 10))
    recent_mem = _RECENT_TOPICS.get(profile.tenant_id, [])

    # Канонические ключи для всех участников сравнения (конфиг + история + сессия).
    keys = _canonical_keys(topics + recent_db + recent_mem)

    def _key(t: str) -> str:
        return keys.get(t, _norm(t))

    recent_db_keys = [_key(t) for t in recent_db]
    covered = set(recent_db_keys[: max(1, len(topics) - 1)]) | {
        _key(t) for t in recent_mem
    }

    fresh = [t for t in topics if _key(t) not in covered]
    if fresh:
        choice, forced = random.choice(fresh), False
    else:
        # Все темы освещены → наименее недавно использованная (макс. индекс ключа в
        # recent_db; отсутствующий в окне считается самым старым).
        def _age(t: str) -> int:
            k = _key(t)
            return recent_db_keys.index(k) if k in recent_db_keys else len(recent_db_keys)

        choice, forced = max(topics, key=_age), True

    window = max(1, len(topics) - 1)
    _RECENT_TOPICS[profile.tenant_id] = (
        _RECENT_TOPICS.get(profile.tenant_id, []) + [choice]
    )[-window:]
    return choice, forced


def _overlap_for_topic(tenant_id: str, topic: str) -> list[str]:
    """Прошлые посты тенанта по этой же теме — «что уже опубликовано» для промпта."""
    return [s["content"] for s in find_similar_posts(tenant_id, topic, limit=3)]


def _make_image(profile: TenantProfile, ctx, topic: str, label: str = "") -> str:
    """Готовит картинку по image_mode профиля. Возвращает путь к файлу или "".

    Источник задаётся профилем:
      "none"  — без картинки (текстовый пост) — ни ИИ, ни сток;
      "stock" — тематическое фото (Pexels), БЕЗ надписей; запрос строим из ТЕМЫ
                (общий вид места: skyline/cityscape/landmark), а не из текста поста;
      "ai" (по умолчанию) — ИИ-иллюстрация.
    Картинка необязательна: при сбое возвращаем "" (текстовый пост), а не валим всё.
    Общий код для автопостинга и превью."""
    image_mode = (getattr(profile, "image_mode", "ai") or "ai").lower()
    if image_mode == "none":
        return ""  # канал настроен на посты без фото
    image_path = ""
    if image_mode == "stock":
        try:
            subject = image_subject_for_topic(topic)
            found = fetch_stock_photo_sync(subject)
            if found:
                image_path = found[0]
        except Exception as e:
            logging.warning(f"Stock-rasm topilmadi ({label}): {e}")
        # фото не нашлось → фолбэк на ИИ-иллюстрацию ниже
    if not image_path:
        try:
            image_path = generate_illustration(ctx, topic)
        except RuntimeError as e:
            logging.warning(f"Rasm yaratilmadi ({label}), matnli post: {e}")
            image_path = ""
    return image_path


def generate_for_tenant(profile: TenantProfile, with_image: bool = True) -> GeneratedContent:
    """Готовит контент для одного арендатора.

    with_image — пост уйдёт с картинкой (тогда текст пишем короче, чтобы влезть в
    подпись к фото). Бросает RuntimeError при сбое генерации. `entry` ещё не
    сохранён — его сохранит publisher после успешной публикации.
    """
    topic, forced_repeat = pick_topic(profile)

    ctx = build_generation_context(profile.tenant_id, topic)
    if ctx is None:
        raise RuntimeError("Tenant profili topilmadi")
    # image_mode="none" → пост без фото: не тратим вызов генерации картинки и пишем
    # текст полной длины (не урезаем под подпись к фото).
    image_off = (getattr(profile, "image_mode", "ai") or "ai").lower() == "none"
    wants_image = with_image and not image_off
    ctx.with_image = wants_image

    # Все темы уже освещены (тема вынужденно повторяется) → подаём прошлые посты по
    # этой теме как «уже опубликовано», чтобы движок выдал заведомо другой угол.
    if forced_repeat:
        ctx.already_published = _overlap_for_topic(profile.tenant_id, topic)

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

    # Без картинки (тариф/ручная публикация её запрещают) не тратим дорогой вызов
    # генерации изображения — пост уйдёт текстом.
    image_path = _make_image(profile, ctx, topic, label=profile.chat_id) if wants_image else ""

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
    """Генерирует пост по запросу (для admin-панели): текст + картинку по image_mode
    (как в автопостинге), чтобы превью совпадало с тем, что реально опубликуется.
    Не публикует и не пишет в историю.

    topic пустой → выбирается из тем профиля (ротация, как у автопостинга).
    extra_context (опц.) — доп. факты/тезисы от оператора, подмешиваются в контекст.

    Возвращает {"text", "topic", "image"} (image — data-URI base64 или ""). Бросает
    RuntimeError, если профиль не найден.
    """
    profile = get_tenant_profile(tenant_id)
    if profile is None:
        raise RuntimeError("Tenant profili topilmadi")

    # Явная тема от оператора уважается (repick не делаем) — дедуп сводится к
    # «обнови угол». Пустая тема → ротация с дедупом, как в автопостинге.
    explicit = (topic or "").strip()
    if explicit:
        chosen_topic, forced_repeat = explicit, True
    else:
        chosen_topic, forced_repeat = pick_topic(profile)

    ctx = build_generation_context(tenant_id, chosen_topic)
    if ctx is None:
        raise RuntimeError("Tenant profili topilmadi")

    # image_mode="none" → превью без фото и текст полной длины (как и в автопостинге).
    ctx.with_image = (getattr(profile, "image_mode", "ai") or "ai").lower() != "none"

    # Прошлые посты по этой теме → «уже опубликовано» (анти-повтор сюжета/фактов).
    if forced_repeat:
        ctx.already_published = _overlap_for_topic(tenant_id, chosen_topic)

    # Доп. контекст оператора подаём как RAG-факты (движок их учитывает).
    extra = (extra_context or "").strip()
    if extra:
        ctx.rag_context = f"{ctx.rag_context}\n\n{extra}" if ctx.rag_context else extra

    # Тот же анти-повтор, что и в превью бота: подмешиваем недавно сгенерированные.
    buf = _RECENT_GENERATED.setdefault(tenant_id, deque(maxlen=12))
    ctx.recent_posts = list(reversed(buf)) + ctx.recent_posts

    post_text = generate_post(ctx)
    buf.append(post_text)

    # Картинку отдаём как data-URI (base64): admin-api не раздаёт файлы статикой, а
    # одна картинка превью прекрасно влезает в JSON-ответ (без файлов/очистки/nginx).
    image_data_uri = ""
    image_path = _make_image(profile, ctx, chosen_topic, label=f"preview:{tenant_id}")
    if image_path:
        try:
            data = Path(image_path).read_bytes()
            mime = "image/png" if image_path.lower().endswith(".png") else "image/jpeg"
            image_data_uri = f"data:{mime};base64," + base64.b64encode(data).decode()
        except Exception as e:
            logging.warning(f"Preview-rasmni o'qib bo'lmadi ({tenant_id}): {e}")

    return {"text": post_text, "topic": chosen_topic, "image": image_data_uri}
