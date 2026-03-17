import asyncio
import logging
import sys
import os
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from generator import create_daily_content
from publisher import send_to_telegram
from database import (
    create_db_and_tables,
    add_channel_to_db,
    get_all_channels,
    remove_channel_from_db,
)
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

# --- КЛАВИАТУРЫ ---


def get_admin_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.button(text="📋 Kanallar ro'yxati")
    builder.button(text="🚀 Hozir post qilish")
    builder.button(text="➕ Kanal qo'shish")
    builder.button(text="🗑 Kanalni o'chirish")
    builder.button(text="ℹ️ Yordam")
    builder.adjust(2, 2, 1)
    return builder.as_markup(resize_keyboard=True)


def get_channels_delete_keyboard():
    builder = InlineKeyboardBuilder()
    channels = get_all_channels()
    for char_id in channels:
        builder.button(text=f"❌ {char_id}", callback_data=f"del_{char_id}")
    builder.adjust(1)
    return builder.as_markup()


# --- ФУНКЦИИ ПЛАНИРОВЩИКА ---


async def scheduled_job():
    channels = get_all_channels()
    if not channels:
        return
    for channel_id in channels:
        content = create_daily_content()
        if content:
            await send_to_telegram(content, channel_id)
            await asyncio.sleep(5)


# --- ОБРАБОТЧИКИ ---


@dp.message(Command("start"))
@dp.message(F.text == "ℹ️ Yordam")
async def cmd_start(message: types.Message):
    if str(message.from_user.id) != ADMIN_ID:
        return
    await message.answer(
        "👋 <b>Boshqaruv paneli</b>\n\n"
        "Kanallarni boshqarish uchun pastdagi tugmalardan foydalaning.",
        reply_markup=get_admin_keyboard(),
    )


@dp.message(F.text == "📋 Kanallar ro'yxati")
async def menu_list(message: types.Message):
    if str(message.from_user.id) != ADMIN_ID:
        return
    channels = get_all_channels()
    if not channels:
        return await message.answer("Hozircha hech qanday kanal qo'shilmagan.")
    response = "📋 <b>Sizning kanallaringiz:</b>\n\n" + "\n".join(
        [f"• {c}" for c in channels]
    )
    await message.answer(response)


@dp.message(F.text == "🚀 Hozir post qilish")
async def menu_post_now(message: types.Message):
    if str(message.from_user.id) != ADMIN_ID:
        return
    await message.answer("🚀 <b>Jarayon boshlandi...</b>")
    await scheduled_job()
    await message.answer("🏁 <b>Tayyor! Barcha kanallarga post yuborildi.</b>")


# --- ДОБАВЛЕНИЕ (Через кнопку) ---
@dp.message(F.text == "➕ Kanal qo'shish")
async def menu_add_init(message: types.Message):
    if str(message.from_user.id) != ADMIN_ID:
        return
    await message.answer(
        "➕ <b>Yangi kanal qo'shish</b>\n\n"
        "Iltimos, kanalning userneymini yuboring (masalan: <code>@mening_kanalim</code>) "
        "yoki kanal ID raqamini kiriting."
    )


# Обработка любого текстового сообщения (если это похоже на @username)
@dp.message(F.text.startswith("@") | F.text.startswith("-100"))
async def process_add_channel(message: types.Message):
    if str(message.from_user.id) != ADMIN_ID:
        return
    chat_id = message.text.strip()
    if add_channel_to_db(chat_id):
        await message.answer(f"✅ <b>{chat_id}</b> muvaffaqiyatli qo'shildi!")
    else:
        await message.answer("Bu kanal allaqachon ro'yxatda bor.")


# --- УДАЛЕНИЕ (Через инлайн-кнопки) ---
@dp.message(F.text == "🗑 Kanalni o'chirish")
async def menu_remove_init(message: types.Message):
    if str(message.from_user.id) != ADMIN_ID:
        return
    channels = get_all_channels()
    if not channels:
        return await message.answer("O'chirish uchun kanallar mavjud emas.")

    await message.answer(
        "🗑 <b>O'chirmoqchi bo'lgan kanalni tanlang:</b>",
        reply_markup=get_channels_delete_keyboard(),
    )


@dp.callback_query(F.data.startswith("del_"))
async def callback_delete(callback: types.CallbackQuery):
    chat_id = callback.data.replace("del_", "")
    if remove_channel_from_db(chat_id):
        await callback.answer(f"{chat_id} o'chirildi")
        await callback.message.edit_text(
            f"🗑 <b>{chat_id}</b> ro'yxatdan olib tashlandi.",
            reply_markup=get_channels_delete_keyboard(),  # Обновляем список кнопок
        )
    else:
        await callback.answer("Xatolik: Kanal topilmadi")


# --- ЗАПУСК ---
async def main():
    create_db_and_tables()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(scheduled_job, "cron", day="*/3", hour=10, minute=0)
    scheduler.start()
    logging.info("Bot ishga tushdi...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot to'xtatildi.")
