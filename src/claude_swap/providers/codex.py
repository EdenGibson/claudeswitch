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
import hashlib
import json
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from claude_swap.oauth import RefreshOutcome, format_reset
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
        decoded = json.loads(base64.urlsafe_b64decode(segment))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


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


#: Treat a token expiring inside this window as already expired. Matches
#: oauth.OAUTH_EXPIRY_BUFFER_MS so both providers behave the same.
EXPIRY_BUFFER_S = 5 * 60


def fingerprint(blob: str) -> str | None:
    """Stable lineage id for a credential.

    Hashes the refresh token, which survives access-token rotation, so two
    generations of the same login compare equal. None only for input that
    carries no refresh token at all.
    """
    tokens = _tokens(blob)
    if not tokens:
        return None
    refresh = tokens.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        return None
    return "sha256:" + hashlib.sha256(refresh.encode("utf-8")).hexdigest()


def access_token_expires_at(blob: str) -> float | None:
    """Unix seconds at which the access token expires, or None if unreadable."""
    tokens = _tokens(blob)
    if not tokens:
        return None
    claims = _decode_jwt_payload(tokens.get("access_token"))
    if not isinstance(claims, dict):
        return None
    exp = claims.get("exp")
    return float(exp) if isinstance(exp, (int, float)) else None


def is_expired(blob: str) -> bool:
    """Whether the access token is expired or expires within the buffer.

    An unreadable blob answers False. Unknown must not read as expired, or a
    parse failure would drive a refresh of a credential that was fine.
    """
    expires_at = access_token_expires_at(blob)
    if expires_at is None:
        return False
    return time.time() + EXPIRY_BUFFER_S >= expires_at


#: Window length in seconds -> the normalized key cswap already uses.
#: Anything else becomes a ``scoped`` entry named by its length in hours, so a
#: new window the provider adds is surfaced rather than silently dropped.
_WINDOW_KEYS = {18000: "five_hour", 604800: "seven_day"}

USAGE_TIMEOUT_S = 5.0


def _window_entry(window: object) -> tuple[str, dict] | None:
    """``(key, entry)`` for one API window, or None when it carries no data."""
    if not isinstance(window, dict):
        return None
    pct = window.get("used_percent")
    if not isinstance(pct, (int, float)):
        return None
    length = window.get("limit_window_seconds")
    if not isinstance(length, int):
        return None

    entry: dict = {"pct": float(pct)}
    reset_at = window.get("reset_at")
    if isinstance(reset_at, (int, float)):
        resets_at = datetime.fromtimestamp(reset_at, tz=timezone.utc).isoformat()
        entry["resets_at"] = resets_at
        entry["countdown"], entry["clock"] = format_reset(resets_at)

    key = _WINDOW_KEYS.get(length)
    if key is None:
        entry["name"] = f"{length // 3600}h"
        return "scoped", entry
    return key, entry


def build_usage_result(data: dict) -> dict | None:
    """Normalize a ``wham/usage`` body into cswap's usage shape.

    Windows are matched on their length, never on which slot the API put them
    in: the live account reports the weekly window as ``primary_window`` while
    other plans report the 5-hour one there.

    ``credits`` is deliberately not mapped. It carries a balance with no limit,
    so it cannot fill the ``spend`` entry's ``{used, limit, pct}`` honestly.
    """
    _logger.debug("Codex usage response: %s", json.dumps(data, indent=2))
    rate_limit = data.get("rate_limit") if isinstance(data, dict) else None
    if not isinstance(rate_limit, dict):
        return None

    result: dict = {}
    scoped: list[dict] = []
    for slot in ("primary_window", "secondary_window"):
        mapped = _window_entry(rate_limit.get(slot))
        if mapped is None:
            continue
        key, entry = mapped
        if key == "scoped":
            scoped.append(entry)
        else:
            result[key] = entry
    if scoped:
        result["scoped"] = scoped
    return result or None


def request_usage_data(access_token: str, account_id: str) -> dict:
    """Raw ``wham/usage`` body. Raises on any HTTP or transport failure."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "chatgpt-account-id": account_id,
        "User-Agent": "claude-swap/1.0",
    }
    req = urllib.request.Request(USAGE_URL, headers=headers)
    with urllib.request.urlopen(req, timeout=USAGE_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode())


def fetch_usage(blob: str) -> dict | None:
    """Normalized usage for one credential blob.

    Raises whatever ``request_usage_data`` raises, so the caller can classify
    the failure with ``oauth._classify_usage_error`` exactly as it does for
    Claude.
    """
    tokens = _tokens(blob)
    if not tokens:
        return None
    access_token = tokens.get("access_token")
    account_id = tokens.get("account_id") or ""
    if not isinstance(access_token, str) or not access_token:
        return None
    return build_usage_result(request_usage_data(access_token, str(account_id)))


REFRESH_TIMEOUT_S = 10.0


def try_refresh(blob: str, timeout_s: float = REFRESH_TIMEOUT_S) -> RefreshOutcome:
    """Exchange the refresh token for a fresh credential blob.

    Returns a whole new auth.json body and never mutates the input. The grant
    rotates the refresh token, so a partial write here costs a browser
    re-login; the caller persists only on ``error is None``.
    """
    try:
        data = json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return RefreshOutcome(None, "no_refresh_token")
    if not isinstance(data, dict):
        return RefreshOutcome(None, "no_refresh_token")
    tokens = data.get("tokens")
    if not isinstance(tokens, dict) or not tokens.get("refresh_token"):
        return RefreshOutcome(None, "no_refresh_token")

    # Exactly the three fields upstream Codex sends, and no scope. The body is
    # JSON here, unlike the authorization_code exchange.
    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": CLIENT_ID,
    }).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "claude-swap/1.0"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        # 400/401 mean this refresh lineage is dead: re-login required, do not
        # retry. Anything else may recover.
        error = "invalid_grant" if exc.code in (400, 401) else "transient"
        _logger.warning("Codex refresh failed: http-%s", exc.code)
        return RefreshOutcome(None, error)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        # OSError covers urllib.error.URLError and TimeoutError, which are both
        # subclasses of it, and also the bare OSError or ConnectionResetError
        # that resp.read() raises. urllib wraps a failure into URLError only at
        # connect time, never at read time.
        _logger.warning("Codex refresh failed: %s", type(exc).__name__)
        return RefreshOutcome(None, "transient")

    if not isinstance(payload, dict):
        _logger.warning(
            "Codex refresh returned a %s, not an object", type(payload).__name__
        )
        return RefreshOutcome(None, "transient")

    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        _logger.warning("Codex refresh returned no access_token")
        return RefreshOutcome(None, "transient")

    new_tokens = dict(tokens)
    new_tokens["access_token"] = access_token
    if isinstance(payload.get("id_token"), str) and payload["id_token"]:
        new_tokens["id_token"] = payload["id_token"]
    if isinstance(payload.get("refresh_token"), str) and payload["refresh_token"]:
        new_tokens["refresh_token"] = payload["refresh_token"]

    refreshed = dict(data)
    refreshed["tokens"] = new_tokens
    refreshed["last_refresh"] = (
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    )
    new_blob = json.dumps(refreshed, indent=2)

    ident = identity(new_blob)
    token_account = (
        {
            "uuid": ident.account_uuid,
            "email": ident.email,
            "organizationUuid": ident.org_uuid,
        }
        if ident
        else None
    )
    return RefreshOutcome(new_blob, None, token_account)
