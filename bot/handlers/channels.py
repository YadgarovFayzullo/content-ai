"""Хендлеры управления каналами: меню, список, добавление, удаление, постинг, владельцы.

Доступ: супер-админ видит/управляет всеми каналами и назначает клиентов;
клиент — только своими (owner_id). Гейт — is_admin (=«авторизован»), супер-операции
— is_super, доступ к конкретному каналу — owns_tenant.
"""
import asyncio
import html

from aiogram import Router, types, F
from aiogram.filters import Command, CommandObject
from aiogram_dialog import DialogManager

from bot.common import (
    is_admin,
    is_super,
    visible_tenants,
)
from bot.keyboards import get_admin_keyboard
from bot.metrics import collect_metrics
from database import (
    confirm_login_request,
    get_tenants_for_owner,
    get_top_posts,
)

router = Router()


@router.message(Command("start"))
@router.message(F.text == "ℹ️ Yordam")
async def cmd_start(
    message: types.Message,
    dialog_manager: DialogManager,
    command: CommandObject = None,
):
    # Deep-link авторизации в веб-панель: /start auth_<login_token>
    if command and command.args and command.args.startswith("auth_"):
        await _handle_web_login(message, command.args[len("auth_"):])
        return
    if not await asyncio.to_thread(is_admin, message.from_user):
        # Неавторизованный: показываем его user_id, чтобы он отправил админу
        # для привязки канала (решает «курицу и яйцо» с получением ID).
        uid = message.from_user.id if message.from_user else "?"
        await message.answer(
            "👋 Bu — <b>Content AI</b> boshqaruv boti.\n\n"
            f"Sizning Telegram <b>user_id</b>'ingiz: <code>{uid}</code>\n\n"
            "Kanal biriktirilishi uchun shu ID'ni administratorga yuboring."
        )
        return
    # Сбрасываем активный aiogram-dialog стек (FSMContext.clear() этого не делал) —
    # /start всегда возвращает пользователя в главное меню из любого диалога.
    await dialog_manager.reset_stack()
    await message.answer(
        "👋 <b>Boshqaruv paneli</b>\n\n"
        "Kanallarni boshqarish uchun pastdagi tugmalardan foydalaning.",
        reply_markup=get_admin_keyboard(is_super(message.from_user)),
    )


async def _handle_web_login(message: types.Message, token: str):
    """Подтверждает вход в веб-панель по deep-link токену.

    Авторизоваться вправе только супер-админ или владелец хотя бы одного канала;
    остальным показываем их user_id, чтобы передать администратору для привязки.
    """
    user = message.from_user
    uid = str(user.id) if user else ""
    super_ = await asyncio.to_thread(is_super, user)
    owned = [] if super_ else await asyncio.to_thread(get_tenants_for_owner, uid)
    eligible = super_ or bool(owned)

    ok = await asyncio.to_thread(
        confirm_login_request, token, uid, not eligible
    )
    if not eligible:
        return await message.answer(
            "🚫 Sizga hali birorta kanal biriktirilmagan, shuning uchun web-panelga "
            "kira olmaysiz.\n\n"
            f"Sizning <b>user_id</b>'ingiz: <code>{uid}</code>\n"
            "Kirish uchun ushbu ID'ni administratorga yuboring."
        )
    if not ok:
        return await message.answer(
            "⌛️ Kirish so'rovi eskirgan yoki yaroqsiz. Saytda qaytadan urinib ko'ring."
        )
    await message.answer(
        "✅ Kirish tasdiqlandi. Saytga qaytib oching — avtomatik kirasiz."
    )


@router.message(F.text == "📋 Kanallar ro'yxati")
async def menu_list(message: types.Message):
    if not await asyncio.to_thread(is_admin, message.from_user):
        return
    super_ = is_super(message.from_user)
    tenants = await asyncio.to_thread(visible_tenants, message.from_user)
    if not tenants:
        return await message.answer("Hozircha hech qanday kanal qo'shilmagan.")
    lines = []
    for t in tenants:
        mark = "🟢" if t.active else "⏸"
        name = f" — {html.escape(t.channel_name)}" if t.channel_name else ""
        owner = f"  👤<code>{t.owner_id}</code>" if (super_ and t.owner_id) else ""
        lines.append(f"{mark} {t.chat_id}{name}{owner}")
    await message.answer("📋 <b>Kanallar:</b>\n\n" + "\n".join(lines))


# «🚀 Hozir post qilish» (подтверждение + массовая публикация) переехало в
# aiogram-dialog: bot/dialogs/channel_admin.py → PostAllSG / post_all_dialog.


@router.message(F.text == "📊 Metrikalarni yig'ish")
async def menu_collect_metrics(message: types.Message):
    if not await asyncio.to_thread(is_admin, message.from_user):
        return
    await message.answer("📊 <b>Metrikalar yig'ilmoqda...</b> (Telethon orqali)")
    saved = await collect_metrics()

    tenants = await asyncio.to_thread(visible_tenants, message.from_user, True)
    lines = [f"✅ {saved} ta o'lchov saqlandi.\n"]
    for t in tenants:
        top = await asyncio.to_thread(get_top_posts, t.tenant_id, 1)
        if top:
            preview = html.escape(top[0][:60].replace("\n", " "))
            lines.append(f"🏆 <b>{html.escape(t.chat_id)}</b>: «{preview}…»")
        else:
            lines.append(f"➖ {html.escape(t.chat_id)}: metrika hali yo'q")
    await message.answer("\n".join(lines))


