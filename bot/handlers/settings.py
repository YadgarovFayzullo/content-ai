"""Хендлеры настроек арендатора: профиль, редактор полей, правила."""
import asyncio

from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.common import EditTenant, is_admin, owns_tenant, visible_tenants, cb_data, edit, reply
from bot.config import EDIT_FIELDS, SCRAPE_HISTORY_LIMIT
from bot.keyboards import (
    get_edit_menu_keyboard,
    get_rule_type_keyboard,
    get_rules_keyboard,
    get_schedule_keyboard,
    get_settings_pick_keyboard,
    get_sources_keyboard,
    render_rules_text,
    render_schedule_text,
    render_settings_card,
    render_sources_text,
)
from bot.scraper import scrape_channel_history
from bot import rag_client
from database import (
    add_tenant_rule,
    add_tenant_source,
    get_all_tenants,
    get_tenant_by_chat_id,
    get_tenant_profile,
    get_tenant_rules,
    get_tenant_sources,
    remove_tenant_rule,
    remove_tenant_source,
    update_tenant_profile,
)

router = Router()


async def _show_card(target_message: Message, tenant_id: str) -> bool:
    profile = await asyncio.to_thread(get_tenant_profile, tenant_id)
    if not profile:
        return False
    rules = await asyncio.to_thread(get_tenant_rules, tenant_id, False)
    await target_message.answer(
        render_settings_card(profile, len(rules)), reply_markup=get_edit_menu_keyboard()
    )
    return True


@router.message(F.text == "⚙️ Sozlamalar")
async def menu_settings(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user):
        return
    await state.clear()
    tenants = await asyncio.to_thread(visible_tenants, message.from_user)
    if not tenants:
        return await message.answer("Avval kanal qo'shing.")
    await message.answer(
        "⚙️ Qaysi kanalni sozlaymiz?",
        reply_markup=get_settings_pick_keyboard(tenants),
    )


@router.callback_query(F.data.startswith("pick:"))
async def cb_pick(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user):
        return await callback.answer()
    chat_id = cb_data(callback)[len("pick:"):]
    profile = await asyncio.to_thread(get_tenant_by_chat_id, chat_id)
    if not profile or not await asyncio.to_thread(owns_tenant, callback.from_user, profile.tenant_id):
        return await callback.answer("Ruxsat yo'q")
    await state.update_data(tenant_id=profile.tenant_id)
    rules = await asyncio.to_thread(get_tenant_rules, profile.tenant_id, False)
    await edit(
        callback,
        render_settings_card(profile, len(rules)), reply_markup=get_edit_menu_keyboard()
    )
    await callback.answer()


@router.callback_query(F.data.startswith("set:"))
async def cb_set_field(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user):
        return await callback.answer()
    code = cb_data(callback)[len("set:"):]
    if code not in EDIT_FIELDS:
        return await callback.answer("?")
    _field, label, kind = EDIT_FIELDS[code]
    await state.update_data(field_code=code)
    await state.set_state(EditTenant.entering_value)
    hint = " (0.0–1.0 oralig'ida son)" if kind == "float" else ""
    await reply(
        callback,
        f"✏️ <b>{label}</b> uchun yangi qiymat yuboring{hint}:\n(Bekor: /start)"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("clr:"))
async def cb_clear_field(callback: types.CallbackQuery, state: FSMContext):
    """Очистить поле (установить пустой/None)."""
    if not is_admin(callback.from_user):
        return await callback.answer()
    data = await state.get_data()
    tenant_id = data.get("tenant_id")
    if not tenant_id:
        return await callback.answer("Sessiya tugadi.")

    code = cb_data(callback)[len("clr:"):]
    if code not in ("tmpl", "cta"):
        return await callback.answer("?")

    field, label, _ = EDIT_FIELDS[code]
    await asyncio.to_thread(
        update_tenant_profile, tenant_id, {field: None}
    )
    await _show_card(callback.message, tenant_id)
    await callback.answer(f"✅ {label} o'chirildi")


@router.message(EditTenant.entering_value)
async def cb_value(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user):
        return
    data = await state.get_data()
    tenant_id = data.get("tenant_id")
    code = data.get("field_code")
    if not tenant_id or code not in EDIT_FIELDS:
        await state.clear()
        return await message.answer("Sessiya tugadi. /start bosing.")

    field, label, kind = EDIT_FIELDS[code]
    value = (message.text or "").strip()
    if kind == "float":
        try:
            value = float(value.replace(",", "."))
            if not 0.0 <= value <= 1.0:
                raise ValueError
        except ValueError:
            return await message.answer("⚠️ 0.0–1.0 oralig'ida son kiriting.")

    await asyncio.to_thread(update_tenant_profile, tenant_id, **{field: value})
    await state.set_state(None)  # выходим из ввода, tenant_id в данных сохраняется
    await message.answer(f"✅ {label} yangilandi.")
    await _show_card(message, tenant_id)


@router.callback_query(F.data == "toggle")
async def cb_toggle(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user):
        return await callback.answer()
    data = await state.get_data()
    tenant_id = data.get("tenant_id")
    if not tenant_id:
        return await callback.answer("Sessiya tugadi")
    profile = await asyncio.to_thread(get_tenant_profile, tenant_id)
    if not profile:
        return await callback.answer("Sessiya tugadi")
    profile.active = not profile.active
    await asyncio.to_thread(update_tenant_profile, tenant_id, active=profile.active)
    rules = await asyncio.to_thread(get_tenant_rules, tenant_id, False)
    await edit(
        callback,
        render_settings_card(profile, len(rules)), reply_markup=get_edit_menu_keyboard()
    )
    await callback.answer("Holat o'zgartirildi")


@router.callback_query(F.data == "modetgl")
async def cb_mode_toggle(callback: types.CallbackQuery, state: FSMContext):
    """Переключает режим контента канала: topic ↔ repost."""
    if not is_admin(callback.from_user):
        return await callback.answer()
    data = await state.get_data()
    tenant_id = data.get("tenant_id")
    if not tenant_id:
        return await callback.answer("Sessiya tugadi")
    profile = await asyncio.to_thread(get_tenant_profile, tenant_id)
    if not profile:
        return await callback.answer("Sessiya tugadi")
    new_mode = "repost" if (profile.content_mode or "topic") != "repost" else "topic"
    await asyncio.to_thread(update_tenant_profile, tenant_id, content_mode=new_mode)
    profile = await asyncio.to_thread(get_tenant_profile, tenant_id)
    rules = await asyncio.to_thread(get_tenant_rules, tenant_id, False)
    await edit(
        callback,
        render_settings_card(profile, len(rules)), reply_markup=get_edit_menu_keyboard()
    )
    await callback.answer(
        "Repost rejimi (manbalardan)" if new_mode == "repost" else "Topik rejimi (original)"
    )


@router.callback_query(F.data == "ragtgl")
async def cb_rag_toggle(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user):
        return await callback.answer()
    data = await state.get_data()
    tenant_id = data.get("tenant_id")
    if not tenant_id:
        return await callback.answer("Sessiya tugadi")
    profile = await asyncio.to_thread(get_tenant_profile, tenant_id)
    if not profile:
        return await callback.answer("Sessiya tugadi")
    new_val = not profile.use_rag
    await asyncio.to_thread(update_tenant_profile, tenant_id, use_rag=new_val)
    rules = await asyncio.to_thread(get_tenant_rules, tenant_id, False)
    profile = await asyncio.to_thread(get_tenant_profile, tenant_id)
    await edit(
        callback,
        render_settings_card(profile, len(rules)), reply_markup=get_edit_menu_keyboard()
    )
    await callback.answer("RAG yoqildi" if new_val else "RAG o'chirildi")


@router.callback_query(F.data == "reftgl")
async def cb_ref_toggle(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user):
        return await callback.answer()
    data = await state.get_data()
    tenant_id = data.get("tenant_id")
    if not tenant_id:
        return await callback.answer("Sessiya tugadi")
    profile = await asyncio.to_thread(get_tenant_profile, tenant_id)
    if not profile:
        return await callback.answer("Sessiya tugadi")
    new_val = not profile.use_references
    await asyncio.to_thread(update_tenant_profile, tenant_id, use_references=new_val)
    rules = await asyncio.to_thread(get_tenant_rules, tenant_id, False)
    profile = await asyncio.to_thread(get_tenant_profile, tenant_id)
    await edit(
        callback,
        render_settings_card(profile, len(rules)), reply_markup=get_edit_menu_keyboard()
    )
    await callback.answer(
        "Manbalar yoqildi" if new_val else "Faqat oʻz kanali"
    )


@router.callback_query(F.data == "rules")
async def cb_rules(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user):
        return await callback.answer()
    data = await state.get_data()
    tenant_id = data.get("tenant_id")
    if not tenant_id:
        return await callback.answer("Sessiya tugadi")
    rules = await asyncio.to_thread(get_tenant_rules, tenant_id, False)
    await edit(callback, render_rules_text(rules), reply_markup=get_rules_keyboard(rules))
    await callback.answer()


@router.callback_query(F.data == "addr")
async def cb_add_rule(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user):
        return await callback.answer()
    await edit(callback, "Qoida turini tanlang:", reply_markup=get_rule_type_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("rt:"))
async def cb_rule_type(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user):
        return await callback.answer()
    rt = cb_data(callback)[len("rt:"):]
    await state.update_data(rule_type=rt)
    await state.set_state(EditTenant.adding_rule_value)
    await reply(callback, f"✏️ <b>{rt}</b> qiymatini yuboring:\n(Bekor: /start)")
    await callback.answer()


@router.message(EditTenant.adding_rule_value)
async def cb_rule_value(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user):
        return
    data = await state.get_data()
    tenant_id = data.get("tenant_id")
    rt = data.get("rule_type")
    if not tenant_id or not rt:
        await state.clear()
        return await message.answer("Sessiya tugadi. /start bosing.")
    value = (message.text or "").strip()
    if not value:
        return await message.answer("Bo'sh qiymat. Qaytadan yuboring:")
    await asyncio.to_thread(add_tenant_rule, tenant_id, rt, value)
    await state.set_state(None)
    rules = await asyncio.to_thread(get_tenant_rules, tenant_id, False)
    await message.answer("✅ Qoida qo'shildi.")
    await message.answer(render_rules_text(rules), reply_markup=get_rules_keyboard(rules))


@router.callback_query(F.data.startswith("delr:"))
async def cb_del_rule(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user):
        return await callback.answer()
    try:
        rule_id = int(cb_data(callback)[len("delr:"):])
    except ValueError:
        return await callback.answer("?")
    await asyncio.to_thread(remove_tenant_rule, rule_id)
    data = await state.get_data()
    tenant_id = data.get("tenant_id")
    rules = await asyncio.to_thread(get_tenant_rules, tenant_id, False) if tenant_id else []
    await edit(callback, render_rules_text(rules), reply_markup=get_rules_keyboard(rules))
    await callback.answer("O'chirildi")


@router.callback_query(F.data == "srcs")
async def cb_sources(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user):
        return await callback.answer()
    data = await state.get_data()
    tenant_id = data.get("tenant_id")
    if not tenant_id:
        return await callback.answer("Sessiya tugadi")
    sources = await asyncio.to_thread(get_tenant_sources, tenant_id)
    await edit(callback, render_sources_text(sources), reply_markup=get_sources_keyboard(sources))
    await callback.answer()


@router.callback_query(F.data == "addsrc")
async def cb_add_source(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user):
        return await callback.answer()
    await state.set_state(EditTenant.adding_source_channel)
    await reply(
        callback,
        "📡 Manba kanal userneymini yuboring (masalan: <code>@tech</code>).\n"
        "Uning postlari faqat kontekst (faktlar) uchun indekslanadi.\n(Bekor: /start)",
    )
    await callback.answer()


@router.message(EditTenant.adding_source_channel)
async def process_add_source(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user):
        return
    data = await state.get_data()
    tenant_id = data.get("tenant_id")
    if not tenant_id:
        await state.clear()
        return await message.answer("Sessiya tugadi. /start bosing.")

    source = (message.text or "").strip()
    if not (source.startswith("@") or source.startswith("-100")):
        return await message.answer("⚠️ @username yoki -100... yuboring.")
    if source.startswith("@"):
        source = source.lower()

    await state.set_state(None)
    await message.answer(f"📥 <b>{source}</b> o'qilmoqda...")

    posts = await scrape_channel_history(source, limit=SCRAPE_HISTORY_LIMIT)
    if not posts:
        return await message.answer(
            "⚠️ Kanal o'qilmadi (yopiq yoki Telethon sozlanmagan). "
            "Faqat <b>ochiq</b> kanallar qo'llab-quvvatlanadi."
        )

    # Префикс источника в id, чтобы point-id не конфликтовал с постами др. каналов.
    for p in posts:
        p["id"] = f"{source}:{p['id']}"
    indexed = await rag_client.index_posts(tenant_id, posts, is_reference=True)

    if indexed:
        await asyncio.to_thread(add_tenant_source, tenant_id, source, indexed)
        await message.answer(
            f"✅ <b>{source}</b> qo'shildi — {indexed} ta post kontekstga indekslandi."
        )
    else:
        await message.answer("⚠️ RAG-servis ishlamayapti — indekslanmadi. Keyinroq urinib ko'ring.")

    sources = await asyncio.to_thread(get_tenant_sources, tenant_id)
    await message.answer(render_sources_text(sources), reply_markup=get_sources_keyboard(sources))


@router.callback_query(F.data.startswith("dels:"))
async def cb_del_source(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user):
        return await callback.answer()
    try:
        src_id = int(cb_data(callback)[len("dels:"):])
    except ValueError:
        return await callback.answer("?")
    await asyncio.to_thread(remove_tenant_source, src_id)
    data = await state.get_data()
    tenant_id = data.get("tenant_id")
    sources = await asyncio.to_thread(get_tenant_sources, tenant_id) if tenant_id else []
    await edit(callback, render_sources_text(sources), reply_markup=get_sources_keyboard(sources))
    await callback.answer("O'chirildi (vektorlar keyingi reindeksda tozalanadi)")


@router.callback_query(F.data == "sched")
async def cb_schedule(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user):
        return await callback.answer()
    data = await state.get_data()
    tenant_id = data.get("tenant_id")
    if not tenant_id:
        return await callback.answer("Sessiya tugadi")
    profile = await asyncio.to_thread(get_tenant_profile, tenant_id)
    if not profile:
        return await callback.answer("Sessiya tugadi")
    await edit(callback, render_schedule_text(profile), reply_markup=get_schedule_keyboard())
    await callback.answer()


@router.callback_query(F.data == "sched_off")
async def cb_sched_off(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user):
        return await callback.answer()
    data = await state.get_data()
    tenant_id = data.get("tenant_id")
    if not tenant_id:
        return await callback.answer("Sessiya tugadi")
    await asyncio.to_thread(update_tenant_profile, tenant_id, schedule_mode="off")
    profile = await asyncio.to_thread(get_tenant_profile, tenant_id)
    await edit(callback, render_schedule_text(profile), reply_markup=get_schedule_keyboard())
    await callback.answer("Jadval o'chirildi")


@router.callback_query(F.data == "sched_freq")
async def cb_sched_freq(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user):
        return await callback.answer()
    await state.set_state(EditTenant.entering_frequency)
    await reply(
        callback,
        "🔢 Kuniga necha marta post chiqsin? <b>1–10</b> oralig'ida son yuboring.\n"
        "Postlar 09:00–21:00 oralig'ida teng taqsimlanadi.\n(Bekor: /start)",
    )
    await callback.answer()


@router.message(EditTenant.entering_frequency)
async def process_frequency(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user):
        return
    data = await state.get_data()
    tenant_id = data.get("tenant_id")
    if not tenant_id:
        await state.clear()
        return await message.answer("Sessiya tugadi. /start bosing.")
    raw = (message.text or "").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= 10):
        return await message.answer("⚠️ 1–10 oralig'ida son yuboring.")
    await asyncio.to_thread(
        update_tenant_profile, tenant_id,
        schedule_mode="frequency", posts_per_day=int(raw),
    )
    await state.set_state(None)
    profile = await asyncio.to_thread(get_tenant_profile, tenant_id)
    await message.answer("✅ Jadval saqlandi.")
    await message.answer(render_schedule_text(profile), reply_markup=get_schedule_keyboard())


@router.callback_query(F.data == "sched_times")
async def cb_sched_times(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user):
        return await callback.answer()
    await state.set_state(EditTenant.entering_times)
    await reply(
        callback,
        "🕐 Post vaqtlarini <b>HH:MM</b> formatida, vergul bilan yuboring.\n"
        "Masalan: <code>09:00, 14:30, 20:00</code>\n(Bekor: /start)",
    )
    await callback.answer()


@router.message(EditTenant.entering_times)
async def process_times(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user):
        return
    data = await state.get_data()
    tenant_id = data.get("tenant_id")
    if not tenant_id:
        await state.clear()
        return await message.answer("Sessiya tugadi. /start bosing.")

    # Парсим и валидируем HH:MM, сортируем, убираем дубли.
    parsed = []
    for part in (message.text or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            return await message.answer(f"⚠️ Noto'g'ri vaqt: <code>{part}</code>. HH:MM formatida.")
        hh, _, mm = part.partition(":")
        if not (hh.isdigit() and mm.isdigit() and 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
            return await message.answer(f"⚠️ Noto'g'ri vaqt: <code>{part}</code>. HH:MM (00:00–23:59).")
        parsed.append(f"{int(hh):02d}:{int(mm):02d}")
    if not parsed:
        return await message.answer("⚠️ Kamida bitta vaqt yuboring.")

    times_str = ",".join(sorted(set(parsed)))
    await asyncio.to_thread(
        update_tenant_profile, tenant_id,
        schedule_mode="times", post_times=times_str,
    )
    await state.set_state(None)
    profile = await asyncio.to_thread(get_tenant_profile, tenant_id)
    await message.answer("✅ Jadval saqlandi.")
    await message.answer(render_schedule_text(profile), reply_markup=get_schedule_keyboard())


@router.callback_query(F.data == "back")
async def cb_back(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user):
        return await callback.answer()
    data = await state.get_data()
    tenant_id = data.get("tenant_id")
    if not tenant_id:
        return await callback.answer("Sessiya tugadi")
    profile = await asyncio.to_thread(get_tenant_profile, tenant_id)
    if not profile:
        return await callback.answer("Sessiya tugadi")
    rules = await asyncio.to_thread(get_tenant_rules, tenant_id, False)
    await edit(
        callback,
        render_settings_card(profile, len(rules)), reply_markup=get_edit_menu_keyboard()
    )
    await callback.answer()


@router.callback_query(F.data == "close")
async def cb_close(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user):
        return await callback.answer()
    await state.clear()
    await edit(callback, "✅ Sozlamalar yopildi.")
    await callback.answer()
