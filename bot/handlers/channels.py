"""Хендлеры управления каналами: меню, список, добавление, удаление, постинг, владельцы.

Доступ: супер-админ видит/управляет всеми каналами и назначает клиентов;
клиент — только своими (owner_id). Гейт — is_admin (=«авторизован»), супер-операции
— is_super, доступ к конкретному каналу — owns_tenant.
"""
import asyncio
import html
from pathlib import Path

from aiogram import Bot, Router, types, F
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext

from bot.common import (
    AddChannel,
    AssignClient,
    is_admin,
    is_super,
    owns_tenant,
    visible_tenants,
    cb_data,
    edit,
    reply,
)
from bot.config import DEFAULT_FORBIDDEN_TOPICS, SCRAPE_HISTORY_LIMIT
from bot.keyboards import (
    get_admin_keyboard,
    get_assign_pick_keyboard,
    get_channels_delete_keyboard,
    get_post_pick_keyboard,
    get_postall_confirm_keyboard,
    get_preview_pick_keyboard,
    get_publish_confirm_keyboard,
)
from database import PostHistory
from bot.scraper import scrape_channel_history
from bot import rag_client
from publisher import send_to_telegram
from repost import produce_content
from style_analyzer import analyze_style
from bot.metrics import collect_metrics
from database import (
    add_tenant_rule,
    assign_tenant_owner,
    confirm_login_request,
    create_tenant,
    get_tenant_by_chat_id,
    get_tenants_for_owner,
    get_top_posts,
    remove_tenant,
    update_tenant_profile,
)

router = Router()


@router.message(Command("start"))
@router.message(F.text == "ℹ️ Yordam")
async def cmd_start(
    message: types.Message, state: FSMContext, command: CommandObject = None
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
    await state.clear()
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


@router.message(F.text == "🚀 Hozir post qilish")
async def menu_post_now(message: types.Message, state: FSMContext, bot: Bot):
    if not await asyncio.to_thread(is_admin, message.from_user):
        return
    await state.clear()
    tenants = await asyncio.to_thread(visible_tenants, message.from_user, True)
    if not tenants:
        return await message.answer("⚠️ Faol kanallar yo'q. Kanal qo'shing yoki faollashtiring.")

    # Подтверждение перед массовой публикацией (защита от случайного нажатия).
    names = "\n".join(f"• {html.escape(t.chat_id)}" for t in tenants)
    await message.answer(
        f"⚠️ <b>Diqqat:</b> post <b>{len(tenants)} ta faol kanalga</b> "
        f"darhol joylanadi:\n\n{names}\n\nDavom etamizmi?",
        reply_markup=get_postall_confirm_keyboard(len(tenants)),
    )


@router.callback_query(F.data == "postallyes")
async def cb_post_all_confirm(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    if not await asyncio.to_thread(is_admin, callback.from_user):
        return await callback.answer()
    await callback.answer()
    tenants = await asyncio.to_thread(visible_tenants, callback.from_user, True)
    if not tenants:
        return await edit(callback, "⚠️ Faol kanallar yo'q.")

    await edit(callback, "🚀 <b>Jarayon boshlandi...</b>")
    lines = []
    ok_count = 0
    for profile in tenants:
        try:
            content = await produce_content(profile)
        except Exception as e:
            lines.append(f"❌ {html.escape(profile.chat_id)}: {html.escape(str(e)[:60])}")
            continue
        ok, detail = await send_to_telegram(bot, content, profile.chat_id)
        ok_count += 1 if ok else 0
        lines.append(
            f"✅ {html.escape(profile.chat_id)}" if ok
            else f"❌ {html.escape(profile.chat_id)}: {html.escape(str(detail)[:60])}"
        )
        await asyncio.sleep(3)

    await reply(
        callback,
        f"🏁 <b>Yakun:</b> {ok_count}/{len(tenants)} kanalga yuborildi\n\n" + "\n".join(lines)
    )


@router.message(F.text == "🎯 Bitta kanalga post")
async def menu_post_one(message: types.Message, state: FSMContext):
    if not await asyncio.to_thread(is_admin, message.from_user):
        return
    await state.clear()
    tenants = await asyncio.to_thread(visible_tenants, message.from_user)
    if not tenants:
        return await message.answer("⚠️ Kanallar yo'q. Avval kanal qo'shing.")
    await message.answer(
        "🎯 <b>Qaysi kanalga post qilamiz?</b>",
        reply_markup=get_post_pick_keyboard(tenants),
    )


@router.callback_query(F.data.startswith("post:"))
async def cb_post_one(callback: types.CallbackQuery, state: FSMContext):
    chat_id = cb_data(callback)[len("post:"):]
    profile = await asyncio.to_thread(get_tenant_by_chat_id, chat_id)
    if not profile or not await asyncio.to_thread(owns_tenant, callback.from_user, profile.tenant_id):
        return await callback.answer("Ruxsat yo'q")

    await callback.answer()
    await edit(callback, f"🚀 <b>{html.escape(chat_id)}</b> uchun post tayyorlanmoqda...")

    try:
        content = await produce_content(profile)
    except Exception as e:
        return await reply(callback, f"❌ Generatsiya xatosi: {html.escape(str(e))}")

    # Не публикуем сразу — показываем, что выйдет, и ждём подтверждения.
    await state.update_data(
        pub_chat_id=chat_id,
        pub_text=content["text"],
        pub_image=content["image_path"],
        pub_topic=content["entry"].topic or "",
        pub_tenant_id=profile.tenant_id,
        # Source-поля repost'а нужны для дедупа после публикации (иначе одна и та
        # же чужая новость репостится снова и снова). В topic-режиме они None.
        pub_source_chat=content["entry"].source_chat_id,
        pub_source_msg=content["entry"].source_message_id,
    )
    await reply(
        callback,
        f"📝 <b>{html.escape(chat_id)}</b> uchun tayyor "
        f"(<i>{html.escape(content['entry'].topic or '')}</i>):\n\n"
        f"{content['text']}\n\n"
        f"⚠️ Shu post <b>kanalga joylanadimi?</b>",
        reply_markup=get_publish_confirm_keyboard(),
    )


@router.callback_query(F.data == "pubyes")
async def cb_publish_confirm(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    if not await asyncio.to_thread(is_admin, callback.from_user):
        return await callback.answer()
    data = await state.get_data()
    chat_id = data.get("pub_chat_id")
    if not chat_id:
        return await callback.answer("Sessiya tugadi")
    profile = await asyncio.to_thread(get_tenant_by_chat_id, chat_id)
    if not profile or not await asyncio.to_thread(owns_tenant, callback.from_user, profile.tenant_id):
        return await callback.answer("Ruxsat yo'q")

    await callback.answer()
    await edit(callback, f"🚀 <b>{html.escape(chat_id)}</b> ga joylanmoqda...")

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
    ok, detail = await send_to_telegram(bot, content, chat_id)
    await state.clear()
    if ok:
        await reply(callback, f"✅ <b>{html.escape(chat_id)}</b> kanaliga post joylandi.")
    else:
        await reply(callback, f"❌ Yuborilmadi: {html.escape(str(detail))}")


@router.callback_query(F.data == "pubno")
async def cb_publish_cancel(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    img = data.get("pub_image")
    if img and Path(img).exists():
        try:
            Path(img).unlink()
        except OSError:
            pass
    await state.clear()
    await callback.answer("Bekor qilindi")
    await edit(callback, "❌ Bekor qilindi — post joylanmadi.")


@router.message(F.text == "👁 Preview (post qilmasdan)")
async def menu_preview(message: types.Message, state: FSMContext):
    if not await asyncio.to_thread(is_admin, message.from_user):
        return
    await state.clear()
    tenants = await asyncio.to_thread(visible_tenants, message.from_user)
    if not tenants:
        return await message.answer("⚠️ Kanallar yo'q. Avval kanal qo'shing.")
    await message.answer(
        "👁 <b>Qaysi kanal uchun preview?</b>\n(Post kanalga chiqmaydi — faqat sizga ko'rsatiladi)",
        reply_markup=get_preview_pick_keyboard(tenants),
    )


@router.callback_query(F.data.startswith("prev:"))
async def cb_preview(callback: types.CallbackQuery):
    chat_id = cb_data(callback)[len("prev:"):]
    profile = await asyncio.to_thread(get_tenant_by_chat_id, chat_id)
    if not profile or not await asyncio.to_thread(owns_tenant, callback.from_user, profile.tenant_id):
        return await callback.answer("Ruxsat yo'q")

    await callback.answer()
    await edit(callback, f"👁 <b>{html.escape(chat_id)}</b> uchun preview tayyorlanmoqda...")

    try:
        content = await produce_content(profile)
    except Exception as e:
        return await reply(callback, f"❌ Generatsiya xatosi: {html.escape(str(e))}")

    # Превью: НЕ публикуем, НЕ сохраняем в историю. Картинку (если была) удаляем.
    image_path = content["image_path"]
    if image_path and Path(image_path).exists():
        try:
            Path(image_path).unlink()
        except OSError:
            pass

    await reply(
        callback,
        f"👁 <b>Preview — {html.escape(chat_id)}</b> "
        f"(<i>{html.escape(content['entry'].topic or '')}</i>):\n\n{content['text']}",
    )


@router.message(F.text == "➕ Kanal qo'shish")
async def menu_add_init(message: types.Message, state: FSMContext):
    if not await asyncio.to_thread(is_admin, message.from_user):
        return
    await state.set_state(AddChannel.waiting_for_channel)
    await message.answer(
        "➕ <b>Yangi kanal qo'shish</b>\n\n"
        "Iltimos, kanalning userneymini yuboring (masalan: <code>@mening_kanalim</code>) "
        "yoki kanal ID raqamini kiriting.\n\n"
        "Bekor qilish uchun /start bosing."
    )


@router.message(AddChannel.waiting_for_channel)
async def process_add_channel(message: types.Message, state: FSMContext, bot: Bot):
    if not await asyncio.to_thread(is_admin, message.from_user):
        return
    chat_id = (message.text or "").strip()
    if not (chat_id.startswith("@") or chat_id.startswith("-100")):
        await message.answer(
            "⚠️ Noto'g'ri format. @username yoki -100... bilan boshlanishi kerak.\n"
            "Qayta urinib ko'ring yoki /start bosing."
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
    await state.clear()

    if not profile:
        await message.answer("Bu kanal allaqachon ro'yxatda bor.")
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


@router.message(F.text == "🗑 Kanalni o'chirish")
async def menu_remove_init(message: types.Message, state: FSMContext):
    if not await asyncio.to_thread(is_admin, message.from_user):
        return
    await state.clear()
    tenants = await asyncio.to_thread(visible_tenants, message.from_user)
    if not tenants:
        return await message.answer("O'chirish uchun kanallar mavjud emas.")
    await message.answer(
        "🗑 <b>O'chirmoqchi bo'lgan kanalni tanlang:</b>",
        reply_markup=get_channels_delete_keyboard(tenants),
    )


@router.callback_query(F.data.startswith("del_"))
async def callback_delete(callback: types.CallbackQuery):
    chat_id = cb_data(callback).replace("del_", "", 1)
    profile = await asyncio.to_thread(get_tenant_by_chat_id, chat_id)
    if not profile or not await asyncio.to_thread(owns_tenant, callback.from_user, profile.tenant_id):
        return await callback.answer("Ruxsat yo'q")
    tenant_id = profile.tenant_id
    removed = await asyncio.to_thread(remove_tenant, chat_id)
    if removed:
        await rag_client.delete_tenant(tenant_id)
        await callback.answer(f"{chat_id} o'chirildi")
        tenants = await asyncio.to_thread(visible_tenants, callback.from_user)
        await edit(
            callback,
            f"🗑 <b>{html.escape(chat_id)}</b> ro'yxatdan olib tashlandi.",
            reply_markup=get_channels_delete_keyboard(tenants),
        )
    else:
        await callback.answer("Xatolik: Kanal topilmadi")


# --- Назначение клиентов (только супер-админ) ---------------------------------


@router.message(F.text == "👤 Mijoz biriktirish")
async def menu_assign(message: types.Message, state: FSMContext):
    if not is_super(message.from_user):
        return
    await state.clear()
    tenants = await asyncio.to_thread(visible_tenants, message.from_user)
    if not tenants:
        return await message.answer("Avval kanal qo'shing.")
    await message.answer(
        "👤 <b>Qaysi kanalga mijoz biriktiramiz?</b>",
        reply_markup=get_assign_pick_keyboard(tenants),
    )


@router.callback_query(F.data.startswith("asg:"))
async def cb_assign_pick(callback: types.CallbackQuery, state: FSMContext):
    if not is_super(callback.from_user):
        return await callback.answer()
    chat_id = cb_data(callback)[len("asg:"):]
    await state.update_data(assign_chat_id=chat_id)
    await state.set_state(AssignClient.waiting_for_user_id)
    await reply(
        callback,
        f"👤 <b>{html.escape(chat_id)}</b> uchun mijozning Telegram <b>user_id</b>'sini yuboring "
        "(raqam). Biriktirishni bekor qilish uchun <code>0</code> yuboring.\n(Chiqish: /start)",
    )
    await callback.answer()


@router.message(AssignClient.waiting_for_user_id)
async def process_assign(message: types.Message, state: FSMContext, bot: Bot):
    if not is_super(message.from_user):
        return
    data = await state.get_data()
    chat_id = data.get("assign_chat_id")
    if not chat_id:
        await state.clear()
        return await message.answer("Sessiya tugadi. /start bosing.")

    raw = (message.text or "").strip()
    if not raw.isdigit():
        return await message.answer("⚠️ Faqat raqamli user_id yuboring (yoki 0).")

    profile = await asyncio.to_thread(get_tenant_by_chat_id, chat_id)
    if not profile:
        await state.clear()
        return await message.answer("Kanal topilmadi.")

    new_owner = None if raw == "0" else raw
    await asyncio.to_thread(assign_tenant_owner, profile.tenant_id, new_owner)
    await state.set_state(None)

    if not new_owner:
        return await message.answer(f"✅ <b>{html.escape(chat_id)}</b> mijozdan ajratildi.")

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
