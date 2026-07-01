"""Кросс-постинг в X/Twitter (OAuth2 user-context).

Две роли:
  1. OAuth2-хелперы (PKCE, authorize URL, обмен/рефреш токенов, users/me) —
     используются admin-api при подключении аккаунта («кнопка Подключить»);
  2. cross_post — зеркалит опубликованный пост канала в привязанный X-аккаунт
     (короткая ≤280-версия). Вызывается из publisher.send_to_telegram, best-effort:
     любой сбой X НЕ должен ломать публикацию в Telegram.

Токены хранятся зашифрованными в БД (database.TwitterAccount); access-токен X живёт
~2 часа, поэтому перед постингом при необходимости рефрешим (offline.access).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import secrets
from datetime import timedelta
from pathlib import Path
from typing import Optional, Tuple

import httpx

from database import (
    _utcnow,
    get_twitter_account,
    update_twitter_tokens,
)

# --- Конфиг X-приложения ------------------------------------------------------
CLIENT_ID = os.getenv("TWITTER_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("TWITTER_CLIENT_SECRET", "")
REDIRECT_URI = os.getenv("TWITTER_REDIRECT_URI", "")

# OAuth2-скоупы: чтение/запись твитов, профиль (users/me), загрузка медиа и
# offline.access для refresh-токена (иначе access протухает через ~2ч без продления).
SCOPES = "tweet.read tweet.write users.read media.write offline.access"

AUTHORIZE_URL = "https://twitter.com/i/oauth2/authorize"
TOKEN_URL = "https://api.twitter.com/2/oauth2/token"
USERS_ME_URL = "https://api.twitter.com/2/users/me"
TWEET_URL = "https://api.twitter.com/2/tweets"
MEDIA_UPLOAD_URL = "https://api.x.com/2/media/upload"

# Рефрешим access-токен, если до истечения осталось меньше этого запаса.
_TOKEN_REFRESH_MARGIN = timedelta(seconds=120)


def is_configured() -> bool:
    """Заданы ли креды X-приложения (иначе connect/cross-post невозможны)."""
    return bool(CLIENT_ID and CLIENT_SECRET and REDIRECT_URI)


# --- PKCE / OAuth2-хелперы (для admin-api) ------------------------------------

def new_pkce() -> Tuple[str, str]:
    """(code_verifier, code_challenge) для PKCE S256."""
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def new_state() -> str:
    return secrets.token_urlsafe(24)


def build_authorize_url(state: str, code_challenge: str) -> str:
    """URL авторизации X, куда редиректим пользователя (кнопка «Подключить»)."""
    from urllib.parse import urlencode

    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def _basic_auth() -> Tuple[str, str]:
    """Basic-auth пара для token-эндпоинта (confidential client)."""
    return (CLIENT_ID, CLIENT_SECRET)


async def exchange_code(code: str, code_verifier: str) -> dict:
    """Меняет authorization code на токены. Возвращает JSON X (access/refresh/expires_in)."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": code_verifier,
        "client_id": CLIENT_ID,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(TOKEN_URL, data=data, auth=_basic_auth())
        resp.raise_for_status()
        return resp.json()


async def refresh_tokens(refresh_token: str) -> dict:
    """Продлевает токены по refresh_token (X ротирует refresh_token при рефреше)."""
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(TOKEN_URL, data=data, auth=_basic_auth())
        resp.raise_for_status()
        return resp.json()


async def fetch_me(access_token: str) -> dict:
    """Профиль подключённого аккаунта: {'id', 'username', ...}."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            USERS_ME_URL, headers={"Authorization": f"Bearer {access_token}"}
        )
        resp.raise_for_status()
        return resp.json().get("data", {})


# --- Кросс-постинг (для publisher) --------------------------------------------

async def _ensure_fresh_access_token(account: dict) -> Optional[str]:
    """Возвращает валидный access-токен, при необходимости рефрешит и сохраняет.

    None — рефреш не удался (аккаунт нужно переподключить). Ошибки не пробрасываем.
    """
    expires_at = account["token_expires_at"]
    if _utcnow() + _TOKEN_REFRESH_MARGIN < expires_at:
        return account["access_token"]

    try:
        tok = await refresh_tokens(account["refresh_token"])
    except Exception as e:
        logging.warning("Twitter token refresh xatosi (%s): %s", account["tenant_id"], e)
        return None

    access = tok.get("access_token")
    # X при рефреше отдаёт новый refresh_token; если вдруг нет — оставляем старый.
    refresh = tok.get("refresh_token") or account["refresh_token"]
    expires_in = int(tok.get("expires_in", 7200))
    if not access:
        return None
    new_expires = _utcnow() + timedelta(seconds=expires_in)
    await asyncio.to_thread(
        update_twitter_tokens, account["tenant_id"], access, refresh, new_expires
    )
    return access


async def _upload_media(access_token: str, image_path: str) -> Optional[str]:
    """Загружает картинку в X (v2 media/upload), возвращает media_id или None."""
    try:
        path = Path(image_path)
        data = await asyncio.to_thread(path.read_bytes)
        files = {"media": (path.name, data)}
        form = {"media_category": "tweet_image"}
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                MEDIA_UPLOAD_URL,
                headers={"Authorization": f"Bearer {access_token}"},
                data=form,
                files=files,
            )
            resp.raise_for_status()
            body = resp.json()
        # v2 отдаёт {"data":{"id": "..."}}; на всякий случай поддержим media_id_string.
        return (body.get("data") or {}).get("id") or body.get("media_id_string")
    except Exception as e:
        logging.warning("Twitter media yuklab bo'lmadi: %s", e)
        return None


async def cross_post(tenant_id: str, text: str, image_path: Optional[str] = None) -> None:
    """Зеркалит пост канала в привязанный X-аккаунт (короткая ≤280-версия).

    Best-effort: если аккаунт не подключён/неактивен, приложение не сконфигурено,
    или X вернул ошибку — просто логируем и выходим, НЕ ломая публикацию в Telegram.
    """
    if not is_configured():
        return
    account = await asyncio.to_thread(get_twitter_account, tenant_id)
    if not account or not account.get("active"):
        return

    access_token = await _ensure_fresh_access_token(account)
    if not access_token:
        return

    # Короткая версия под твит. tweetify тянет generator (groq) — импорт ленивый,
    # чтобы admin-api, использующий OAuth-хелперы этого модуля, не тащил генератор.
    from database import get_tenant_profile
    from generator import tweetify

    profile = await asyncio.to_thread(get_tenant_profile, tenant_id)
    tweet_text = await asyncio.to_thread(tweetify, profile, text)

    media_ids = []
    if image_path and Path(image_path).exists():
        media_id = await _upload_media(access_token, image_path)
        if media_id:
            media_ids.append(media_id)

    payload: dict = {"text": tweet_text}
    if media_ids:
        payload["media"] = {"media_ids": media_ids}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                TWEET_URL,
                headers={"Authorization": f"Bearer {access_token}"},
                json=payload,
            )
            resp.raise_for_status()
        logging.info(
            "Twitter cross-post OK (@%s / %s)", account.get("screen_name"), tenant_id
        )
    except Exception as e:
        detail = getattr(getattr(e, "response", None), "text", "")
        logging.warning("Twitter cross-post xatosi (%s): %s %s", tenant_id, e, detail)
