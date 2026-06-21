"""aiogram-dialog: админ-флоу управления каналами (добавление, назначение клиента).

Заменяет прежний FSM (AddChannel / AssignClient) из bot/handlers/channels.py.
Точки входа — reply-кнопки «➕ Kanal qo'shish» и «👤 Mijoz biriktirish»
(+ команды-алиасы /v2addchannel, /v2assign для поэтапного теста).
"""
import asyncio
import html
from pathlib import Path

from aiogram import F, Router, types
from aiogram.enums import ContentType
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import FSInputFile
from aiogram_dialog import Dialog, DialogManager, ShowMode, StartMode, Window
from aiogram_dialog.widgets.input import MessageInput
from aiogram_dialog.widgets.kbd import Button, Cancel, Column, Select, SwitchTo
from aiogram_dialog.widgets.text import Const, Format

from bot import rag_client
from bot.common import is_admin, is_super, owns_tenant, visible_tenants
from bot.config import DEFAULT_FORBIDDEN_TOPICS, SCRAPE_HISTORY_LIMIT
from bot.scraper import scrape_channel_history
from publisher import CAPTION_LIMIT, send_to_telegram
from repost import produce_content
from style_analyzer import analyze_style
from database import (
    PostHistory,
    add_tenant_rule,
    assign_tenant_owner,
    create_tenant,
    get_tenant_by_chat_id,
    remove_tenant,
    update_tenant_profile,
)


class AddChannelSG(StatesGroup):
    input = State()


class AssignClientSG(StatesGroup):
    select = State()
    user_id = State()


class RemoveChannelSG(StatesGroup):
    select = State()
    confirm = State()


class PublishSG(StatesGroup):
    """Выбор канала → генерация → превью (фото+текст) → публикация/отмена.

    Единый флоу для «🎯 Bitta kanalga post» и «👁 Preview»: обе reply-кнопки
    стартуют его, а кнопки превью используют общие on_publish/on_cancel.
    """
    select = State()
    confirm = State()


class PostAllSG(StatesGroup):
    """Подтверждение → массовая публикация во ВСЕ активные каналы.

    Заменяет прежний FSM-флоу «🚀 Hozir post qilish» (postallyes/postallno
    callback-кнопки) из bot/handlers/channels.py.
    """
    confirm = State()


# --- Добавление канала: одно окно ввода + тяжёлая обработка --------------------


async def on_channel_input(
    message: types.Message, widget, dialog_manager: DialogManager
):
    if not await asyncio.to_thread(is_admin, message.from_user):
        return
    bot = message.bot
    chat_id = (message.text or "").strip()
    if not (chat_id.startswith("@") or chat_id.startswith("-100")):
        await message.answer(
            "⚠️ Noto'g'ri format. @username yoki -100... bilan boshlanishi kerak.\n"
            "Qayta urinib ko'ring."
        )
        return

    # Юзернеймы в Telegram регистронезависимы — нормализуем, чтобы @IEANDGS и
    # @ieandgs не создавали дубль (chat_id уникален и регистрозависим в БД).
    if chat_id.startswith("@"):
        chat_id = chat_id.lower()

    try:
        chat = await bot.get_chat(chat_id)
    except Exception as e:
        await message.answer(
            f"❌ Kanalga ulanib bo'lmadi: <code>{html.escape(str(e))}</code>\n"
            "Bot kanalga admin sifatida qo'shilganini tekshiring."
        )
        return

    channel_name = chat.title or (f"@{chat.username}" if chat.username else chat_id)
    # Добавивший становится владельцем канала (супер-админ потом может переназначить).
    profile = await asyncio.to_thread(
        create_tenant, chat_id, channel_name=channel_name, owner_id=str(message.from_user.id)
    )

    if not profile:
        await message.answer("Bu kanal allaqachon ro'yxatda bor.")
        await dialog_manager.done()
        return

    await asyncio.to_thread(
        add_tenant_rule, profile.tenant_id, "forbidden_topic", DEFAULT_FORBIDDEN_TOPICS,
        True,  # is_system — служебное правило, скрыто от клиента в «Qoidalar»
    )

    await message.answer(
        f"✅ <b>{html.escape(chat_id)}</b> qo'shildi!\n"
        f"<i>tenant_id:</i> <code>{profile.tenant_id[:8]}…</code>\n\n"
        "📥 Kanal tarixi o'qilmoqda va tahlil qilinmoqda..."
    )

    # Скрейпим историю канала. Дальше два НЕЗАВИСИМЫХ потребителя постов:
    # анализ стиля (нужен только LLM) и индексация в RAG (нужен RAG-сервис).
    posts = await scrape_channel_history(chat_id, limit=SCRAPE_HISTORY_LIMIT)

    if not posts:
        await message.answer(
            "⚠️ Kanal tarixini o'qib bo'lmadi (Telethon sozlanmagan yoki post yo'q).\n"
            "Bot umumiy bilim asosida ishlaydi. Sozlamalarni qo'lda kiriting."
        )
        await dialog_manager.done()
        return

    # 1) Автоанализ стиля → автозаполнение профиля (tone/audience/style/topics).
    style = await asyncio.to_thread(analyze_style, posts)
    if style:
        await asyncio.to_thread(update_tenant_profile, profile.tenant_id, **style)

    # 1b) Типичная длина поста канала (медиана — устойчива к «⚡️444» и лонгридам).
    lengths = sorted(len(p["text"]) for p in posts if p.get("text"))
    if lengths:
        median_len = lengths[len(lengths) // 2]
        await asyncio.to_thread(
            update_tenant_profile, profile.tenant_id, avg_post_length=median_len
        )

    # 2) Индексация в RAG (если сервис недоступен — indexed=0, но это не фатально).
    indexed = await rag_client.index_posts(profile.tenant_id, posts)

    style_line = (
        f"\n🎨 Aniqlangan uslub: <i>{html.escape(style.get('tone', '—'))}</i>"
        if style else ""
    )
    rag_line = (
        f"\n🧠 {indexed} ta post RAG'ga indekslandi."
        if indexed
        else "\n⚠️ RAG-servis ishlamayapti — postlar indekslanmadi (faktlar bo'lmaydi)."
    )
    await message.answer(
        f"📥 {len(posts)} ta post o'qildi.{style_line}{rag_line}\n"
        "Sozlamalarni «⚙️ Sozlamalar» orqali to'g'rilashingiz mumkin."
    )
    await dialog_manager.done()


# --- Назначение клиента (только супер-админ): выбор канала → ввод user_id ------


async def assign_select_getter(dialog_manager: DialogManager, **kwargs):
    user = dialog_manager.event.from_user
    tenants = await asyncio.to_thread(visible_tenants, user)
    # (подпись кнопки, chat_id) — chat_id служит item_id для Select.
    channels = [
        (f"{t.channel_name}{' 👤' if t.owner_id else ''}", t.chat_id) for t in tenants
    ]
    return {"channels": channels, "has_channels": bool(channels)}


async def on_assign_channel_selected(
    callback: types.CallbackQuery,
    widget,
    dialog_manager: DialogManager,
    item_id: str,
):
    if not is_super(callback.from_user):
        return await callback.answer()
    dialog_manager.dialog_data["assign_chat_id"] = item_id
    await dialog_manager.switch_to(AssignClientSG.user_id)


async def assign_user_id_getter(dialog_manager: DialogManager, **kwargs):
    return {"chat_id": dialog_manager.dialog_data.get("assign_chat_id", "")}


async def on_assign_user_input(
    message: types.Message, widget, dialog_manager: DialogManager
):
    if not is_super(message.from_user):
        return
    bot = message.bot
    chat_id = dialog_manager.dialog_data.get("assign_chat_id")
    if not chat_id:
        await message.answer("Sessiya tugadi.")
        await dialog_manager.done()
        return

    raw = (message.text or "").strip()
    if not raw.isdigit():
        return await message.answer("⚠️ Faqat raqamli user_id yuboring (yoki 0).")

    profile = await asyncio.to_thread(get_tenant_by_chat_id, chat_id)
    if not profile:
        await message.answer("Kanal topilmadi.")
        await dialog_manager.done()
        return

    new_owner = None if raw == "0" else raw
    await asyncio.to_thread(assign_tenant_owner, profile.tenant_id, new_owner)

    if not new_owner:
        await message.answer(f"✅ <b>{html.escape(chat_id)}</b> mijozdan ajratildi.")
        await dialog_manager.done()
        return

    # Уведомляем клиента в личку. Сработает, только если он уже запускал бота
    # (Telegram не даёт писать первым тем, кто не нажал /start).
    notified = False
    try:
        await bot.send_message(
            chat_id=int(new_owner),
            text=(
                f"🎉 <b>Sizga kanal biriktirildi!</b>\n\n"
                f"Kanal: <b>{html.escape(chat_id)}</b>"
                f"{(' — ' + html.escape(profile.channel_name)) if profile.channel_name else ''}\n\n"
                "Boshqarish uchun /start bosing — sozlamalar, preview va post qilish "
                "shu yerda."
            ),
        )
        notified = True
    except Exception:
        notified = False

    note = (
        "📨 Mijozga xabar yuborildi."
        if notified
        else "⚠️ Mijozga xabar yuborilmadi — u avval botni /start qilishi kerak."
    )
    await message.answer(
        f"✅ <b>{html.escape(chat_id)}</b> mijozga biriktirildi: <code>{new_owner}</code>\n"
        f"{note}"
    )
    await dialog_manager.done()


# --- Удаление канала (с подтверждением): выбор канала → подтверждение → delete --


async def remove_select_getter(dialog_manager: DialogManager, **kwargs):
    user = dialog_manager.event.from_user
    tenants = await asyncio.to_thread(visible_tenants, user)
    channels = [
        (f"{'🟢' if t.active else '⏸'} {t.channel_name or t.chat_id}", t.chat_id)
        for t in tenants
    ]
    return {"channels": channels, "has_channels": bool(channels)}


async def on_remove_channel_selected(
    callback: types.CallbackQuery,
    widget,
    dialog_manager: DialogManager,
    item_id: str,
):
    profile = await asyncio.to_thread(get_tenant_by_chat_id, item_id)
    if not profile or not await asyncio.to_thread(
        owns_tenant, callback.from_user, profile.tenant_id
    ):
        return await callback.answer("Ruxsat yo'q")
    dialog_manager.dialog_data["remove_chat_id"] = item_id
    dialog_manager.dialog_data["remove_name"] = profile.channel_name or item_id
    await dialog_manager.switch_to(RemoveChannelSG.confirm)


async def remove_confirm_getter(dialog_manager: DialogManager, **kwargs):
    return {
        "chat_id": dialog_manager.dialog_data.get("remove_chat_id", ""),
        "name": dialog_manager.dialog_data.get("remove_name", ""),
    }


async def on_remove_confirm(
    callback: types.CallbackQuery, widget, dialog_manager: DialogManager
):
    chat_id = dialog_manager.dialog_data.get("remove_chat_id")
    if not chat_id:
        await callback.answer("Sessiya tugadi")
        await dialog_manager.done()
        return
    profile = await asyncio.to_thread(get_tenant_by_chat_id, chat_id)
    # Повторная проверка прав на момент удаления (владелец мог смениться).
    if not profile or not await asyncio.to_thread(
        owns_tenant, callback.from_user, profile.tenant_id
    ):
        await callback.answer("Ruxsat yo'q")
        await dialog_manager.done()
        return

    tenant_id = profile.tenant_id
    removed = await asyncio.to_thread(remove_tenant, chat_id)
    if not removed:
        await callback.answer("Xatolik: kanal topilmadi")
        await dialog_manager.done()
        return

    # БД-запись удалена — чистим и RAG-индекс канала (иначе осиротевшие вектора).
    await rag_client.delete_tenant(tenant_id)
    await callback.answer(f"{chat_id} o'chirildi")
    await callback.message.answer(
        f"🗑 <b>{html.escape(chat_id)}</b> ro'yxatdan olib tashlandi."
    )
    await dialog_manager.done()


# --- Публикация: выбор канала → генерация+превью → публикация/отмена ----------
#
# Превью (фото+текст) шлём отдельным сообщением — так обходим лимит подписи к фото
# (1024) и переиспользуем проверенную логику отправки. Сгенерированный пост лежит
# в dialog_data до решения: ✅ публикуем тем же контентом, ❌ удаляем temp-картинку.


async def publish_select_getter(dialog_manager: DialogManager, **kwargs):
    user = dialog_manager.event.from_user
    tenants = await asyncio.to_thread(visible_tenants, user)
    channels = [
        (f"{'🟢' if t.active else '⏸'} {t.channel_name or t.chat_id}", t.chat_id)
        for t in tenants
    ]
    return {"channels": channels, "has_channels": bool(channels)}


async def _send_preview(message: types.Message, chat_id: str, content: dict):
    """Показывает то, что выйдет в канал: фото + текст (или только текст)."""
    caption = (
        f"👁 <b>Preview — {html.escape(chat_id)}</b> "
        f"(<i>{html.escape(content['entry'].topic or '')}</i>):\n\n{content['text']}"
    )
    image_path = content["image_path"]
    has_image = bool(image_path) and Path(image_path).exists()
    if not has_image:
        await message.answer(caption)
        return
    photo = FSInputFile(image_path)
    # Подпись к фото ограничена 1024 символами — длиннее шлём отдельным текстом.
    if len(caption) <= CAPTION_LIMIT:
        try:
            await message.answer_photo(photo, caption=caption)
        except TelegramBadRequest:
            await message.answer_photo(photo)
            await message.answer(caption)
    else:
        await message.answer_photo(photo)
        await message.answer(caption)


async def on_publish_channel_selected(
    callback: types.CallbackQuery,
    widget,
    dialog_manager: DialogManager,
    item_id: str,
):
    profile = await asyncio.to_thread(get_tenant_by_chat_id, item_id)
    if not profile or not await asyncio.to_thread(
        owns_tenant, callback.from_user, profile.tenant_id
    ):
        return await callback.answer("Ruxsat yo'q")

    await callback.answer()
    await callback.message.answer(
        f"🚀 <b>{html.escape(item_id)}</b> uchun post tayyorlanmoqda..."
    )
    try:
        content = await produce_content(profile)
    except Exception as e:
        await callback.message.answer(f"❌ Generatsiya xatosi: {html.escape(str(e))}")
        await dialog_manager.done()
        return

    # Кладём только примитивы, нужные для повторной сборки PostHistory на публикации
    # (source-поля — для дедупа репоста, story_* — для семантического дедупа V2).
    entry = content["entry"]
    dialog_manager.dialog_data.update(
        {
            "pub_chat_id": item_id,
            "pub_text": content["text"],
            "pub_image": content["image_path"],
            "pub_topic": entry.topic or "",
            "pub_tenant_id": profile.tenant_id,
            "pub_source_chat": entry.source_chat_id,
            "pub_source_msg": entry.source_message_id,
            "pub_story_vec": content.get("story_vec"),
            "pub_story_keys": content.get("story_keys"),
        }
    )
    await _send_preview(callback.message, item_id, content)
    # ShowMode.SEND — окно подтверждения уходит НОВЫМ сообщением ПОД превью.
    # Иначе aiogram-dialog редактирует старое окно выбора канала на месте (оно
    # выше превью), и подтверждение «preview yuqorida» оказывается над превью.
    dialog_manager.show_mode = ShowMode.SEND
    await dialog_manager.switch_to(PublishSG.confirm)


async def publish_confirm_getter(dialog_manager: DialogManager, **kwargs):
    return {"chat_id": dialog_manager.dialog_data.get("pub_chat_id", "")}


async def on_publish_confirm(
    callback: types.CallbackQuery, widget, dialog_manager: DialogManager
):
    """Общий обработчик ✅: публикует ранее сгенерированный пост в канал."""
    data = dialog_manager.dialog_data
    chat_id = data.get("pub_chat_id")
    if not chat_id:
        await callback.answer("Sessiya tugadi")
        await dialog_manager.done()
        return
    profile = await asyncio.to_thread(get_tenant_by_chat_id, chat_id)
    if not profile or not await asyncio.to_thread(
        owns_tenant, callback.from_user, profile.tenant_id
    ):
        await callback.answer("Ruxsat yo'q")
        await dialog_manager.done()
        return

    await callback.answer()
    await callback.message.answer(f"🚀 <b>{html.escape(chat_id)}</b> ga joylanmoqda...")

    entry = PostHistory(
        tenant_id=data["pub_tenant_id"],
        topic=data.get("pub_topic", ""),
        content=data["pub_text"],
        image_path=data.get("pub_image", ""),
        posted=False,
        source_chat_id=data.get("pub_source_chat"),
        source_message_id=data.get("pub_source_msg"),
    )
    content = {
        "text": data["pub_text"],
        "image_path": data.get("pub_image", ""),
        "entry": entry,
    }
    if data.get("pub_story_vec") and data.get("pub_story_keys"):
        content["story_vec"] = data["pub_story_vec"]
        content["story_keys"] = data["pub_story_keys"]

    ok, detail = await send_to_telegram(callback.bot, content, chat_id)
    if ok:
        await callback.message.answer(
            f"✅ <b>{html.escape(chat_id)}</b> kanaliga post joylandi."
        )
    else:
        await callback.message.answer(f"❌ Yuborilmadi: {html.escape(str(detail))}")
    await dialog_manager.done()


async def on_publish_cancel(
    callback: types.CallbackQuery, widget, dialog_manager: DialogManager
):
    """Общий обработчик ❌: ничего не публикует, чистит temp-картинку превью."""
    img = dialog_manager.dialog_data.get("pub_image")
    if img and Path(img).exists():
        try:
            Path(img).unlink()
        except OSError:
            pass
    await callback.answer("Bekor qilindi")
    await callback.message.answer("❌ Bekor qilindi — post joylanmadi.")
    await dialog_manager.done()


async def post_all_getter(dialog_manager: DialogManager, **kwargs):
    user = dialog_manager.event.from_user
    tenants = await asyncio.to_thread(visible_tenants, user, True)
    names = "\n".join(f"• {html.escape(t.chat_id)}" for t in tenants)
    return {"count": len(tenants), "names": names}


async def on_post_all_confirm(
    callback: types.CallbackQuery, widget, dialog_manager: DialogManager
):
    """Генерирует и публикует пост в каждый активный канал, шлёт сводку."""
    if not await asyncio.to_thread(is_admin, callback.from_user):
        await callback.answer("Ruxsat yo'q")
        await dialog_manager.done()
        return
    tenants = await asyncio.to_thread(visible_tenants, callback.from_user, True)
    if not tenants:
        await callback.answer()
        await callback.message.answer("⚠️ Faol kanallar yo'q.")
        await dialog_manager.done()
        return

    await callback.answer()
    await callback.message.answer("🚀 <b>Jarayon boshlandi...</b>")
    lines = []
    ok_count = 0
    for profile in tenants:
        try:
            content = await produce_content(profile)
        except Exception as e:
            lines.append(f"❌ {html.escape(profile.chat_id)}: {html.escape(str(e)[:60])}")
            continue
        ok, detail = await send_to_telegram(callback.bot, content, profile.chat_id)
        ok_count += 1 if ok else 0
        lines.append(
            f"✅ {html.escape(profile.chat_id)}" if ok
            else f"❌ {html.escape(profile.chat_id)}: {html.escape(str(detail)[:60])}"
        )
        await asyncio.sleep(3)

    await callback.message.answer(
        f"🏁 <b>Yakun:</b> {ok_count}/{len(tenants)} kanalga yuborildi\n\n"
        + "\n".join(lines)
    )
    await dialog_manager.done()


add_channel_dialog = Dialog(
    Window(
        Const(
            "➕ <b>Yangi kanal qo'shish</b>\n\n"
            "Iltimos, kanalning userneymini yuboring (masalan: <code>@mening_kanalim</code>) "
            "yoki kanal ID raqamini kiriting."
        ),
        MessageInput(on_channel_input, content_types=ContentType.TEXT),
        Cancel(Const("🔙 Bekor qilish")),
        state=AddChannelSG.input,
    ),
)


assign_client_dialog = Dialog(
    Window(
        Const("👤 <b>Qaysi kanalga mijoz biriktiramiz?</b>"),
        Column(
            Select(
                Format("{item[0]}"),
                id="asg_ch",
                item_id_getter=lambda item: item[1],
                items="channels",
                on_click=on_assign_channel_selected,
            ),
        ),
        Cancel(Const("🔙 Bekor qilish")),
        state=AssignClientSG.select,
        getter=assign_select_getter,
    ),
    Window(
        Format(
            "👤 <b>{chat_id}</b> uchun mijozning Telegram <b>user_id</b>'sini yuboring "
            "(raqam).\nBiriktirishni bekor qilish uchun <code>0</code> yuboring."
        ),
        MessageInput(on_assign_user_input, content_types=ContentType.TEXT),
        SwitchTo(Const("🔙 Orqaga"), id="asg_back", state=AssignClientSG.select),
        state=AssignClientSG.user_id,
        getter=assign_user_id_getter,
    ),
)


remove_channel_dialog = Dialog(
    Window(
        Const("🗑 <b>O'chirmoqchi bo'lgan kanalni tanlang:</b>"),
        Column(
            Select(
                Format("{item[0]}"),
                id="rm_ch",
                item_id_getter=lambda item: item[1],
                items="channels",
                on_click=on_remove_channel_selected,
            ),
        ),
        Cancel(Const("🔙 Bekor qilish")),
        state=RemoveChannelSG.select,
        getter=remove_select_getter,
    ),
    Window(
        Format(
            "⚠️ <b>{name}</b> (<code>{chat_id}</code>) butunlay o'chirilsinmi?\n\n"
            "Kanal profili va uning RAG-indeksi o'chadi. Bu amalni qaytarib bo'lmaydi."
        ),
        Button(Const("✅ Ha, o'chirish"), id="rm_yes", on_click=on_remove_confirm),
        SwitchTo(Const("🔙 Orqaga"), id="rm_back", state=RemoveChannelSG.select),
        Cancel(Const("❌ Bekor qilish")),
        state=RemoveChannelSG.confirm,
        getter=remove_confirm_getter,
    ),
)


publish_dialog = Dialog(
    Window(
        Const(
            "🎯 <b>Qaysi kanal uchun post tayyorlaymiz?</b>\n"
            "(Avval preview ko'rsatiladi — joylashdan oldin tasdiqlaysiz)"
        ),
        Column(
            Select(
                Format("{item[0]}"),
                id="pub_ch",
                item_id_getter=lambda item: item[1],
                items="channels",
                on_click=on_publish_channel_selected,
            ),
        ),
        Cancel(Const("🔙 Bekor qilish")),
        state=PublishSG.select,
        getter=publish_select_getter,
    ),
    Window(
        Format(
            "👆 <b>{chat_id}</b> uchun preview yuqorida.\n"
            "⚠️ Shu post <b>kanalga joylansinmi?</b>"
        ),
        # Обе кнопки → общие on_publish_confirm / on_publish_cancel.
        Button(Const("✅ Ha, joylash"), id="pub_yes", on_click=on_publish_confirm),
        Button(Const("❌ Bekor"), id="pub_no", on_click=on_publish_cancel),
        state=PublishSG.confirm,
        getter=publish_confirm_getter,
    ),
)


post_all_dialog = Dialog(
    Window(
        Format(
            "⚠️ <b>Diqqat:</b> post <b>{count} ta faol kanalga</b> darhol joylanadi:\n\n"
            "{names}\n\nDavom etamizmi?"
        ),
        Button(Format("✅ Ha, {count} kanalga"), id="pa_yes", on_click=on_post_all_confirm),
        Cancel(Const("❌ Bekor")),
        state=PostAllSG.confirm,
        getter=post_all_getter,
    ),
)


# Точки входа: reply-кнопки admin-меню → запуск соответствующего диалога.
entry_router = Router()


@entry_router.message(F.text == "🚀 Hozir post qilish")
async def open_post_all_dialog(message: types.Message, dialog_manager: DialogManager):
    if not await asyncio.to_thread(is_admin, message.from_user):
        return
    tenants = await asyncio.to_thread(visible_tenants, message.from_user, True)
    if not tenants:
        return await message.answer(
            "⚠️ Faol kanallar yo'q. Kanal qo'shing yoki faollashtiring."
        )
    await dialog_manager.start(PostAllSG.confirm, mode=StartMode.RESET_STACK)


@entry_router.message(F.text == "🎯 Bitta kanalga post")
async def open_publish_dialog(message: types.Message, dialog_manager: DialogManager):
    # Один флоу: выбор канала → preview (фото+текст) → publish/cancel.
    if not await asyncio.to_thread(is_admin, message.from_user):
        return
    tenants = await asyncio.to_thread(visible_tenants, message.from_user)
    if not tenants:
        return await message.answer("⚠️ Kanallar yo'q. Avval kanal qo'shing.")
    await dialog_manager.start(PublishSG.select, mode=StartMode.RESET_STACK)


@entry_router.message(F.text == "➕ Kanal qo'shish")
async def open_add_channel_dialog(message: types.Message, dialog_manager: DialogManager):
    if not await asyncio.to_thread(is_admin, message.from_user):
        return
    await dialog_manager.start(AddChannelSG.input, mode=StartMode.RESET_STACK)


@entry_router.message(F.text == "👤 Mijoz biriktirish")
async def open_assign_client_dialog(message: types.Message, dialog_manager: DialogManager):
    if not is_super(message.from_user):
        return
    await dialog_manager.start(AssignClientSG.select, mode=StartMode.RESET_STACK)


@entry_router.message(F.text == "🗑 Kanalni o'chirish")
async def open_remove_channel_dialog(message: types.Message, dialog_manager: DialogManager):
    if not await asyncio.to_thread(is_admin, message.from_user):
        return
    await dialog_manager.start(RemoveChannelSG.select, mode=StartMode.RESET_STACK)
