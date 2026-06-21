"""Точка входа: инициализация бота, подключение роутеров и планировщика."""
import asyncio
import logging
import sys

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram_dialog import setup_dialogs

from bot.config import TOKEN
from bot.handlers import channels_router
from bot.dialogs import (
    add_channel_dialog,
    assign_client_dialog,
    publish_dialog,
    post_all_dialog,
    remove_channel_dialog,
    channel_admin_entry_router,
    settings_dialog,
    settings_entry_router,
)
from bot.scheduler import schedule_tick, reindex_references
from bot.metrics import collect_metrics
from bot.internal_api import start_internal_api
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
# aiogram-dialog: точка входа («⚙️ Sozlamalar» / /v2settings) + сам диалог.
# setup_dialogs ставит мидлвари DialogManager — вызывать после include всех роутеров.
dp.include_router(settings_entry_router)
dp.include_router(settings_dialog)
dp.include_router(channel_admin_entry_router)
dp.include_router(add_channel_dialog)
dp.include_router(assign_client_dialog)
dp.include_router(remove_channel_dialog)
dp.include_router(publish_dialog)
dp.include_router(post_all_dialog)
setup_dialogs(dp)


async def main():
    create_db_and_tables()
    set_retriever(_HttpRagRetriever())
    # coalesce — схлопывать накопившиеся пропуски в один запуск;
    # misfire_grace_time — терпеть опоздание старта до 55с (а не дефолтную 1с),
    # чтобы редкая задержка не выбрасывала минуту целиком;
    # max_instances — разрешить параллельные тики (сама работа всё равно в фоне).
    scheduler = AsyncIOScheduler(
        job_defaults={"coalesce": True, "misfire_grace_time": 55, "max_instances": 5}
    )
    # Автопостинг по расписанию: минутный тик публикует в каналы, у которых
    # сейчас запланирован пост (режимы frequency/times per-tenant). Тик мгновенный —
    # генерация уходит в фоновые задачи внутри schedule_tick.
    scheduler.add_job(schedule_tick, "cron", minute="*", args=[bot])
    # Сбор метрик постов дважды в сутки (просмотры/пересылки/реакции через Telethon).
    scheduler.add_job(collect_metrics, "cron", hour="9,21", minute=0)
    # Ежедневный пере-скрейпинг референс-каналов (раз в сутки, ночью) — пул фактов
    # растёт сам, не «застывает» на снимке момента добавления.
    scheduler.add_job(reindex_references, "cron", hour=5, minute=0)
    scheduler.start()
    # Внутренний HTTP-API для admin-api (publish / publish-all / collect-metrics):
    # бот — единственный владелец aiogram-Bot и Telethon-сессии.
    await start_internal_api(bot)
    logging.info("Bot ishga tushdi...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot to'xtatildi.")
