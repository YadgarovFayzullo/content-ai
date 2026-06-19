"""Слой публикации (транспорт).

Отправляет готовый пост в Telegram-канал арендатора и сохраняет запись в
posts_history. Не генерирует контент и не ходит за конфигом арендатора —
получает всё готовым от orchestrator.
"""
import os
import asyncio
import html
from pathlib import Path
from typing import Optional, Tuple

from aiogram import Bot
from aiogram.types import FSInputFile, Message
from aiogram.exceptions import TelegramBadRequest
from dotenv import load_dotenv

from database import save_post
from orchestrator import GeneratedContent

load_dotenv()

ADMIN_ID = os.getenv("ADMIN_ID")

CAPTION_LIMIT = 1024  # лимит подписи к фото в Telegram


async def _send_text(bot: Bot, chat_id: str, text: str) -> Message:
    """Отправляет текст; при ошибке HTML-разметки повторяет без parse_mode."""
    try:
        return await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
    except TelegramBadRequest:
        return await bot.send_message(chat_id=chat_id, text=text, parse_mode=None)


async def _deliver(bot: Bot, chat_id: str, text: str, image_path: Optional[str]) -> Message:
    """Доставляет пост: фото+подпись если влезает, иначе фото и текст раздельно."""
    has_image = bool(image_path) and Path(image_path).exists()

    if has_image and len(text) <= CAPTION_LIMIT:
        try:
            return await bot.send_photo(
                chat_id=chat_id,
                photo=FSInputFile(image_path),
                caption=text,
                parse_mode="HTML",
            )
        except TelegramBadRequest:
            # Подпись не распарсилась как HTML — шлём фото без подписи + текст отдельно.
            await bot.send_photo(chat_id=chat_id, photo=FSInputFile(image_path))
            return await _send_text(bot, chat_id, text)

    if has_image:
        # Текст длиннее лимита подписи — фото отдельным сообщением.
        await bot.send_photo(chat_id=chat_id, photo=FSInputFile(image_path))

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

    try:
        chat = await bot.get_chat(target_chat_id)
        channel_link = f"@{chat.username}" if chat.username else chat.title

        message = await _deliver(bot, target_chat_id, post_text, image_path)

        # Помечаем как опубликованный и сохраняем в историю арендатора.
        # message_id нужен для последующего сбора метрик поста.
        entry.posted = True
        entry.message_id = message.message_id
        await asyncio.to_thread(save_post, entry)

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
