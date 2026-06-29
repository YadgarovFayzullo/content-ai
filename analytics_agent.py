"""ИИ-агент-менеджер канала: аналитика по запросу из дашборда.

Два слоя, и граница между ними принципиальна:

  1) build_insights() — ДЕТЕРМИНИРОВАННЫЙ расчёт реальных чисел из БД
     (post_metrics, подписчики, Telegram Broadcast Stats). Никакого LLM. Это
     «карточки» AI Insights и данные для графиков.

  2) chat() — LLM (Groq) ТОЛЬКО формулирует выводы поверх уже посчитанных чисел
     в формате «Что произошло? / Почему? / Что делать дальше?». Модель не считает
     и не выдумывает метрики — все цифры приходят из слоя (1).

Честность по «% активных подписчиков»: Telegram НЕ отдаёт, кто открывал канал.
Поэтому «активность» оцениваем двумя честными прокси, и оба подписаны тем, что
именно измеряют:
  - notifications_pct — % подписчиков с включёнными уведомлениями (официальная
    статистика канала, есть не у всех каналов);
  - reach_rate_pct — средний охват поста к числу подписчиков (avg_views/subs):
    какая доля аудитории в среднем реально видит пост.
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import List, Optional
from zoneinfo import ZoneInfo

from database import (
    get_latest_broadcast_stat,
    get_tenant_profile,
    get_tenant_stats,
    get_window_post_metrics,
)
from tiers import allows, is_unlimited, limit_of, normalize_tier

# Минимум постов в окне, ниже которого почасовая разбивка и прогноз статистически
# бессмысленны (1–2 поста дают «100% активности в один час» — это шум, не сигнал).
_MIN_POSTS_FOR_HOURLY = 5

# Демпфирование линейного прогноза охвата: дополнительные посты дают НЕ строго
# пропорциональный прирост (пересечение аудитории, усталость) — поэтому отдаём
# диапазон [low, high] от линейной оценки, а не одно «честное» число.
_FORECAST_LOW = 0.6
_FORECAST_HIGH = 0.9

# Сколько постов и какой длины подавать LLM «на чтение». Берём самые
# просматриваемые (на них учиться, что заходит), текст обрезаем — чтобы агент
# видел заголовок/зачин/структуру/CTA, не раздувая промпт всем каналом.
_POSTS_SAMPLE_MAX = 12
_POST_CONTENT_TRUNCATE = 700

# Фолбэк-чтение: если наш сервис в канале ещё ничего не публиковал, агенту всё
# равно нужно что читать — берём последние посты САМОГО канала живым скрейпом
# через внутренний API бота (единственный владелец Telethon-сессии).
_CHANNEL_POSTS_FALLBACK = 10
_INTERNAL_BOT_URL = os.getenv("INTERNAL_BOT_URL", "http://bot:8002")
_INTERNAL_TOKEN = os.getenv("ADMIN_TOKEN", "")


def _live_channel_posts(tenant_id: str, limit: int = _CHANNEL_POSTS_FALLBACK) -> List[dict]:
    """Последние посты самого канала (живой скрейп через бот). Best-effort: при
    любой ошибке/недоступности бота — пустой список (аналитика не падает).

    Возвращает [{"text", "date"}] — текст без метрик (это посты канала, не наши;
    просмотров/реакций по ним у нас нет), обрезанный до _POST_CONTENT_TRUNCATE."""
    import httpx

    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.get(
                f"{_INTERNAL_BOT_URL}/internal/tenants/{tenant_id}/channel-posts",
                params={"limit": limit},
                headers={"X-Internal-Token": _INTERNAL_TOKEN},
            )
            r.raise_for_status()
            raw = r.json().get("posts", [])
    except Exception as e:
        logging.warning("Не удалось получить живые посты канала (%s): %s", tenant_id, e)
        return []

    out: List[dict] = []
    for p in raw:
        text = " ".join((p.get("text") or "").split())
        if not text:
            continue
        if len(text) > _POST_CONTENT_TRUNCATE:
            text = text[:_POST_CONTENT_TRUNCATE].rstrip() + "…"
        out.append({"text": text, "date": p.get("date")})
    return out[:limit]


def _peak_hours(window_posts: List[dict]) -> Optional[dict]:
    """Лучшее непрерывное 3-часовое окно публикации по сумме просмотров.

    Это активность по ВРЕМЕНИ ПУБЛИКАЦИИ (когда вышедшие посты набирают охват) —
    честный прокси к активным часам аудитории, а не данные о заходах (их нет).
    Считаем в UTC и отдаём сырые часы (start_hour_utc/end_hour_utc); перевод в
    локальную таймзону пользователя делает _localize_active_hours()."""
    if len(window_posts) < _MIN_POSTS_FOR_HOURLY:
        return None
    by_hour: dict[int, int] = defaultdict(int)
    for p in window_posts:
        dt: datetime = p["posted_at"]
        by_hour[dt.hour] += p["views"]
    total = sum(by_hour.values())
    if total <= 0:
        return None

    best_start, best_views = 0, -1
    for start in range(24):
        win = sum(by_hour[(start + i) % 24] for i in range(3))
        if win > best_views:
            best_start, best_views = start, win

    end = (best_start + 3) % 24
    return {
        "start_hour_utc": best_start,
        "end_hour_utc": end,
        "share_pct": round(best_views / total * 100, 1),
        "by_hour": {h: by_hour.get(h, 0) for h in range(24)},
    }


def _localize_active_hours(active_hours: Optional[dict], tz: Optional[str]) -> Optional[dict]:
    """Добавляет в active_hours человекочитаемое окно `window` в таймзоне `tz`.

    `tz` — IANA-имя из браузера пользователя (напр. "Asia/Tashkent"). Если не
    задано или нераспознаваемо — показываем UTC, чтобы не врать о времени.
    Конвертируем через настоящий datetime (today), поэтому корректны и смещения
    с минутами (Иран +3:30, Индия +5:30), и DST."""
    if not active_hours:
        return active_hours

    zone, label = None, "UTC"
    if tz:
        try:
            zone = ZoneInfo(tz)
            label = tz
        except Exception:
            zone = None  # неизвестная таймзона → честный UTC

    def _fmt(utc_hour: int) -> str:
        base = datetime.now(timezone.utc).replace(
            hour=utc_hour, minute=0, second=0, microsecond=0
        )
        local = base.astimezone(zone) if zone else base
        return local.strftime("%H:%M")

    out = dict(active_hours)
    out["window"] = (
        f"{_fmt(active_hours['start_hour_utc'])}–"
        f"{_fmt(active_hours['end_hour_utc'])} {label}"
    )
    out["timezone"] = label
    return out


def _topic_contrast(by_topic: List[dict]) -> Optional[dict]:
    """Во сколько раз лучшая тема обгоняет худшую по реакциям на пост.

    Даёт инсайт вида «посты про X получают в N раз больше реакций, чем про Y».
    Нужно ≥2 темы с ненулевыми реакциями, иначе сравнивать нечего."""
    scored = [t for t in by_topic if t.get("avg_reactions", 0) > 0]
    if len(scored) < 2:
        return None
    top = max(scored, key=lambda t: t["avg_reactions"])
    bottom = min(scored, key=lambda t: t["avg_reactions"])
    if bottom["avg_reactions"] <= 0:
        return None
    return {
        "top_topic": top["topic"],
        "bottom_topic": bottom["topic"],
        "ratio": round(top["avg_reactions"] / bottom["avg_reactions"], 1),
        "top_avg_reactions": top["avg_reactions"],
        "bottom_avg_reactions": bottom["avg_reactions"],
    }


def _forecast(total_published: int, days: int, avg_views: int, extra_per_week: int = 2) -> Optional[dict]:
    """Грубая оценка прироста недельного охвата при +extra_per_week постов/нед.

    Линейная база (доп. посты × средний охват), демпфированная в диапазон —
    честно подаётся как ОЦЕНКА, не гарантия. None, если постить ещё нечего
    (нет истории) или окно слишком короткое."""
    if total_published <= 0 or days < 7 or avg_views <= 0:
        return None
    posts_per_week = total_published / (days / 7)
    if posts_per_week <= 0:
        return None
    linear_growth = extra_per_week / posts_per_week * 100
    return {
        "current_posts_per_week": round(posts_per_week, 1),
        "extra_posts_per_week": extra_per_week,
        "projected_reach_growth_pct": [
            round(linear_growth * _FORECAST_LOW),
            round(linear_growth * _FORECAST_HIGH),
        ],
        "basis": "linear estimate from avg reach, dampened for audience overlap",
    }


def _posts_sample(window_posts: List[dict]) -> List[dict]:
    """Текст постов «на чтение» агенту: самые просматриваемые, текст обрезан.

    Сортируем по просмотрам (агент видит, ЧТО заходит у канала), берём до
    _POSTS_SAMPLE_MAX, схлопываем пробелы и режем до _POST_CONTENT_TRUNCATE."""
    posts = [p for p in window_posts if (p.get("content") or "").strip()]
    if not posts:
        return []
    chosen = sorted(posts, key=lambda p: p.get("views", 0), reverse=True)[:_POSTS_SAMPLE_MAX]
    out: List[dict] = []
    for p in chosen:
        text = " ".join((p["content"] or "").split())
        if len(text) > _POST_CONTENT_TRUNCATE:
            text = text[:_POST_CONTENT_TRUNCATE].rstrip() + "…"
        out.append(
            {
                "topic": p.get("topic") or "—",
                "views": p.get("views", 0),
                "reactions": p.get("reactions", 0),
                "forwards": p.get("forwards", 0),
                "text": text,
            }
        )
    return out


def build_insights(tenant_id: str, days: int = 30, tz: Optional[str] = None) -> dict:
    """Детерминированные AI Insights канала за `days` дней (без LLM).

    Возвращает реальные числа: оба прокси «% активных», активные часы, контраст
    тем, прогноз охвата + сырой summary/by_topic для графиков и для LLM-слоя.
    `tz` — IANA-таймзона пользователя (из браузера): в ней показываем пик
    активности; None → UTC. posts_sample — тексты постов для чтения LLM-слоем."""
    stats = get_tenant_stats(tenant_id, days=days, limit=100)
    summary = stats.get("summary", {})
    by_topic = stats.get("by_topic", [])
    window_posts = get_window_post_metrics(tenant_id, days)
    broadcast = get_latest_broadcast_stat(tenant_id)

    # Потолок тарифа канала — чтобы LLM-слой не обещал больше постов/день, чем
    # реально разрешено (иначе устный совет «2 поста» расходится с карточкой,
    # которую backend зажимает до лимита тарифа).
    profile = get_tenant_profile(tenant_id)
    tier = normalize_tier(getattr(profile, "subscription_tier", "starter"))
    tier_info = {
        "name": tier,
        "scheduling": allows(tier, "scheduling"),
        "max_posts_per_day": limit_of(tier, "max_posts_per_day"),
        "repost_mode": allows(tier, "repost_mode"),
    }

    subs = summary.get("subscribers")
    avg_views = summary.get("avg_views_per_post", 0)

    # Тексты «на чтение» агенту. Приоритет — наши опубликованные посты (по ним есть
    # метрики). Если их нет (сервис в этом канале ещё ничего не публиковал) —
    # живой скрейп последних постов самого канала, чтобы агенту было что разбирать.
    posts_sample = _posts_sample(window_posts)
    posts_origin = "published" if posts_sample else ""
    if not posts_sample:
        live = _live_channel_posts(tenant_id)
        if live:
            posts_sample, posts_origin = live, "channel"

    # Прокси «% активных». reach_rate работает всегда, где есть подписчики и охват;
    # notifications — только если канал отдаёт Broadcast Stats. Доли отображающий
    # слой ОБЯЗАН подписывать тем, что измеряет (см. notes).
    reach_rate_pct = (
        round(avg_views / subs * 100, 1) if subs and avg_views else None
    )
    notifications_pct = (
        broadcast.get("enabled_notifications_pct") if broadcast else None
    )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": days,
        "subscribers": subs,
        "active": {
            "reach_rate_pct": reach_rate_pct,
            "notifications_pct": notifications_pct,
            "notes": (
                "reach_rate_pct = средний охват поста к числу подписчиков "
                "(какая доля аудитории в среднем видит пост). notifications_pct = "
                "% подписчиков с включёнными уведомлениями (официальная статистика "
                "Telegram). Telegram не отдаёт, кто открывал канал, — это прокси, "
                "а не точная доля активных."
            ),
        },
        "tier": tier_info,
        "active_hours": _localize_active_hours(_peak_hours(window_posts), tz),
        "topic_contrast": _topic_contrast(by_topic),
        "forecast": _forecast(
            summary.get("total_published", 0), days, avg_views
        ),
        "summary": summary,
        "by_topic": by_topic,
        "posts_sample": posts_sample,
        "posts_sample_origin": posts_origin,
    }


def _insights_to_prompt(insights: dict) -> str:
    """Компактное текстовое представление insights для LLM (только факты/числа)."""
    s = insights.get("summary", {})
    lines = [
        f"Окно анализа: последние {insights['window_days']} дней.",
        f"Подписчиков: {insights.get('subscribers') if insights.get('subscribers') is not None else 'неизвестно'}.",
        f"Опубликовано постов: {s.get('total_published', 0)}.",
        f"Средний охват на пост: {s.get('avg_views_per_post', 0)}; "
        f"реакций: {s.get('avg_reactions_per_post', 0)}; "
        f"пересылок: {s.get('avg_forwards_per_post', 0)}.",
    ]
    if s.get("subscribers_delta") is not None:
        lines.append(f"Изменение числа подписчиков за период: {s['subscribers_delta']:+d}.")

    tier = insights.get("tier")
    if tier:
        if not tier.get("scheduling"):
            lines.append(
                f"Тариф канала: {tier['name']}. Автопостинг по расписанию на этом тарифе "
                "НЕдоступен (нужен апгрейд) — не предлагай настроить расписание."
            )
        else:
            maxp = tier.get("max_posts_per_day")
            if isinstance(maxp, int) and maxp >= 0:
                lines.append(
                    f"Тариф канала: {tier['name']}. ПОТОЛОК автопостинга: {maxp} пост(ов)/день. "
                    f"НИКОГДА не рекомендуй, не обещай и не указывай больше {maxp} постов в день — "
                    "это жёсткий лимит тарифа (для большего нужен апгрейд)."
                )
        if not tier.get("repost_mode"):
            lines.append(
                "Режим репостов (пересборка чужих новостей) на этом тарифе недоступен — "
                'в рекомендациях по расписанию используй только тип "topic".'
            )

    active = insights.get("active", {})
    if active.get("reach_rate_pct") is not None:
        lines.append(
            f"Средний охват к подписчикам (reach rate): {active['reach_rate_pct']}% "
            "— доля аудитории, в среднем видящая пост."
        )
    if active.get("notifications_pct") is not None:
        lines.append(
            f"С включёнными уведомлениями: {active['notifications_pct']}% подписчиков "
            "(официальная статистика Telegram)."
        )
    lines.append(
        "Важно: Telegram НЕ отдаёт, кто открывал канал. Это прокси «активности», "
        "не точные данные о заходах — так и объясняй пользователю."
    )

    ah = insights.get("active_hours")
    if ah:
        lines.append(
            f"Пик публикаций по охвату: {ah['window']} — на это окно приходится "
            f"{ah['share_pct']}% просмотров."
        )

    tc = insights.get("topic_contrast")
    if tc:
        lines.append(
            f"Темы: «{tc['top_topic']}» собирают в {tc['ratio']}× больше реакций на пост "
            f"({tc['top_avg_reactions']} против {tc['bottom_avg_reactions']} у «{tc['bottom_topic']}»)."
        )

    if insights.get("by_topic"):
        top = insights["by_topic"][:5]
        lines.append("Топ темы (тема: посты / ср.просмотры / ср.реакции):")
        for t in top:
            lines.append(
                f"  - {t['topic']}: {t['post_count']} / {t['avg_views']} / {t['avg_reactions']}"
            )

    f = insights.get("forecast")
    if f:
        lo, hi = f["projected_reach_growth_pct"]
        lines.append(
            f"Прогноз (оценка): сейчас ~{f['current_posts_per_week']} постов/нед; "
            f"+{f['extra_posts_per_week']} поста/нед → прирост охвата ≈ {lo}–{hi}%."
        )

    sample = insights.get("posts_sample") or []
    origin = insights.get("posts_sample_origin")
    if sample and origin == "channel":
        # Живой скрейп: это реальные посты самого канала, но опубликованы не нами,
        # поэтому метрик (просмотры/реакции) по ним нет — только текст.
        lines.append(
            f"\n=== ТЕКСТЫ ПОСЛЕДНИХ ПОСТОВ КАНАЛА (живой скрейп, {len(sample)} шт., "
            "текст обрезан) — это реальные посты самого канала. Наш сервис их НЕ "
            "публиковал, поэтому метрик (просмотры/реакции) по ним нет. ЧИТАЙ их и "
            "оценивай заголовки, структуру, подачу, CTA ==="
        )
        for i, p in enumerate(sample, 1):
            lines.append(f"[{i}] {p['text']}")
    elif sample:
        lines.append(
            f"\n=== ТЕКСТЫ ПОСТОВ (выборка {len(sample)} самых просматриваемых, "
            "текст обрезан) — ЧИТАЙ их, оценивай заголовки, структуру, подачу, CTA ==="
        )
        for i, p in enumerate(sample, 1):
            lines.append(
                f"[{i}] тема «{p['topic']}» · {p['views']} просм. / "
                f"{p['reactions']} реакц. / {p['forwards']} пересыл.\n{p['text']}"
            )
    return "\n".join(lines)


# ─── Agentic-слой: предложение настроить расписание ────────────────────────────
#
# «Как в Notion»: ИИ не только советует, но и может ВЫПОЛНИТЬ действие. Когда совет
# содержит конкретную рекомендацию по расписанию, модель добавляет в конец ответа
# служебный блок-намерение (его вырезаем из текста). По намерению backend ДЕТЕРМИНИ-
# РОВАННО собирает готовый недельный план (времена — из реального пика активности,
# тип слотов — из режима канала и лимитов тарифа). Сам план применяет отдельный
# confirm-эндпоинт, когда владелец нажимает «Да, настрой». LLM лишь решает КОГДА
# предложить и в каком объёме (postов/день, типы) — но НЕ выдумывает времена.

# Окно «дневных» слотов (локальный час) — как в легаси frequency-режиме (09:00–21:00).
_DAY_BAND = (9, 21)
# Маркеры служебного блока-намерения. Внутри — компактный JSON, который модель
# формирует, когда рекомендует расписание. Текст между маркерами не показываем.
_ACTION_RE = re.compile(
    r"\[\[SCHEDULE_ACTION\]\](.*?)\[\[/SCHEDULE_ACTION\]\]", re.DOTALL
)

# Страховка на случай, когда модель УСТНО предлагает «настрою расписание за вас?»,
# но забывает служебный блок: без блока карточки-кнопки нет, и текстовое «да»
# зацикливается (модель снова предлагает). Если ответ содержит такую фразу-
# предложение, а блока нет — собираем предложение по дефолтам. Мультиязычно
# (язык канала может быть ru/uz/en), поэтому ловим ключевые корни во всех трёх.
_OFFER_RE = re.compile(
    r"настро\w*\s+(?:это\s+|вам\s+|вот\s+)?(?:расписан|график)"  # ru: «настрою расписание/график»
    r"|(?:расписан\w*|график\w*)\s+за\s+вас"                      # ru: «расписание за вас»
    r"|set\s*(?:it\s*)?up\s+(?:this\s+|the\s+|a\s+)?schedul"      # en: «set up the schedule»
    r"|schedul\w*\s+for\s+you|want\s+me\s+to\s+set"               # en: «schedule for you / want me to set»
    r"|jadval\w*\s+sozla|sozlab\s+ber",                           # uz: «jadval sozlash / sozlab beraman»
    re.IGNORECASE,
)


def _offers_schedule(reply: str) -> bool:
    """True, если ответ содержит фразу-предложение настроить расписание (любой из
    языков канала). Используется как страховка, когда модель забыла служебный блок."""
    return bool(_OFFER_RE.search(reply or ""))


def _extract_action(reply: str) -> tuple[Optional[dict], str]:
    """Вынимает служебный блок-намерение из ответа LLM и возвращает (hint, clean_reply).

    hint — распарсенный JSON намерения ({"posts_per_day", "content_split"}) либо None,
    если блока нет. clean_reply — текст ответа без служебного блока (его видит юзер)."""
    m = _ACTION_RE.search(reply or "")
    if not m:
        return None, (reply or "").strip()
    clean = _ACTION_RE.sub("", reply).strip()
    try:
        hint = json.loads(m.group(1).strip())
    except Exception:
        hint = {}  # блок есть, но JSON битый — всё равно предлагаем (по дефолтам)
    return (hint if isinstance(hint, dict) else {}), clean


def _to_local_hour(utc_hour: int, tz_name: str) -> int:
    """UTC-час → час в таймзоне расписания (целое 0..23)."""
    base = datetime.now(timezone.utc).replace(
        hour=utc_hour % 24, minute=0, second=0, microsecond=0
    )
    try:
        return base.astimezone(ZoneInfo(tz_name)).hour
    except Exception:
        return utc_hour % 24


def _peak_local_hour(insights: dict, tz_name: str) -> Optional[int]:
    """Локальный час пика активности (центр 3-часового окна) — якорь расписания.

    None, если пик не посчитан (постов в окне слишком мало)."""
    ah = insights.get("active_hours")
    if not ah:
        return None
    # Центр окна = старт + 1 (окно ровно 3 часа); считаем в TZ расписания, а не в
    # браузерной — слоты ИМЕННО в этой TZ и сработают (см. bot/scheduler.py).
    return _to_local_hour(ah["start_hour_utc"] + 1, tz_name)


def _proposed_times(n: int, peak_hour: Optional[int]) -> List[str]:
    """N времён постинга ("HH:MM") за день: равномерно по дневному окну, но со
    сдвигом так, чтобы один из слотов попал на пик активности канала."""
    lo, hi = _DAY_BAND
    if n <= 1:
        h = peak_hour if peak_hour is not None else 12
        return [f"{h:02d}:00"]

    step = (hi - lo) / (n - 1)
    hours = [round(lo + step * i) for i in range(n)]
    if peak_hour is not None:
        nearest = min(range(n), key=lambda i: abs(hours[i] - peak_hour))
        shift = peak_hour - hours[nearest]
        # Сдвигаем все слоты к пику, но удерживаем их в дневном окне [lo, hi] —
        # иначе крайний слот может уехать в ночь (напр. пик 16:00 → слот 04:00).
        hours = [min(max(h + shift, lo), hi) for h in hours]

    # Дедуп с раздвижкой (после сдвига/клампа два слота могут совпасть на краю).
    seen: set[int] = set()
    out: List[str] = []
    for h in sorted(hours):
        while h in seen and h < hi:
            h += 1
        seen.add(h)
        out.append(f"{h:02d}:00")
    return out


def _content_split(
    n: int, hint_split, content_mode: str, repost_allowed: bool
) -> List[str]:
    """Тип контента для каждого из N слотов дня по порядку.

    Приоритет — явный hint от LLM (напр. ["repost","topic"]); иначе из режима канала
    (both → чередуем repost/topic; repost/topic → один тип). Если тариф не разрешает
    repost — всё в topic (гейт продублируется при применении)."""
    types: List[str] = []
    if isinstance(hint_split, list):
        types = [t for t in hint_split if t in ("topic", "repost")]
    if not types:
        if content_mode == "both":
            types = ["repost", "topic"]
        elif content_mode == "repost":
            types = ["repost"]
        else:
            types = ["topic"]
    types = [types[i % len(types)] for i in range(n)]
    if not repost_allowed:
        types = ["topic"] * n
    return types


def build_schedule_proposal(
    profile, insights: dict, hint: Optional[dict], tz_name: Optional[str] = None
) -> Optional[dict]:
    """Детерминированно собирает предложение расписания из реальных данных канала.

    hint — намерение LLM ({"posts_per_day", "content_split"}); времена и финальный
    план считаем ЗДЕСЬ (не доверяя их модели): времена — из пика активности, объём
    зажат лимитом тарифа, тип слотов — из режима канала/тарифа. Возвращает payload
    для фронта ИЛИ None, если канал не на тарифе с расписанием.

    Структура: {type, posts_per_day, daily:[{time,content_type}], slots:[7×daily],
    timezone, peak_hour_local}. slots — готовая недельная сетка для apply-эндпоинта."""
    hint = hint or {}
    tier = getattr(profile, "subscription_tier", "starter")
    if not allows(tier, "scheduling"):
        return None

    n = hint.get("posts_per_day")
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 2
    n = max(1, n)
    maxp = limit_of(tier, "max_posts_per_day")
    if not is_unlimited(maxp):
        n = min(n, maxp)

    tz_name = tz_name or os.getenv("TZ_NAME", "Asia/Tashkent")
    times = _proposed_times(n, _peak_local_hour(insights, tz_name))
    n = len(times)  # дедуп мог изменить количество

    types = _content_split(
        n,
        hint.get("content_split"),
        getattr(profile, "content_mode", "topic") or "topic",
        allows(tier, "repost_mode"),
    )
    daily = [{"time": t, "content_type": ct} for t, ct in zip(times, types)]
    slots = [
        {"weekday": wd, "time": d["time"], "content_type": d["content_type"], "enabled": True}
        for wd in range(7)
        for d in daily
    ]
    return {
        "type": "set_schedule",
        "posts_per_day": n,
        "daily": daily,
        "slots": slots,
        "timezone": tz_name,
        "peak_hour_local": _peak_local_hour(insights, tz_name),
    }


_SYSTEM_PROMPT = (
    "Ты — ИИ-агент-менеджер Telegram-канала внутри дашборда владельца. Твоя задача "
    "— отвечать на КОНКРЕТНЫЙ вопрос владельца, опираясь ИСКЛЮЧИТЕЛЬНО на блок РЕАЛЬНЫХ "
    "ДАННЫХ ниже. Категорически запрещено выдумывать или округлять «на глаз» любые "
    "числа, которых нет в данных. Если данных для ответа не хватает — честно скажи "
    "об этом и предложи, что включить/собрать.\n\n"
    "ГЛАВНОЕ ПРАВИЛО: отвечай именно на то, о чём спросили. Если владелец спрашивает "
    "«как увеличить просмотры», «что постить», «когда публиковать» и т.п. — давай "
    "прямой, практический ответ на этот вопрос, подкреплённый цифрами из данных. НЕ "
    "пересказывай шаблонный отчёт, если его не просили.\n\n"
    "Трёхпунктовую структуру (1) Что произошло? 2) Почему? 3) Что делать дальше?) "
    "используй ТОЛЬКО когда владелец просит общий разбор/аналитику канала «в целом» "
    "(«проанализируй канал», «что по статистике», «как дела у канала»). Для всех "
    "остальных вопросов — обычный связный ответ по сути, без этих заголовков.\n\n"
    "ЧТЕНИЕ ПОСТОВ: в блоке данных есть раздел «ТЕКСТЫ ПОСТОВ» — это реальный текст "
    "постов канала. Когда владелец просит «прочитай посты», «как улучшить контент/"
    "канал», «что не так с постами» — РЕАЛЬНО разбирай эти тексты: заголовки и первую "
    "строку (хук), структуру, длину, читаемость, наличие и силу призыва к действию "
    "(CTA), и связывай это с просмотрами/реакциями каждого поста. Давай конкретные "
    "правки по конкретным постам, а НЕ отписку «проанализируйте содержание сами». "
    "Если раздела с текстами нет (постов в окне нет) — честно скажи, что читать пока "
    "нечего.\n\n"
    "Про «% активных»: Telegram не сообщает, кто открывал канал. Не называй прокси "
    "(reach rate / уведомления) точным числом активных — поясняй, что это оценка. "
    "Отвечай кратко, по делу, на языке канала.\n\n"
    "НАСТРОЙКА РАСПИСАНИЯ ЗА ВЛАДЕЛЬЦА: автопостинг настраивается как СУТОЧНАЯ сетка "
    "— сколько постов В ДЕНЬ и какого типа (НЕ «постов в неделю»). Рекомендацию по "
    "объёму всегда формулируй в постах/день. Если твой совет включает КОНКРЕТНУЮ "
    "рекомендацию по автопостингу (сколько постов в день и какого типа), и это "
    "уместно — заверши ответ короткой фразой-предложением «Хотите, я настрою это "
    "расписание за вас?» (на языке канала). КРИТИЧЕСКИ ВАЖНО: если ты добавил эту "
    "фразу-предложение — ты ОБЯЗАН добавить и служебный блок ниже; без блока кнопка "
    "у пользователя не появится и предложение будет пустым. Добавь в САМОМ КОНЦЕ "
    "служебный блок ровно в таком формате (пользователь его НЕ увидит, времена не указывай — "
    "их подберёт система по пику активности):\n"
    "[[SCHEDULE_ACTION]]{\"posts_per_day\": <число>, \"content_split\": [<типы>]}[[/SCHEDULE_ACTION]]\n"
    "где content_split — массив длиной posts_per_day с типом каждого поста дня по "
    "порядку: \"repost\" (пересборка чужой новости) или \"topic\" (оригинальный пост). "
    "Пример для «2 поста в день, первый репост, второй тема»: "
    "[[SCHEDULE_ACTION]]{\"posts_per_day\": 2, \"content_split\": [\"repost\", \"topic\"]}[[/SCHEDULE_ACTION]]. "
    "Добавляй этот блок ТОЛЬКО когда реально рекомендуешь расписание; если про "
    "расписание речи нет — блок не добавляй.\n\n"
    "ПОТОЛОК ТАРИФА (КРИТИЧНО): в блоке данных указан потолок автопостинга — сколько "
    "постов в день максимум разрешено тарифом канала. НИКОГДА не рекомендуй, не обещай "
    "словами и не указывай в служебном блоке (posts_per_day / content_split) больше "
    "постов в день, чем этот потолок. Если по данным оптимально было бы больше — назови "
    "потолок тарифа честно и предложи апгрейд, но сама рекомендация и блок ОБЯЗАНЫ "
    "оставаться в пределах потолка. Если автопостинг на тарифе недоступен — не предлагай "
    "настроить расписание вовсе."
)


def chat(
    tenant_id: str,
    message: str,
    history: Optional[List[dict]] = None,
    days: int = 30,
    tz: Optional[str] = None,
) -> dict:
    """ИИ-менеджер отвечает на вопрос о канале поверх реальных метрик.

    Возвращает {"reply": str, "insights": dict, "proposed_action": dict|None}: текст
    для чата + сами числа (фронт может показать карточки/графики рядом с ответом) +
    необязательное ПРЕДЛОЖЕНИЕ ДЕЙСТВИЯ — готовый план расписания, который владелец
    может применить одной кнопкой («хотите, настрою?» → confirm-эндпоинт). history —
    прошлые реплики чата [{role, content}] для контекста. tz — IANA-таймзона юзера
    (из браузера), в ней показывается пик активности."""
    from generator import groq_chat

    insights = build_insights(tenant_id, days, tz)
    profile = get_tenant_profile(tenant_id)
    channel_name = getattr(profile, "channel_name", "") or tenant_id
    language = getattr(profile, "language", "") or "ru"

    data_block = _insights_to_prompt(insights)
    user = (
        f"Канал: {channel_name}. Язык канала: {language}.\n\n"
        f"=== РЕАЛЬНЫЕ ДАННЫЕ КАНАЛА ===\n{data_block}\n"
        f"=== КОНЕЦ ДАННЫХ ===\n\n"
        f"Вопрос владельца: {message.strip()}"
    )
    raw = groq_chat(_SYSTEM_PROMPT, user, temperature=0.4, history=history)

    # Вырезаем служебный блок-намерение и, если он был, собираем готовое предложение
    # расписания из реальных данных (времена — из пика, объём — в рамках тарифа).
    action_hint, reply = _extract_action(raw)
    # Если модель устно предложила настроить расписание, но не вставила блок —
    # всё равно собираем предложение (по дефолтам), чтобы устное «Хотите, настрою?»
    # ВСЕГДА сопровождалось карточкой с кнопкой, а не уводило «да» в пустоту.
    if action_hint is None and _offers_schedule(reply):
        action_hint = {}
    proposed_action = None
    if action_hint is not None:
        try:
            proposed_action = build_schedule_proposal(profile, insights, action_hint)
        except Exception as e:
            logging.warning("Не удалось собрать предложение расписания (%s): %s", tenant_id, e)

    return {"reply": reply, "insights": insights, "proposed_action": proposed_action}
