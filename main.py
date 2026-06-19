"""Точка входа: инициализация бота, подключение роутеров и планировщика."""
import asyncio
import logging
import sys

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage

from bot.config import TOKEN
from bot.handlers import channels_router, settings_router
from bot.scheduler import schedule_tick, reindex_references
from bot.metrics import collect_metrics
from bot import rag_client
from database import create_db_and_tables
from rag import set_retriever


class _HttpRagRetriever:
    """Адаптер RagRetriever → RAG-сервис по HTTP (изоляция по tenant_id)."""

    def retrieve(
        self,
        tenant_id: str,
        topic: str,
        include_own: bool = True,
        include_references: bool = True,
    ):
        # Лимит=0 для выключенного источника — этот поиск не выполняется вовсе.
        own_limit = 4 if include_own else 0
        ref_limit = 6 if include_references else 0
        return rag_client.retrieve(
            tenant_id, topic, own_limit=own_limit, ref_limit=ref_limit
        )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=MemoryStorage())
dp.include_router(channels_router)
dp.include_router(settings_router)


async def main():
    create_db_and_tables()
    set_retriever(_HttpRagRetriever())
    scheduler = AsyncIOScheduler()
    # Автопостинг по расписанию: минутный тик публикует в каналы, у которых
    # сейчас запланирован пост (режимы frequency/times per-tenant).
    scheduler.add_job(schedule_tick, "cron", minute="*", args=[bot])
    # Сбор метрик постов дважды в сутки (просмотры/пересылки/реакции через Telethon).
    scheduler.add_job(collect_metrics, "cron", hour="9,21", minute=0)
    # Ежедневный пере-скрейпинг референс-каналов (раз в сутки, ночью) — пул фактов
    # растёт сам, не «застывает» на снимке момента добавления.
    scheduler.add_job(reindex_references, "cron", hour=5, minute=0)
    scheduler.start()
    logging.info("Bot ishga tushdi...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot to'xtatildi.")
