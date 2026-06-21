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

# FRONTEND_URL может содержать несколько origin'ов через запятую. Браузер шлёт
# Origin без завершающего слэша и без пути, а CORSMiddleware сравнивает строки
# строго — поэтому нормализуем: убираем пробелы/слэш и автоматически добавляем
# парный вариант с www. / без www., чтобы apex и поддомен оба работали.
def _expand_origins(raw: str) -> list[str]:
    origins: list[str] = []
    for part in raw.split(","):
        origin = part.strip().rstrip("/")
        if not origin:
            continue
        origins.append(origin)
        # apex <-> www парные варианты
        if "://www." in origin:
            origins.append(origin.replace("://www.", "://", 1))
        else:
            scheme, _, rest = origin.partition("://")
            if scheme and rest:
                origins.append(f"{scheme}://www.{rest}")
    return origins


_DEFAULT_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:8080",
    "http://127.0.0.1:8080",
]

CORS_CONFIG = {
    "allow_origins": _DEFAULT_ORIGINS + _expand_origins(os.getenv("FRONTEND_URL", "")),
    "allow_credentials": True,
    "allow_methods": ["*"],
    "allow_headers": ["*"],
}

# Убираем дубли, сохраняя порядок.
CORS_CONFIG["allow_origins"] = list(dict.fromkeys(CORS_CONFIG["allow_origins"]))

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
