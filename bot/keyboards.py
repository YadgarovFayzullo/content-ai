"""Клавиатуры и рендеринг текстовых карточек."""
import html
from typing import Sequence

from aiogram.types import ReplyKeyboardMarkup
from aiogram.utils.keyboard import ReplyKeyboardBuilder

from database import TenantProfile, TenantRule


def get_admin_keyboard(is_super: bool = False) -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.button(text="📋 Kanallar ro'yxati")
    builder.button(text="🚀 Hozir post qilish")
    builder.button(text="🎯 Bitta kanalga post")
    builder.button(text="➕ Kanal qo'shish")
    builder.button(text="🗑 Kanalni o'chirish")
    builder.button(text="⚙️ Sozlamalar")
    builder.button(text="📊 Metrikalarni yig'ish")
    if is_super:
        builder.button(text="👤 Mijoz biriktirish")
    builder.button(text="ℹ️ Yordam")
    # 7 общих кнопок + (опц.) назначение + помощь
    builder.adjust(2, 2, 2, 1, 1, 1) if is_super else builder.adjust(2, 2, 2, 1, 1)
    return builder.as_markup(resize_keyboard=True)


def render_sources_text(sources) -> str:
    if not sources:
        return (
            "📡 <b>Manba kanallar</b>\n\n"
            "Hozircha yo'q. Stilingizga yaqin community/kanallarni qo'shing — "
            "ularning postlari faqat <b>kontekst</b> (faktlar) uchun RAG'ga "
            "indekslanadi, sizning kanal stilingizni o'zgartirmaydi."
        )
    body = "\n".join(
        f"• {html.escape(s.source_chat_id)} — {s.posts_indexed} ta post"
        f" · kvota {s.priority}"
        for s in sources
    )
    return (
        "📡 <b>Manba kanallar</b>\n\n" + body
        + "\n\n<i>Kvota qancha katta bo'lsa, yangilik o'sha manbadan birinchi "
        "olinadi (repost rejimi).</i>"
    )


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
