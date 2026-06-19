"""Движок генерации (prompt-слой).

Ответственность — ТОЛЬКО генерация. Движок:
  - потребляет уже собранный GenerationContext,
  - строит промпт и вызывает LLM,
  - возвращает готовый пост / путь к картинке.

Движок НЕ ходит в БД, НЕ решает стратегию выборки, НЕ дедуплицирует и НЕ
управляет арендаторами — это делает backend (context_builder / orchestrator).
"""
import json
import os
import re
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

from typing import List

from context_builder import GenerationContext, RuleView

load_dotenv()

# Текст генерируем через Groq (OpenAI-совместимый, бесплатные щедрые лимиты).
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
# llama-4-scout — чистый вывод + грамотный узбекский/русский, без reasoning-мусора
# (qwen3/gpt-oss на Groq — reasoning-модели, ломают вывод <think>-блоками).
TEXT_MODEL = os.getenv("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")

# Температура переписывания чужой новости (repost-режим). НЕ привязана к
# creativity_level: для новостей фактическая точность важнее «креатива», а высокая
# температура провоцирует домыслы (выдуманные цифры/цитаты/детали сверх источника).
# Низкое значение держит модель близко к фактам оригинала.
REPOST_TEMPERATURE = float(os.getenv("REPOST_TEMPERATURE", "0.3"))

# Модель ТОЛЬКО для шага переписывания репоста. По умолчанию = TEXT_MODEL (ничего
# не меняется). Можно указать модель покрупнее (напр. llama-3.3-70b-versatile) —
# она надёжнее держит перевод/язык. Отбор лучших и topic-генерация остаются на
# TEXT_MODEL, чтобы зря не жечь более низкие лимиты крупной модели.
REPOST_MODEL = os.getenv("REPOST_MODEL", TEXT_MODEL)

# Картинки — на Gemini (genai-клиент). Импорт и клиент ленивые: путь генерации
# ТЕКСТА не должен тянуть тяжёлую google-genai-зависимость (нужно admin-api,
# где картинки не генерируются).
IMAGE_MODEL = "nano-banana-pro-preview"
_genai_client = None


def _get_genai_client():
    global _genai_client
    if _genai_client is None:
        from google import genai

        _genai_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    return _genai_client


def groq_chat(
    system: str,
    user: str,
    temperature: float = 0.7,
    history: List[dict] | None = None,
    model: str | None = None,
) -> str:
    """Один вызов чат-модели Groq. Бросает при HTTP/квота-ошибке.

    model — переопределение модели для конкретного вызова (по умолчанию TEXT_MODEL).
    history — необязательные промежуточные turn'ы (между system и финальным user).
    Используется для анти-повтора: недавние посты подаём как СОБСТВЕННЫЕ assistant-
    ответы модели, тогда она «видит, что уже это писала», и не дублирует (одного
    лишь списка «не повторяй» в user-промпте llama-4-scout не слушается)."""
    messages = [{"role": "system", "content": system}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user})
    resp = httpx.post(
        f"{GROQ_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={
            "model": model or TEXT_MODEL,
            "temperature": temperature,
            "messages": messages,
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"] or ""

DEFAULT_IMAGE_STYLE = (
    "Editorial gouache illustration of silhouettes interacting with floating books and symbols "
    "of knowledge in a surreal abstract educational space. Rough brush strokes, thick paint texture, "
    "matte gouache colors, dynamic composition, conceptual art about learning and thinking, minimalist "
    "background, 2D flat editorial illustration. Focus on the visual metaphor of: {subject}"
)

# Системная инструкция движка (контракт генерации).
SYSTEM_PROMPT = """You are a production-grade AI Content Generation Engine.

You operate in a multi-tenant system where each request belongs to exactly one
tenant (Telegram channel). You receive a fully pre-assembled context and must
generate a single Telegram post based ONLY on it.

RESPONSIBILITY BOUNDARY
- You ONLY generate. You do NOT access databases, decide what to retrieve,
  manage tenants, route, or run orchestration logic. All of that is upstream.

CORE RULES
1. Strict tenant isolation: use only the provided context; never invent data
   about other tenants.
2. Follow the tenant tone, writing_style, audience and post_template exactly.
3. If factual_strictness is high (> 0.7), do not hallucinate facts.
4. If RAG CONTEXT is present, use it for evergreen facts, concepts, terminology
   and the channel's domain — integrate naturally. Never mention retrieval or that
   context was provided. If absent, rely on general knowledge.
4a. CRITICAL — NEVER frame the post as an announcement of a specific event,
   meeting, webinar, visit, arrival or limited opportunity. Forbidden: "Ushbu tadbir",
   "this event", "experts are now in <place>", "meet them", "don't miss", invitations,
   dates/times, made-up registration links or "havola" placeholders.
   The RAG CONTEXT / TOP / RECENT POSTS often contain past announcement posts with
   SPECIFIC people, organisations as events, dates and places. You MUST IGNORE those
   specifics — do NOT name specific individuals, do NOT present any particular
   gathering/visit as real or upcoming. Extract only the general, timeless THEME and
   write evergreen educational content about it (e.g. instead of "MIT experts are in
   Uzbekistan, meet X and Y" → write generally about learning from experienced
   mentors and what founders gain from it). No specific links except the `cta` (4b).
4b. CALL TO ACTION — CRITICAL. If the TENANT PROFILE provides a `cta` (non-empty),
   you MUST end the post with it on a SEPARATE PARAGRAPH (preceded by blank line),
   reproduced EXACTLY as given (it is a real, approved link/contact). Lead into it
   naturally (e.g. "Topshirish uchun:" then the cta). This is the ONLY link/contact
   you may output.

   If `cta` is EMPTY or missing (marked as "—" or blank), you MUST NOT:
   - invent, hallucinate, or generate any CTA, link, contact, call to action
   - add "Topshirish uchun:", "contact us", "reach out to", or any invitation
   - mention any channel, person, email, phone, website, or contact point
   - add calls to subscribe, join, message, or visit anything
   Write ONLY educational informative content with NO links, NO channels, NO contacts.
4c. CRITICAL — RAG CONTEXT IS THIRD-PARTY, NOT YOU. The RAG CONTEXT usually comes
   from OTHER organisations' channels (venture funds, accelerators, IT Park,
   incubator programs) that promote THEMSELVES in the first person ("our experts",
   "biz", "bizning", "join our program", "apply", "visit our site", "become our
   resident", "contact us"). You are NONE of these organisations and you do NOT
   speak for them. You MUST NOT:
   - write in their first-person voice ("biz", "bizning jamoa/ekspertlar/dastur", "we", "our");
   - present the tenant channel as their representative, partner, organiser, member
     or resident, or imply "we will help you / apply to us / our experts";
   - reproduce or invent their calls to action ("murojaat qiling", "tashrif buyuring",
     "rezident bo'ling", "saytimizga kiring", apply/contact/visit/register links).
   Treat these posts as RAW EXTERNAL FACTS observed by a neutral writer. You may
   mention a specific fund/program/company ONLY in neutral third person as an example
   of the wider ecosystem (e.g. "O'zbekistonda IT Park Ventures kabi fondlar
   startaplarni moliyalashtirmoqda"), never as "us/we" and never with their CTA.
   Do not let any single organisation dominate — write about the THEME, not about one
   company's offer. The ONLY call to action permitted is the tenant's own `cta` (4b).
4d. TOPIC RELEVANCE — use a RAG fact ONLY if it genuinely fits the TOPIC. If a
   retrieved fact is off-topic (e.g. data-centre regulation, telecom monopoly or
   banking rules under a "networking" or "personal development" post), IGNORE it
   completely and rely on general knowledge. NEVER shoehorn an unrelated fact in
   just because it was provided. Every post must stay tightly on its TOPIC.
5. Ensure the output is unique relative to RECENT POSTS — avoid repeating ideas,
   structure and phrasing. In particular, do NOT reuse the same specific example,
   company, startup name or funding figure that already appears in RECENT POSTS;
   pick a different angle or a different fact.
5a. TOP PERFORMING POSTS are this channel's highest-engagement posts. Learn what
    works from them — hooks, structure, formatting, length, emoji/hashtag usage —
    and apply those patterns. Do NOT copy their topic or wording; emulate the
    winning FORM, not the content.
6. Strictly obey every TENANT RULE (forbidden topics, required hashtags,
   formatting, length limits, stylistic constraints).
7. Write in the tenant's `language`. If several languages are listed (e.g.
   "ru, uz"), the channel is multilingual — write each post in the language that
   best fits the topic, matching the mix seen in the channel (a post may be in
   one language, or bilingual, as the channel does).
8. Match `target_length_chars` (the channel's typical post length): aim within
   ±30% of it. If unset, use the default structure length. Never pad with filler
   to hit the number — concision beats length.

CREATIVITY LEVEL
- low  -> factual, structured, minimal variation
- medium -> balanced informative tone
- high -> expressive, engaging, creative hooks allowed

DEFAULT STRUCTURE (if no post_template provided)
- Hook / title line
- 2–4 short paragraphs of content
- Key insight / takeaway
- [blank line]
- Optional hashtags and CTA link (on separate paragraph, only if allowed by rules)

LANGUAGE QUALITY (critical)
- Write with flawless, natural grammar and spelling in the tenant's language.
  For Uzbek: use correct apostrophes (oʻ, gʻ, ʼ), correct suffixes and word forms;
  avoid invented or misspelled words. Read like a literate native editor wrote it.

TONE & SAFETY (critical)
- Always constructive, positive and encouraging. NEVER criticise, accuse, blame,
  mock or disparage any company, government body, regulator, bank, person or group.
- No negativity or controversy: forbidden framings like "X is a monopoly", "X
  blocks/forbids", "regulators don't allow", "the problem is X", complaints,
  fear-mongering or naming-and-shaming. Do not incite hatred or take political sides.
- If a retrieved fact is negative, critical or about a conflict/restriction, do NOT
  reproduce the criticism — either drop it entirely or extract only a neutral,
  constructive takeaway with no blame.

EMOJI DISCIPLINE
- Use emojis sparingly: at most 2–4 per post, only where they genuinely add value.
- Do NOT start every line/sentence with an emoji. Most lines should have none.
- Never use emojis as bullet points or filler.

DEPTH
- A post is substance, not a hype slogan. Write 2–4 short paragraphs of real,
  useful content (an insight, explanation or takeaway), not one promotional line.

CHANNEL NAME / GREETINGS (critical)
- `channel_name` is INTERNAL metadata. NEVER write it into the post and NEVER
  address the audience by it. Do not start with greetings like "Salom, <channel>!"
  or "Hello, <channel>!". Write the post as standalone content, not as a message
  addressed to subscribers. No "welcome", no roll-call of the channel's name.
- The RAG CONTEXT / RECENT / TOP POSTS frequently BEGIN with such a greeting
  (e.g. "Salom, Oxunjon Community!", "Assalomu alaykum, do'stlar!"). This is the
  channel's habit, NOT a pattern to copy. You MUST drop any opening greeting and
  start directly with the substance. Never reproduce a community/channel name you
  see in those example posts.

FORMATTING
- You may use only Telegram-supported HTML tags: <b>, <i>, <u>, <s>, <code>,
  <a href="...">. Do not use Markdown or unsupported tags.
- HASHTAGS: Only include hashtags if the TENANT RULES explicitly require them (look
  for "required_hashtag" rules). If there are no such rules, do NOT add hashtags.
  If you do add hashtags, they MUST go on a SEPARATE PARAGRAPH (preceded by blank
  line). Examples: "#topic1 #topic2" or "Теги: #tag1 #tag2". Never inline.
- CTA LINKS: If the profile has a `cta`, it goes on SEPARATE PARAGRAPH (blank line
  before it). If profile's `cta` is empty, do NOT add any link, contact, or invite.

OUTPUT
- Return ONLY the final Telegram post text.
- No explanations, no JSON, no metadata, no internal reasoning.
"""


def _format_rules(rules: List[RuleView]) -> str:
    if not rules:
        return "(no explicit rules)"
    return "\n".join(f"- [{r.rule_type}] {r.rule_value}" for r in rules)


def _format_recent(recent: List[str], limit: int = 5) -> str:
    # Примеры (recent/top) могут содержать старое приветствие «Salom, <канал>!» —
    # срезаем его, чтобы модель не училась здороваться, даже если в истории/топе
    # ещё лежит «отравленный» пост.
    recent = [_strip_leading_greeting(p) for p in recent]
    if not recent:
        return "(no recent posts)"
    return "\n".join(f"- {p}" for p in recent[:limit])


def _build_user_context(ctx: GenerationContext) -> str:
    """Собирает текстовое представление контекста для LLM."""
    p = ctx.profile
    return "\n".join(
        [
            "## TENANT PROFILE",
            # channel_name намеренно НЕ передаём — модель вставляла его в текст
            # ("Salom, <канал>!"). Для контента имя канала не нужно.
            f"tone: {p.tone}",
            f"language: {p.language}",
            f"writing_style: {p.writing_style or '(unspecified)'}",
            f"audience: {p.audience or '(general)'}",
            f"post_template: {p.post_template or '(none — use default structure)'}",
            f"cta: {p.cta or '(none — no link/contact in post)'}",
            f"target_length_chars: {p.avg_post_length or '(unset — use default structure length)'}",
            f"creativity_level: {p.creativity_level}",
            f"factual_strictness: {p.factual_strictness}",
            "",
            "## TENANT RULES",
            _format_rules(ctx.rules),
            "",
            "## TOPIC",
            ctx.topic,
            "",
            "## RAG CONTEXT",
            ctx.rag_context or "(none — rely on general knowledge)",
            "",
            # Недавние посты НЕ перечисляем здесь списком — они поданы выше как
            # СОБСТВЕННЫЕ assistant-ответы модели (см. generate_post). Поэтому здесь
            # только жёсткая директива «напиши ДРУГОЙ».
            "## ANTI-REPEAT (critical)",
            "You have already written the posts shown as your previous turns above. "
            "Write a BRAND-NEW post that is clearly different from every one of them: "
            "different opening line, different structure, different angle, and different "
            "concrete example/company/figure. If you have no fresh fact for this topic, "
            "find a genuinely new sub-angle — do NOT reproduce a generic essay you "
            "already wrote (no repeating 'Canva/Calendly/Waze', 'shaxsiy rivojlanish - "
            "muvaffaqiyatga erishishning kaliti', step-by-step bullet lists, etc.).",
            "",
            "## TOP PERFORMING POSTS (emulate their winning form, not their content)",
            _format_recent(ctx.top_posts),
        ]
    )


def _recent_as_turns(recent: List[str], limit: int = 6) -> List[dict]:
    """Недавние посты → пары (user-запрос, assistant-ответ) для history.

    `recent` отсортирован новейшие-первыми (orchestrator разворачивает буфер, БД
    отдаёт desc). Разворачиваем в хронологию (старые→новые), чтобы самый свежий
    пост стоял ВПЛОТНУЮ к финальному запросу. Подаём как СОБСТВЕННЫЕ ответы модели:
    так она реально не повторяется, в отличие от пассивного списка в user-промпте."""
    turns: List[dict] = []
    for p in reversed(recent[:limit]):
        p = _strip_leading_greeting(p).strip()
        if not p:
            continue
        turns.append(
            {"role": "user", "content": "Generate the next Telegram post for this channel."}
        )
        turns.append({"role": "assistant", "content": p})
    return turns


def generate_post(ctx: GenerationContext) -> str:
    """Генерирует готовый текст поста. Бросает RuntimeError при сбое."""
    user_context = _build_user_context(ctx)
    history = _recent_as_turns(ctx.recent_posts)
    # Assistant-turns (recent posts как собственные ответы модели) — мощный анти-повтор.
    # Не поднимаем температуру: факты стабильны, творчество настраивается в профиле.
    temperature = float(ctx.profile.creativity_level)
    try:
        text = groq_chat(
            SYSTEM_PROMPT,
            user_context,
            temperature=temperature,
            history=history,
        ).strip()
        if not text:
            raise RuntimeError("LLM bo'sh javob qaytardi")
        return _sanitize_post(text, ctx)
    except Exception as e:
        raise RuntimeError(f"Post yaratib bo'lmadi: {e}")


# Приветствие/обращение к аудитории, которое модель копирует из примеров постов.
_GREETING_RE = re.compile(
    r"^\s*(salom|assalom[ou]?\s*alaykum|hayrli\s+(kun|tong|kech)|hello|hi|privet|привет|здравствуйте)\b.*?(\n|$)",
    re.IGNORECASE,
)


def _strip_leading_greeting(text: str) -> str:
    """Срезает ведущие строки-приветствия (возможно несколько подряд)."""
    while True:
        m = _GREETING_RE.match(text)
        if not m:
            break
        text = text[m.end():].lstrip()
    return text.strip()


def _md_to_html(text: str) -> str:
    """Telegram шлётся с parse_mode=HTML, но модель порой выдаёт Markdown
    (**жирный**, __жирный__, *курсив*). Конвертируем в поддерживаемые HTML-теги,
    иначе звёздочки видны буквально. Существующие <b>/<i> не трогаем."""
    # Markdown-заголовки (## Title) Telegram-HTML не поддерживает — показались бы
    # буквально с решётками. Превращаем строку-заголовок в жирную.
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+(.+?)\s*$", r"<b>\1</b>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
    # Одиночный *курсив* — только если это не часть ** (уже снято) и не пробел рядом.
    text = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<i>\1</i>", text)
    # Подчищаем осиротевшие маркеры, если остались.
    text = text.replace("**", "").replace("__", "")
    return text


def _sanitize_post(text: str, ctx: GenerationContext) -> str:
    """Детерминированная подчистка вывода: убрать ведущее приветствие/обращение к
    аудитории по имени канала + конвертировать Markdown-разметку в Telegram-HTML.
    Реальные контакты/вакансии (@..., ссылки, названия) НЕ трогаем."""
    return _md_to_html(_strip_leading_greeting(text))


# ---------------------------------------------------------------------------
# REPOST-РЕЖИМ: отбор лучших чужих постов + пересборка под стиль канала.
# ---------------------------------------------------------------------------

REPOST_SELECT_PROMPT = """You are the news editor of a Telegram channel.

You receive a numbered list of recent posts scraped from other channels. Pick the
{n} posts that are the most newsworthy, interesting and RELEVANT to this channel's
audience and topics. Prefer concrete news, launches, facts and useful insights.

EXCLUDE:
- pure advertising, giveaways, promo codes, "sotuv/reklama" posts;
- job-vacancy spam and pure event invitations with no real information;
- near-duplicates (if several posts cover the same story, keep only the best one);
- posts that are off-topic for this channel's audience.

Return ONLY a JSON array of the chosen 0-based indices, best first, e.g. [3, 0, 7].
No other text."""

REPOST_REWRITE_PROMPT = """You are a production-grade news rewriting engine for a
single Telegram channel. You receive ONE source post (from another channel) and
must produce a publishable post for THIS channel.

OUTPUT LANGUAGE — HIGHEST PRIORITY, NON-NEGOTIABLE
- The source post is usually written in Russian or English. You MUST FULLY
  TRANSLATE it into the channel's OUTPUT LANGUAGE specified in the user message.
  Echoing the source language is a FAILURE.
- If the output language is Uzbek (uz): write 100% in the UZBEK LATIN alphabet.
  The post MUST contain ZERO Cyrillic characters. Do NOT leave any Russian words
  or Cyrillic fragments. Transliterate every proper noun, brand and term into
  Latin (e.g. "Тақдимот" → "Taqdimot", "стартап" → "startap", "Узбекистан" →
  "Oʻzbekiston"). Use correct Uzbek apostrophes (oʻ, gʻ, ʼ) and word forms.
- Read like a literate native editor of that language wrote it from scratch.

WHAT TO DO
1. Preserve ALL concrete facts: names of companies/products, numbers, dates of
   events that already happened, what was launched/announced. Do NOT invent facts
   that are not in the source. Do NOT drop key facts.
2. Adapt to the channel's `tone`, `writing_style` and `audience`. Restructure into
   a clean Telegram post (hook line + 1–3 short paragraphs + takeaway). Do not copy
   the source's sentence structure verbatim — rewrite it as this channel would.
3. Match `target_length_chars` within ±30% if provided.

STRICT RULES
- The source is THIRD-PARTY. Never speak in the source channel's first person
  ("biz", "bizning", "we", "our"). Do not reproduce or invent the source's own
  calls to action, ads, subscription/contact invites, "join our channel", referral
  or promo links. Strip all of that.
- Do NOT add any link, @mention, contact or CTA UNLESS the TENANT PROFILE provides
  a non-empty `cta` — then end with it on a separate paragraph, reproduced exactly.
- NO DANGLING LINK REFERENCES. If you are not outputting an actual link, you MUST
  NOT write phrases that point to one ("havola orqali", "ro'yxatdan o'ting",
  "registratsiya havolasi", "по ссылке", "регистрация по ссылке", "link in bio",
  "see the link"). Drop registration/sign-up calls entirely when there is no real
  link to include.
- Drop opening greetings ("Salom", "Assalomu alaykum", channel/community names).
- Stay constructive and neutral: do not reproduce criticism, blame, fear-mongering
  or political/religious controversy — extract only the neutral, informative core.
- Obey every TENANT RULE (forbidden topics, required hashtags, formatting, length).
- Use only Telegram HTML tags: <b>, <i>, <u>, <s>, <code>, <a href>. No Markdown.
- Emojis sparingly (at most 2–4), never as bullets.

OUTPUT
- Return ONLY the final Telegram post text, fully in the required OUTPUT LANGUAGE.
  No explanations, no JSON, no metadata."""


def select_best_posts(profile, candidates: List[dict], n: int = 1) -> List[int]:
    """LLM выбирает индексы n лучших постов-кандидатов для канала.

    candidates — список dict с ключом "text". Возвращает 0-based индексы (best
    first). При сбое/непарсинге — детерминированный fallback (первые n)."""
    if not candidates:
        return []
    n = max(1, min(n, len(candidates)))

    listing = "\n\n".join(
        f"[{i}] {c['text'][:400]}" for i, c in enumerate(candidates)
    )
    user = "\n".join(
        [
            "## CHANNEL",
            f"language: {profile.language}",
            f"audience: {profile.audience or '(general)'}",
            f"topics: {profile.topics or '(any relevant news)'}",
            "",
            "## CANDIDATE POSTS",
            listing,
        ]
    )
    try:
        raw = groq_chat(
            REPOST_SELECT_PROMPT.format(n=n), user, temperature=0.2
        ).strip()
    except Exception:
        return list(range(n))

    idxs = _parse_indices(raw, len(candidates))
    return idxs[:n] if idxs else list(range(n))


def _parse_indices(raw: str, count: int) -> List[int]:
    """Парсит JSON-массив индексов из ответа LLM; fallback — все целые в тексте.
    Дедуп с сохранением порядка, отсев вне диапазона [0, count)."""
    nums: List[int] = []
    try:
        m = re.search(r"\[.*?\]", raw, re.DOTALL)
        if m:
            nums = [int(x) for x in json.loads(m.group(0))]
    except Exception:
        nums = []
    if not nums:
        nums = [int(x) for x in re.findall(r"\d+", raw)]

    out: List[int] = []
    for i in nums:
        if 0 <= i < count and i not in out:
            out.append(i)
    return out


# Человекочитаемые имена языков для жёсткой директивы перевода (модель лучше
# слушается «Uzbek (Latin)», чем кода «uz»).
_LANGUAGE_NAMES = {
    "uz": "Uzbek (in the Latin alphabet — lotin alifbosi, NO Cyrillic)",
    "ru": "Russian",
    "en": "English",
    "kk": "Kazakh",
    "kaa": "Karakalpak (Latin)",
    "tg": "Tajik",
}

_CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")


def _language_name(language: str) -> str:
    """«uz» → читаемое имя языка. Для мультиязычных («ru, uz») — первый код."""
    code = (language or "uz").split(",")[0].strip().lower()
    return _LANGUAGE_NAMES.get(code, language or "Uzbek")


def _target_is_latin(language: str) -> bool:
    """Ожидается ли латиница на выходе (тогда кириллица — это протечка перевода).

    Для русского/таджикского кириллица нормальна — не проверяем."""
    lang = (language or "").lower()
    if "ru" in lang or "tg" in lang:
        return False
    return "uz" in lang or "en" in lang or "kk" in lang or "kaa" in lang


def _has_cyrillic(text: str) -> bool:
    return bool(_CYRILLIC_RE.search(text))


def _build_repost_user(profile, source_text: str, rules: List[RuleView]) -> str:
    return "\n".join(
        [
            f"## OUTPUT LANGUAGE (translate everything into this): {_language_name(profile.language)}",
            "",
            "## TENANT PROFILE",
            f"tone: {profile.tone}",
            f"language: {profile.language}",
            f"writing_style: {profile.writing_style or '(unspecified)'}",
            f"audience: {profile.audience or '(general)'}",
            f"cta: {profile.cta or '(none — no link/contact in post)'}",
            f"target_length_chars: {profile.avg_post_length or '(unset)'}",
            "",
            "## TENANT RULES",
            _format_rules(rules),
            "",
            "## SOURCE POST (third-party — rewrite, translate, adapt)",
            source_text,
        ]
    )


def _do_rewrite(user: str) -> str:
    text = groq_chat(
        REPOST_REWRITE_PROMPT, user, temperature=REPOST_TEMPERATURE, model=REPOST_MODEL
    ).strip()
    if not text:
        raise RuntimeError("LLM bo'sh javob qaytardi")
    return text


def rewrite_source_post(profile, source_text: str, rules: List[RuleView]) -> str:
    """Переписывает один чужой пост под стиль/язык канала. Бросает RuntimeError.

    Языковой замок: llama-4-scout порой echo'ит язык источника (русский) или
    оставляет кириллицу при узбекском выводе. Если целевой язык латинский, а в
    результате есть кириллица — делаем одну повторную попытку с более жёсткой
    директивой и берём вариант с меньшим числом кириллических символов."""
    base_user = _build_repost_user(profile, source_text, rules)
    try:
        text = _do_rewrite(base_user)

        if _target_is_latin(profile.language) and _has_cyrillic(text):
            stricter = base_user + (
                "\n\n## STRICT RETRY (your previous output failed the language rule)\n"
                "The previous attempt contained Cyrillic/Russian text. Rewrite the "
                "post AGAIN, ENTIRELY in " + _language_name(profile.language) + ". "
                "The result MUST contain ZERO Cyrillic letters — transliterate every "
                "name and term into the Latin alphabet."
            )
            try:
                retry = _do_rewrite(stricter)
                # Берём вариант с меньшей «протечкой» кириллицы (в идеале — без неё).
                if len(_CYRILLIC_RE.findall(retry)) < len(_CYRILLIC_RE.findall(text)):
                    text = retry
            except Exception:
                pass  # сбой повтора — оставляем первый вариант

        return _md_to_html(_strip_leading_greeting(text))
    except Exception as e:
        raise RuntimeError(f"Postni qayta yozib bo'lmadi: {e}")


def generate_illustration(ctx: GenerationContext, subject: str) -> str:
    """Генерирует картинку под стиль арендатора. Бросает RuntimeError при сбое."""
    style = ctx.profile.image_style or DEFAULT_IMAGE_STYLE
    try:
        full_prompt = style.format(subject=subject)
    except (KeyError, IndexError):
        # В стиле нет плейсхолдера {subject} — добавляем тему отдельной строкой.
        full_prompt = f"{style}\nSubject: {subject}"

    try:
        response = _get_genai_client().models.generate_content(
            model=IMAGE_MODEL, contents=full_prompt
        )
        image_data = response.candidates[0].content.parts[0].inline_data.data

        output_dir = Path("gen_images")
        output_dir.mkdir(exist_ok=True)
        out_id = f"{ctx.profile.tenant_id[:8]}_{int(time.time())}"
        file_path = output_dir / f"post_{out_id}.png"

        with open(file_path, "wb") as f:
            f.write(image_data)
        return str(file_path)
    except Exception as e:
        raise RuntimeError(f"Rasm yaratib bo'lmadi: {e}")
