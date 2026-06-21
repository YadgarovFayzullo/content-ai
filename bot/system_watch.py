"""Фоновый наблюдатель за здоровьем сервера → пинги супер-админу в Telegram.

Две задачи (вешаются на APScheduler в main.py):
  • system_watch  — раз в N минут собирает статус и шлёт ТОЛЬКО новые/изменившиеся
                    инциденты (анти-спам: не дублируем один и тот же активный
                    инцидент, и сообщаем, когда он закрылся);
  • system_digest — раз в сутки шлёт сводку (контейнеры/ресурсы), даже если всё ок.

Получатель — ADMIN_ID (супер-админ). Если он не задан, задачи тихо не работают.
"""
import logging

from aiogram import Bot

from bot.config import ADMIN_ID
from monitoring import collect_status

logger = logging.getLogger(__name__)

# Ключи активных инцидентов из прошлого тика — чтобы не спамить одним и тем же и
# сообщать о закрытии. Живёт в процессе бота (одиночный инстанс).
_ACTIVE: set[str] = set()

_SEV_ICON = {"crit": "🔴", "warn": "🟡"}


def _incident_key(inc: dict) -> str:
    return f"{inc['kind']}:{inc['message']}"


async def _send(bot: Bot, text: str) -> None:
    if not ADMIN_ID:
        return
    try:
        await bot.send_message(ADMIN_ID, text, disable_web_page_preview=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("system_watch: не смог отправить супер-админу: %s", e)


async def system_watch(bot: Bot) -> None:
    """Тик наблюдателя: шлёт новые инциденты и уведомления о закрытии прежних."""
    if not ADMIN_ID:
        return
    try:
        snap = await collect_status(with_logs=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("system_watch: сбор статуса упал: %s", e)
        return

    incidents = snap.get("incidents", [])
    current = {_incident_key(i): i for i in incidents}

    new_keys = [k for k in current if k not in _ACTIVE]
    resolved_keys = [k for k in _ACTIVE if k not in current]

    if new_keys:
        lines = ["⚠️ <b>Инциденты на сервере</b>"]
        for k in new_keys:
            inc = current[k]
            lines.append(f"{_SEV_ICON.get(inc['severity'], '•')} {inc['message']}")
            if inc.get("detail"):
                lines.append(f"   <code>{_esc(inc['detail'])}</code>")
        await _send(bot, "\n".join(lines))

    if resolved_keys:
        lines = ["✅ <b>Инциденты закрыты</b>"]
        for k in resolved_keys:
            # message хранится в ключе после первого ':'
            lines.append(f"• {k.split(':', 1)[1]}")
        await _send(bot, "\n".join(lines))

    _ACTIVE.clear()
    _ACTIVE.update(current.keys())


async def system_digest(bot: Bot) -> None:
    """Ежедневная сводка: ресурсы хоста + список контейнеров."""
    if not ADMIN_ID:
        return
    try:
        snap = await collect_status(with_logs=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("system_digest: сбор статуса упал: %s", e)
        return

    await _send(bot, format_digest(snap))


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_digest(snap: dict) -> str:
    """Человекочитаемая сводка для Telegram (HTML)."""
    h = snap["host"]
    lines = [
        "🖥️ <b>Сводка по серверу</b>",
        f"CPU: {h['cpu_pct']}%  ·  RAM: {h['mem']['pct']}% "
        f"({h['mem']['used_mb']}/{h['mem']['total_mb']} MB)",
        f"Диск: {h['disk']['pct']}% ({h['disk']['used_gb']}/{h['disk']['total_gb']} GB)"
        f"  ·  load: {h['load1']}",
        "",
        "<b>Контейнеры:</b>",
    ]
    if not snap["docker_ok"]:
        lines.append(f"🔴 Docker недоступен: {_esc(snap.get('docker_error') or '')}")
    else:
        for c in snap["containers"]:
            icon = "🟢" if c["state"] == "running" else "🔴"
            health = f" ({c['health']})" if c.get("health") else ""
            lines.append(
                f"{icon} {c['name']}{health} — CPU {c['cpu_pct']}% / RAM {c['mem_mb']}MB"
            )

    errs = snap.get("log_errors") or []
    if errs:
        lines.append("")
        lines.append("<b>Ошибки в логах:</b>")
        for e in errs:
            lines.append(f"🟡 {e['container']}: {e['count']}")

    incidents = snap.get("incidents") or []
    if incidents:
        lines.append("")
        lines.append(f"⚠️ Активных инцидентов: {len(incidents)}")
    else:
        lines.append("")
        lines.append("✅ Инцидентов нет")
    return "\n".join(lines)
