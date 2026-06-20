"""Рендер новостной карточки: реальное фото источника + тёмный градиент снизу +
жирный заголовок поверх (стиль Kursiv/IT-Park).

Язык-агностично: заголовок передаётся уже на языке канала, шрифт Roboto Bold
покрывает кириллицу и латиницу. Используется repost-режимом (см. repost.py)
вместо публикации голого исходного фото.
"""
from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import List, Optional

from PIL import Image, ImageDraw, ImageFont

# Эмодзи/пиктограммы/dingbats/variation-selectors — Roboto их не покрывает, рисует
# «тофу»-квадрат. Чистим из заголовка и кредита (текст канала-источника).
_NON_RENDERABLE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\U00002190-\U000021FF\U00002300-\U000023FF\U00002B00-\U00002BFF"
    "\U0000FE00-\U0000FE0F\U00002000-\U0000200F]"
)


def _clean(text: str) -> str:
    """Убирает эмодзи/символы без глифа и схлопывает пробелы."""
    return re.sub(r"\s+", " ", _NON_RENDERABLE.sub("", text or "")).strip()

# Жирный шрифт с кириллицей+латиницей. Бандлится в репо (Docker не имеет Arial),
# но допускаем переопределение и системные фолбэки.
_FONT_CANDIDATES = [
    os.getenv("NEWS_CARD_FONT", ""),
    str(Path(__file__).parent / "assets" / "fonts" / "Roboto-Bold.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]

# Холст 4:5 — вертикальный формат под ленту Telegram.
CARD_W = 1080
CARD_H = 1350
PAD = 64                      # боковые/нижние поля
MAX_TEXT_LINES = 6
MAX_FONT = 92
MIN_FONT = 44
CREDIT_FONT = 28
LINE_RATIO = 1.12            # межстрочный = размер * LINE_RATIO


def _font_path() -> str:
    for p in _FONT_CANDIDATES:
        if p and Path(p).exists():
            return p
    raise RuntimeError("News-card shrifti topilmadi (Roboto-Bold.ttf).")


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(_font_path(), size)


def _cover(img: Image.Image, w: int, h: int) -> Image.Image:
    """Масштабирует и обрезает по центру так, чтобы заполнить w×h без полей."""
    img = img.convert("RGB")
    scale = max(w / img.width, h / img.height)
    nw, nh = max(w, round(img.width * scale)), max(h, round(img.height * scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - w) // 2, (nh - h) // 2
    return img.crop((left, top, left + w, top + h))


def _gradient(w: int, h: int) -> Image.Image:
    """Вертикальный градиент: прозрачный сверху → тёмный снизу. Нижняя ~60% высоты
    плавно затемняется, чтобы белый заголовок читался на любом фоне."""
    start = int(h * 0.38)          # выше этой линии — почти прозрачно
    max_alpha = 240
    grad = Image.new("L", (1, h), 0)
    px = grad.load()
    for y in range(h):
        if y <= start:
            a = 0
        else:
            t = (y - start) / (h - start)
            a = int((t ** 1.35) * max_alpha)
        px[0, y] = a
    alpha = grad.resize((w, h))
    overlay = Image.new("RGBA", (w, h), (8, 12, 20, 255))
    overlay.putalpha(alpha)
    return overlay


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
          max_w: int) -> List[str]:
    """Перенос по словам под ширину max_w. Слишком длинное слово остаётся как есть."""
    lines: List[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        if not words:
            continue
        cur = words[0]
        for word in words[1:]:
            trial = f"{cur} {word}"
            if draw.textlength(trial, font=font) <= max_w:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
    return lines


def _fit(draw: ImageDraw.ImageDraw, text: str, max_w: int, max_h: int):
    """Подбирает самый крупный размер шрифта, при котором заголовок умещается в
    max_w по ширине и max_h/MAX_TEXT_LINES по высоте. Возвращает (font, lines)."""
    chosen = None
    for size in range(MAX_FONT, MIN_FONT - 1, -4):
        font = _load_font(size)
        lines = _wrap(draw, text, font, max_w)
        line_h = int(size * LINE_RATIO)
        if len(lines) <= MAX_TEXT_LINES and len(lines) * line_h <= max_h:
            return font, lines
        chosen = (font, lines)
    # Ничего не уместилось целиком — минимальный кегль, обрезаем по числу строк.
    font, lines = chosen if chosen else (_load_font(MIN_FONT), [text])
    return font, lines[:MAX_TEXT_LINES]


def render_news_card(
    photo_path: str,
    headline: str,
    credit: Optional[str] = None,
    out_dir: str = "gen_images",
) -> str:
    """Накладывает заголовок (и опц. кредит «Photo: …») на фото с тёмным градиентом.
    Возвращает путь к JPEG. Бросает при сбое чтения шрифта/фото."""
    headline = _clean(headline)
    credit = _clean(credit) if credit else None
    if not headline:
        raise ValueError("Bo'sh sarlavha — news-card render qilinmaydi.")

    base = _cover(Image.open(photo_path), CARD_W, CARD_H)
    base = Image.alpha_composite(base.convert("RGBA"), _gradient(CARD_W, CARD_H))
    img = base.convert("RGB")
    draw = ImageDraw.Draw(img)

    max_w = CARD_W - 2 * PAD
    credit_h = CREDIT_FONT + 24 if credit else 0
    max_text_h = int(CARD_H * 0.46)
    font, lines = _fit(draw, headline, max_w, max_text_h)
    line_h = int(font.size * LINE_RATIO)

    # Заголовок прижат к низу, над строкой кредита.
    block_h = len(lines) * line_h
    y = CARD_H - PAD - credit_h - block_h
    for line in lines:
        draw.text((PAD + 2, y + 2), line, font=font, fill=(0, 0, 0))   # тень
        draw.text((PAD, y), line, font=font, fill=(255, 255, 255))
        y += line_h

    if credit:
        cfont = _load_font(CREDIT_FONT)
        label = f"Photo: {credit}"
        cw = draw.textlength(label, font=cfont)
        cx, cy = CARD_W - PAD - cw, CARD_H - PAD - CREDIT_FONT
        draw.text((cx + 1, cy + 1), label, font=cfont, fill=(0, 0, 0))
        draw.text((cx, cy), label, font=cfont, fill=(235, 235, 235))

    Path(out_dir).mkdir(exist_ok=True)
    stem = Path(photo_path).stem
    out = Path(out_dir) / f"card_{stem}_{int(time.time())}.jpg"
    img.save(out, "JPEG", quality=90)
    logging.info("News-card render qilindi: %s", out)
    return str(out)
