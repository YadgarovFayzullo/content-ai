"""aiogram-dialog: меню настроек арендатора.

Полностью заменяет прежний FSM-флоу настроек. 12 окон: выбор канала → карточка
профиля (быстрые переключатели active/mode/rag/refs) → поля, правила, источники,
расписание.

Запуск — reply-кнопкой «⚙️ Sozlamalar» (или командой-алиасом /v2settings).
"""
import asyncio

from aiogram import F, Router, types
from aiogram.enums import ContentType
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram_dialog import Dialog, DialogManager, StartMode, Window
from aiogram_dialog.widgets.input import MessageInput
from aiogram_dialog.widgets.kbd import Button, Cancel, Column, Select, SwitchTo
from aiogram_dialog.widgets.text import Const, Format

from bot import rag_client
from bot.common import is_admin, owns_tenant, visible_tenants
from bot.config import EDIT_FIELDS, RULE_TYPES, SCRAPE_HISTORY_LIMIT
from bot.keyboards import (
    render_rules_text,
    render_schedule_text,
    render_settings_card,
    render_sources_text,
)
from bot.scraper import scrape_channel_history
from database import (
    add_tenant_rule,
    add_tenant_source,
    get_tenant_profile,
    get_tenant_rules,
    get_tenant_sources,
    remove_tenant_rule,
    remove_tenant_source,
    set_tenant_source_priority,
    update_tenant_profile,
)


class SettingsSG(StatesGroup):
    select = State()
    card = State()
    fields = State()
    edit_value = State()
    rules = State()
    rule_type = State()
    add_rule_value = State()
    sources = State()
    add_source = State()
    source_priority = State()
    set_source_priority = State()
    schedule = State()
    sched_frequency = State()
    sched_times = State()


# --- Окно 1: выбор канала ------------------------------------------------------


async def select_getter(dialog_manager: DialogManager, **kwargs):
    user = dialog_manager.event.from_user
    tenants = await asyncio.to_thread(visible_tenants, user)
    # (подпись кнопки, tenant_id) — tenant_id служит item_id для Select.
    channels = [
        (f"{'🟢' if t.active else '⏸'} {t.channel_name}", t.tenant_id) for t in tenants
    ]
    return {"channels": channels, "has_channels": bool(channels)}


async def on_channel_selected(
    callback: types.CallbackQuery,
    widget,
    dialog_manager: DialogManager,
    item_id: str,
):
    if not await asyncio.to_thread(owns_tenant, callback.from_user, item_id):
        return await callback.answer("Ruxsat yo'q")
    dialog_manager.dialog_data["tenant_id"] = item_id
    await dialog_manager.switch_to(SettingsSG.card)


# --- Окно 2: карточка профиля --------------------------------------------------


async def card_getter(dialog_manager: DialogManager, **kwargs):
    tenant_id = dialog_manager.dialog_data.get("tenant_id")
    profile = await asyncio.to_thread(get_tenant_profile, tenant_id)
    if not profile:
        return {"card": "⚠️ Kanal topilmadi (sessiya eskirgan). /v2settings bosing."}
    rules = await asyncio.to_thread(get_tenant_rules, tenant_id, False)
    return {"card": render_settings_card(profile, len(rules))}


async def _tenant_id(dialog_manager: DialogManager):
    return dialog_manager.dialog_data.get("tenant_id")


async def on_toggle_active(callback, button, dialog_manager: DialogManager):
    tid = await _tenant_id(dialog_manager)
    profile = await asyncio.to_thread(get_tenant_profile, tid)
    if not profile:
        return await callback.answer("Sessiya tugadi")
    await asyncio.to_thread(update_tenant_profile, tid, active=not profile.active)
    await callback.answer("Holat o'zgartirildi")


async def on_toggle_mode(callback, button, dialog_manager: DialogManager):
    tid = await _tenant_id(dialog_manager)
    profile = await asyncio.to_thread(get_tenant_profile, tid)
    if not profile:
        return await callback.answer("Sessiya tugadi")
    new_mode = "repost" if (profile.content_mode or "topic") != "repost" else "topic"
    await asyncio.to_thread(update_tenant_profile, tid, content_mode=new_mode)
    await callback.answer(
        "Repost rejimi (manbalardan)" if new_mode == "repost" else "Topik rejimi (original)"
    )


async def on_toggle_rag(callback, button, dialog_manager: DialogManager):
    tid = await _tenant_id(dialog_manager)
    profile = await asyncio.to_thread(get_tenant_profile, tid)
    if not profile:
        return await callback.answer("Sessiya tugadi")
    new_val = not profile.use_rag
    await asyncio.to_thread(update_tenant_profile, tid, use_rag=new_val)
    await callback.answer("RAG yoqildi" if new_val else "RAG o'chirildi")


async def on_toggle_ref(callback, button, dialog_manager: DialogManager):
    tid = await _tenant_id(dialog_manager)
    profile = await asyncio.to_thread(get_tenant_profile, tid)
    if not profile:
        return await callback.answer("Sessiya tugadi")
    new_val = not profile.use_references
    await asyncio.to_thread(update_tenant_profile, tid, use_references=new_val)
    await callback.answer("Manbalar yoqildi" if new_val else "Faqat oʻz kanali")


# --- Окно 3: список редактируемых полей ---------------------------------------


async def fields_getter(dialog_manager: DialogManager, **kwargs):
    # (подпись, code) — code служит item_id.
    return {"fields": [(label, code) for code, (_f, label, _k) in EDIT_FIELDS.items()]}


async def on_field_selected(
    callback: types.CallbackQuery,
    widget,
    dialog_manager: DialogManager,
    item_id: str,
):
    dialog_manager.dialog_data["field_code"] = item_id
    await dialog_manager.switch_to(SettingsSG.edit_value)


async def _clear_field(callback, dialog_manager: DialogManager, code: str):
    tid = await _tenant_id(dialog_manager)
    field, label, _ = EDIT_FIELDS[code]
    await asyncio.to_thread(update_tenant_profile, tid, **{field: None})
    await callback.answer(f"✅ {label} o'chirildi")


async def on_clear_tmpl(callback, button, dialog_manager: DialogManager):
    await _clear_field(callback, dialog_manager, "tmpl")


async def on_clear_cta(callback, button, dialog_manager: DialogManager):
    await _clear_field(callback, dialog_manager, "cta")


# --- Окно 4: ввод значения поля ------------------------------------------------


async def edit_value_getter(dialog_manager: DialogManager, **kwargs):
    code = dialog_manager.dialog_data.get("field_code")
    _field, label, kind = EDIT_FIELDS[code]
    hint = " (0.0–1.0 oralig'ida son)" if kind == "float" else ""
    return {"label": label, "hint": hint}


async def on_value_input(
    message: types.Message, widget, dialog_manager: DialogManager
):
    code = dialog_manager.dialog_data.get("field_code")
    tid = await _tenant_id(dialog_manager)
    if not code or code not in EDIT_FIELDS or not tid:
        return await message.answer("Sessiya tugadi. /v2settings bosing.")
    field, label, kind = EDIT_FIELDS[code]
    value = (message.text or "").strip()
    if kind == "float":
        try:
            value = float(value.replace(",", "."))
            if not 0.0 <= value <= 1.0:
                raise ValueError
        except ValueError:
            return await message.answer("⚠️ 0.0–1.0 oralig'ida son kiriting.")
    await asyncio.to_thread(update_tenant_profile, tid, **{field: value})
    await message.answer(f"✅ {label} yangilandi.")
    await dialog_manager.switch_to(SettingsSG.card)


# --- Окно 5: правила (список + удаление) --------------------------------------


async def rules_getter(dialog_manager: DialogManager, **kwargs):
    tid = await _tenant_id(dialog_manager)
    rules = await asyncio.to_thread(get_tenant_rules, tid, False)
    # (подпись для кнопки удаления, str(id)).
    items = [
        (f"🗑 [{r.rule_type}] {r.rule_value[:20]}", str(r.id)) for r in rules
    ]
    return {"rules_text": render_rules_text(rules), "rules": items}


async def on_rule_delete(
    callback: types.CallbackQuery,
    widget,
    dialog_manager: DialogManager,
    item_id: str,
):
    try:
        rule_id = int(item_id)
    except ValueError:
        return await callback.answer("?")
    await asyncio.to_thread(remove_tenant_rule, rule_id)
    await callback.answer("O'chirildi")


# --- Окно 6: выбор типа правила -----------------------------------------------


async def rule_types_getter(dialog_manager: DialogManager, **kwargs):
    return {"rule_types": [(rt, rt) for rt in RULE_TYPES]}


async def on_rule_type_selected(
    callback: types.CallbackQuery,
    widget,
    dialog_manager: DialogManager,
    item_id: str,
):
    dialog_manager.dialog_data["rule_type"] = item_id
    await dialog_manager.switch_to(SettingsSG.add_rule_value)


# --- Окно 7: ввод значения правила --------------------------------------------


async def add_rule_value_getter(dialog_manager: DialogManager, **kwargs):
    return {"rule_type": dialog_manager.dialog_data.get("rule_type", "")}


async def on_rule_value_input(
    message: types.Message, widget, dialog_manager: DialogManager
):
    tid = await _tenant_id(dialog_manager)
    rt = dialog_manager.dialog_data.get("rule_type")
    if not tid or not rt:
        return await message.answer("Sessiya tugadi. /v2settings bosing.")
    value = (message.text or "").strip()
    if not value:
        return await message.answer("Bo'sh qiymat. Qaytadan yuboring:")
    await asyncio.to_thread(add_tenant_rule, tid, rt, value)
    await message.answer("✅ Qoida qo'shildi.")
    await dialog_manager.switch_to(SettingsSG.rules)


# --- Окно 8: источники (список + удаление) ------------------------------------


async def sources_getter(dialog_manager: DialogManager, **kwargs):
    tid = await _tenant_id(dialog_manager)
    sources = await asyncio.to_thread(get_tenant_sources, tid)
    items = [
        (f"🗑 {s.source_chat_id} ({s.posts_indexed})", str(s.id)) for s in sources
    ]
    return {"sources_text": render_sources_text(sources), "sources": items}


async def on_source_delete(
    callback: types.CallbackQuery,
    widget,
    dialog_manager: DialogManager,
    item_id: str,
):
    try:
        src_id = int(item_id)
    except ValueError:
        return await callback.answer("?")
    await asyncio.to_thread(remove_tenant_source, src_id)
    await callback.answer("O'chirildi (vektorlar keyingi reindeksda tozalanadi)")


# --- Окно 9: добавление источника ---------------------------------------------


async def on_source_input(
    message: types.Message, widget, dialog_manager: DialogManager
):
    tid = await _tenant_id(dialog_manager)
    if not tid:
        return await message.answer("Sessiya tugadi. /v2settings bosing.")

    source = (message.text or "").strip()
    if not (source.startswith("@") or source.startswith("-100")):
        return await message.answer("⚠️ @username yoki -100... yuboring.")
    if source.startswith("@"):
        source = source.lower()

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
    indexed = await rag_client.index_posts(tid, posts, is_reference=True)

    if indexed:
        await asyncio.to_thread(add_tenant_source, tid, source, indexed)
        await message.answer(
            f"✅ <b>{source}</b> qo'shildi — {indexed} ta post kontekstga indekslandi."
        )
    else:
        await message.answer(
            "⚠️ RAG-servis ishlamayapti — indekslanmadi. Keyinroq urinib ko'ring."
        )
    await dialog_manager.switch_to(SettingsSG.sources)


# --- Окно 9b: выбор источника для квоты ---------------------------------------


async def source_priority_getter(dialog_manager: DialogManager, **kwargs):
    tid = await _tenant_id(dialog_manager)
    sources = await asyncio.to_thread(get_tenant_sources, tid)
    items = [
        (f"{s.source_chat_id} (kvota {s.priority})", str(s.id)) for s in sources
    ]
    return {"sources": items, "has_sources": bool(items)}


async def on_source_priority_selected(
    callback: types.CallbackQuery,
    widget,
    dialog_manager: DialogManager,
    item_id: str,
):
    dialog_manager.dialog_data["priority_src_id"] = item_id
    await dialog_manager.switch_to(SettingsSG.set_source_priority)


# --- Окно 9c: ввод значения квоты ---------------------------------------------


async def on_priority_input(
    message: types.Message, widget, dialog_manager: DialogManager
):
    src_id = dialog_manager.dialog_data.get("priority_src_id")
    if not src_id:
        return await message.answer("Sessiya tugadi. /v2settings bosing.")
    raw = (message.text or "").strip()
    try:
        priority = int(raw)
    except ValueError:
        return await message.answer("⚠️ Butun son yuboring (masalan: 0, 1, 5).")
    if not 0 <= priority <= 100:
        return await message.answer("⚠️ 0–100 oralig'ida son yuboring.")
    await asyncio.to_thread(set_tenant_source_priority, int(src_id), priority)
    await message.answer(f"✅ Kvota saqlandi: {priority}.")
    await dialog_manager.switch_to(SettingsSG.sources)


# --- Окно 10: расписание ------------------------------------------------------


async def schedule_getter(dialog_manager: DialogManager, **kwargs):
    tid = await _tenant_id(dialog_manager)
    profile = await asyncio.to_thread(get_tenant_profile, tid)
    if not profile:
        return {"schedule_text": "⚠️ Sessiya tugadi. /v2settings bosing."}
    return {"schedule_text": render_schedule_text(profile)}


async def on_sched_off(callback, button, dialog_manager: DialogManager):
    tid = await _tenant_id(dialog_manager)
    if not tid:
        return await callback.answer("Sessiya tugadi")
    await asyncio.to_thread(update_tenant_profile, tid, schedule_mode="off")
    await callback.answer("Jadval o'chirildi")


# --- Окно 11: частота постов в день -------------------------------------------


async def on_frequency_input(
    message: types.Message, widget, dialog_manager: DialogManager
):
    tid = await _tenant_id(dialog_manager)
    if not tid:
        return await message.answer("Sessiya tugadi. /v2settings bosing.")
    raw = (message.text or "").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= 10):
        return await message.answer("⚠️ 1–10 oralig'ida son yuboring.")
    await asyncio.to_thread(
        update_tenant_profile, tid, schedule_mode="frequency", posts_per_day=int(raw)
    )
    await message.answer("✅ Jadval saqlandi.")
    await dialog_manager.switch_to(SettingsSG.schedule)


# --- Окно 12: точные времена постов -------------------------------------------


async def on_times_input(
    message: types.Message, widget, dialog_manager: DialogManager
):
    tid = await _tenant_id(dialog_manager)
    if not tid:
        return await message.answer("Sessiya tugadi. /v2settings bosing.")

    # Парсим и валидируем HH:MM, сортируем, убираем дубли.
    parsed = []
    for part in (message.text or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            return await message.answer(
                f"⚠️ Noto'g'ri vaqt: <code>{part}</code>. HH:MM formatida."
            )
        hh, _, mm = part.partition(":")
        if not (hh.isdigit() and mm.isdigit() and 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
            return await message.answer(
                f"⚠️ Noto'g'ri vaqt: <code>{part}</code>. HH:MM (00:00–23:59)."
            )
        parsed.append(f"{int(hh):02d}:{int(mm):02d}")
    if not parsed:
        return await message.answer("⚠️ Kamida bitta vaqt yuboring.")

    times_str = ",".join(sorted(set(parsed)))
    await asyncio.to_thread(
        update_tenant_profile, tid, schedule_mode="times", post_times=times_str
    )
    await message.answer("✅ Jadval saqlandi.")
    await dialog_manager.switch_to(SettingsSG.schedule)


settings_dialog = Dialog(
    Window(
        Const("⚙️ Qaysi kanalni sozlaymiz?"),
        Column(
            Select(
                Format("{item[0]}"),
                id="ch",
                item_id_getter=lambda item: item[1],
                items="channels",
                on_click=on_channel_selected,
            ),
        ),
        Cancel(Const("🔙 Yopish")),
        state=SettingsSG.select,
        getter=select_getter,
    ),
    Window(
        Format("{card}"),
        Button(Const("🔁 Faol/Pauza"), id="toggle", on_click=on_toggle_active),
        Button(Const("🔀 Rejim (topik/repost)"), id="mode", on_click=on_toggle_mode),
        Button(Const("🧠 RAG yoq/o'chir"), id="rag", on_click=on_toggle_rag),
        Button(Const("📡 Manbalar yoq/o'chir"), id="ref", on_click=on_toggle_ref),
        SwitchTo(Const("✏️ Maydonlarni tahrirlash"), id="fields", state=SettingsSG.fields),
        SwitchTo(Const("📏 Qoidalar"), id="rules", state=SettingsSG.rules),
        SwitchTo(Const("📡 Manba kanallar"), id="srcs", state=SettingsSG.sources),
        SwitchTo(Const("🕐 Jadval"), id="sched", state=SettingsSG.schedule),
        SwitchTo(Const("🔙 Kanallar"), id="back_sel", state=SettingsSG.select),
        Cancel(Const("✅ Yopish")),
        state=SettingsSG.card,
        getter=card_getter,
    ),
    Window(
        Const("✏️ Qaysi maydonni tahrirlaymiz?"),
        Column(
            Select(
                Format("{item[0]}"),
                id="fld",
                item_id_getter=lambda item: item[1],
                items="fields",
                on_click=on_field_selected,
            ),
        ),
        Button(Const("✖️ Shablonni tozalash"), id="clr_tmpl", on_click=on_clear_tmpl),
        Button(Const("✖️ CTA'ni tozalash"), id="clr_cta", on_click=on_clear_cta),
        SwitchTo(Const("🔙 Orqaga"), id="back_card", state=SettingsSG.card),
        state=SettingsSG.fields,
        getter=fields_getter,
    ),
    Window(
        Format("✏️ <b>{label}</b> uchun yangi qiymat yuboring{hint}:"),
        MessageInput(on_value_input, content_types=ContentType.TEXT),
        SwitchTo(Const("🔙 Orqaga"), id="back_fields", state=SettingsSG.fields),
        state=SettingsSG.edit_value,
        getter=edit_value_getter,
    ),
    Window(
        Format("{rules_text}"),
        Column(
            Select(
                Format("{item[0]}"),
                id="rule_del",
                item_id_getter=lambda item: item[1],
                items="rules",
                on_click=on_rule_delete,
            ),
        ),
        SwitchTo(Const("➕ Qoida qo'shish"), id="add_rule", state=SettingsSG.rule_type),
        SwitchTo(Const("🔙 Orqaga"), id="rules_back", state=SettingsSG.card),
        state=SettingsSG.rules,
        getter=rules_getter,
    ),
    Window(
        Const("Qoida turini tanlang:"),
        Column(
            Select(
                Format("{item[0]}"),
                id="rt",
                item_id_getter=lambda item: item[1],
                items="rule_types",
                on_click=on_rule_type_selected,
            ),
        ),
        SwitchTo(Const("🔙 Orqaga"), id="rt_back", state=SettingsSG.rules),
        state=SettingsSG.rule_type,
        getter=rule_types_getter,
    ),
    Window(
        Format("✏️ <b>{rule_type}</b> qiymatini yuboring:"),
        MessageInput(on_rule_value_input, content_types=ContentType.TEXT),
        SwitchTo(Const("🔙 Orqaga"), id="arv_back", state=SettingsSG.rule_type),
        state=SettingsSG.add_rule_value,
        getter=add_rule_value_getter,
    ),
    Window(
        Format("{sources_text}"),
        Column(
            Select(
                Format("{item[0]}"),
                id="src_del",
                item_id_getter=lambda item: item[1],
                items="sources",
                on_click=on_source_delete,
            ),
        ),
        SwitchTo(Const("➕ Manba qo'shish"), id="add_src", state=SettingsSG.add_source),
        SwitchTo(Const("🔢 Kvota (tartib)"), id="src_prio", state=SettingsSG.source_priority),
        SwitchTo(Const("🔙 Orqaga"), id="srcs_back", state=SettingsSG.card),
        state=SettingsSG.sources,
        getter=sources_getter,
    ),
    Window(
        Const(
            "📡 Manba kanal userneymini yuboring (masalan: <code>@tech</code>).\n"
            "Uning postlari faqat kontekst (faktlar) uchun indekslanadi."
        ),
        MessageInput(on_source_input, content_types=ContentType.TEXT),
        SwitchTo(Const("🔙 Orqaga"), id="addsrc_back", state=SettingsSG.sources),
        state=SettingsSG.add_source,
    ),
    Window(
        Const(
            "🔢 Qaysi manbaning kvotasini o'zgartiramiz?\n"
            "<i>Kvota katta bo'lgan manbadan yangilik birinchi olinadi.</i>"
        ),
        Column(
            Select(
                Format("{item[0]}"),
                id="src_prio_sel",
                item_id_getter=lambda item: item[1],
                items="sources",
                on_click=on_source_priority_selected,
            ),
        ),
        SwitchTo(Const("🔙 Orqaga"), id="srcprio_back", state=SettingsSG.sources),
        state=SettingsSG.source_priority,
        getter=source_priority_getter,
    ),
    Window(
        Const(
            "🔢 Yangi kvota qiymatini yuboring (<b>0–100</b> oralig'ida butun son).\n"
            "Katta qiymat = yuqori ustuvorlik (yangilik birinchi shu manbadan)."
        ),
        MessageInput(on_priority_input, content_types=ContentType.TEXT),
        SwitchTo(Const("🔙 Orqaga"), id="setprio_back", state=SettingsSG.source_priority),
        state=SettingsSG.set_source_priority,
    ),
    Window(
        Format("{schedule_text}"),
        SwitchTo(Const("🔢 Kuniga N marta"), id="s_freq", state=SettingsSG.sched_frequency),
        SwitchTo(Const("🕐 Maxsus vaqtlar"), id="s_times", state=SettingsSG.sched_times),
        Button(Const("⏸ O'chirish"), id="s_off", on_click=on_sched_off),
        SwitchTo(Const("🔙 Orqaga"), id="sched_back", state=SettingsSG.card),
        state=SettingsSG.schedule,
        getter=schedule_getter,
    ),
    Window(
        Const(
            "🔢 Kuniga necha marta post chiqsin? <b>1–10</b> oralig'ida son yuboring.\n"
            "Postlar 09:00–21:00 oralig'ida teng taqsimlanadi."
        ),
        MessageInput(on_frequency_input, content_types=ContentType.TEXT),
        SwitchTo(Const("🔙 Orqaga"), id="freq_back", state=SettingsSG.schedule),
        state=SettingsSG.sched_frequency,
    ),
    Window(
        Const(
            "🕐 Post vaqtlarini <b>HH:MM</b> formatida, vergul bilan yuboring.\n"
            "Masalan: <code>09:00, 14:30, 20:00</code>"
        ),
        MessageInput(on_times_input, content_types=ContentType.TEXT),
        SwitchTo(Const("🔙 Orqaga"), id="times_back", state=SettingsSG.schedule),
        state=SettingsSG.sched_times,
    ),
)


# Точка входа в диалог настроек: reply-кнопка «⚙️ Sozlamalar» и команда-алиас.
entry_router = Router()


@entry_router.message(Command("v2settings"))
@entry_router.message(F.text == "⚙️ Sozlamalar")
async def open_settings_dialog(message: types.Message, dialog_manager: DialogManager):
    if not await asyncio.to_thread(is_admin, message.from_user):
        return
    await dialog_manager.start(SettingsSG.select, mode=StartMode.RESET_STACK)
