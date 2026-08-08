"""Builders for synthetic Codex credential blobs.

Signatures are never verified by the adapter (it only reads claims), so an
unsigned JWT with a real-shaped payload is a faithful stand-in.
"""

from __future__ import annotations

import base64
import json
import time


def _b64url(data: dict) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def make_jwt(payload: dict) -> str:
    """An unsigned JWT with the given payload."""
    header = _b64url({"alg": "none", "typ": "JWT"})
    return f"{header}.{_b64url(payload)}.signature"


def make_codex_auth(
    email: str = "user@example.com",
    account_id: str = "acc-0001",
    user_id: str = "user-0001",
    plan: str = "plus",
    org_id: str = "org-0001",
    org_title: str = "Personal",
    refresh_token: str = "refresh-token-0001",
    access_expires_in: int = 864000,
    now: float | None = None,
) -> str:
    """A whole auth.json body as a JSON string."""
    issued = int(now if now is not None else time.time())
    id_token = make_jwt({
        "email": email,
        "email_verified": True,
        "name": "Test User",
        "iss": "https://auth.openai.com",
        "iat": issued,
        "exp": issued + 3600,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
            "chatgpt_plan_type": plan,
            "chatgpt_user_id": user_id,
            "user_id": user_id,
            "organizations": [
                {"id": org_id, "title": org_title, "is_default": True, "role": "owner"}
            ],
        },
    })
    access_token = make_jwt({
        "aud": ["https://api.openai.com/v1"],
        "iat": issued,
        "exp": issued + access_expires_in,
        "https://api.openai.com/profile": {"email": email},
    })
    return json.dumps({
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": id_token,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "account_id": account_id,
        },
        "last_refresh": "2026-08-07T13:07:00.000Z",
    })
