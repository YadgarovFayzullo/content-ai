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
from collections import defaultdict
from datetime import datetime, timezone
from typing import List, Optional

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


def _peak_hours(window_posts: List[dict]) -> Optional[dict]:
    """Лучшее непрерывное 3-часовое окно публикации по сумме просмотров.

    Это активность по ВРЕМЕНИ ПУБЛИКАЦИИ (когда вышедшие посты набирают охват) —
    честный прокси к активным часам аудитории, а не данные о заходах (их нет).
    Часы — UTC; перевод в таймзону канала остаётся на отображающий слой."""
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
        "window": f"{best_start:02d}:00–{end:02d}:00 UTC",
        "share_pct": round(best_views / total * 100, 1),
        "by_hour": {h: by_hour.get(h, 0) for h in range(24)},
    }


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


def build_insights(tenant_id: str, days: int = 30) -> dict:
    """Детерминированные AI Insights канала за `days` дней (без LLM).

    Возвращает реальные числа: оба прокси «% активных», активные часы, контраст
    тем, прогноз охвата + сырой summary/by_topic для графиков и для LLM-слоя."""
    stats = get_tenant_stats(tenant_id, days=days, limit=100)
    summary = stats.get("summary", {})
    by_topic = stats.get("by_topic", [])
    window_posts = get_window_post_metrics(tenant_id, days)
    broadcast = get_latest_broadcast_stat(tenant_id)

    subs = summary.get("subscribers")
    avg_views = summary.get("avg_views_per_post", 0)

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
        "active_hours": _peak_hours(window_posts),
        "topic_contrast": _topic_contrast(by_topic),
        "forecast": _forecast(
            summary.get("total_published", 0), days, avg_views
        ),
        "summary": summary,
        "by_topic": by_topic,
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
    return "\n".join(lines)


_SYSTEM_PROMPT = (
    "Ты — ИИ-агент-менеджер Telegram-канала внутри дашборда владельца. Твоя задача "
    "— анализировать канал по его запросу, опираясь ИСКЛЮЧИТЕЛЬНО на блок РЕАЛЬНЫХ "
    "ДАННЫХ ниже. Категорически запрещено выдумывать или округлять «на глаз» любые "
    "числа, которых нет в данных. Если данных для ответа не хватает — честно скажи "
    "об этом и предложи, что включить/собрать.\n\n"
    "Структурируй ответ по трём пунктам, когда это уместно:\n"
    "1) Что произошло? — факты из данных.\n"
    "2) Почему произошло? — обоснованная интерпретация (помечай гипотезы как гипотезы).\n"
    "3) Что делать дальше? — 1–3 конкретных действия.\n\n"
    "Про «% активных»: Telegram не сообщает, кто открывал канал. Не называй прокси "
    "(reach rate / уведомления) точным числом активных — поясняй, что это оценка. "
    "Отвечай кратко, по делу, на языке канала."
)


def chat(
    tenant_id: str,
    message: str,
    history: Optional[List[dict]] = None,
    days: int = 30,
) -> dict:
    """ИИ-менеджер отвечает на вопрос о канале поверх реальных метрик.

    Возвращает {"reply": str, "insights": dict}: текст для чата + сами числа
    (фронт может показать карточки/графики рядом с ответом). history — прошлые
    реплики чата [{role, content}] для контекста диалога."""
    from generator import groq_chat

    insights = build_insights(tenant_id, days)
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
