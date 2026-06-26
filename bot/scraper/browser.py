"""Headless-браузер (Playwright) для прохождения JS-challenge (Cloudflare и т.п.).

Один браузер на процесс (как и Telethon-клиент): запуск chromium дорогой.
Импорт ленивый — если playwright не установлен, веб-скрейп всё равно работает
(просто без рендера). `_browser_unavailable` запоминает неудачу, чтобы не
пытаться поднять браузер на каждой странице.
"""
from __future__ import annotations

import logging
from asyncio import Lock
from typing import Any, Optional

from bot.config import WEB_RENDER
from bot.scraper.text import _BROWSER_HEADERS


_browser: Any = None
_playwright: Any = None
_browser_lock = Lock()
_browser_unavailable = False


async def _get_browser() -> Any:
    """Единый headless-chromium на процесс. None, если рендер выключен
    (WEB_RENDER=0), playwright не установлен или браузер не поднялся."""
    global _browser, _playwright, _browser_unavailable

    if not WEB_RENDER or _browser_unavailable:
        return None

    async with _browser_lock:
        if _browser is not None:
            return _browser
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            _browser_unavailable = True
            logging.warning(
                "Playwright o'rnatilmagan — JS-challenge sahifalar render "
                "qilinmaydi (faqat RSS/oddiy sahifalar)."
            )
            return None
        try:
            _playwright = await async_playwright().start()
            _browser = await _playwright.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            logging.info("Playwright chromium ishga tushdi")
            return _browser
        except Exception as e:
            _browser_unavailable = True
            logging.warning("Playwright ishga tushmadi: %s", e)
            return None


async def _fetch_rendered(url: str) -> Optional[str]:
    """Рендерит страницу headless-браузером и возвращает её HTML после того, как
    JS (в т.ч. Cloudflare challenge) отработал. None при недоступности/ошибке."""
    browser = await _get_browser()
    if browser is None:
        return None
    context = None
    try:
        context = await browser.new_context(
            user_agent=_BROWSER_HEADERS["User-Agent"], locale="en-US"
        )
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        # Дать Cloudflare-challenge время отработать и подгрузить контент.
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        return await page.content()
    except Exception as e:
        logging.warning("Render qilishda xato (%s): %s", url, e)
        return None
    finally:
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass
