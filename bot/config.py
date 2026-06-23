"""Конфигурация и константы бота."""
import os

from dotenv import load_dotenv

load_dotenv()

_token = os.getenv("TELEGRAM_BOT_TOKEN")
if not _token:
    raise RuntimeError("TELEGRAM_BOT_TOKEN .env faylida ko'rsatilmagan")

TOKEN: str = _token
ADMIN_ID = os.getenv("ADMIN_ID")

TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
TELETHON_SESSION = os.getenv("TELETHON_SESSION")

RAG_URL = os.getenv("RAG_URL", "http://localhost:8000")

SCRAPE_HISTORY_LIMIT = int(os.getenv("SCRAPE_HISTORY_LIMIT", "600"))

# Сколько статей максимум тянуть с одной новостной индекс-страницы (сайт-источник).
# Каждая статья — отдельный HTTP-запрос, поэтому потолок ниже, чем у каналов.
WEB_MAX_ARTICLES = int(os.getenv("WEB_MAX_ARTICLES", "40"))

# Рендерить ли страницы headless-браузером (Playwright), когда обычный HTTP-запрос
# упирается в JS-challenge Cloudflare (403). Требует установленного playwright +
# chromium (см. Dockerfile). На слабых инстансах можно выключить: WEB_RENDER=0.
WEB_RENDER = os.getenv("WEB_RENDER", "1").lower() not in ("0", "false", "no")

DEFAULT_FORBIDDEN_TOPICS = (
    "o'lim, terror, din, ekstremizm, siyosat, urush, narkotik, kasallik, jinoyat, "
    "смерть, террор, религия, политика, война, наркотики, болезнь, преступление"
)

EDIT_FIELDS = {
    "tone":   ("tone", "Ohang", "text"),
    "style":  ("writing_style", "Yozuv uslubi", "text"),
    "aud":    ("audience", "Auditoriya", "text"),
    "topics": ("topics", "Mavzular (vergul bilan)", "text"),
    "cta":    ("cta", "CTA / Havola (topshirish uchun)", "text"),
    "lang":   ("language", "Til", "text"),
    "tmpl":   ("post_template", "Post shabloni", "text"),
    "img":    ("image_style", "Rasm uslubi", "text"),
    "creat":  ("creativity_level", "Kreativlik (0–1)", "float"),
    "fact":   ("factual_strictness", "Faktik qat'iylik (0–1)", "float"),
}

RULE_TYPES = ["forbidden_topic", "required_hashtag", "formatting", "length_limit", "stylistic"]
