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

import logging
import os
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
    "Отвечай кратко, по делу, на языке канала."
)


def chat(
    tenant_id: str,
    message: str,
    history: Optional[List[dict]] = None,
    days: int = 30,
    tz: Optional[str] = None,
) -> dict:
    """ИИ-менеджер отвечает на вопрос о канале поверх реальных метрик.

    Возвращает {"reply": str, "insights": dict}: текст для чата + сами числа
    (фронт может показать карточки/графики рядом с ответом). history — прошлые
    реплики чата [{role, content}] для контекста диалога. tz — IANA-таймзона
    пользователя (из браузера), в ней показывается пик активности."""
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
    reply = groq_chat(_SYSTEM_PROMPT, user, temperature=0.4, history=history)
    return {"reply": reply.strip(), "insights": insights}
