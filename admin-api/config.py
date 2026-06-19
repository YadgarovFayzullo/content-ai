"""Admin API configuration."""
import os
from dotenv import load_dotenv

load_dotenv()

# ============================================================================
# API Configuration
# ============================================================================

API_TITLE = "Content AI Admin API"
API_VERSION = "1.0.0"

# Admin API token (общий супер-админский ключ)
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "a12345678")

# Telegram user_id супер-админа (тот же, что использует бот) — видит все каналы.
ADMIN_ID = os.getenv("ADMIN_ID", "")

# Токен Telegram-бота — нужен для resolve bot username (deep-link авторизации).
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# ============================================================================
# CORS Configuration
# ============================================================================

CORS_CONFIG = {
    "allow_origins": [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        os.getenv("FRONTEND_URL", ""),
    ],
    "allow_credentials": True,
    "allow_methods": ["*"],
    "allow_headers": ["*"],
}

# Remove empty strings from allowed origins
CORS_CONFIG["allow_origins"] = [
    origin for origin in CORS_CONFIG["allow_origins"] if origin
]

# ============================================================================
# Database Configuration
# ============================================================================

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres_pass@localhost:5432/content_ai"
)

# ============================================================================
# RAG Service (отдельный контейнер)
# ============================================================================

# Тот же адрес, что использует бот (bot/config.py: RAG_URL).
RAG_URL = os.getenv("RAG_URL", "http://localhost:8000")
