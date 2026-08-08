"""OpenAI Codex (ChatGPT subscription) credential adapter.

Reads and writes ``$CODEX_HOME/auth.json``, the single file the Codex CLI
authenticates from. Identity comes out of the ``id_token`` claims with no
network call and no signature check: the file is already trusted local state,
so validating it would only add a dependency without adding a guarantee.

Endpoint notes, all confirmed against a live account on 2026-08-08:

- ``GET https://chatgpt.com/backend-api/wham/usage`` answers 200 with the
  rate-limit windows. It is undocumented and can change without notice.
- Access tokens carry ``exp - iat == 864000`` (10 days), so polling an idle
  account almost never needs a refresh first.
"""

from __future__ import annotations

import base64
import json
import logging

from claude_swap.providers.base import AccountIdentity

_logger = logging.getLogger("claude-swap")

#: The claim object the ChatGPT auth service adds to the id_token.
AUTH_CLAIM = "https://api.openai.com/auth"

#: OAuth client the Codex CLI identifies as. Read out of a live auth.json.
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

TOKEN_URL = "https://auth.openai.com/oauth/token"
USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


def _decode_jwt_payload(token: object) -> dict | None:
    """Claims out of a JWT, without verifying the signature."""
    if not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None
    segment = parts[1]
    segment += "=" * (-len(segment) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(segment))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _tokens(blob: str) -> dict | None:
    """The ``tokens`` object out of an auth.json body."""
    try:
        data = json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    tokens = data.get("tokens")
    return tokens if isinstance(tokens, dict) else None


def identity(blob: str) -> AccountIdentity | None:
    """Identity out of an auth.json body, or None when unreadable."""
    tokens = _tokens(blob)
    if not tokens:
        return None
    claims = _decode_jwt_payload(tokens.get("id_token"))
    if not isinstance(claims, dict):
        return None
    auth = claims.get(AUTH_CLAIM)
    auth = auth if isinstance(auth, dict) else {}

    email = claims.get("email") or ""
    account_uuid = auth.get("chatgpt_account_id") or tokens.get("account_id") or ""
    if not isinstance(email, str) or not isinstance(account_uuid, str):
        return None
    if not email and not account_uuid:
        return None

    org_uuid = ""
    org_name = ""
    orgs = auth.get("organizations")
    if isinstance(orgs, list) and orgs:
        default = next(
            (o for o in orgs if isinstance(o, dict) and o.get("is_default")), None
        )
        chosen = default if default is not None else orgs[0]
        if isinstance(chosen, dict):
            org_uuid = str(chosen.get("id") or "")
            org_name = str(chosen.get("title") or "")

    plan = auth.get("chatgpt_plan_type") or ""
    return AccountIdentity(
        email=email,
        account_uuid=account_uuid,
        org_uuid=org_uuid,
        org_name=org_name,
        plan=str(plan),
    )
