# Phase 1: Codex Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a second, independent account pool to cswap that manages OpenAI Codex (ChatGPT subscription) credentials — add, list, switch, remove, and live quota — for the native `codex` CLI.

**Architecture:** All new files. A `providers/` package holds credential-format knowledge for one provider. A `CodexAccountStore` owns the Codex pool under `<backup_root>/providers/codex/`, reusing the existing `UsageStore`, `FileLock`, `atomic_write_json` and `printer` modules unchanged. The CLI gains one namespace command, `cswap codex <sub>`, hooked into `main()` with three lines. `switcher.py` is not touched at all.

**Tech Stack:** Python 3.12+, stdlib only (`urllib.request`, `base64`, `json`, `hashlib`), pytest with `-n auto`.

---

## Deviation from the design spec, and why

The spec (`docs/design/2026-08-08-multi-provider-design.md`, section 1) said the Claude code paths
would move into a `providers/claude.py` adapter and `switcher.py` would call through a Protocol.

**This plan does not do that, and Phase 1 does not need it.** Extracting a seam from a 5610-line
file that upstream rewrites weekly is the single largest merge cost available, and nothing in
Phase 1 consumes two providers polymorphically. The Protocol is still written (Task 1) so Phase 2
and Phase 3 have the interface to code against, and `CodexProvider` implements it. `ClaudeProvider`
gets written when the TUI or the autoswitch engine first needs to hold both at once.

Net effect on `switcher.py`: zero lines changed. Net effect on `cli.py`: three lines added.

Also deferred out of Phase 1, deliberately, with nothing depending on them:

- `cswap codex run` (per-terminal `CODEX_HOME` profiles). `$CODEX_HOME` holds `config.toml`,
  session rollouts and sqlite state, so a profile directory has to mirror far more than
  `auth.json`. That is its own plan.
- macOS Keychain storage for Codex credentials. Phase 1 uses the base64 file backend on every
  platform, matching what `.enc` already is on Linux.
- Aliases, directory mappings, disable/enable, export/import for the Codex pool.
- The `spend` usage entry. The Codex `credits` block carries a balance with no limit, so there is
  no honest mapping onto the existing `{used, limit, pct, currency}` shape.

## Storage layout this plan creates

```
<backup_root>/providers/codex/
    sequence.json                       account metadata + active slot
    .lock                               FileLock for sequence.json mutations
    credentials/
        .creds-1-<email>.enc            base64 of the whole auth.json, mode 0600
    cache/
        usage.json                      UsageStore table
        .usage.lock
```

`<backup_root>` is `paths.get_backup_root()`, already `~/.local/share/claude-swap` on Linux.
Nothing outside `providers/codex/` is read or written, so the 8 existing Claude accounts are
untouched and no migration runs.

## File structure

| File | Responsibility |
|---|---|
| `src/claude_swap/providers/__init__.py` | Package marker, re-exports `AccountIdentity`, `Provider` |
| `src/claude_swap/providers/base.py` | `AccountIdentity` dataclass and the `Provider` Protocol |
| `src/claude_swap/providers/codex.py` | Everything format-specific to `~/.codex/auth.json`: identity, fingerprint, expiry, refresh, usage fetch and normalization |
| `src/claude_swap/codex_store.py` | The Codex account pool: sequence file, credential backups, add/list/switch/remove, usage collection |
| `src/claude_swap/codex_cli.py` | `cswap codex list\|status\|add\|switch\|remove` argument parsing and rendering |
| `src/claude_swap/paths.py` | +3 functions for Codex and provider-root paths |
| `src/claude_swap/cli.py` | +3 lines: dispatch `codex` to `codex_cli` |
| `tests/providers/test_codex_identity.py` | Identity, fingerprint, expiry |
| `tests/providers/test_codex_usage.py` | Usage normalization and fetch |
| `tests/providers/test_codex_refresh.py` | Refresh grant outcomes |
| `tests/test_codex_paths.py` | Path resolution |
| `tests/test_codex_store.py` | Pool behaviour |
| `tests/test_codex_cli.py` | Command surface |
| `tests/fixtures/codex_usage_response.json` | Recorded `wham/usage` body, redacted |
| `tests/providers/conftest.py` | `make_codex_auth()` builder shared by the provider tests |

---

## Task 1: Provider seam types

**Files:**
- Create: `src/claude_swap/providers/__init__.py`
- Create: `src/claude_swap/providers/base.py`
- Test: `tests/providers/__init__.py`, `tests/providers/test_base.py`

- [ ] **Step 1: Write the failing test**

Create `tests/providers/__init__.py` as an empty file, then `tests/providers/test_base.py`:

```python
"""Provider seam types."""

from __future__ import annotations

from claude_swap.providers import AccountIdentity


def test_identity_display_label_uses_org_name():
    ident = AccountIdentity(
        email="a@example.com",
        account_uuid="acc-1",
        org_uuid="org-1",
        org_name="Acme",
        plan="pro",
    )
    assert ident.display_label == "a@example.com [Acme]"


def test_identity_display_label_falls_back_to_personal():
    ident = AccountIdentity(email="a@example.com", account_uuid="acc-1")
    assert ident.display_label == "a@example.com [personal]"


def test_identity_is_hashable_and_frozen():
    ident = AccountIdentity(email="a@example.com", account_uuid="acc-1")
    assert {ident: 1}[ident] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/providers/test_base.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'claude_swap.providers'`

- [ ] **Step 3: Write the implementation**

Create `src/claude_swap/providers/base.py`:

```python
"""Provider seam: the shape every account provider presents to cswap.

One provider owns one credential format and one vendor endpoint set. Nothing
here knows about slots, storage or rotation — that belongs to the store that
composes a provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from claude_swap.oauth import RefreshOutcome


@dataclass(frozen=True)
class AccountIdentity:
    """Who a credential belongs to, as read out of the credential itself.

    ``account_uuid`` is the provider's own stable account id. It is the second
    half of the identity tuple ``UsageStore`` guards rows with, so it must not
    change across a token refresh.
    """

    email: str
    account_uuid: str
    org_uuid: str = ""
    org_name: str = ""
    plan: str = ""

    @property
    def display_label(self) -> str:
        """``email [OrgName]``, or ``email [personal]`` with no org."""
        tag = self.org_name if self.org_name else "personal"
        return f"{self.email} [{tag}]"


@runtime_checkable
class Provider(Protocol):
    """What a provider must supply. Implemented by ``CodexProvider``.

    A ``ClaudeProvider`` is deliberately absent: Phase 1 never holds two
    providers at once, and writing one means moving code out of switcher.py,
    which is the most expensive file in the tree to keep merged with upstream.
    """

    name: str
    display_name: str

    def active_path(self) -> Path:
        """Where the vendor CLI reads its live credential."""
        ...

    def read_active(self) -> str | None:
        """The live credential blob, or None when absent or unreadable."""
        ...

    def write_active(self, blob: str) -> None:
        """Replace the live credential atomically, mode 0600."""
        ...

    def identity(self, blob: str) -> AccountIdentity | None:
        """Identity read out of the blob with no network call."""
        ...

    def fingerprint(self, blob: str) -> str | None:
        """Stable lineage id, unchanged by access-token rotation."""
        ...

    def is_expired(self, blob: str) -> bool:
        """Whether the blob's access token is expired or about to be."""
        ...

    def try_refresh(self, blob: str) -> RefreshOutcome:
        """Exchange the refresh token for a fresh blob."""
        ...

    def fetch_usage(self, blob: str) -> dict | None:
        """Normalized usage dict, in the shape ``oauth.build_usage_result``
        returns, or None when the response carried no window data."""
        ...
```

Create `src/claude_swap/providers/__init__.py`:

```python
"""Account providers."""

from claude_swap.providers.base import AccountIdentity, Provider

__all__ = ["AccountIdentity", "Provider"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/providers/test_base.py -q`
Expected: PASS, 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/claude_swap/providers tests/providers
git commit -m "feat(providers): add provider seam types"
```

---

## Task 2: Codex identity from the id_token

**Files:**
- Create: `src/claude_swap/providers/codex.py`
- Create: `tests/providers/conftest.py`
- Test: `tests/providers/test_codex_identity.py`

The real `~/.codex/auth.json` shape, measured on 2026-08-08:

```json
{
  "auth_mode": "chatgpt",
  "OPENAI_API_KEY": null,
  "tokens": {
    "id_token": "<jwt>",
    "access_token": "<jwt>",
    "refresh_token": "<opaque>",
    "account_id": "d127f2b7-...."
  },
  "last_refresh": "2026-08-07T13:07:00.000Z"
}
```

The `id_token` payload carries `email`, `name`, and a claim object under the key
`https://api.openai.com/auth` holding `chatgpt_account_id`, `chatgpt_plan_type` and
`organizations` (a list of `{id, title, is_default, role}`).

- [ ] **Step 1: Write the shared fixture builder**

Create `tests/providers/conftest.py`:

```python
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
```

- [ ] **Step 2: Write the failing test**

Create `tests/providers/test_codex_identity.py`:

```python
"""Codex identity, fingerprint and expiry."""

from __future__ import annotations

import json
import time

from claude_swap.providers import codex
from tests.providers.conftest import make_codex_auth, make_jwt


def test_identity_reads_every_field():
    blob = make_codex_auth(
        email="eden@example.com",
        account_id="acc-42",
        plan="pro",
        org_id="org-42",
        org_title="Derive",
    )
    ident = codex.identity(blob)
    assert ident is not None
    assert ident.email == "eden@example.com"
    assert ident.account_uuid == "acc-42"
    assert ident.org_uuid == "org-42"
    assert ident.org_name == "Derive"
    assert ident.plan == "pro"


def test_identity_survives_an_expired_id_token():
    """Claims are read, never validated, so expiry must not matter."""
    blob = make_codex_auth(now=time.time() - 86_400)
    assert codex.identity(blob) is not None


def test_identity_with_no_organizations_is_personal():
    raw = json.loads(make_codex_auth())
    payload_claims = {
        "email": "solo@example.com",
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "acc-solo",
            "chatgpt_plan_type": "plus",
            "organizations": [],
        },
    }
    raw["tokens"]["id_token"] = make_jwt(payload_claims)
    ident = codex.identity(json.dumps(raw))
    assert ident is not None
    assert ident.org_uuid == ""
    assert ident.display_label == "solo@example.com [personal]"


def test_identity_of_garbage_is_none():
    assert codex.identity("not json") is None
    assert codex.identity(json.dumps({"tokens": {}})) is None
    assert codex.identity(json.dumps({"tokens": {"id_token": "a.b"}})) is None
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/providers/test_codex_identity.py -q`
Expected: FAIL, `ImportError: cannot import name 'codex'`

- [ ] **Step 4: Write the implementation**

Create `src/claude_swap/providers/codex.py`:

```python
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
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/providers/test_codex_identity.py -q`
Expected: PASS, 4 passed

- [ ] **Step 6: Commit**

```bash
git add src/claude_swap/providers/codex.py tests/providers/
git commit -m "feat(codex): read account identity from the id_token"
```

---

## Task 3: Codex fingerprint and access-token expiry

**Files:**
- Modify: `src/claude_swap/providers/codex.py`
- Test: `tests/providers/test_codex_identity.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/providers/test_codex_identity.py`:

```python
def test_fingerprint_tracks_the_refresh_token_only():
    a = make_codex_auth(refresh_token="same", access_expires_in=864000)
    b = make_codex_auth(refresh_token="same", access_expires_in=100)
    c = make_codex_auth(refresh_token="different")
    assert codex.fingerprint(a) == codex.fingerprint(b)
    assert codex.fingerprint(a) != codex.fingerprint(c)


def test_fingerprint_of_garbage_is_none():
    assert codex.fingerprint("") is None
    assert codex.fingerprint("not json") is None


def test_expiry_reads_the_access_token_exp():
    fresh = make_codex_auth(access_expires_in=864_000)
    assert codex.is_expired(fresh) is False

    stale = make_codex_auth(access_expires_in=-1)
    assert codex.is_expired(stale) is True


def test_expiry_buffer_treats_an_imminent_expiry_as_expired():
    """Five minutes of headroom, matching oauth.OAUTH_EXPIRY_BUFFER_MS."""
    soon = make_codex_auth(access_expires_in=60)
    assert codex.is_expired(soon) is True


def test_unreadable_blob_is_not_reported_expired():
    """Unknown must not read as expired, or a bad parse triggers a refresh
    storm against a credential that was fine."""
    assert codex.is_expired("not json") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/providers/test_codex_identity.py -q`
Expected: FAIL, `AttributeError: module 'claude_swap.providers.codex' has no attribute 'fingerprint'`

- [ ] **Step 3: Write the implementation**

Append to `src/claude_swap/providers/codex.py`:

```python
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
    import time

    expires_at = access_token_expires_at(blob)
    if expires_at is None:
        return False
    return time.time() + EXPIRY_BUFFER_S >= expires_at
```

Move `import time` to the module's top-level import block rather than leaving it inside the
function; it is written inline above only to keep the appended hunk self-contained.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/providers/test_codex_identity.py -q`
Expected: PASS, 9 passed

- [ ] **Step 5: Commit**

```bash
git add src/claude_swap/providers/codex.py tests/providers/test_codex_identity.py
git commit -m "feat(codex): add credential fingerprint and expiry"
```

---

## Task 4: Codex usage normalization

**Files:**
- Create: `tests/fixtures/codex_usage_response.json`
- Modify: `src/claude_swap/providers/codex.py`
- Test: `tests/providers/test_codex_usage.py`

The normalized output must match what `oauth.build_usage_result` produces for Claude, because
`oauth.account_headroom`, `poll_policy.binding_pct` and the TUI all read that shape.

- [ ] **Step 1: Record the fixture**

Create `tests/fixtures/codex_usage_response.json`. This is the real 200 body captured on
2026-08-08 with the account identifiers replaced:

```json
{
  "user_id": "user-REDACTED",
  "account_id": "acc-REDACTED",
  "email": "user@example.com",
  "plan_type": "plus",
  "rate_limit": {
    "allowed": true,
    "limit_reached": false,
    "primary_window": {
      "used_percent": 74,
      "limit_window_seconds": 604800,
      "reset_after_seconds": 592660,
      "reset_at": 1786709786
    },
    "secondary_window": {
      "used_percent": 12.5,
      "limit_window_seconds": 18000,
      "reset_after_seconds": 3600,
      "reset_at": 1786121386
    }
  },
  "code_review_rate_limit": null,
  "additional_rate_limits": null,
  "credits": {
    "has_credits": false,
    "unlimited": false,
    "overage_limit_reached": false,
    "balance": "0"
  },
  "spend_control": {"reached": false, "individual_limit": null},
  "rate_limit_reached_type": null,
  "promo": null
}
```

The live account reported `secondary_window: null`. A populated secondary is added here so both
branches are covered; a null secondary is tested separately below.

- [ ] **Step 2: Write the failing test**

Create `tests/providers/test_codex_usage.py`:

```python
"""Codex usage normalization and fetch."""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import oauth
from claude_swap.providers import codex
from tests.providers.conftest import make_codex_auth

FIXTURE = Path(__file__).parent.parent / "fixtures" / "codex_usage_response.json"


def _response() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_windows_map_by_length_not_by_position():
    """A 604800s window is the 7-day one wherever the API puts it."""
    result = codex.build_usage_result(_response())
    assert result is not None
    assert result["seven_day"]["pct"] == 74.0
    assert result["five_hour"]["pct"] == 12.5


def test_windows_carry_an_iso_resets_at_the_shared_formatter_accepts():
    result = codex.build_usage_result(_response())
    resets_at = result["seven_day"]["resets_at"]
    countdown, clock = oauth.format_reset(resets_at)
    assert isinstance(countdown, str) and countdown
    assert isinstance(clock, str) and clock


def test_headroom_uses_the_binding_window():
    """The whole point of normalizing: the existing math must just work."""
    result = codex.build_usage_result(_response())
    assert oauth.account_headroom(result) == pytest.approx(26.0)


def test_a_null_secondary_window_is_omitted_not_zeroed():
    data = _response()
    data["rate_limit"]["secondary_window"] = None
    result = codex.build_usage_result(data)
    assert "five_hour" not in result
    assert result["seven_day"]["pct"] == 74.0


def test_an_unrecognised_window_length_becomes_a_scoped_entry():
    data = _response()
    data["rate_limit"]["secondary_window"]["limit_window_seconds"] = 86400
    result = codex.build_usage_result(data)
    assert "five_hour" not in result
    assert result["scoped"] == [
        {
            "name": "24h",
            "pct": 12.5,
            "resets_at": result["scoped"][0]["resets_at"],
            "countdown": result["scoped"][0]["countdown"],
            "clock": result["scoped"][0]["clock"],
        }
    ]


def test_no_windows_at_all_returns_none():
    assert codex.build_usage_result({"rate_limit": {}}) is None
    assert codex.build_usage_result({}) is None


def test_fetch_usage_sends_the_bearer_and_account_header():
    blob = make_codex_auth(account_id="acc-hdr")
    captured = {}

    class _Resp:
        def read(self):
            return FIXTURE.read_bytes()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        return _Resp()

    with patch("urllib.request.urlopen", _fake_urlopen):
        result = codex.fetch_usage(blob)

    assert captured["url"] == codex.USAGE_URL
    assert captured["headers"]["Authorization"].startswith("Bearer ")
    assert captured["headers"]["Chatgpt-account-id"] == "acc-hdr"
    assert result["seven_day"]["pct"] == 74.0


def test_fetch_usage_propagates_http_errors():
    def _raise(req, timeout=None):
        raise urllib.error.HTTPError(codex.USAGE_URL, 429, "Too Many", {}, None)

    with patch("urllib.request.urlopen", _raise):
        with pytest.raises(urllib.error.HTTPError):
            codex.fetch_usage(make_codex_auth())
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/providers/test_codex_usage.py -q`
Expected: FAIL, `AttributeError: module 'claude_swap.providers.codex' has no attribute 'build_usage_result'`

- [ ] **Step 4: Write the implementation**

Append to `src/claude_swap/providers/codex.py`:

```python
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
```

Add to the module's top-level imports:

```python
import urllib.request
from datetime import datetime, timezone

from claude_swap.oauth import format_reset
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/providers/test_codex_usage.py -q`
Expected: PASS, 8 passed

- [ ] **Step 6: Commit**

```bash
git add src/claude_swap/providers/codex.py tests/providers/test_codex_usage.py tests/fixtures/codex_usage_response.json
git commit -m "feat(codex): normalize wham/usage into cswap's usage shape"
```

---

## Task 5: Codex token refresh

**Files:**
- Modify: `src/claude_swap/providers/codex.py`
- Test: `tests/providers/test_codex_refresh.py`

**Read this before writing code.** The grant response rotates the refresh token. Writing a partial
or wrong result back over `auth.json` destroys the login and needs a browser re-auth. The
implementation therefore never mutates the caller's blob; it returns a new complete blob and lets
the store decide when to persist it. The live flow is unverified against the real endpoint. Task 12
covers verifying it against a spare account before it ever runs against the primary one.

- [ ] **Step 1: Write the failing test**

Create `tests/providers/test_codex_refresh.py`:

```python
"""Codex refresh-token grant outcomes."""

from __future__ import annotations

import json
import urllib.error
from unittest.mock import patch

from claude_swap.providers import codex
from tests.providers.conftest import make_codex_auth, make_jwt


def _grant_response(body: dict):
    class _Resp:
        def read(self):
            return json.dumps(body).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _Resp()


def test_success_returns_a_whole_new_blob_with_the_rotated_tokens():
    original = make_codex_auth(refresh_token="old-refresh", account_id="acc-1")
    new_access = make_jwt({"exp": 9_999_999_999})
    new_id = make_jwt({
        "email": "user@example.com",
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "acc-1",
            "chatgpt_plan_type": "plus",
            "organizations": [],
        },
    })
    captured = {}

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode())
        return _grant_response({
            "access_token": new_access,
            "id_token": new_id,
            "refresh_token": "new-refresh",
        })

    with patch("urllib.request.urlopen", _fake_urlopen):
        outcome = codex.try_refresh(original)

    assert captured["url"] == codex.TOKEN_URL
    assert captured["body"]["grant_type"] == "refresh_token"
    assert captured["body"]["refresh_token"] == "old-refresh"
    assert captured["body"]["client_id"] == codex.CLIENT_ID

    assert outcome.error is None
    refreshed = json.loads(outcome.credentials)
    assert refreshed["tokens"]["refresh_token"] == "new-refresh"
    assert refreshed["tokens"]["access_token"] == new_access
    assert refreshed["tokens"]["account_id"] == "acc-1"
    assert outcome.token_account == {
        "uuid": "acc-1",
        "email": "user@example.com",
        "organizationUuid": "",
    }


def test_a_response_without_a_new_refresh_token_keeps_the_old_one():
    original = make_codex_auth(refresh_token="keep-me")
    with patch(
        "urllib.request.urlopen",
        lambda req, timeout=None: _grant_response({"access_token": make_jwt({"exp": 1})}),
    ):
        outcome = codex.try_refresh(original)
    assert json.loads(outcome.credentials)["tokens"]["refresh_token"] == "keep-me"


def test_no_refresh_token_is_a_permanent_failure():
    blob = json.dumps({"tokens": {"access_token": "x"}})
    outcome = codex.try_refresh(blob)
    assert outcome.credentials is None
    assert outcome.error == "no_refresh_token"


def test_a_400_is_invalid_grant_and_permanent():
    def _raise(req, timeout=None):
        raise urllib.error.HTTPError(codex.TOKEN_URL, 400, "Bad", {}, None)

    with patch("urllib.request.urlopen", _raise):
        outcome = codex.try_refresh(make_codex_auth())
    assert outcome.error == "invalid_grant"


def test_a_500_is_transient():
    def _raise(req, timeout=None):
        raise urllib.error.HTTPError(codex.TOKEN_URL, 503, "Down", {}, None)

    with patch("urllib.request.urlopen", _raise):
        outcome = codex.try_refresh(make_codex_auth())
    assert outcome.error == "transient"


def test_a_response_missing_an_access_token_is_transient_and_writes_nothing():
    with patch(
        "urllib.request.urlopen",
        lambda req, timeout=None: _grant_response({"token_type": "Bearer"}),
    ):
        outcome = codex.try_refresh(make_codex_auth())
    assert outcome.credentials is None
    assert outcome.error == "transient"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/providers/test_codex_refresh.py -q`
Expected: FAIL, `AttributeError: module 'claude_swap.providers.codex' has no attribute 'try_refresh'`

- [ ] **Step 3: Write the implementation**

Append to `src/claude_swap/providers/codex.py`:

```python
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

    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": CLIENT_ID,
        "scope": "openid profile email offline_access",
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
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        _logger.warning("Codex refresh failed: %s", type(exc).__name__)
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
```

Add to the module's top-level imports:

```python
import urllib.error

from claude_swap.oauth import RefreshOutcome
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/providers/test_codex_refresh.py -q`
Expected: PASS, 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/claude_swap/providers/codex.py tests/providers/test_codex_refresh.py
git commit -m "feat(codex): add refresh-token grant"
```

---

## Task 6: Codex and provider paths

**Files:**
- Modify: `src/claude_swap/paths.py`
- Test: `tests/test_codex_paths.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_codex_paths.py`:

```python
"""Codex and provider path resolution."""

from __future__ import annotations

from pathlib import Path

from claude_swap import paths


def test_codex_home_defaults_under_home(temp_home: Path):
    assert paths.get_codex_home() == temp_home / ".codex"


def test_codex_home_honours_the_env_var(temp_home: Path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(temp_home / "elsewhere"))
    assert paths.get_codex_home() == temp_home / "elsewhere"


def test_codex_auth_path_sits_inside_codex_home(temp_home: Path):
    assert paths.get_codex_auth_path() == temp_home / ".codex" / "auth.json"


def test_provider_root_is_namespaced_under_the_backup_root(temp_home: Path):
    root = paths.get_provider_root("codex")
    assert root == paths.get_backup_root() / "providers" / "codex"


def test_provider_root_rejects_a_traversing_name(temp_home: Path):
    import pytest

    with pytest.raises(ValueError):
        paths.get_provider_root("../escape")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_codex_paths.py -q`
Expected: FAIL, `AttributeError: module 'claude_swap.paths' has no attribute 'get_codex_home'`

- [ ] **Step 3: Write the implementation**

Append to `src/claude_swap/paths.py`:

```python
#: Provider names may only be simple identifiers. The name becomes a directory
#: under the backup root, so anything else could escape it.
_PROVIDER_NAME_RE = re.compile(r"^[a-z0-9_-]+$")


def get_codex_home() -> Path:
    """Return the Codex config directory (``CODEX_HOME`` or ``~/.codex``).

    Mirrors the Codex CLI's own resolution, the same way
    :func:`get_claude_config_home` mirrors Claude Code's.
    """
    env = os.environ.get("CODEX_HOME")
    if env:
        return Path(env)
    return Path.home() / ".codex"


def get_codex_auth_path() -> Path:
    """Return the path to the Codex credentials file."""
    return get_codex_home() / "auth.json"


def get_provider_root(provider: str) -> Path:
    """Return the cswap state root for a non-default provider.

    Claude keeps the backup root itself, unchanged, so existing installs need
    no migration. Every other provider gets ``<backup_root>/providers/<name>``.
    """
    if not _PROVIDER_NAME_RE.match(provider):
        raise ValueError(f"invalid provider name: {provider!r}")
    return get_backup_root() / "providers" / provider
```

Add `import re` to the module's top-level imports.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_codex_paths.py -q`
Expected: PASS, 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/claude_swap/paths.py tests/test_codex_paths.py
git commit -m "feat(paths): resolve Codex and provider-namespaced paths"
```

---

## Task 7: Codex account store — sequence file

**Files:**
- Create: `src/claude_swap/codex_store.py`
- Test: `tests/test_codex_store.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_codex_store.py`:

```python
"""The Codex account pool."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_swap import paths
from claude_swap.codex_store import CodexAccountStore
from tests.providers.conftest import make_codex_auth


@pytest.fixture
def store(temp_home: Path) -> CodexAccountStore:
    return CodexAccountStore()


def test_a_fresh_store_has_no_accounts(store: CodexAccountStore):
    assert store.accounts() == []
    assert store.active_number() is None


def test_the_state_tree_is_namespaced_and_leaves_claude_alone(
    store: CodexAccountStore, temp_home: Path
):
    store.ensure_dirs()
    root = paths.get_backup_root() / "providers" / "codex"
    assert (root / "credentials").is_dir()
    assert (root / "cache").is_dir()
    assert not (paths.get_backup_root() / "sequence.json").exists()


def test_the_sequence_file_records_its_schema_version(store: CodexAccountStore):
    store.ensure_dirs()
    store._write_sequence({"accounts": {}})
    written = json.loads(store.sequence_file.read_text(encoding="utf-8"))
    assert written["schemaVersion"] == 1
    assert "lastUpdated" in written


def test_next_slot_fills_the_lowest_free_number(store: CodexAccountStore):
    store.ensure_dirs()
    store._write_sequence({"accounts": {"1": {}, "3": {}}})
    assert store.next_slot() == "2"


def test_next_slot_on_an_empty_store_is_one(store: CodexAccountStore):
    assert store.next_slot() == "1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_codex_store.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'claude_swap.codex_store'`

- [ ] **Step 3: Write the implementation**

Create `src/claude_swap/codex_store.py`:

```python
"""The Codex account pool.

An independent pool: it shares no state with the Claude accounts and lives
entirely under ``<backup_root>/providers/codex/``. Storage mirrors the Claude
layout so the two read the same way, but nothing is shared except the generic
helpers (``FileLock``, ``atomic_write_json``, ``UsageStore``).

Credentials are stored base64-encoded with mode 0600, which is exactly what the
Claude ``.enc`` files already are on Linux. macOS Keychain storage is not
implemented for this provider.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from claude_swap import paths
from claude_swap.fsutil import replace_with_retry
from claude_swap.locking import FileLock
from claude_swap.settings import atomic_write_json

PROVIDER = "codex"
SCHEMA_VERSION = 1

_logger = logging.getLogger("claude-swap")


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CodexAccountStore:
    """Slots, credentials and the active pointer for the Codex pool."""

    def __init__(self) -> None:
        self.root = paths.get_provider_root(PROVIDER)
        self.credentials_dir = self.root / "credentials"
        self.cache_dir = self.root / "cache"
        self.sequence_file = self.root / "sequence.json"
        self._lock_path = self.root / ".lock"

    # -- directories and raw I/O -------------------------------------------

    def ensure_dirs(self) -> None:
        """Create the provider state tree with 0700 directories."""
        for directory in (self.root, self.credentials_dir, self.cache_dir):
            directory.mkdir(parents=True, exist_ok=True)
            if sys.platform != "win32":
                os.chmod(directory, 0o700)

    def _lock(self) -> FileLock:
        self.ensure_dirs()
        return FileLock(self._lock_path)

    def _read_sequence(self) -> dict:
        """The sequence file, or an empty shape when absent or unreadable."""
        try:
            data = json.loads(self.sequence_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {"schemaVersion": SCHEMA_VERSION, "accounts": {}}
        if not isinstance(data, dict) or not isinstance(data.get("accounts"), dict):
            return {"schemaVersion": SCHEMA_VERSION, "accounts": {}}
        return data

    def _write_sequence(self, data: dict) -> None:
        """Stamp and atomically write the sequence file."""
        self.ensure_dirs()
        data["schemaVersion"] = SCHEMA_VERSION
        data["lastUpdated"] = _timestamp()
        atomic_write_json(self.sequence_file, data)

    # -- slots --------------------------------------------------------------

    def accounts(self) -> list[tuple[str, dict]]:
        """``(slot, record)`` pairs in numeric slot order."""
        rows = self._read_sequence()["accounts"]
        return sorted(rows.items(), key=lambda kv: int(kv[0]))

    def active_number(self) -> str | None:
        """The slot recorded as active, or None."""
        value = self._read_sequence().get("activeAccountNumber")
        return str(value) if value is not None else None

    def next_slot(self) -> str:
        """The lowest unused slot number, as a string."""
        taken = {int(n) for n in self._read_sequence()["accounts"]}
        candidate = 1
        while candidate in taken:
            candidate += 1
        return str(candidate)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_codex_store.py -q`
Expected: PASS, 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/claude_swap/codex_store.py tests/test_codex_store.py
git commit -m "feat(codex): add the account store sequence file"
```

---

## Task 8: Codex account store — credential backups

**Files:**
- Modify: `src/claude_swap/codex_store.py`
- Test: `tests/test_codex_store.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_codex_store.py`:

```python
def test_a_stored_credential_round_trips(store: CodexAccountStore):
    blob = make_codex_auth(email="round@example.com")
    store.write_credential("1", "round@example.com", blob)
    assert store.read_credential("1", "round@example.com") == blob


def test_a_stored_credential_is_not_plain_text_on_disk(store: CodexAccountStore):
    blob = make_codex_auth(email="secret@example.com", refresh_token="TOPSECRET")
    store.write_credential("1", "secret@example.com", blob)
    on_disk = store.credential_path("1", "secret@example.com").read_text()
    assert "TOPSECRET" not in on_disk


@pytest.mark.skipif(
    __import__("sys").platform == "win32", reason="POSIX modes only"
)
def test_a_stored_credential_is_owner_only(store: CodexAccountStore):
    store.write_credential("1", "m@example.com", make_codex_auth())
    mode = store.credential_path("1", "m@example.com").stat().st_mode & 0o777
    assert mode == 0o600


def test_reading_a_missing_credential_returns_empty(store: CodexAccountStore):
    assert store.read_credential("9", "nobody@example.com") == ""


def test_deleting_a_credential_is_idempotent(store: CodexAccountStore):
    store.write_credential("1", "d@example.com", make_codex_auth())
    store.delete_credential("1", "d@example.com")
    store.delete_credential("1", "d@example.com")
    assert store.read_credential("1", "d@example.com") == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_codex_store.py -q`
Expected: FAIL, `AttributeError: 'CodexAccountStore' object has no attribute 'write_credential'`

- [ ] **Step 3: Write the implementation**

Append to `CodexAccountStore` in `src/claude_swap/codex_store.py`:

```python
    # -- credential backups -------------------------------------------------

    def credential_path(self, slot: str, email: str) -> Path:
        """Backup file for one slot. Named like the Claude ones."""
        return self.credentials_dir / f".creds-{slot}-{email}.enc"

    def write_credential(self, slot: str, email: str, blob: str) -> None:
        """Atomically store a credential, base64-encoded, mode 0600."""
        self.ensure_dirs()
        target = self.credential_path(slot, email)
        encoded = base64.b64encode(blob.encode("utf-8"))
        import tempfile

        fd, tmp_path = tempfile.mkstemp(dir=str(self.credentials_dir), suffix=".tmp")
        try:
            os.write(fd, encoded)
            os.close(fd)
            fd = -1
            replace_with_retry(tmp_path, str(target))
            if sys.platform != "win32":
                os.chmod(str(target), 0o600)
        except BaseException:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def read_credential(self, slot: str, email: str) -> str:
        """The stored credential, or an empty string when absent or corrupt."""
        try:
            encoded = self.credential_path(slot, email).read_bytes()
        except OSError:
            return ""
        try:
            return base64.b64decode(encoded).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            _logger.warning("Codex credential for slot %s is unreadable", slot)
            return ""

    def delete_credential(self, slot: str, email: str) -> None:
        """Remove a stored credential. Absent is not an error."""
        self.credential_path(slot, email).unlink(missing_ok=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_codex_store.py -q`
Expected: PASS, 10 passed

- [ ] **Step 5: Commit**

```bash
git add src/claude_swap/codex_store.py tests/test_codex_store.py
git commit -m "feat(codex): store credentials base64-encoded at 0600"
```

---

## Task 9: Codex account store — add, switch, remove

**Files:**
- Modify: `src/claude_swap/codex_store.py`
- Test: `tests/test_codex_store.py`

The one behaviour that is easy to get wrong: `switch` must first re-capture whatever is live in
`~/.codex/auth.json` back into the slot that owns it. The Codex CLI refreshes tokens in place, so
switching away without re-capturing would restore a 10-day-old access token later, and eventually a
dead refresh token.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_codex_store.py`:

```python
def _write_live(temp_home: Path, blob: str) -> Path:
    live = temp_home / ".codex" / "auth.json"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text(blob, encoding="utf-8")
    return live


def test_add_captures_the_live_credential_into_slot_one(
    store: CodexAccountStore, temp_home: Path
):
    _write_live(temp_home, make_codex_auth(email="first@example.com", account_id="acc-1"))
    slot, ident = store.add_current()
    assert slot == "1"
    assert ident.email == "first@example.com"
    assert store.active_number() == "1"
    assert store.read_credential("1", "first@example.com")


def test_add_records_the_identity_fields(store: CodexAccountStore, temp_home: Path):
    _write_live(
        temp_home,
        make_codex_auth(
            email="rec@example.com", account_id="acc-9", plan="pro", org_title="Derive"
        ),
    )
    store.add_current()
    record = dict(store.accounts())["1"]
    assert record["email"] == "rec@example.com"
    assert record["accountId"] == "acc-9"
    assert record["plan"] == "pro"
    assert record["organizationName"] == "Derive"
    assert record["added"]


def test_add_refuses_a_duplicate_account(store: CodexAccountStore, temp_home: Path):
    _write_live(temp_home, make_codex_auth(email="dup@example.com", account_id="acc-d"))
    store.add_current()
    with pytest.raises(ValueError, match="already managed"):
        store.add_current()


def test_add_refuses_when_no_live_credential_exists(store: CodexAccountStore):
    with pytest.raises(ValueError, match="No Codex credential"):
        store.add_current()


def test_switch_writes_the_target_credential_live(
    store: CodexAccountStore, temp_home: Path
):
    _write_live(temp_home, make_codex_auth(email="a@example.com", account_id="acc-a"))
    store.add_current()
    _write_live(temp_home, make_codex_auth(email="b@example.com", account_id="acc-b"))
    store.add_current()

    store.switch("1")

    live = json.loads((temp_home / ".codex" / "auth.json").read_text())
    assert live["tokens"]["account_id"] == "acc-a"
    assert store.active_number() == "1"


def test_switch_recaptures_the_live_credential_before_replacing_it(
    store: CodexAccountStore, temp_home: Path
):
    """The Codex CLI rotates tokens in place. Losing that write would restore a
    stale token later, so switching away must save what is live first."""
    _write_live(temp_home, make_codex_auth(email="a@example.com", account_id="acc-a"))
    store.add_current()
    _write_live(temp_home, make_codex_auth(email="b@example.com", account_id="acc-b"))
    store.add_current()

    rotated = make_codex_auth(
        email="b@example.com", account_id="acc-b", refresh_token="rotated-by-codex"
    )
    _write_live(temp_home, rotated)

    store.switch("1")

    saved = store.read_credential("2", "b@example.com")
    assert json.loads(saved)["tokens"]["refresh_token"] == "rotated-by-codex"


def test_switch_resolves_an_email_as_well_as_a_slot(
    store: CodexAccountStore, temp_home: Path
):
    _write_live(temp_home, make_codex_auth(email="by@example.com", account_id="acc-by"))
    store.add_current()
    _write_live(temp_home, make_codex_auth(email="other@example.com", account_id="acc-o"))
    store.add_current()

    store.switch("by@example.com")
    assert store.active_number() == "1"


def test_switch_to_an_unknown_account_raises(store: CodexAccountStore):
    with pytest.raises(ValueError, match="No Codex account"):
        store.switch("7")


def test_remove_drops_the_record_and_the_credential(
    store: CodexAccountStore, temp_home: Path
):
    _write_live(temp_home, make_codex_auth(email="gone@example.com", account_id="acc-g"))
    store.add_current()
    path = store.credential_path("1", "gone@example.com")

    store.remove("1")

    assert store.accounts() == []
    assert not path.exists()
    assert store.active_number() is None


def test_removing_a_non_active_account_leaves_the_active_pointer(
    store: CodexAccountStore, temp_home: Path
):
    _write_live(temp_home, make_codex_auth(email="keep@example.com", account_id="acc-k"))
    store.add_current()
    _write_live(temp_home, make_codex_auth(email="drop@example.com", account_id="acc-d"))
    store.add_current()
    store.switch("1")

    store.remove("2")

    assert store.active_number() == "1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_codex_store.py -q`
Expected: FAIL, `AttributeError: 'CodexAccountStore' object has no attribute 'add_current'`

- [ ] **Step 3: Write the implementation**

Append to `CodexAccountStore` in `src/claude_swap/codex_store.py`:

```python
    # -- the live credential ------------------------------------------------

    def live_path(self) -> Path:
        """Where the Codex CLI reads its credential."""
        return paths.get_codex_auth_path()

    def read_live(self) -> str:
        """The live credential, or an empty string when absent."""
        try:
            return self.live_path().read_text(encoding="utf-8")
        except OSError:
            return ""

    def write_live(self, blob: str) -> None:
        """Atomically replace the live credential, mode 0600."""
        target = self.live_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        import tempfile

        fd, tmp_path = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        try:
            os.write(fd, blob.encode("utf-8"))
            os.close(fd)
            fd = -1
            replace_with_retry(tmp_path, str(target))
            if sys.platform != "win32":
                os.chmod(str(target), 0o600)
        except BaseException:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # -- resolution ---------------------------------------------------------

    def resolve(self, identifier: str) -> tuple[str, dict]:
        """``(slot, record)`` for a slot number or an email address.

        Raises ValueError naming the identifier when nothing matches.
        """
        rows = self._read_sequence()["accounts"]
        if identifier in rows:
            return identifier, rows[identifier]
        wanted = identifier.strip().lower()
        for slot, record in sorted(rows.items(), key=lambda kv: int(kv[0])):
            if str(record.get("email", "")).lower() == wanted:
                return slot, record
        raise ValueError(f"No Codex account matches '{identifier}'")

    # -- mutations ----------------------------------------------------------

    def _recapture_active(self, data: dict) -> None:
        """Save the live credential back into the slot that owns it.

        The Codex CLI refreshes tokens in place. Without this, switching away
        discards that rotation and a later switch back restores a stale token.
        Mutates nothing when the live credential belongs to no known slot.
        """
        live = self.read_live()
        if not live:
            return
        ident = codex.identity(live)
        if ident is None:
            return
        for slot, record in data["accounts"].items():
            if record.get("accountId") == ident.account_uuid:
                self.write_credential(slot, str(record.get("email", "")), live)
                return

    def add_current(self) -> tuple[str, AccountIdentity]:
        """Capture the live credential into a new slot and make it active.

        Raises ValueError when nothing is logged in, when the credential is
        unreadable, or when the account is already managed.
        """
        live = self.read_live()
        if not live:
            raise ValueError(
                f"No Codex credential at {self.live_path()}. Run 'codex login' first."
            )
        ident = codex.identity(live)
        if ident is None:
            raise ValueError(f"Could not read a Codex account from {self.live_path()}")

        with self._lock():
            data = self._read_sequence()
            for slot, record in data["accounts"].items():
                if record.get("accountId") == ident.account_uuid:
                    raise ValueError(
                        f"{ident.email} is already managed in slot {slot}"
                    )
            slot = self.next_slot()
            data["accounts"][slot] = {
                "email": ident.email,
                "accountId": ident.account_uuid,
                "organizationUuid": ident.org_uuid,
                "organizationName": ident.org_name,
                "plan": ident.plan,
                "added": _timestamp(),
            }
            data["activeAccountNumber"] = int(slot)
            self.write_credential(slot, ident.email, live)
            self._write_sequence(data)
        return slot, ident

    def switch(self, identifier: str) -> tuple[str, str]:
        """Make an account live. Returns ``(slot, email)``.

        Raises ValueError when the account is unknown or its stored credential
        is missing.
        """
        with self._lock():
            data = self._read_sequence()
            slot, record = self.resolve(identifier)
            email = str(record.get("email", ""))
            blob = self.read_credential(slot, email)
            if not blob:
                raise ValueError(
                    f"No stored credential for slot {slot} ({email}). "
                    f"Re-add it with 'cswap codex add'."
                )
            self._recapture_active(data)
            self.write_live(blob)
            data["activeAccountNumber"] = int(slot)
            self._write_sequence(data)
        return slot, email

    def remove(self, identifier: str) -> tuple[str, str]:
        """Forget an account and delete its stored credential.

        Returns ``(slot, email)``. The live ``auth.json`` is left alone: this
        removes cswap's copy, never the user's current login.
        """
        with self._lock():
            data = self._read_sequence()
            slot, record = self.resolve(identifier)
            email = str(record.get("email", ""))
            del data["accounts"][slot]
            if str(data.get("activeAccountNumber")) == slot:
                data.pop("activeAccountNumber", None)
            self.delete_credential(slot, email)
            self._write_sequence(data)
        return slot, email
```

Add to the module's top-level imports:

```python
from claude_swap.providers import codex
from claude_swap.providers.base import AccountIdentity
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_codex_store.py -q`
Expected: PASS, 20 passed

- [ ] **Step 5: Commit**

```bash
git add src/claude_swap/codex_store.py tests/test_codex_store.py
git commit -m "feat(codex): add, switch and remove accounts"
```

---

## Task 10: Codex usage collection

**Files:**
- Modify: `src/claude_swap/codex_store.py`
- Test: `tests/test_codex_store.py`

Usage rows go in the pool's own `cache/usage.json` through the existing `UsageStore`, keyed by the
identity tuple `(email, accountId)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_codex_store.py`:

```python
from unittest.mock import patch

from claude_swap.usage_store import UsageEntry


def _seed_two_accounts(store: CodexAccountStore, temp_home: Path) -> None:
    _write_live(temp_home, make_codex_auth(email="a@example.com", account_id="acc-a"))
    store.add_current()
    _write_live(temp_home, make_codex_auth(email="b@example.com", account_id="acc-b"))
    store.add_current()


def test_collect_usage_stores_a_row_per_account(
    store: CodexAccountStore, temp_home: Path
):
    _seed_two_accounts(store, temp_home)
    usage = {"five_hour": {"pct": 10.0}, "seven_day": {"pct": 40.0}}

    with patch("claude_swap.providers.codex.fetch_usage", return_value=usage):
        entries = store.collect_usage()

    assert set(entries) == {"1", "2"}
    assert entries["1"].last_good == usage
    assert entries["2"].last_good == usage


def test_collect_usage_records_a_failure_without_losing_the_last_good(
    store: CodexAccountStore, temp_home: Path
):
    _seed_two_accounts(store, temp_home)
    good = {"seven_day": {"pct": 40.0}}
    with patch("claude_swap.providers.codex.fetch_usage", return_value=good):
        store.collect_usage()

    with patch(
        "claude_swap.providers.codex.fetch_usage", side_effect=TimeoutError()
    ):
        entries = store.collect_usage(force=True)

    assert entries["1"].last_good == good
    assert entries["1"].last_error == "timeout"


def test_collect_usage_refreshes_an_expired_token_first(
    store: CodexAccountStore, temp_home: Path
):
    _write_live(
        temp_home,
        make_codex_auth(email="x@example.com", account_id="acc-x", access_expires_in=-5),
    )
    store.add_current()

    fresh = make_codex_auth(email="x@example.com", account_id="acc-x")
    from claude_swap.oauth import RefreshOutcome

    with patch(
        "claude_swap.providers.codex.try_refresh",
        return_value=RefreshOutcome(fresh, None),
    ) as refresh:
        with patch(
            "claude_swap.providers.codex.fetch_usage",
            return_value={"seven_day": {"pct": 1.0}},
        ):
            store.collect_usage(force=True)

    assert refresh.call_count == 1
    assert store.read_credential("1", "x@example.com") == fresh


def test_collect_usage_marks_a_dead_refresh_lineage(
    store: CodexAccountStore, temp_home: Path
):
    _write_live(
        temp_home,
        make_codex_auth(email="d@example.com", account_id="acc-d", access_expires_in=-5),
    )
    store.add_current()
    from claude_swap.oauth import RefreshOutcome

    with patch(
        "claude_swap.providers.codex.try_refresh",
        return_value=RefreshOutcome(None, "invalid_grant"),
    ):
        entries = store.collect_usage(force=True)

    assert entries["1"].sentinel == "token expired"


def test_usage_entries_are_returned_for_accounts_never_fetched(
    store: CodexAccountStore, temp_home: Path
):
    _seed_two_accounts(store, temp_home)
    entries = store.usage_entries()
    assert set(entries) == {"1", "2"}
    assert all(isinstance(e, UsageEntry) for e in entries.values())
    assert entries["1"].last_good is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_codex_store.py -q`
Expected: FAIL, `AttributeError: 'CodexAccountStore' object has no attribute 'collect_usage'`

- [ ] **Step 3: Write the implementation**

Append to `CodexAccountStore` in `src/claude_swap/codex_store.py`:

```python
    # -- usage --------------------------------------------------------------

    def _identities(self) -> dict[str, tuple[str, str]]:
        """Slot -> ``(email, accountId)``, the identity ``UsageStore`` guards on."""
        return {
            slot: (str(record.get("email", "")), str(record.get("accountId", "")))
            for slot, record in self.accounts()
        }

    def _usage_store(self) -> UsageStore:
        self.ensure_dirs()
        return UsageStore(self.cache_dir)

    def usage_entries(self) -> dict[str, UsageEntry]:
        """Stored usage per slot, with no network call."""
        return self._usage_store().entries(self._identities())

    def collect_usage(self, force: bool = False) -> dict[str, UsageEntry]:
        """Fetch usage for every account whose row is due, then return them all.

        Refreshes an expired access token first and persists the rotated
        credential, because a refresh that is not written back is lost work and
        the next poll would repeat it. A dead refresh lineage is surfaced as the
        ``token expired`` sentinel rather than a fetch error, so the UI can say
        what the user must actually do.
        """
        usage_store = self._usage_store()
        identities = self._identities()
        entries = usage_store.entries(identities)
        now = time.time()

        outcomes: dict[str, FetchRecord] = {}
        sentinels: dict[str, str] = {}

        for slot, (email, _account_id) in identities.items():
            entry = entries.get(slot)
            if not force and entry is not None and entry.fresh(now):
                continue

            blob = self.read_credential(slot, email)
            if not blob:
                continue

            if codex.is_expired(blob):
                refreshed = codex.try_refresh(blob)
                if refreshed.error is not None:
                    if refreshed.error in ("invalid_grant", "no_refresh_token"):
                        sentinels[slot] = "token expired"
                    continue
                blob = refreshed.credentials
                self.write_credential(slot, email, blob)

            try:
                usage = codex.fetch_usage(blob)
            except Exception as exc:  # noqa: BLE001 - classified just below
                kind, retry_after = classify_usage_error(exc)
                outcomes[slot] = FetchRecord(error=kind, retry_after_s=retry_after)
                continue
            outcomes[slot] = FetchRecord(usage=usage)

        if outcomes:
            usage_store.record(outcomes, identities)

        merged = usage_store.entries(identities)
        for slot, sentinel in sentinels.items():
            merged[slot] = with_sentinel(merged.get(slot, UsageEntry()), sentinel)
        return merged
```

`UsageStore.record` takes whole batches — ``record(outcomes: dict[slot, FetchRecord],
identities: dict[slot, Identity])`` — so every slot is written in one fenced transaction. Do not
call it once per slot.

Add to the module's top-level imports:

```python
import time

from claude_swap.oauth import _classify_usage_error as classify_usage_error
from claude_swap.usage_store import FetchRecord, UsageEntry, UsageStore, with_sentinel
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_codex_store.py -q`
Expected: PASS, 25 passed

- [ ] **Step 5: Commit**

```bash
git add src/claude_swap/codex_store.py tests/test_codex_store.py
git commit -m "feat(codex): collect and store per-account usage"
```

---

## Task 11: The `cswap codex` command

**Files:**
- Create: `src/claude_swap/codex_cli.py`
- Modify: `src/claude_swap/cli.py`
- Test: `tests/test_codex_cli.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_codex_cli.py`:

```python
"""The cswap codex command surface."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import cli
from claude_swap.codex_cli import codex_command
from claude_swap.codex_store import CodexAccountStore
from tests.providers.conftest import make_codex_auth


def _write_live(temp_home: Path, blob: str) -> None:
    live = temp_home / ".codex" / "auth.json"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text(blob, encoding="utf-8")


def test_list_on_an_empty_pool_explains_how_to_start(temp_home: Path, capsys):
    codex_command(["list"])
    assert "No Codex accounts" in capsys.readouterr().out


def test_add_then_list_shows_the_account(temp_home: Path, capsys):
    _write_live(temp_home, make_codex_auth(email="cli@example.com", plan="pro"))
    codex_command(["add"])
    capsys.readouterr()

    codex_command(["list"])
    out = capsys.readouterr().out
    assert "cli@example.com" in out
    assert "pro" in out


def test_list_marks_the_active_account(temp_home: Path, capsys):
    _write_live(temp_home, make_codex_auth(email="one@example.com", account_id="acc-1"))
    codex_command(["add"])
    _write_live(temp_home, make_codex_auth(email="two@example.com", account_id="acc-2"))
    codex_command(["add"])
    capsys.readouterr()

    codex_command(["list"])
    lines = [ln for ln in capsys.readouterr().out.splitlines() if "@example.com" in ln]
    active = [ln for ln in lines if "*" in ln]
    assert len(active) == 1
    assert "two@example.com" in active[0]


def test_switch_changes_the_live_credential(temp_home: Path, capsys):
    _write_live(temp_home, make_codex_auth(email="a@example.com", account_id="acc-a"))
    codex_command(["add"])
    _write_live(temp_home, make_codex_auth(email="b@example.com", account_id="acc-b"))
    codex_command(["add"])
    capsys.readouterr()

    codex_command(["switch", "1"])

    live = json.loads((temp_home / ".codex" / "auth.json").read_text())
    assert live["tokens"]["account_id"] == "acc-a"
    assert "a@example.com" in capsys.readouterr().out


def test_switch_to_an_unknown_account_exits_nonzero(temp_home: Path, capsys):
    with pytest.raises(SystemExit) as exc:
        codex_command(["switch", "nope"])
    assert exc.value.code == 1
    assert "No Codex account" in capsys.readouterr().err


def test_add_without_a_login_exits_nonzero(temp_home: Path, capsys):
    with pytest.raises(SystemExit) as exc:
        codex_command(["add"])
    assert exc.value.code == 1
    assert "codex login" in capsys.readouterr().err


def test_remove_asks_for_confirmation_and_yes_skips_it(temp_home: Path, capsys):
    _write_live(temp_home, make_codex_auth(email="rm@example.com"))
    codex_command(["add"])
    capsys.readouterr()

    codex_command(["remove", "1", "--yes"])
    assert CodexAccountStore().accounts() == []


def test_status_names_the_active_account(temp_home: Path, capsys):
    _write_live(temp_home, make_codex_auth(email="st@example.com"))
    codex_command(["add"])
    capsys.readouterr()

    codex_command(["status"])
    assert "st@example.com" in capsys.readouterr().out


def test_list_json_is_machine_readable(temp_home: Path, capsys):
    _write_live(temp_home, make_codex_auth(email="j@example.com", account_id="acc-j"))
    codex_command(["add"])
    capsys.readouterr()

    codex_command(["list", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["provider"] == "codex"
    assert payload["accounts"][0]["email"] == "j@example.com"
    assert payload["accounts"][0]["slot"] == "1"
    assert payload["accounts"][0]["active"] is True


def test_main_dispatches_the_codex_namespace(temp_home: Path, monkeypatch):
    called = {}
    monkeypatch.setattr(
        "claude_swap.codex_cli.codex_command", lambda argv: called.setdefault("argv", argv)
    )
    monkeypatch.setattr("sys.argv", ["cswap", "codex", "list"])
    cli.main()
    assert called["argv"] == ["list"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_codex_cli.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'claude_swap.codex_cli'`

- [ ] **Step 3: Write the implementation**

Create `src/claude_swap/codex_cli.py`:

```python
"""``cswap codex <sub>`` — the Codex account pool's command surface.

A separate namespace, not new flags on the existing commands: the two pools are
independent in Phase 1, and a namespace keeps the diff against upstream's
``cli.py`` to the three dispatch lines in ``main()``.
"""

from __future__ import annotations

import argparse
import json
import sys

from claude_swap import printer
from claude_swap.codex_store import CodexAccountStore
from claude_swap.oauth import account_headroom, fresh_reset_strings


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cswap codex",
        usage="%(prog)s <command> [args]",
        description="""Manage OpenAI Codex (ChatGPT subscription) accounts.

Commands:
  %(prog)s list                 list managed Codex accounts
  %(prog)s status               show the active Codex account
  %(prog)s add                  add the account currently in ~/.codex/auth.json
  %(prog)s switch <num|email>   make an account live
  %(prog)s remove <num|email>   forget an account""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subs = parser.add_subparsers(dest="command", required=True)

    p_list = subs.add_parser("list", aliases=["ls"])
    p_list.add_argument("--json", action="store_true", help="machine-readable output")
    p_list.add_argument(
        "--no-usage", action="store_true", help="skip the usage fetch"
    )

    subs.add_parser("status")
    subs.add_parser("add")

    p_switch = subs.add_parser("switch")
    p_switch.add_argument("account", metavar="NUM|EMAIL")

    p_remove = subs.add_parser("remove", aliases=["rm"])
    p_remove.add_argument("account", metavar="NUM|EMAIL")
    p_remove.add_argument(
        "--yes", "-y", action="store_true", help="skip the confirmation"
    )
    return parser


def _usage_summary(entry) -> str:
    """One-line quota summary for a usage entry."""
    if entry is None:
        return "-"
    if entry.sentinel:
        return entry.sentinel
    usage = entry.last_good
    if not usage:
        return "-"
    parts = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        window = usage.get(key)
        if not isinstance(window, dict):
            continue
        text = f"{label} {window['pct']:.0f}%"
        reset = fresh_reset_strings(window)
        if reset:
            text += f" (resets {reset[0]})"
        parts.append(text)
    return "  ".join(parts) if parts else "-"


def _cmd_list(store: CodexAccountStore, args) -> None:
    rows = store.accounts()
    active = store.active_number()

    if args.json:
        entries = store.usage_entries()
        print(json.dumps({
            "provider": "codex",
            "activeSlot": active,
            "accounts": [
                {
                    "slot": slot,
                    "email": record.get("email", ""),
                    "accountId": record.get("accountId", ""),
                    "organizationName": record.get("organizationName", ""),
                    "plan": record.get("plan", ""),
                    "active": slot == active,
                    "usage": entries[slot].last_good if slot in entries else None,
                    "headroom": account_headroom(
                        entries[slot].last_good if slot in entries else None
                    ),
                }
                for slot, record in rows
            ],
        }, indent=2))
        return

    if not rows:
        print("No Codex accounts yet.")
        print("Run 'codex login', then 'cswap codex add'.")
        return

    entries = {} if args.no_usage else store.collect_usage()
    print(printer.bolded("Codex accounts"))
    for slot, record in rows:
        marker = "*" if slot == active else " "
        plan = record.get("plan") or "?"
        org = record.get("organizationName") or "personal"
        label = f"{record.get('email', '')} [{org}]"
        print(
            f" {marker} {slot}. {label}  {printer.muted(plan)}  "
            f"{_usage_summary(entries.get(slot))}"
        )


def _cmd_status(store: CodexAccountStore) -> None:
    active = store.active_number()
    if active is None:
        print("No active Codex account.")
        return
    record = dict(store.accounts()).get(active, {})
    print(f"Active Codex account: {active}. {record.get('email', '')}")
    print(f"Credential file: {store.live_path()}")


def _cmd_add(store: CodexAccountStore) -> None:
    slot, ident = store.add_current()
    print(f"Added Codex account {slot}: {ident.display_label} ({ident.plan or '?'})")


def _cmd_switch(store: CodexAccountStore, args) -> None:
    slot, email = store.switch(args.account)
    print(f"Switched to Codex account {slot}: {email}")


def _cmd_remove(store: CodexAccountStore, args) -> None:
    slot, record = store.resolve(args.account)
    email = record.get("email", "")
    if not args.yes:
        answer = input(f"Remove Codex account {slot} ({email})? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Cancelled.")
            return
    store.remove(args.account)
    print(f"Removed Codex account {slot}: {email}")


def codex_command(argv: list[str]) -> None:
    """Entry point for ``cswap codex``. Exits 1 on any expected failure."""
    args = _build_parser().parse_args(argv)
    store = CodexAccountStore()
    try:
        if args.command in ("list", "ls"):
            _cmd_list(store, args)
        elif args.command == "status":
            _cmd_status(store)
        elif args.command == "add":
            _cmd_add(store)
        elif args.command == "switch":
            _cmd_switch(store, args)
        elif args.command in ("remove", "rm"):
            _cmd_remove(store, args)
    except ValueError as exc:
        printer.error(str(exc))
        sys.exit(1)
```

Then modify `src/claude_swap/cli.py`. In `main()`, after the last pre-dispatch namespace block
(`move`, which ends around line 891) and before the bare-`cswap` TUI comment, add:

```python
    if argv and argv[0] == "codex":
        from claude_swap.codex_cli import codex_command

        codex_command(argv[1:])
        return
```

And add one line to the `description` block of the parser, after the `cswap auto` line:

```
  %(prog)s codex <cmd>                manage Codex (ChatGPT) accounts
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_codex_cli.py -q`
Expected: PASS, 10 passed

- [ ] **Step 5: Run the whole suite to prove Claude behaviour is unchanged**

Run: `uv run pytest -q`
Expected: PASS. The pre-existing count is 1827 collected; the new tests add to it and
nothing previously passing may fail.

- [ ] **Step 6: Commit**

```bash
git add src/claude_swap/codex_cli.py src/claude_swap/cli.py tests/test_codex_cli.py
git commit -m "feat(codex): add the cswap codex command"
```

---

## Task 12: Verify the refresh grant against a real spare account

This is the one part of the design that no unit test can confirm. It is a manual task and it must
not be skipped, because the mocked tests in Task 5 prove only that the code does what we assumed
the endpoint does.

**Do not run this against the primary Codex account.** The grant rotates the refresh token.

- [ ] **Step 1: Prepare an isolated second account**

```bash
mkdir -p ~/tmp/codex-spare
CODEX_HOME=~/tmp/codex-spare codex login
cp ~/tmp/codex-spare/auth.json ~/tmp/codex-spare/auth.json.backup
```

- [ ] **Step 2: Drive the adapter's refresh once, out of band**

```bash
cd ~/code/cswap && uv run python - <<'PY'
import json, pathlib
from claude_swap.providers import codex

path = pathlib.Path.home() / "tmp/codex-spare/auth.json"
blob = path.read_text()
outcome = codex.try_refresh(blob)
print("error:", outcome.error)
print("token_account:", outcome.token_account)
if outcome.credentials:
    old = json.loads(blob)["tokens"]
    new = json.loads(outcome.credentials)["tokens"]
    print("access rotated:", old["access_token"] != new["access_token"])
    print("refresh rotated:", old["refresh_token"] != new["refresh_token"])
    path.write_text(outcome.credentials)
PY
```

Expected: `error: None`, a `token_account` naming the spare account's email, and
`access rotated: True`.

- [ ] **Step 3: Prove the rotated credential still works**

```bash
CODEX_HOME=~/tmp/codex-spare codex exec "reply with the single word ok"
```

Expected: the command completes without an auth prompt. If it demands a login, restore
`auth.json.backup`, and record in the plan that the grant body needs a different `scope` or
`Content-Type` (form-encoded rather than JSON) before Task 5's implementation can ship.

- [ ] **Step 4: Record the outcome**

Append a short "Refresh verification" section to
`docs/design/2026-08-08-multi-provider-design.md` stating the date, the result, and whether the
refresh token rotated. Risk 2 in that document says the flow is unverified; this is what closes it.

```bash
git add docs/design/2026-08-08-multi-provider-design.md
git commit -m "docs: record the Codex refresh grant verification"
```

---

## Task 13: Documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/design/2026-08-08-multi-provider-design.md`

- [ ] **Step 1: Add a README section**

Insert after the existing usage section:

```markdown
## Codex accounts

`cswap` also manages OpenAI Codex (ChatGPT subscription) accounts, as a pool independent of the
Claude ones. Their state lives under `<backup root>/providers/codex/` and nothing about the Claude
accounts changes.

```bash
codex login          # log in as the account you want to add
cswap codex add      # capture it into a slot
cswap codex list     # every Codex account, with live quota
cswap codex switch 2 # make slot 2 the live ~/.codex/auth.json
cswap codex status
cswap codex remove 2
```

Quota comes from the same rate-limit windows the Codex CLI itself reports, so the 5-hour and
weekly percentages line up with what `/status` shows inside Codex.

Not yet supported for Codex accounts: `run` profiles, aliases, directory mappings, autoswitch, the
TUI dashboard, and macOS Keychain storage.
```

- [ ] **Step 2: Mark Phase 1 done in the design doc**

Change the Phasing section's Phase 1 paragraph to name what actually shipped, and add a line
recording that the `providers/claude.py` adapter was deliberately not written in Phase 1, with the
merge-cost reason.

- [ ] **Step 3: Commit**

```bash
git add README.md docs/design/2026-08-08-multi-provider-design.md
git commit -m "docs: document the Codex account pool"
```

---

## Task 14: Confirm the upstream merge cost is still near zero

- [ ] **Step 1: Measure the diff against upstream in the files upstream owns**

```bash
cd ~/code/cswap
git fetch upstream
git diff upstream/main --stat -- src/claude_swap/switcher.py src/claude_swap/credentials.py src/claude_swap/oauth.py src/claude_swap/usage_store.py
```

Expected: no output at all. Those four files must be untouched. Any change there is a defect in
this plan's execution, not an acceptable cost.

```bash
git diff upstream/main --stat -- src/claude_swap/cli.py src/claude_swap/paths.py
```

Expected: `cli.py` about 6 lines added, `paths.py` about 30 lines added, no deletions in either.

- [ ] **Step 2: Rebase cleanly onto current upstream**

```bash
git rebase upstream/main
uv run pytest -q
```

Expected: the rebase applies with no conflicts, and the suite passes.

- [ ] **Step 3: Commit nothing**

This task produces no commit. It is the gate that proves the "additive, track upstream" decision
held.

---

## Definition of done

- [ ] `uv run pytest -q` passes, with at least 1827 pre-existing tests still passing.
- [ ] `git diff upstream/main --stat -- src/claude_swap/switcher.py` prints nothing.
- [ ] `cswap codex add` captures the live account, and `cswap codex list` shows its real 5h and 7d
      percentages next to what `codex` itself reports.
- [ ] `cswap codex switch <n>` swaps `~/.codex/auth.json`, and `codex exec "say ok"` afterwards runs
      as the account that was switched to.
- [ ] The refresh grant is verified against a real spare account, and the result is recorded in the
      design doc.
- [ ] `cswap list`, `cswap switch`, `cswap tui` and `cswap auto` behave exactly as before.
