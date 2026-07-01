"""Слой публикации (транспорт).

Отправляет готовый пост в Telegram-канал арендатора и сохраняет запись в
posts_history. Не генерирует контент и не ходит за конфигом арендатора —
получает всё готовым от orchestrator.
"""
import os
import re
import asyncio
import html
from pathlib import Path
from typing import List, Optional, Tuple

from aiogram import Bot
from aiogram.types import FSInputFile, Message
from aiogram.exceptions import TelegramBadRequest
from dotenv import load_dotenv

from database import save_post, save_repost_story, get_tenant_profile
from orchestrator import GeneratedContent
from tiers import allows
from twitter_publisher import cross_post

load_dotenv()

ADMIN_ID = os.getenv("ADMIN_ID")

# Бренд-хэндл для подписи бесплатного тарифа. Telegram сам линкует @username.
BRAND_HANDLE = os.getenv("BRAND_HANDLE", "@publixai")

CAPTION_LIMIT = 1024  # лимит подписи к фото в Telegram

# Парные HTML-теги, которые понимает Telegram, и HTML-сущности — нужны, чтобы
# при обрезке подписи под лимит не разрезать разметку посередине.
_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)(?:[^>]*)?>")
_ENTITY_RE = re.compile(r"&(?:#[0-9]+|#x[0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]*);")
_ELLIPSIS = "…"


def _open_html_tags(fragment: str) -> List[str]:
    """Список незакрытых тегов в HTML-фрагменте (в порядке открытия) — чтобы
    после обрезки корректно их закрыть и не поломать разметку подписи."""
    stack: List[str] = []
    for m in _TAG_RE.finditer(fragment):
        closing, name = m.group(1), m.group(2).lower()
        if closing:
            for idx in range(len(stack) - 1, -1, -1):
                if stack[idx] == name:
                    del stack[idx]
                    break
        else:
            stack.append(name)
    return stack


def _fit_caption(text: str, limit: int) -> str:
    """Обрезает HTML-пост под лимит подписи Telegram, чтобы фото и текст ушли одним
    сообщением, а не раздельно.

    Лимит подписи Telegram считается по ВИДИМОМУ тексту (сама разметка и закрывающие
    теги в счёт не идут), поэтому меряем видимые символы. Не разрываем теги и
    HTML-сущности, режем по границе слова, в конец дописываем «…» и закрываем
    оставшиеся открытыми теги.
    """
    if len(text) <= limit:
        return text

    budget = limit - len(_ELLIPSIS)
    if budget <= 0:
        return _ELLIPSIS

    i, n, visible = 0, len(text), 0
    last_word_cut: Optional[int] = None
    while i < n and visible < budget:
        ch = text[i]
        if ch == "<":
            m = _TAG_RE.match(text, i)
            if m:
                i = m.end()
                continue
        if ch == "&":
            m = _ENTITY_RE.match(text, i)
            if m:
                i = m.end()
                visible += 1
                continue
        if ch in " \n\t":
            last_word_cut = i
        i += 1
        visible += 1

    if i >= n:
        return text  # весь текст уложился по видимой длине — ничего не меняем

    # Предпочитаем границу слова, если она недалеко от точки среза.
    cut = last_word_cut if (last_word_cut is not None and i - last_word_cut < 60) else i
    head = text[:cut].rstrip()
    closers = "".join(f"</{t}>" for t in reversed(_open_html_tags(head)))
    return f"{head}{_ELLIPSIS}{closers}"

# Локализованная подпись бесплатного тарифа по языку канала. {handle} — бренд.
# Для языков вне списка — английский фолбэк (как _language_name в generator).
_ATTRIBUTION = {
    "uz": "🤖 {handle} orqali tayyorlandi",
    "ru": "🤖 Создано в {handle}",
    "en": "🤖 Made with {handle}",
    "kk": "🤖 {handle} арқылы жасалған",
    "kaa": "🤖 {handle} arqalı tayarlandı",
    "tg": "🤖 Бо {handle} тайёр шуд",
}


def _attribution_line(language: str) -> str:
    """Подпись на языке канала. Мультиязычный профиль ("ru, uz") — первый код."""
    code = (language or "uz").split(",")[0].strip().lower()
    template = _ATTRIBUTION.get(code, _ATTRIBUTION["en"])
    return template.format(handle=BRAND_HANDLE)


def _with_attribution(text: str, profile) -> str:
    """Дописывает подпись бренда, если тариф канала её требует (бесплатный starter).

    Идемпотентно: если подпись уже в хвосте — не дублируем (на случай повторной
    публикации того же текста)."""
    if profile is None or not allows(getattr(profile, "subscription_tier", None), "attribution"):
        return text
    line = _attribution_line(getattr(profile, "language", "uz"))
    if text.rstrip().endswith(line):
        return text
    return f"{text}\n\n{line}"


async def _send_text(bot: Bot, chat_id: str, text: str) -> Message:
    """Отправляет текст; при ошибке HTML-разметки повторяет без parse_mode."""
    try:
        return await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
    except TelegramBadRequest:
        return await bot.send_message(chat_id=chat_id, text=text, parse_mode=None)


async def _deliver(bot: Bot, chat_id: str, text: str, image_path: Optional[str]) -> Message:
    """Доставляет пост. С картинкой — всегда одним сообщением (фото + подпись):
    если текст не помещается в лимит подписи, обрезаем его, чтобы фото и текст не
    разъезжались на два сообщения. Без картинки — обычный текст."""
    has_image = bool(image_path) and Path(image_path).exists()

    if has_image:
        caption = _fit_caption(text, CAPTION_LIMIT)
        try:
            return await bot.send_photo(
                chat_id=chat_id,
                photo=FSInputFile(image_path),
                caption=caption,
                parse_mode="HTML",
            )
        except TelegramBadRequest:
            # Подпись не распарсилась как HTML — шлём фото без подписи + текст отдельно.
            await bot.send_photo(chat_id=chat_id, photo=FSInputFile(image_path))
            return await _send_text(bot, chat_id, text)

    return await _send_text(bot, chat_id, text)


async def send_to_telegram(
    bot: Bot, content: GeneratedContent, target_chat_id: str
) -> Tuple[bool, str]:
    """Публикует пост в канал арендатора.

    content = {"text", "image_path", "entry"} (от orchestrator).
    Возвращает (ok: bool, detail: str) — успех и текст для отчёта.
    """
    post_text = content["text"]
    image_path = content["image_path"]
    entry = content["entry"]

    # Подпись бренда для бесплатного тарифа — здесь, в единой точке публикации,
    # чтобы покрыть все пути (автопостинг, «опубликовать сейчас» из бота/панели)
    # и не попадать в превью-черновики, которые сюда не приходят.
    profile = await asyncio.to_thread(get_tenant_profile, entry.tenant_id)
    post_text = _with_attribution(post_text, profile)

    try:
        chat = await bot.get_chat(target_chat_id)
        channel_link = f"@{chat.username}" if chat.username else chat.title

        message = await _deliver(bot, target_chat_id, post_text, image_path)

        # Помечаем как опубликованный и сохраняем в историю арендатора.
        # message_id нужен для последующего сбора метрик поста.
        entry.posted = True
        entry.message_id = message.message_id
        await asyncio.to_thread(save_post, entry)

        # Repost V2: сохраняем «историю» (centroid кластера + ключи всех членов)
        # для семантического дедупа. Заполнено только в repost-режиме с эмбеддингами.
        story_vec = content.get("story_vec")
        story_keys = content.get("story_keys")
        if story_vec and story_keys:
            await asyncio.to_thread(
                save_repost_story, entry.tenant_id, story_vec, story_keys, post_text[:200]
            )

        # Кросс-пост в привязанный X-аккаунт (короткая ≤280-версия). Берём
        # ОРИГИНАЛЬНЫЙ текст (без Telegram-атрибуции). Best-effort: сбой X не должен
        # ронять уже успешную публикацию в Telegram, поэтому глушим исключения.
        try:
            await cross_post(entry.tenant_id, content["text"], image_path)
        except Exception as e:
            print(f"Twitter cross-post o'tkazib yuborildi ({target_chat_id}): {e}")

        if ADMIN_ID:
            post_url = (
                f"https://t.me/{chat.username}/{message.message_id}"
                if chat.username
                else "link mavjud emas"
            )
            admin_msg = (
                f"✅ <b>Post joylandi!</b>\n"
                f"Kanal: {channel_link}\n"
                f"Havola: {post_url}"
            )
            await bot.send_message(chat_id=ADMIN_ID, text=admin_msg, parse_mode="HTML")

        print(f"Bajarildi: {channel_link}")
        return True, channel_link

    except Exception as e:
        print(f"Xato ({target_chat_id} kanalida): {e}")
        if ADMIN_ID:
            try:
                await bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"❌ Xato ({html.escape(str(target_chat_id))}): {html.escape(str(e))}",
                )
            except Exception:
                pass
        return False, str(e)

    finally:
        # Удаляем сгенерированную картинку, чтобы диск не рос бесконечно.
        if image_path and Path(image_path).exists():
            try:
                Path(image_path).unlink()
            except OSError:
                pass
