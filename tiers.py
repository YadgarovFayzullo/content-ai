"""Тарифы (starter/pro/premium) и их ограничения — restrict mode.

ЕДИНЫЙ источник правды о лимитах. Импортируется и admin-api (бэкенд, который
видит фронт), и ботом (планировщик/публикация) — чтобы ограничения применялись
одинаково везде, а не дублировались магическими числами по коду.

Принцип: тариф привязан к КАНАЛУ (TenantProfile.subscription_tier). Часть лимитов
по смыслу относится к клиенту (например, число каналов) — для них берётся «лучший»
тариф среди каналов клиента (см. best_tier / max_channels_for_owner в database.py).

Числовой лимит UNLIMITED (= -1) означает «без ограничения».
"""
from typing import Dict, Any

UNLIMITED = -1

# Порядок тарифов по возрастанию прав (для сравнения «не ниже чем»).
TIER_ORDER = ("starter", "pro", "premium")
DEFAULT_TIER = "starter"

# Матрица лимитов. Числа — квоты (UNLIMITED = без ограничения), bool — гейт фичи,
# строки — режим (analytics). Меняется ТОЛЬКО здесь — оба сервиса подхватят.
TIER_LIMITS: Dict[str, Dict[str, Any]] = {
    "starter": {
        "max_channels": 1,          # каналов на клиента (по лучшему тарифу его каналов)
        "max_sources": 1,           # референс-источников на канал
        "max_posts_per_day": 1,     # потолок авто-расписания
        "scheduling": True,         # авто-постинг по расписанию
        "repost_mode": False,       # content_mode = "repost"
        "rag": False,               # use_rag / use_references
        "image_generation": False,  # картинки к постам
        "manual_publish": True,     # кнопка «опубликовать сейчас»
        "analytics": "basic",       # basic — окно ≤ 7 дней, без разбивки по темам
    },
    "pro": {
        "max_channels": 3,
        "max_sources": 5,
        "max_posts_per_day": 5,
        "scheduling": True,
        "repost_mode": True,
        "rag": True,
        "image_generation": True,
        "manual_publish": True,
        "analytics": "full",
    },
    "premium": {
        # Конечные потолки (НЕ UNLIMITED) — расходы на LLM/картинки растут с объёмом,
        # безлимит = неконтролируемый счёт. Поднимать осознанно.
        "max_channels": 10,
        "max_sources": 15,
        "max_posts_per_day": 10,
        "scheduling": True,
        "repost_mode": True,
        "rag": True,
        "image_generation": True,    # единственная фича сверх pro
        "manual_publish": True,
        "analytics": "full",
    },
}

# Максимальное окно аналитики (дни) для режима "basic".
BASIC_ANALYTICS_MAX_DAYS = 7


def normalize_tier(tier: Any) -> str:
    """Приводит произвольное значение к валидному тарифу (иначе DEFAULT_TIER)."""
    t = (tier or "").strip().lower() if isinstance(tier, str) else ""
    return t if t in TIER_LIMITS else DEFAULT_TIER


def limits_for(tier: Any) -> Dict[str, Any]:
    """Полный словарь лимитов тарифа (копия — мутировать у вызывающего нельзя)."""
    return dict(TIER_LIMITS[normalize_tier(tier)])


def allows(tier: Any, feature: str) -> bool:
    """True, если фича-флаг (bool-ключ матрицы) включён на тарифе."""
    return bool(TIER_LIMITS[normalize_tier(tier)].get(feature, False))


def limit_of(tier: Any, key: str) -> int:
    """Числовой лимит тарифа (UNLIMITED = -1, если ключа нет — тоже без лимита)."""
    val = TIER_LIMITS[normalize_tier(tier)].get(key, UNLIMITED)
    return val if isinstance(val, int) else UNLIMITED


def is_unlimited(value: int) -> bool:
    return value == UNLIMITED


def within_limit(count: int, max_value: int) -> bool:
    """Помещается ли `count` в лимит (UNLIMITED — всегда True)."""
    return is_unlimited(max_value) or count <= max_value


def tier_rank(tier: Any) -> int:
    """Позиция тарифа в порядке прав (для сравнения «лучший из»)."""
    t = normalize_tier(tier)
    return TIER_ORDER.index(t) if t in TIER_ORDER else 0


def best_tier(tiers) -> str:
    """Наивысший тариф из набора (для лимитов клиентского уровня, напр. max_channels)."""
    best = DEFAULT_TIER
    for t in tiers:
        if tier_rank(t) > tier_rank(best):
            best = normalize_tier(t)
    return best


def required_tier_for(feature: str) -> str:
    """Минимальный тариф, на котором фича/квота доступна (для подсказки апгрейда).

    Для bool-фич — первый тариф с True. Для числовых квот — первый тариф, где лимит
    выше стартового (или unlimited). Если уже на starter — возвращает starter.
    """
    base = TIER_LIMITS[DEFAULT_TIER].get(feature)
    for tier in TIER_ORDER:
        val = TIER_LIMITS[tier].get(feature)
        if isinstance(val, bool):
            if val:
                return tier
        elif isinstance(val, int):
            if is_unlimited(val) or (isinstance(base, int) and not is_unlimited(base) and val > base):
                return tier
    return TIER_ORDER[-1]
