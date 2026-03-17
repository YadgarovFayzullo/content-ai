import os
import asyncio
from aiogram import Bot
from aiogram.types import FSInputFile
from dotenv import load_dotenv
from database import save_fact
from pathlib import Path

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")

async def send_to_telegram(content, target_chat_id):
    """
    Отправляет пост в конкретный канал. 
    target_chat_id может быть @username или числовой ID.
    """
    bot = Bot(token=TOKEN)
    
    fact_data = content['data']
    image_path = content['image_url']
    fact_entry = content['entry']
    
    try:
        # Получаем данные канала динамически
        chat = await bot.get_chat(target_chat_id)
        
        # Генерируем ссылку: если есть юзернейм - через @, если нет - название
        channel_link = f"@{chat.username}" if chat.username else chat.title
        
        caption = (
            "<b>🔬 Daily Fact</b>\n\n"
            f"📚 {fact_data['fact']}\n\n"
            f"{fact_data['explanation']}\n\n"
            f"{' '.join(fact_data['hashtags'])}\n\n"
            f"<b>Obuna bo'ling:</b> {channel_link}"
        )
        
        if not image_path or not Path(image_path).exists():
            return

        photo = FSInputFile(image_path)
        message = await bot.send_photo(
            chat_id=target_chat_id,
            photo=photo,
            caption=caption,
            parse_mode="HTML"
        )
        
        # Сохраняем в базу (только при первом успешном постинге в основной цикл)
        # Мы помечаем его как posted=True
        fact_entry.posted = True
        save_fact(fact_entry)
        
        # Отчет админу со ссылкой именно на этот канал
        if ADMIN_ID:
            post_url = f"https://t.me/{chat.username}/{message.message_id}" if chat.username else "link mavjud emas"
            admin_msg = (
                f"✅ <b>Post joylandi!</b>\n"
                f"Kanal: {channel_link}\n"
                f"Havola: {post_url}"
            )
            await bot.send_message(chat_id=ADMIN_ID, text=admin_msg, parse_mode="HTML")
            
        print(f"Bajarildi: {channel_link}")
        
    except Exception as e:
        print(f"Xato ({target_chat_id} kanalida): {e}")
        if ADMIN_ID:
            try: await bot.send_message(chat_id=ADMIN_ID, text=f"❌ Xato ({target_chat_id}): {e}")
            except: pass
    finally:
        await bot.session.close()
