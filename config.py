"""Centralized configuration for Content AI services."""
import os
from dotenv import load_dotenv

load_dotenv()

# ============================================================================
# API Configuration
# ============================================================================

API_TITLE = "Content AI Admin API"
API_VERSION = "1.0.0"

# Admin API token
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "a12345678")

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
    "sqlite:///facts.db"
)

# ============================================================================
# RAG Configuration
# ============================================================================

RAG_ENABLED = os.getenv("RAG_ENABLED", "true").lower() == "true"
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")

# ============================================================================
# Bot Configuration
# ============================================================================

TELETHON_SESSION = os.getenv("TELETHON_SESSION", "session")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
API_ID = os.getenv("API_ID", "")
API_HASH = os.getenv("API_HASH", "")

# ============================================================================
# Groq Configuration
# ============================================================================

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "mixtral-8x7b-32768")

# ============================================================================
# Logging Configuration
# ============================================================================

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
