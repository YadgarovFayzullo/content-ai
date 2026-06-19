"""Клавиатуры и рендеринг текстовых карточек."""
import html
from typing import Sequence

from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder

from bot.config import EDIT_FIELDS, RULE_TYPES
from database import TenantProfile, TenantRule


def get_admin_keyboard(is_super: bool = False) -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.button(text="📋 Kanallar ro'yxati")
    builder.button(text="🚀 Hozir post qilish")
    builder.button(text="🎯 Bitta kanalga post")
    builder.button(text="👁 Preview (post qilmasdan)")
    builder.button(text="➕ Kanal qo'shish")
    builder.button(text="🗑 Kanalni o'chirish")
    builder.button(text="⚙️ Sozlamalar")
    builder.button(text="📊 Metrikalarni yig'ish")
    if is_super:
        builder.button(text="👤 Mijoz biriktirish")
    builder.button(text="ℹ️ Yordam")
    # 8 общих кнопок + (опц.) назначение + помощь
    builder.adjust(2, 2, 2, 2, 1, 1) if is_super else builder.adjust(2, 2, 2, 2, 1)
    return builder.as_markup(resize_keyboard=True)


def get_assign_pick_keyboard(tenants: Sequence[TenantProfile]) -> InlineKeyboardMarkup:
    """Выбор канала для назначения клиента (callback asg:<chat_id>)."""
    builder = InlineKeyboardBuilder()
    for t in tenants:
        owner = " 👤" if t.owner_id else ""
        builder.button(text=f"{t.chat_id}{owner}", callback_data=f"asg:{t.chat_id}")
    builder.adjust(1)
    return builder.as_markup()


def get_channels_delete_keyboard(tenants: Sequence[TenantProfile]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for t in tenants:
        builder.button(text=f"❌ {t.chat_id}", callback_data=f"del_{t.chat_id}")
    builder.adjust(1)
    return builder.as_markup()


def get_post_pick_keyboard(tenants: Sequence[TenantProfile]) -> InlineKeyboardMarkup:
    """Выбор одного канала для разовой публикации (callback post:<chat_id>)."""
    builder = InlineKeyboardBuilder()
    for t in tenants:
        mark = "🟢" if t.active else "⏸"
        builder.button(text=f"{mark} {t.chat_id}", callback_data=f"post:{t.chat_id}")
    builder.adjust(1)
    return builder.as_markup()


def get_preview_pick_keyboard(tenants: Sequence[TenantProfile]) -> InlineKeyboardMarkup:
    """Выбор канала для превью-генерации (callback prev:<chat_id>, без публикации)."""
    builder = InlineKeyboardBuilder()
    for t in tenants:
        mark = "🟢" if t.active else "⏸"
        builder.button(text=f"{mark} {t.chat_id}", callback_data=f"prev:{t.chat_id}")
    builder.adjust(1)
    return builder.as_markup()


def get_publish_confirm_keyboard() -> InlineKeyboardMarkup:
    """Подтверждение реальной публикации одного сгенерированного поста."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Ha, joylash", callback_data="pubyes")
    builder.button(text="❌ Bekor", callback_data="pubno")
    builder.adjust(2)
    return builder.as_markup()


def get_postall_confirm_keyboard(count: int) -> InlineKeyboardMarkup:
    """Подтверждение публикации во ВСЕ активные каналы."""
    builder = InlineKeyboardBuilder()
    builder.button(text=f"✅ Ha, {count} kanalga", callback_data="postallyes")
    builder.button(text="❌ Bekor", callback_data="pubno")
    builder.adjust(2)
    return builder.as_markup()


def get_settings_pick_keyboard(tenants: Sequence[TenantProfile]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for t in tenants:
        mark = "🟢" if t.active else "⏸"
        builder.button(text=f"{mark} {t.chat_id}", callback_data=f"pick:{t.chat_id}")
    builder.adjust(1)
    return builder.as_markup()


def get_edit_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for code, (_field, label, _kind) in EDIT_FIELDS.items():
        builder.button(text=label, callback_data=f"set:{code}")
        # Добавляем кнопку очистки для шаблона и CTA
        if code in ("tmpl", "cta"):
            builder.button(text="✖️", callback_data=f"clr:{code}")
    builder.button(text="🔁 Faol/Pauza", callback_data="toggle")
    builder.button(text="🔀 Rejim (topik/repost)", callback_data="modetgl")
    builder.button(text="🧠 RAG yoq/o'chir", callback_data="ragtgl")
    builder.button(text="📡 Manbalar yoq/o'chir", callback_data="reftgl")
    builder.button(text="📏 Qoidalar", callback_data="rules")
    builder.button(text="📡 Manba kanallar", callback_data="srcs")
    builder.button(text="🕐 Jadval", callback_data="sched")
    builder.button(text="🔙 Yopish", callback_data="close")
    builder.adjust(2)
    return builder.as_markup()


def get_rules_keyboard(rules: Sequence[TenantRule]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for r in rules:
        builder.button(text=f"🗑 [{r.rule_type}] {r.rule_value[:20]}", callback_data=f"delr:{r.id}")
    builder.button(text="➕ Qoida qo'shish", callback_data="addr")
    builder.button(text="🔙 Orqaga", callback_data="back")
    builder.adjust(1)
    return builder.as_markup()


def get_rule_type_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for rt in RULE_TYPES:
        builder.button(text=rt, callback_data=f"rt:{rt}")
    builder.button(text="🔙 Orqaga", callback_data="rules")
    builder.adjust(1)
    return builder.as_markup()


def get_sources_keyboard(sources) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for s in sources:
        builder.button(
            text=f"🗑 {s.source_chat_id} ({s.posts_indexed})",
            callback_data=f"dels:{s.id}",
        )
    builder.button(text="➕ Manba qo'shish", callback_data="addsrc")
    builder.button(text="🔙 Orqaga", callback_data="back")
    builder.adjust(1)
    return builder.as_markup()


def render_sources_text(sources) -> str:
    if not sources:
        return (
            "📡 <b>Manba kanallar</b>\n\n"
            "Hozircha yo'q. Stilingizga yaqin community/kanallarni qo'shing — "
            "ularning postlari faqat <b>kontekst</b> (faktlar) uchun RAG'ga "
            "indekslanadi, sizning kanal stilingizni o'zgartirmaydi."
        )
    body = "\n".join(
        f"• {html.escape(s.source_chat_id)} — {s.posts_indexed} ta post" for s in sources
    )
    return "📡 <b>Manba kanallar</b>\n\n" + body


def get_schedule_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔢 Kuniga N marta", callback_data="sched_freq")
    builder.button(text="🕐 Maxsus vaqtlar", callback_data="sched_times")
    builder.button(text="⏸ O'chirish", callback_data="sched_off")
    builder.button(text="🔙 Orqaga", callback_data="back")
    builder.adjust(1)
    return builder.as_markup()


def render_schedule_text(profile: TenantProfile) -> str:
    from bot.scheduler import tenant_post_times

    mode = profile.schedule_mode or "off"
    if mode == "off":
        cur = "⏸ <b>O'chirilgan</b> — avtomatik post chiqmaydi."
    elif mode == "frequency":
        times = ", ".join(tenant_post_times(profile)) or "—"
        cur = f"🔢 Kuniga <b>{profile.posts_per_day}</b> marta\nVaqtlar: <code>{times}</code>"
    else:
        times = ", ".join(tenant_post_times(profile)) or "—"
        cur = f"🕐 Maxsus vaqtlar: <code>{times}</code>"
    return (
        "🕐 <b>Avtomatik jadval</b>\n\n"
        f"Joriy: {cur}\n\n"
        "Vaqt mintaqasi: Asia/Tashkent. Quyidan rejimni tanlang:"
    )


def render_settings_card(profile: TenantProfile, rules_count: int) -> str:
    status = "🟢 faol" if profile.active else "⏸ pauza"
    mode = (profile.content_mode or "topic")
    mode_label = "🔀 Repost (manbalardan)" if mode == "repost" else "✍️ Topik (original)"

    def v(x):
        return html.escape(str(x)) if x not in (None, "") else "—"

    return (
        f"⚙️ <b>{html.escape(profile.chat_id)}</b> ({v(profile.channel_name)})\n"
        f"Holat: {status}\n"
        f"Rejim: {mode_label}\n\n"
        f"Ohang: {v(profile.tone)}\n"
        f"Til: {v(profile.language)}\n"
        f"Uslub: {v(profile.writing_style)}\n"
        f"Auditoriya: {v(profile.audience)}\n"
        f"Mavzular: {v(profile.topics)}\n"
        f"Shablon: {v(profile.post_template)}\n"
        f"Rasm uslubi: {v(profile.image_style)}\n"
        f"Kreativlik: {profile.creativity_level}\n"
        f"Faktik qat'iylik: {profile.factual_strictness}\n"
        f"RAG (faktlar): {'🟢 yoniq' if profile.use_rag else '⚪️ ochiq emas'}\n"
        f"Manba kanallar (refs): {'🟢 yoniq' if profile.use_references else '⚪️ faqat oʻz kanali'}\n"
        f"Jadval: {_schedule_summary(profile)}\n\n"
        f"Qoidalar: {rules_count} ta"
    )


def _schedule_summary(profile: TenantProfile) -> str:
    mode = profile.schedule_mode or "off"
    if mode == "frequency":
        return f"kuniga {profile.posts_per_day} marta"
    if mode == "times":
        return profile.post_times or "—"
    return "⏸ o'chirilgan"


def render_rules_text(rules: Sequence[TenantRule]) -> str:
    if not rules:
        return "📏 <b>Qoidalar</b>\n\nHozircha yo'q."
    body = "\n".join(f"• [{r.rule_type}] {html.escape(r.rule_value)}" for r in rules)
    return "📏 <b>Qoidalar</b>\n\n" + body
