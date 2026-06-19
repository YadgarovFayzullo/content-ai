"""Анализ стиля канала по его постам → поля TenantProfile.

Backend-слой: берёт выборку скрейпленных постов и просит LLM вывести tone,
audience, writing_style, language и topics. Результат пишется в профиль
арендатора, чтобы движок генерации сразу писал в голосе канала, а не на дефолтах.

Синхронная функция — вызывайте через asyncio.to_thread из async-кода.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from generator import groq_chat

# Сколько постов отдаём модели на анализ (хватает для устойчивого профиля).
SAMPLE_SIZE = 60

_ANALYSIS_PROMPT = """You analyze a Telegram channel's existing posts and infer its editorial profile.

Return ONLY a JSON object with exactly these keys:
- "tone": one short word/phrase (e.g. "friendly", "formal", "energetic")
- "audience": short description of the target audience
- "writing_style": one sentence describing the style (sentence length, emoji use, structure)
- "language": comma-separated short codes of ALL languages the channel posts in,
  ordered by frequency (e.g. "ru, uz" for a bilingual channel, "uz" if only one).
  Detect every language actually used across the posts, not just the dominant one.
- "topics": comma-separated list of exactly 10 distinct recurring topics,
  specific and non-overlapping (prefer narrow themes like "fintech funding",
  "product launch", "B2B sales" over broad ones like "business"). Derive them
  ONLY from what the posts are actually about. If the channel truly has fewer,
  expand into closely related sub-themes to reach 10.

Base every field ONLY on the provided posts. No explanations, no markdown, JSON only.

POSTS:
{posts}
"""


def _parse_json(raw: str) -> Optional[dict[str, Any]]:
    """Достаёт JSON из ответа LLM (на случай ```json-обёрток)."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def analyze_style(posts: list[dict[str, Any]]) -> Optional[dict[str, str]]:
    """Возвращает поля профиля из постов или None, если анализ не удался.

    posts — элементы вида {"id", "text", "date"} (как из scraper).
    """
    texts = [p.get("text", "").strip() for p in posts if p.get("text", "").strip()]
    if len(texts) < 3:
        return None  # слишком мало данных для надёжного вывода

    sample = "\n\n---\n\n".join(texts[:SAMPLE_SIZE])
    prompt = _ANALYSIS_PROMPT.format(posts=sample)

    try:
        raw = groq_chat(
            "You are a precise analyst. Output valid JSON only, no prose.",
            prompt,
            temperature=0.2,
        )
        data = _parse_json(raw)
    except Exception as e:
        logging.error(f"Stil tahlili xatosi: {e}")
        return None

    if not data:
        return None

    allowed = {"tone", "audience", "writing_style", "language", "topics"}
    result = {k: str(v).strip() for k, v in data.items() if k in allowed and v}
    return result or None
