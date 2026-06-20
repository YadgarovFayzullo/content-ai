"""Поиск чистого тематического фото в интернете (Pexels) под новость.

Зачем: фото из канала-источника часто уже содержит вшитый текст/логотипы —
накладывать на него заголовок плохо. Вместо этого берём чистый сток по смыслу
новости (английский visual subject) и кладём заголовок поверх (см. news_card.py).

Ключ: PEXELS_API_KEY. Нет ключа/ошибка/нет результатов → None (вызывающий код
делает фолбэк на AI-иллюстрацию).
"""
from __future__ import annotations

import logging
import os
import random
import time
from pathlib import Path
from typing import Optional, Tuple

import httpx

PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "")
_PEXELS_URL = "https://api.pexels.com/v1/search"
# Сколько верхних результатов рассматривать — из них берём случайный, чтобы
# разные новости не получали одну и ту же картинку.
_POOL = int(os.getenv("PEXELS_POOL", "15"))


async def fetch_stock_photo(
    query: str, out_dir: str = "gen_images"
) -> Optional[Tuple[str, str]]:
    """Качает портретное сток-фото по запросу. (путь, имя_автора) или None.

    query — короткая англоязычная фраза (visual subject новости)."""
    query = (query or "").strip()
    if not query:
        return None
    if not PEXELS_API_KEY:
        logging.warning("PEXELS_API_KEY yo'q — internetdan rasm izlanmadi.")
        return None

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                _PEXELS_URL,
                headers={"Authorization": PEXELS_API_KEY},
                params={
                    "query": query,
                    "orientation": "portrait",
                    "per_page": _POOL,
                    "size": "large",
                },
            )
            r.raise_for_status()
            photos = r.json().get("photos") or []
            if not photos:
                logging.info("Pexels: '%s' bo'yicha rasm topilmadi.", query)
                return None

            photo = random.choice(photos[:_POOL])
            src = photo.get("src") or {}
            img_url = src.get("large2x") or src.get("portrait") or src.get("large")
            if not img_url:
                return None
            author = photo.get("photographer") or "Pexels"

            rb = await client.get(img_url)
            rb.raise_for_status()
            data = rb.content
    except Exception as e:
        logging.warning("Pexels rasm izlashda xato ('%s'): %s", query, e)
        return None

    if not data:
        return None
    Path(out_dir).mkdir(exist_ok=True)
    dest = Path(out_dir) / f"stock_{photo.get('id', int(time.time()))}_{int(time.time())}.jpg"
    dest.write_bytes(data)
    logging.info("Pexels rasm yuklab olindi: %s ('%s')", dest, query)
    return str(dest), author
