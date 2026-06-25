"""Движок генерации (prompt-слой).

Ответственность — ТОЛЬКО генерация. Движок:
  - потребляет уже собранный GenerationContext,
  - строит промпт и вызывает LLM,
  - возвращает готовый пост / путь к картинке.

Движок НЕ ходит в БД, НЕ решает стратегию выборки, НЕ дедуплицирует и НЕ
управляет арендаторами — это делает backend (context_builder / orchestrator).
"""
import json
import logging
import os
import re
import time
from collections import Counter
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
REPOST_TEMPERATURE = float(os.getenv("REPOST_TEMPERATURE", "0.2"))

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


def suggest_topics(
    *,
    post_template: str = "",
    writing_style: str = "",
    audience: str = "",
    tone: str = "",
    language: str = "",
    content_mode: str = "topic",
    count: int = 12,
) -> List[str]:
    """Предлагает список тем (topics) для канала на основе того, что владелец уже
    задал: шаблон поста, стиль, аудитория, тон. Решает ловушку «пустые темы +
    тематический шаблон» — ИИ подбирает конкретные темы под рубрику.

    Если шаблон содержит плейсхолдер (<country>/<destination>/<product>...), ИИ
    предлагает конкретные значения для него. Возвращает до `count` коротких тем.
    """
    cfg = "\n".join(
        [
            f"post_template: {post_template or '(none)'}",
            f"writing_style: {writing_style or '(none)'}",
            f"audience: {audience or '(none)'}",
            f"tone: {tone or '(none)'}",
            f"language: {language or '(infer from the template/style)'}",
            f"content_mode: {content_mode}",
        ]
    )
    system = (
        "You suggest concrete POST TOPICS for a Telegram channel from its content "
        "configuration. Each topic is a short phrase (1-4 words) the channel can "
        "write a separate post about. If the post_template has a placeholder such "
        "as <country>, <destination>, <product>, <city>, suggest concrete fillers "
        "for it (e.g. real countries). Topics must be in the channel's own language "
        f"(match the template/style). Return EXACTLY {count} topics as a single "
        "comma-separated line. No numbering, no quotes, no commentary, no trailing dot."
    )
    user = f"CONTENT CONFIGURATION:\n{cfg}\n\nReturn {count} comma-separated topics."
    raw = groq_chat(system, user, temperature=0.7)

    # Парсим устойчиво: режем по запятым/переводам строк, чистим нумерацию/кавычки/буллеты.
    out: List[str] = []
    seen = set()
    for part in re.split(r"[,\n]", raw):
        t = re.sub(r"^\s*\d+[\.\)]\s*", "", part).strip().strip("\"'`•-–*").strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:count]


def canonical_topics(topics: List[str]) -> dict:
    """Сводит темы к язык-независимому каноническому ключу для дедупа ротации.

    Для каждого входа возвращает короткий lowercase-ASCII идентификатор основного
    сюжета: разные язык/написание/обёртки одного и того же сводятся к одному ключу
    («дубай», «Dubai», «discover дубай» → "dubai"; «испания», «Spain» → "spain").

    Best-effort: при отсутствии ключа Groq или ошибке возвращает {} — вызывающий
    откатывается на строковую нормализацию (тогда дедуп работает как раньше, в
    пределах одного языка). Один батч-вызов на все темы (не по одной)."""
    items = [t for t in (topics or []) if t and t.strip()]
    if not items or not GROQ_API_KEY:
        return {}
    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(items))
    system = (
        "You map content topics to a language-agnostic canonical key for "
        "deduplication. For each numbered topic output a short lowercase ASCII "
        "key identifying its core subject: translate non-English to English, "
        "transliterate proper nouns to their common English spelling, drop filler "
        "words (discover, explore, visit, top-5, guide to). Same real-world subject "
        "in any language/spelling MUST get the identical key (e.g. 'Дубай' and "
        "'Dubai' -> dubai; 'Испания' and 'Spain' -> spain). Return ONLY a JSON "
        "object mapping the input number (as string) to its key. No commentary."
    )
    user = f"TOPICS:\n{numbered}\n\nReturn a JSON object like {{\"1\": \"dubai\"}}."
    try:
        raw = groq_chat(system, user, temperature=0.0)
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
        start, end = text.find("{"), text.rfind("}")
        data = json.loads(text[start : end + 1]) if start != -1 and end != -1 else {}
    except Exception as e:
        logging.warning("canonical_topics xatosi: %s", e)
        return {}

    out: dict = {}
    for i, topic in enumerate(items):
        key = data.get(str(i + 1))
        if isinstance(key, str) and key.strip():
            out[topic] = re.sub(r"\s+", " ", key.strip().lower())
    return out


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

LIST / BULLET VARIETY (critical)
- When the post has a numbered or bulleted list, EVERY item MUST open differently.
  Do NOT begin items with the same word, the same grammatical pattern, or the
  topic/place name. FORBIDDEN: every bullet starting with "<Place> — город, где..."
  / "<Place> — город, который..." / "<Place> is a city where..." / "<Place> has...".
- Do NOT use the post's topic/subject name inside list items AT ALL — neither at
  the start nor in the middle. The subject is already in the title/intro, so inside
  a bullet it is redundant. FORBIDDEN in every item: "Новая Зеландия — ...",
  "В Новой Зеландии ...", "Дубай имеет ...". Write the bullet's content directly
  ("Фьорды тянутся на километры...", "Население — около 5 млн...") without naming
  the country/subject again.
- The label before a colon must NOT be echoed right after it. FORBIDDEN:
  "Пляжи Дубая: Дубай имеет множество пляжей...", "Тайская кухня: Бангкок — город,
  где можно насладиться тайской кухней...". Instead continue with NEW information:
  "Пляжи Дубая: золотистый песок Джумейры тянется на километры вдоль залива."
- Vary the opening of each item: start one with a vivid fact or number, one with a
  verb, one with a concrete noun, one with an adjective. Across the whole post the
  item openings must be visibly different from one another.
- BANNED superlative cliché: do NOT describe items with the empty formula "<X> —
  один из самых известных/крупнейших/старейших…", "one of the most famous/largest/
  oldest…", "...laridan biri". It is vague filler that repeats across items. Lead
  with a CONCRETE specific instead — a number, a date, a height/size, the name of a
  detail, or a vivid fact (e.g. not "Эйфелева башня — одна из самых известных
  достопримечательностей", but "Эйфелева башня: 324 метра стали, построена за 2 года
  к выставке 1889-го").

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
            # Жёсткий язык-лок: модель оставляла имена собственные на исходном языке
            # (TOPIC="Sydney" → заголовок "Discover Sydney" при русском контенте).
            "## LANGUAGE LOCK (critical)",
            f"Write the ENTIRE post strictly in {p.language}. This includes ALL proper "
            f"nouns — place, city, country and brand names from the TOPIC and facts MUST "
            f"be transliterated/localized into {p.language} (e.g. for Russian: "
            f"'Sydney'→'Сидней', 'Barcelona'→'Барселона', 'New York'→'Нью-Йорк'). "
            f"Do NOT leave any word in English or another language (no 'iconic', no raw "
            f"Latin names). The TOPIC may be given in English — localize it before use.",
            "",
            # Лимит подписи к фото в Telegram — 1024 символа. Если пост уйдёт С
            # КАРТИНКОЙ (ctx.with_image), держим его заведомо короче (800), чтобы фото
            # и текст ушли одним сообщением и текст не обрезался. БЕЗ картинки этого
            # лимита нет — обычный пост до дефолтных 4096 Telegram.
            *(
                [
                    "## MESSAGE FORMAT (hard limit)",
                    "This post is sent as a PHOTO with the text as its CAPTION — a "
                    "SINGLE message. The WHOLE post (including any HTML tags, emoji "
                    "and hashtags) MUST be UNDER 800 characters. This is a HARD limit "
                    "that overrides any longer target length — be concise, keep facts "
                    "short.",
                    "",
                ]
                if getattr(ctx, "with_image", True)
                else []
            ),
            "## STYLE TOGGLES (override the general EMOJI/HASHTAG rules above)",
            (
                "EMOJI: ENABLED — actively use relevant emoji to improve readability: a "
                "fitting emoji in the title and at the start of MOST list items/sections "
                "(aim for ~5-8 per post). Keep them relevant, don't spam."
                if getattr(p, "use_emoji", True)
                else "EMOJI: DISABLED — do NOT use any emoji anywhere in the post."
            ),
            (
                "HASHTAGS: ENABLED — end the post with 2-4 relevant hashtags on a "
                f"separate last paragraph (preceded by a blank line), written in {p.language}."
                if getattr(p, "use_hashtags", False)
                else "HASHTAGS: DISABLED — do NOT add hashtags (unless a TENANT RULE "
                "explicitly requires a specific one)."
            ),
            "",
            "## TENANT RULES",
            _format_rules(ctx.rules),
            "",
            "## TOPIC",
            ctx.topic,
            "",
            # Дедуп перед генерацией: посты тенанта по ТОЙ ЖЕ теме, уже опубликованные.
            # Сильнее общего ANTI-REPEAT — это конкретно «ты уже писал ПРО ЭТО».
            (
                "## ALREADY PUBLISHED ON THIS SUBJECT (critical — do NOT repeat)\n"
                "The channel has ALREADY published the post(s) below on essentially this "
                "same subject. Do NOT restate their facts, structure, examples or wording. "
                "Take a clearly NEW angle / sub-topic, or add genuinely fresh information "
                "not present below:\n"
                + _format_recent(ctx.already_published, limit=3)
                + "\n"
                if getattr(ctx, "already_published", None)
                else ""
            ),
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


# Пункт списка: «1. …», «- …», «• …». Захватываем содержимое пункта.
_LIST_ITEM_RE = re.compile(r"(?m)^\s*(?:\d+[.)]|[-*•·])\s+(.+)$")
_WORD_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁёЎўҚқҒғҲҳ’ʼ']+")


def _item_openings(item: str) -> tuple[str, str]:
    """Сигнатуры «как начинается» пункт: (первое слово, первые два слова) ОПИСАНИЯ
    (после метки-двоеточия), без эмодзи/HTML/пунктуации, в нижнем регистре.
    «🛍️ <b>Рынки</b>: Дубай - город…» → ("дубай", "дубай город"). Два слова ловят
    шаблон, где первое слово разное, а конструкция одна («…страна, где можно найти»)."""
    s = re.sub(r"<[^>]+>", "", item)          # убрать HTML-теги
    if ":" in s:                              # отбросить метку «Рынки:» перед двоеточием
        s = s.split(":", 1)[1]
    words = [w.lower() for w in _WORD_RE.findall(s)]
    if not words:
        return "", ""
    one = words[0]
    two = " ".join(words[:2]) if len(words) >= 2 else one
    return one, two


# Превосходное клише «X — один из самых известных/крупнейших/старейших…»,
# «one of the most …», узб. «…laridan biri». Слабая модель ставит эту пустую
# конструкцию в каждый пункт, причём ПЕРВЫЕ слова разные, поэтому _item_openings
# её не ловит — детектим по самой фразе отдельно.
_CLICHE_RE = re.compile(
    r"\b(?:один|одна|одно)\s+из\b"   # ru: «один из самых/крупнейших/лучших…»
    r"|\bone\s+of\s+the\b"           # en: «one of the most/largest/oldest…»
    r"|lar(?:i)?dan\s+biri\b",       # uz: «…laridan biri»
    re.IGNORECASE,
)


def _cliche_count(items: List[str]) -> int:
    """Сколько пунктов используют превосходное клише «один из самых…» / «one of the…».
    ≥2 — болезнь шаблонного списка, которую не видят зачины пунктов."""
    n = 0
    for it in items:
        if _CLICHE_RE.search(re.sub(r"<[^>]+>", "", it)):
            n += 1
    return n


def _repetition_score(text: str) -> int:
    """Насколько шаблонны пункты: максимум среди «сколько пунктов делят одно первое
    слово / одни первые два слова» и «сколько пунктов используют клише „один из…“».
    ≥2 = есть повтор. 0, если пунктов мало (<3) — не дёргаем ретрай зря."""
    items = _LIST_ITEM_RE.findall(text)
    if len(items) < 3:
        return 0
    cliche = _cliche_count(items)
    ones, twos = [], []
    for it in items:
        o, t = _item_openings(it)
        if o:
            ones.append(o)
        if t:
            twos.append(t)
    if len(ones) < 3:
        return cliche
    best = Counter(ones).most_common(1)[0][1]
    if twos:
        best = max(best, Counter(twos).most_common(1)[0][1])
    return max(best, cliche)


def _has_repetitive_openings(text: str) -> bool:
    """True, если ≥2 пунктов списка начинаются одинаково (по первому слову ИЛИ по
    первым двум словам) — болезнь слабой модели: «<Город> — город, где…» в каждом
    пункте. Срабатывает только при ≥3 пунктах."""
    return _repetition_score(text) >= 2


# Предлоги, которые могут стоять перед названием темы в начале пункта
# («В Новой Зеландии…», «На Гавайях…») — пропускаем их при срезе.
_PREPOS = {"в", "во", "на", "из", "о", "об", "у", "к", "с", "со"}


def _subject_stems(subject: str) -> list[str]:
    """Стемы значимых слов темы для матча склонённых форм («Новая Зеландия» →
    ['нов','зелан', сматчит 'Новой Зеландии', 'Новая Зеландия', 'Новозеландский').
    Грубое усечение хвоста (len-3, минимум 3 буквы) — морфоанализатора нет, но для
    отсечения зачина-названия этого достаточно."""
    return [
        w[: max(3, len(w) - 3)].lower()
        for w in _WORD_RE.findall(subject or "")
        if len(w) >= 4
    ]


def _strip_lead_subject(desc: str, stems: list[str]) -> tuple[str, bool]:
    """Срезает в начале описания зачин-название темы: [предлог] + подряд идущие
    слова-формы темы (по стему) + хвостовой разделитель, и поднимает заглавную.
    «В Новой Зеландии находится X» → «Находится X»; «Дубай - город…» → «Город…».
    Возвращает (новый_текст, изменилось)."""
    toks = list(_WORD_RE.finditer(desc))
    if not toks:
        return desc, False
    i = 1 if toks[0].group(0).lower() in _PREPOS else 0
    j = i
    while j < len(toks) and any(toks[j].group(0).lower().startswith(s) for s in stems):
        j += 1
    if j == i:  # ни одного слова темы в зачине — не трогаем
        return desc, False
    rest = re.sub(r"^\s*[-–—:,]?\s*", "", desc[toks[j - 1].end():])
    m = re.search(r"[A-Za-zА-Яа-яЁё]", rest)
    if m:
        k = m.start()
        rest = rest[:k] + rest[k].upper() + rest[k + 1:]
    return rest, True


def _repeated_opening_stems(text: str) -> set[str]:
    """Стемы слов, которыми РЕАЛЬНО начинаются (после необязат. предлога) ≥2 описаний
    пунктов. Ловит повтор имени, даже если модель сузила тему (город вместо страны):
    ctx.topic='Новая Зеландия', а пункты начинаются с 'Окленд' — здесь добавится
    стем 'окл', и зачин срежется."""
    firsts = []
    for it in _LIST_ITEM_RE.findall(text):
        s = re.sub(r"<[^>]+>", "", it)
        if ": " in s:
            s = s.split(": ", 1)[1]
        toks = _WORD_RE.findall(s)
        if toks and toks[0].lower() in _PREPOS and len(toks) > 1:
            toks = toks[1:]
        if toks and len(toks[0]) >= 4:
            firsts.append(toks[0][: max(3, len(toks[0]) - 3)].lower())
    return {s for s, c in Counter(firsts).items() if c >= 2}


def _strip_repeated_subject(text: str, subject: str) -> str:
    """Детерминированно убирает повторяющийся зачин-название в начале ОПИСАНИЙ
    пунктов. Слабая модель (любая) под шаблоном «<Метка>: <описание>» упорно
    начинает описание с названия темы; промпт/ретраи это не лечат до конца. Срезаем
    только когда имя стоит в начале ≥2 пунктов (иначе это не повтор). Имя берём и из
    ctx.topic, и эмпирически из самого текста (на случай сужения темы моделью)."""
    stems = set(_subject_stems(subject)) | _repeated_opening_stems(text)
    if not stems:
        return text
    lines = text.split("\n")
    cand = []
    for idx, line in enumerate(lines):
        m = _LIST_ITEM_RE.match(line)
        if not m:
            continue
        body = m.group(1)
        label, desc = body.split(": ", 1) if ": " in body else ("", body)
        new_desc, changed = _strip_lead_subject(desc, stems)
        if changed:
            cand.append((idx, line, body, label, new_desc))
    if len(cand) < 2:
        return text
    for idx, line, body, label, new_desc in cand:
        new_body = f"{label}: {new_desc}" if label else new_desc
        prefix = line[: line.index(body)] if body in line else ""
        lines[idx] = prefix + new_body
    return "\n".join(lines)


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
        post = _sanitize_post(text, ctx)

        # Анти-шаблон: если пункты начинаются одинаково — корректирующие
        # перегенерации. Мягкое правило в SYSTEM_PROMPT тонет в большом промпте, и
        # слабая модель его игнорит; здесь директиву ставим В НАЧАЛО user-промпта
        # (самое заметное место), повышаем температуру и пробуем до 3 раз. Берём
        # наименее повторяющийся вариант, даже если идеала нет — никогда не вернём
        # хуже исходного.
        if _has_repetitive_openings(post):
            directive = (
                "## LIST VARIETY — HARD REQUIREMENT (read this FIRST)\n"
                "A previous attempt FAILED this rule: multiple list items began the "
                "SAME way — repeating the topic/place name ('<Place> - страна, где...') "
                "or the same construction in every item. Rewrite the WHOLE post so that "
                "EVERY list item starts with a DIFFERENT first word AND a different "
                "structure: make one open with a number/fact, one with a verb, one with "
                "a concrete noun, one with an adjective. NEVER restate the subject's "
                "name at the start of an item, and NEVER echo the label before the "
                "colon right after it.\n"
                "ALSO BANNED: the superlative cliché in items — 'X — один из самых "
                "известных/крупнейших/старейших…', 'one of the most famous/largest/"
                "oldest…'. It is empty filler. Lead each item with a CONCRETE specific: "
                "a number, a date, a height/size, a name of a detail, or a vivid fact.\n\n"
            )
            best_post, best_score = post, _repetition_score(post)
            for attempt in range(3):
                try:
                    retry = groq_chat(
                        SYSTEM_PROMPT,
                        directive + user_context,
                        temperature=min(1.0, temperature + 0.15 * (attempt + 1)),
                        history=history,
                    ).strip()
                except Exception:
                    break  # сбой ретрая — оставляем лучшее найденное, пост важнее
                retry_post = _sanitize_post(retry, ctx) if retry else ""
                if not retry_post:
                    continue
                score = _repetition_score(retry_post)
                if score < best_score:
                    best_post, best_score = retry_post, score
                if score < 2:  # правило выполнено — дальше не пробуем
                    break
            post = best_post

        # Финальная гарантия (всегда): если имя темы ведёт ≥2 пункта — срезаем его
        # программно, модель-независимо. Вне ретрай-блока: чистим зачины-имена даже
        # когда первые СЛОВА формально разные ('Новую…' / 'В Новой…') и score<2.
        post = _strip_repeated_subject(post, ctx.topic)

        return post
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
    out = _md_to_html(_strip_leading_greeting(text))
    # Схлопнуть лишние пустые строки: правила «хэштеги/CTA отдельным абзацем»
    # заставляют модель плодить по 2-3 пустых строки подряд — в Telegram это
    # выглядит как большие «пустоты». Оставляем максимум одну пустую строку.
    out = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", out)
    return out.strip()


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
1a. NUMBERS & NAMED ENTITIES — ZERO FABRICATION. This is the most important rule.
   - Use ONLY the company names, person names, fund names, amounts, percentages and
     dates that appear in the source. Never introduce a name or figure the source
     does not contain.
   - KEEP every real detail the source DOES give: if the source lists a per-investor
     breakdown of a round, reproduce it exactly. Only forbidden is INVENTING a
     breakdown the source never stated — do not split a total into made-up parts.
   - If a detail (investor, amount, date, participant) is not in the source, simply
     omit it. Vague-but-true beats specific-but-invented.
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


# В repost-режиме стиль ВСЕГДА сухой и нейтральный (как у новостного канала),
# независимо от декоративных полей профиля (tone/writing_style/audience). Репост —
# это переписанная чужая новость: важны факты, а не «энтузиазм»/эмодзи/призывы,
# которые профиль мог заказать для topic-режима. Дано как явные строки профиля,
# чтобы движок не подмешивал украшательства и CTA из настроек канала.
def _repost_profile_lines(profile) -> List[str]:
    return [
        "## TENANT PROFILE",
        "tone: neutral, factual (news wire style)",
        f"language: {profile.language}",
        "writing_style: dry and informative — plain sentences, no hype, minimal "
        "emojis, NO calls to action",
        "audience: (general)",
        f"cta: {profile.cta or '(none — no link/contact in post)'}",
        f"target_length_chars: {profile.avg_post_length or '(unset)'}",
    ]


def _build_repost_user(profile, source_text: str, rules: List[RuleView]) -> str:
    return "\n".join(
        [
            f"## OUTPUT LANGUAGE (translate everything into this): {_language_name(profile.language)}",
            "",
            *_repost_profile_lines(profile),
            "",
            "## TENANT RULES",
            _format_rules(rules),
            "",
            "## SOURCE POST (third-party — rewrite, translate, adapt)",
            source_text,
        ]
    )


def _do_llm(system: str, user: str) -> str:
    text = groq_chat(
        system, user, temperature=REPOST_TEMPERATURE, model=REPOST_MODEL
    ).strip()
    if not text:
        raise RuntimeError("LLM bo'sh javob qaytardi")
    return text


def _generate_with_lang_lock(system: str, base_user: str, language: str) -> str:
    """Вызов LLM + языковой замок. llama-4-scout порой echo'ит язык источника
    (русский) или оставляет кириллицу при узбекском выводе. Если целевой язык
    латинский, а в результате есть кириллица — одна повторная попытка с более
    жёсткой директивой; берём вариант с меньшей «протечкой». Возвращает HTML."""
    text = _do_llm(system, base_user)
    if _target_is_latin(language) and _has_cyrillic(text):
        stricter = base_user + (
            "\n\n## STRICT RETRY (your previous output failed the language rule)\n"
            "The previous attempt contained Cyrillic/Russian text. Rewrite AGAIN, "
            "ENTIRELY in " + _language_name(language) + ". The result MUST contain "
            "ZERO Cyrillic letters — transliterate every name and term into Latin."
        )
        try:
            retry = _do_llm(system, stricter)
            if len(_CYRILLIC_RE.findall(retry)) < len(_CYRILLIC_RE.findall(text)):
                text = retry
        except Exception:
            pass  # сбой повтора — оставляем первый вариант
    return _md_to_html(_strip_leading_greeting(text))


def rewrite_source_post(profile, source_text: str, rules: List[RuleView]) -> str:
    """Переписывает ОДИН чужой пост под стиль/язык канала. Бросает RuntimeError."""
    base_user = _build_repost_user(profile, source_text, rules)
    try:
        return _generate_with_lang_lock(REPOST_REWRITE_PROMPT, base_user, profile.language)
    except Exception as e:
        raise RuntimeError(f"Postni qayta yozib bo'lmadi: {e}")


# Канонизация (V2): несколько сообщений об ОДНОМ событии → один богатый пост.
CANONICALIZE_PROMPT = """You are a production-grade news rewriting engine for a
single Telegram channel. You receive SEVERAL source posts (from different channels)
that all report the SAME real-world event. Produce ONE publishable post for THIS
channel that MERGES them.

OUTPUT LANGUAGE — HIGHEST PRIORITY, NON-NEGOTIABLE
- The sources are usually in Russian or English. You MUST FULLY TRANSLATE into the
  channel's OUTPUT LANGUAGE specified in the user message. Echoing the source
  language is a FAILURE.
- If the output language is Uzbek (uz): write 100% in the UZBEK LATIN alphabet.
  ZERO Cyrillic characters. Transliterate every proper noun and term into Latin
  ("Тақдимот" → "Taqdimot", "стартап" → "startap"). Correct Uzbek apostrophes
  (oʻ, gʻ, ʼ).

MERGING (the core task)
- These posts describe the SAME event. Build a single coherent story.
- COMBINE complementary facts: if one source gives the amount and another the
  investors or the date, include all of them — once.
- DO NOT repeat the same fact multiple times. Deduplicate overlapping statements.
- If sources CONFLICT on a number/name, prefer the more specific and the one
  supported by more sources; never average or invent a compromise figure.
- Preserve ALL concrete facts (companies, products, numbers, dates of past events).
  Do NOT invent anything absent from every source. Do NOT add facts of your own.
- NUMBERS & NAMED ENTITIES — ZERO FABRICATION: use only the names/amounts/percentages/
  dates that appear in at least one source. KEEP every real detail the sources give —
  if any source lists a per-investor breakdown, reproduce it exactly. Only forbidden
  is INVENTING data no source states (a made-up investor, figure, or a fabricated
  split of a total). Omit unknown details rather than inventing them.
- The result is ONE post about ONE event — never a list of separate news items.

STYLE & RULES
- Adapt to the channel's tone, writing_style and audience. Clean structure: hook
  line + 1–3 short paragraphs + takeaway. Match target_length_chars (±30%).
- Third-party sources: never speak in their first person ("biz", "we", "our");
  strip their ads, subscription/contact invites, referral/promo links.
- Do NOT add any link/@mention/CTA unless TENANT PROFILE has a non-empty `cta`
  (then end with it on a separate paragraph, verbatim). NO dangling link references
  ("havola orqali", "по ссылке", "register via the link") when there is no real link.
- Drop greetings and channel/community names. Stay constructive and neutral.
- Obey every TENANT RULE. Telegram HTML only (<b>,<i>,<u>,<s>,<code>,<a href>),
  no Markdown. Emojis sparingly (≤4), never as bullets.

OUTPUT
- Return ONLY the final Telegram post text, fully in the required OUTPUT LANGUAGE.
  No explanations, no JSON, no metadata."""


def _build_canon_user(profile, member_texts: List[str], rules: List[RuleView]) -> str:
    reports = "\n\n".join(
        f"--- SOURCE {i + 1} ---\n{t}" for i, t in enumerate(member_texts)
    )
    return "\n".join(
        [
            f"## OUTPUT LANGUAGE (translate everything into this): {_language_name(profile.language)}",
            "",
            *_repost_profile_lines(profile),
            "",
            "## TENANT RULES",
            _format_rules(rules),
            "",
            "## SOURCE REPORTS (multiple posts about the SAME event — merge into one)",
            reports,
        ]
    )


def canonicalize_cluster(
    profile, member_texts: List[str], rules: List[RuleView]
) -> str:
    """Сводит несколько сообщений об одном событии в один пост. Для кластера из
    одного поста дешевле и безопаснее обычный rewrite. Бросает RuntimeError."""
    members = [t for t in member_texts if t and t.strip()]
    if len(members) <= 1:
        return rewrite_source_post(profile, members[0] if members else "", rules)
    base_user = _build_canon_user(profile, members, rules)
    try:
        return _generate_with_lang_lock(CANONICALIZE_PROMPT, base_user, profile.language)
    except Exception as e:
        raise RuntimeError(f"Klasterni birlashtirib bo'lmadi: {e}")


IMAGE_SUBJECT_PROMPT = (
    "You turn a news post into a SHORT visual subject for an illustration prompt.\n"
    "Reply with ONLY one concise English noun phrase (5-12 words) capturing the core "
    "of the news as a VISUAL METAPHOR an artist can draw: the main actor + what "
    "happens. Output the phrase ALONE.\n"
    "Strict format:\n"
    "- Output the phrase and NOTHING else. No preamble, no 'Here is', no explanation, "
    "no label, no colon, no quotes, no trailing punctuation.\n"
    "- English only, regardless of the source language.\n"
    "- Keep concrete, drawable entities (company/product, money, growth, product type).\n"
    "- Include a key number only if it is the point (e.g. a funding amount).\n"
    "- Do NOT invent facts; use only what the post states.\n"
    "Example output: proptech startup raising 150k dollars in venture funding"
)

# Преамбулы, которые llama иногда добавляет перед ответом, несмотря на инструкцию.
_SUBJECT_PREAMBLE = re.compile(
    r"^(here('?s| is)|sure|output|phrase|subject|the phrase)\b.*?:?\s*$",
    re.IGNORECASE,
)


def image_subject(text: str) -> str:
    """Короткий английский смысловой subject для картинки (визуальная метафора сути
    новости), вместо дословно обрезанной первой строки. При сбое LLM — фолбэк на
    первую строку, обрезанную по границе слова."""
    plain = re.sub(r"<[^>]+>", " ", text).strip()
    if not plain:
        return "startup news"
    try:
        raw = groq_chat(IMAGE_SUBJECT_PROMPT, plain[:1500], temperature=0.2)
        # Берём последнюю содержательную строку, отбросив строки-преамбулы
        # ("Here is ...:") — реальная фраза обычно идёт после них.
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        lines = [ln for ln in lines if not _SUBJECT_PREAMBLE.match(ln)]
        if lines:
            out = lines[-1].strip().strip('"\'').rstrip(".").strip()
            if out and len(out) <= 160:
                return out
    except Exception as e:
        logging.warning(f"image_subject LLM xatosi: {e}")
    return _subject_fallback(plain)


TOPIC_STOCK_QUERY_PROMPT = (
    "You build a SHORT English stock-photo search query for a Telegram post about a "
    "given TOPIC. The photo must be a WIDE, GENERAL ESTABLISHING shot of the place or "
    "subject — a recognizable overview: city skyline, cityscape, famous landmark, "
    "panorama or aerial view. STRICTLY AVOID staged/abstract/lifestyle scenes "
    "(no person at a window, no hands, no close-ups, no models, no interiors).\n"
    "Rules:\n"
    "- Output English only (translate the topic if needed).\n"
    "- For a city/country: '<Place> city skyline cityscape landmark'.\n"
    "- 2-6 words, no quotes, no punctuation, no explanation — the query ALONE.\n"
    "Examples: 'Париж' -> 'Paris city skyline Eiffel Tower'; "
    "'Tokyo' -> 'Tokyo cityscape skyline'; 'Bali' -> 'Bali landscape aerial view'."
)


def image_subject_for_topic(topic: str) -> str:
    """Англоязычный запрос к стоку под ТЕМУ поста — общий вид места/достопримечательности
    (skyline/cityscape/landmark), а не абстрактная сцена. Фолбэк — тема + 'skyline'."""
    t = (topic or "").strip()
    if not t:
        return "city skyline aerial view"
    try:
        raw = groq_chat(TOPIC_STOCK_QUERY_PROMPT, t[:200], temperature=0.2)
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        lines = [ln for ln in lines if not _SUBJECT_PREAMBLE.match(ln)]
        if lines:
            out = lines[-1].strip().strip("\"'`").rstrip(".").strip()
            if out and len(out) <= 100:
                return out
    except Exception as e:
        logging.warning(f"image_subject_for_topic LLM xatosi: {e}")
    return f"{t} skyline cityscape landmark"


def _subject_fallback(plain: str) -> str:
    """Первая содержательная строка, обрезанная по ГРАНИЦЕ СЛОВА (≤120 симв.)."""
    first = next((ln for ln in plain.splitlines() if ln.strip()), plain).strip()
    if len(first) <= 120:
        return first or "news"
    cut = first[:120].rsplit(" ", 1)[0].strip()
    return cut or first[:120]


HEADLINE_PROMPT = (
    "You write the OVERLAY HEADLINE for a news image card (like a press photo "
    "caption). You receive a finished Telegram news post. Reply with ONE short, "
    "punchy headline that captures the news.\n"
    "Strict rules:\n"
    "- SAME LANGUAGE as the post — never translate. If the post is in Uzbek Latin, "
    "stay in Uzbek Latin (zero Cyrillic).\n"
    "- 4-10 words, max ~70 characters. No final period. No quotes, no hashtags, "
    "no emojis, no source/channel names, no 'Photo:' label.\n"
    "- Keep the key actor + what happened (and the key number if it is the point).\n"
    "- Use ONLY facts from the post; invent nothing.\n"
    "- Output the headline ALONE — no preamble, no label, no explanation."
)


def news_headline(profile, text: str) -> str:
    """Короткий заголовок для накладки на картинку — НА ЯЗЫКЕ поста (не перевод).
    Источник — готовый пост. Фолбэк при сбое LLM — первая строка по границе слова."""
    plain = re.sub(r"<[^>]+>", " ", text or "")
    plain = re.sub(r"\s+", " ", plain).strip()
    if not plain:
        return ""
    try:
        user = f"## OUTPUT LANGUAGE (do NOT translate): {_language_name(profile.language)}\n\n## POST\n{plain[:1800]}"
        raw = groq_chat(HEADLINE_PROMPT, user, temperature=0.3)
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        lines = [ln for ln in lines if not _SUBJECT_PREAMBLE.match(ln)]
        if lines:
            out = lines[-1].strip().strip('"\'').rstrip(".").strip()
            if out and len(out) <= 140:
                return out
    except Exception as e:
        logging.warning(f"news_headline LLM xatosi: {e}")
    return _subject_fallback(plain)


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
