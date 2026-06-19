"""Одноразовый вход в Telethon → печатает StringSession для .env.

Запуск (локально, интерактивно):
    python -m scripts.telethon_login

Введите номер телефона и код из Telegram. Полученную строку положите в .env как
TELETHON_SESSION=... (плюс TELEGRAM_API_ID / TELEGRAM_API_HASH с my.telegram.org).
"""
import os

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

load_dotenv()

api_id = int(os.environ["TELEGRAM_API_ID"])
api_hash = os.environ["TELEGRAM_API_HASH"]

with TelegramClient(StringSession(), api_id, api_hash) as client:
    print("\n=== TELETHON_SESSION ===")
    print(client.session.save())
    print("========================\n")
