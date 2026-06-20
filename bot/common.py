"""Общие хелперы доступа и безопасной отправки, используемые хендлерами."""
from typing import List, Optional

from aiogram import types
from aiogram.types import InlineKeyboardMarkup, Message

from bot.config import ADMIN_ID
from database import (
    TenantProfile,
    get_active_tenants,
    get_active_tenants_for_owner,
    get_all_tenants,
    get_tenants_for_owner,
    is_tenant_owner,
)


def is_super(user: Optional[types.User]) -> bool:
    """Супер-админ (владелец системы) — может назначать клиентов и видит всё."""
    return user is not None and str(user.id) == ADMIN_ID


def is_admin(user: Optional[types.User]) -> bool:
    """Авторизован пользоваться ботом: супер-админ ИЛИ клиент, владеющий ≥1 каналом.

    Имя сохранено для совместимости с хендлерами; семантика — «есть доступ».
    Синхронные обращения к БД дешёвые (индекс по owner_id), вызовов мало.
    """
    if user is None:
        return False
    if is_super(user):
        return True
    return bool(get_tenants_for_owner(str(user.id)))


def visible_tenants(
    user: Optional[types.User], active_only: bool = False
) -> List[TenantProfile]:
    """Каналы, видимые пользователю: супер-админу — все, клиенту — только свои."""
    if user is None:
        return []
    if is_super(user):
        return get_active_tenants() if active_only else get_all_tenants()
    uid = str(user.id)
    return get_active_tenants_for_owner(uid) if active_only else get_tenants_for_owner(uid)


def owns_tenant(user: Optional[types.User], tenant_id: str) -> bool:
    """Может ли пользователь управлять этим тенантом (супер-админ или владелец)."""
    if user is None:
        return False
    return is_super(user) or is_tenant_owner(tenant_id, str(user.id))


def cb_data(callback: types.CallbackQuery) -> str:
    """callback.data, нормализованный к str (aiogram типизирует его как Optional)."""
    return callback.data or ""


async def edit(
    callback: types.CallbackQuery,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    """Безопасно редактирует сообщение под callback (оно может быть недоступно)."""
    msg = callback.message
    if isinstance(msg, Message):
        await msg.edit_text(text, reply_markup=reply_markup)


async def reply(
    callback: types.CallbackQuery,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    """Безопасно отвечает на сообщение под callback (опц. с inline-клавиатурой)."""
    msg = callback.message
    if isinstance(msg, Message):
        await msg.answer(text, reply_markup=reply_markup)
