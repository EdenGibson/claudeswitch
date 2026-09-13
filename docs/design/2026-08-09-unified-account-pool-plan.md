# Unified Account Pool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Codex accounts become full members of the one account pool, so every cswap command and every UI surface treats them the same as Claude accounts.

**Architecture:** `sequence.json` becomes the single registry for both providers. Each account entry gains an optional `"provider"` key; absent means `"claude"`, so existing data reads unchanged. Credential blobs stay in their own store — Claude keeps keychain/file backups, Codex keeps `codex/`. A new `provider_ops.py` dispatch layer routes the six credential operations to the right backend, so `switcher.py` branches in a small number of named places instead of growing a second implementation.

**Tech Stack:** Python 3.12, argparse, textual (TUI), pytest with xdist.

---

## Decisions this plan locks in

**1. "Active" is per provider, not global.**

Claude Code reads `~/.claude/.credentials.json`. The Codex CLI reads `~/.codex/auth.json`. They are separate backends. One global active pointer would mean switching to a Codex account leaves Claude Code with no credential, which is wrong. So `sequence.json` keeps `activeAccountNumber` for Claude and gains `activeProviderAccounts` for the rest. `cswap list` prints one active marker per provider.

**2. Cross-provider auto-rotation is out of scope, and is blocked on Phase 2.**

`cswap auto` rotates to the account with the most headroom. Rotating from a Claude account to a Codex account does not redirect Claude Code anywhere — Claude Code cannot read a Codex credential. That redirect is exactly what the Phase 2 router (CLIProxyAPI) provides. Until Phase 2 lands, `auto` rotates within the active account's provider and says so when it skips a cross-provider candidate.

**3. Downgrade hazard, recorded not fixed.**

Upstream cswap 0.24.1 does not know the `provider` key. If a user reverts to upstream while Codex slots sit in `sequence.json`, upstream reads those slots as Claude accounts with missing credentials. Task 17 documents `cswap remove` before downgrading. No code guards against it.

---

## File Structure

**New files**

| File | Responsibility |
|---|---|
| `src/claude_swap/provider_ops.py` | The dispatch layer: a `ProviderOps` protocol, `ClaudeOps`, `CodexOps`, and `ops_for(provider)`. |
| `src/claude_swap/pool_migration.py` | One idempotent migration that folds the standalone Codex store into `sequence.json`. |
| `tests/test_provider_ops.py` | Dispatch layer. |
| `tests/test_pool_migration.py` | Migration, including re-runs. |
| `tests/test_pool_list.py` | `list`, `status`, `--json` with both providers present. |
| `tests/test_pool_switch.py` | `switch` dispatch, rotation, strategy. |
| `tests/test_pool_lifecycle.py` | `add`, `remove`, `swap`, `move`, `export`, `import`. |
| `tests/test_pool_tui.py` | TUI rows and switch dispatch. |

**Modified upstream files** — this breaks Phase 1's zero-deletion property. Expected shape:

| File | Nature of change |
|---|---|
| `src/claude_swap/models.py` | Add `provider` to `AccountInfo` and `AccountSnapshot`. Additive. |
| `src/claude_swap/switcher.py` | Branch in named places only: `_build_accounts_info`, `_collect_usage_entries`, `_perform_switch` entry, `add_account`, `remove_account`, `_relocate_locked`, `_swap_accounts_locked`, `list_accounts`, `status`, `_build_list_payload`. |
| `src/claude_swap/autoswitch.py` | Filter candidates to the active provider. |
| `src/claude_swap/cli.py` | `--provider` on `add`, provider-aware `run`. |
| `src/claude_swap/tui/widgets.py` | Provider tag in three renderers. |
| `src/claude_swap/tui/dashboard.py` | Switch action dispatch. |
| `src/claude_swap/transfer.py` | Export/import carries provider and Codex blobs. |
| `src/claude_swap/codex_cli.py` | Becomes a thin alias over the unified commands. |

**Deleted:** nothing. `codex_store.py` stays as the Codex credential backend; only its numbering authority moves to `sequence.json`.

---

## Task 1: Provider on the account model

**Files:**
- Modify: `src/claude_swap/models.py:78-120` (`AccountInfo`), `src/claude_swap/models.py:123-148` (`AccountSnapshot`)
- Test: `tests/test_pool_models.py` (create)

- [ ] **Step 1: Write the failing tests**

```python
"""Provider identity on the shared account models."""

from __future__ import annotations

from claude_swap.models import AccountInfo


def test_account_info_defaults_to_claude():
    """An entry written by upstream has no provider key."""
    info = AccountInfo.from_dict(1, {"email": "a@example.com"})
    assert info.provider == "claude"


def test_account_info_reads_the_provider_key():
    info = AccountInfo.from_dict(9, {"email": "a@example.com", "provider": "codex"})
    assert info.provider == "codex"


def test_claude_accounts_round_trip_without_a_provider_key():
    """Never write the default back — upstream must still read our file."""
    info = AccountInfo.from_dict(1, {"email": "a@example.com"})
    assert "provider" not in info.to_dict()


def test_codex_accounts_round_trip_with_a_provider_key():
    info = AccountInfo.from_dict(9, {"email": "a@example.com", "provider": "codex"})
    assert info.to_dict()["provider"] == "codex"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_pool_models.py -q`
Expected: FAIL, `AttributeError: 'AccountInfo' object has no attribute 'provider'`

- [ ] **Step 3: Implement**

In `src/claude_swap/models.py`, add the field to `AccountInfo` after `number`:

```python
    number: int
    #: Which credential backend owns this account. Absent from the stored
    #: entry means "claude" — upstream cswap wrote it and knows no other
    #: provider, so the default must stay the silent one.
    provider: str = "claude"
```

In `from_dict`, add:

```python
            provider=data.get("provider") or "claude",
```

In `to_dict`, append before the return:

```python
        payload = {
            "email": self.email,
            "uuid": self.uuid,
            "organizationUuid": self.organization_uuid,
            "organizationName": self.organization_name,
            "added": self.added,
        }
        # Only non-default providers are written. A Claude entry stays
        # byte-identical to what upstream produces.
        if self.provider != "claude":
            payload["provider"] = self.provider
        return payload
```

Add the same field to `AccountSnapshot`, after `kind`:

```python
    provider: str = "claude"
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_pool_models.py -q`
Expected: 4 passed

- [ ] **Step 5: Run the full suite for regressions**

Run: `uv run pytest -q`
Expected: no new failures against the pre-task baseline

- [ ] **Step 6: Commit**

```bash
git add src/claude_swap/models.py tests/test_pool_models.py
git commit -m "feat(pool): carry a provider on the shared account models"
```

---

## Task 2: The provider dispatch layer

**Files:**
- Create: `src/claude_swap/provider_ops.py`
- Test: `tests/test_provider_ops.py`

This is the seam the rest of the plan depends on. Every later task calls `ops_for(provider)` rather than branching on a string.

- [ ] **Step 1: Write the failing tests**

```python
"""The provider dispatch layer."""

from __future__ import annotations

import pytest

from claude_swap.provider_ops import ClaudeOps, CodexOps, ops_for


def test_ops_for_claude_returns_claude_ops():
    assert isinstance(ops_for("claude"), ClaudeOps)


def test_ops_for_codex_returns_codex_ops():
    assert isinstance(ops_for("codex"), CodexOps)


def test_ops_for_an_unknown_provider_raises():
    with pytest.raises(ValueError, match="unknown provider"):
        ops_for("gemini")


def test_ops_for_treats_none_as_claude():
    """A stored entry with no provider key is a Claude account."""
    assert isinstance(ops_for(None), ClaudeOps)


def test_every_ops_declares_its_live_path(temp_home):
    assert ops_for("claude").live_path().name == ".credentials.json"
    assert ops_for("codex").live_path().name == "auth.json"


def test_every_ops_names_the_binary_it_launches():
    assert ops_for("claude").binary == "claude"
    assert ops_for("codex").binary == "codex"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_provider_ops.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'claude_swap.provider_ops'`

- [ ] **Step 3: Implement**

```python
"""Route credential operations to the backend that owns them.

``sequence.json`` is one registry for accounts of every provider, but the
credential itself lives wherever its CLI expects it: Claude Code reads
``~/.claude/.credentials.json``, the Codex CLI reads ``~/.codex/auth.json``.
This module is the one place that knows which is which. Callers ask for
``ops_for(account.provider)`` and never test the string themselves.

Nothing here holds state. An ops object is cheap to build per call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from claude_swap import paths
from claude_swap.providers import AccountIdentity


@runtime_checkable
class ProviderOps(Protocol):
    """The operations a provider must supply to join the pool."""

    name: str
    #: The executable ``cswap run`` launches for this provider.
    binary: str
    #: Human label for the provider column.
    label: str

    def live_path(self) -> Path:
        """Where this provider's CLI keeps the credential it is using now."""

    def read_live(self) -> str | None:
        """The live credential blob, or None when nobody is logged in."""

    def write_live(self, blob: str) -> None:
        """Make ``blob`` the credential this provider's CLI will use."""

    def identity(self, blob: str) -> AccountIdentity | None:
        """Who ``blob`` belongs to, or None when it cannot be read."""

    def fetch_usage(self, blob: str) -> dict | None:
        """Normalized usage for ``blob``: {five_hour, seven_day, scoped, spend}."""


class ClaudeOps:
    """Claude Code credentials. Delegates to the switcher's existing paths."""

    name = "claude"
    binary = "claude"
    label = "claude"

    def live_path(self) -> Path:
        return paths.get_credentials_path()

    def read_live(self) -> str | None:
        path = self.live_path()
        return path.read_text(encoding="utf-8") if path.exists() else None

    def write_live(self, blob: str) -> None:
        # The Claude path must keep going through the switcher, which owns
        # the keychain fallback and the rollback record. Callers that hold a
        # switcher use it directly; this exists only so the protocol is total.
        raise NotImplementedError(
            "Claude activation goes through ClaudeAccountSwitcher._write_credentials"
        )

    def identity(self, blob: str) -> AccountIdentity | None:
        """Claude identity needs a network call, so this reads the profile.

        ``oauth`` has no offline identity reader — the OAuth token carries no
        email claim. ``fetch_oauth_profile`` is the only source, so callers
        that already hold the registry entry must prefer it and never call
        this on a hot path.
        """
        from claude_swap import oauth

        token = oauth.extract_access_token(blob)
        if not token:
            return None
        profile = oauth.fetch_oauth_profile(token)
        if not profile:
            return None
        # fetch_oauth_profile answers only uuid/email/organizationUuid, and
        # the last two may be None. org_name and plan are not available from
        # this endpoint; the registry entry holds them.
        return AccountIdentity(
            email=profile.get("email") or "",
            account_uuid=profile.get("uuid") or "",
            org_uuid=profile.get("organizationUuid") or "",
            org_name="",
            plan="",
        )

    def fetch_usage(self, blob: str) -> dict | None:
        """``oauth.fetch_usage`` takes an access token, not a blob."""
        from claude_swap import oauth

        token = oauth.extract_access_token(blob)
        return oauth.fetch_usage(token) if token else None


class CodexOps:
    """Codex (ChatGPT) credentials."""

    name = "codex"
    binary = "codex"
    label = "codex"

    def live_path(self) -> Path:
        return paths.get_codex_auth_path()

    def read_live(self) -> str | None:
        path = self.live_path()
        return path.read_text(encoding="utf-8") if path.exists() else None

    def write_live(self, blob: str) -> None:
        from claude_swap.codex_store import CodexAccountStore

        CodexAccountStore().write_live(blob)

    def identity(self, blob: str) -> AccountIdentity | None:
        from claude_swap.providers import codex

        return codex.identity(blob)

    def fetch_usage(self, blob: str) -> dict | None:
        from claude_swap.providers import codex

        return codex.fetch_usage(blob)


_OPS: dict[str, ProviderOps] = {"claude": ClaudeOps(), "codex": CodexOps()}


def ops_for(provider: str | None) -> ProviderOps:
    """The ops for ``provider``; None or empty means the Claude default."""
    key = provider or "claude"
    try:
        return _OPS[key]
    except KeyError:
        raise ValueError(f"unknown provider: {key!r}") from None


def provider_names() -> tuple[str, ...]:
    """Every provider the pool can hold, Claude first."""
    return ("claude", "codex")
```

**Two asymmetries the implementer must not smooth over.**

`oauth.fetch_usage` takes an access token; `codex.fetch_usage` takes the whole blob. `ClaudeOps` extracts the token, `CodexOps` does not. Do not change either provider module to match the other — `codex.fetch_usage` needs the blob so it can refresh.

`ClaudeOps.write_live` raises. Claude activation carries keychain fallback, a config backup, and a rollback record that only `ClaudeAccountSwitcher` owns. `CodexOps.write_live` is a file write and needs none of it. Any caller that activates a Claude account already holds a switcher, so the protocol member exists for shape only.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_provider_ops.py -q`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/claude_swap/provider_ops.py tests/test_provider_ops.py
git commit -m "feat(pool): add the provider dispatch layer"
```

---

## Task 3: Fold the Codex store into the registry

**Files:**
- Create: `src/claude_swap/pool_migration.py`
- Modify: `src/claude_swap/switcher.py:2125-2140` (`_get_sequence_data_migrated`)
- Test: `tests/test_pool_migration.py`

The Codex store currently keeps its own `sequence.json` with its own numbering starting at 1. Those numbers collide with Claude slots. This migration moves each Codex account into the main registry at a fresh slot, renames its credential file to match, and marks the Codex store migrated so it never runs twice.

- [ ] **Step 1: Write the failing tests**

```python
"""Folding the standalone Codex store into the main registry."""

from __future__ import annotations

import json

from claude_swap.codex_store import CodexAccountStore
from claude_swap.pool_migration import migrate_codex_into_pool
from tests.providers.conftest import make_codex_auth


def _seq(temp_home):
    from claude_swap import paths

    return paths.get_backup_root() / "sequence.json"


def test_a_codex_account_lands_at_a_fresh_slot(temp_home):
    store = CodexAccountStore()
    store.write_live(make_codex_auth(email="c@example.com", account_id="acc-c"))
    store.add_from_live()

    seq_path = _seq(temp_home)
    seq_path.parent.mkdir(parents=True, exist_ok=True)
    seq_path.write_text(json.dumps({
        "activeAccountNumber": 1,
        "sequence": [1],
        "accounts": {"1": {"email": "a@example.com"}},
    }))

    migrate_codex_into_pool()

    data = json.loads(seq_path.read_text())
    assert data["sequence"] == [1, 2]
    assert data["accounts"]["2"]["email"] == "c@example.com"
    assert data["accounts"]["2"]["provider"] == "codex"
    # The Claude slot is untouched, including its lack of a provider key.
    assert "provider" not in data["accounts"]["1"]


def test_the_migration_is_idempotent(temp_home):
    store = CodexAccountStore()
    store.write_live(make_codex_auth(email="c@example.com", account_id="acc-c"))
    store.add_from_live()

    migrate_codex_into_pool()
    first = json.loads(_seq(temp_home).read_text())
    migrate_codex_into_pool()
    second = json.loads(_seq(temp_home).read_text())

    assert first == second


def test_the_credential_survives_the_renumber(temp_home):
    store = CodexAccountStore()
    blob = make_codex_auth(email="c@example.com", account_id="acc-c")
    store.write_live(blob)
    store.add_from_live()

    migrate_codex_into_pool()

    data = json.loads(_seq(temp_home).read_text())
    slot = next(n for n, a in data["accounts"].items() if a.get("provider") == "codex")
    assert store.read_credential(slot, "c@example.com") == blob


def test_an_empty_codex_store_changes_nothing(temp_home):
    seq_path = _seq(temp_home)
    seq_path.parent.mkdir(parents=True, exist_ok=True)
    seq_path.write_text(json.dumps({
        "activeAccountNumber": 1, "sequence": [1],
        "accounts": {"1": {"email": "a@example.com"}},
    }))
    before = seq_path.read_text()

    migrate_codex_into_pool()

    assert seq_path.read_text() == before


def test_the_active_codex_account_becomes_the_active_provider_entry(temp_home):
    store = CodexAccountStore()
    store.write_live(make_codex_auth(email="c@example.com", account_id="acc-c"))
    store.add_from_live()

    migrate_codex_into_pool()

    data = json.loads(_seq(temp_home).read_text())
    slot = next(n for n, a in data["accounts"].items() if a.get("provider") == "codex")
    assert data["activeProviderAccounts"]["codex"] == slot
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_pool_migration.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'claude_swap.pool_migration'`

- [ ] **Step 3: Implement**

Write `migrate_codex_into_pool()` so it:

1. Takes the main registry's `FileLock` for the whole operation.
2. Reads the Codex store's own sequence. Returns immediately when it holds no accounts, or when it already carries `"migrated": true`.
3. For each Codex slot, in its own order: allocates the next free main-registry slot, writes an account entry with `email`, `uuid` (the Codex `account_id`), `organizationName`, `added`, and `provider: "codex"`.
4. Renames the credential file from the Codex-local slot to the new slot with `replace_with_retry`.
5. Records `activeProviderAccounts["codex"]` from the Codex store's own `activeAccountNumber`.
6. Writes the main registry with `atomic_write_json`, then marks the Codex sequence `{"migrated": true}` — in that order, so a crash between the two leaves the migration re-runnable rather than lost.
7. Never touches `activeAccountNumber`.

Call it from `_get_sequence_data_migrated`, beside `_migrate_org_fields`, so it runs on first read of any command.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_pool_migration.py -q`
Expected: 5 passed

- [ ] **Step 5: Verify against the real store, on a copy**

```bash
cp -r ~/.local/share/claude-swap /tmp/cswap-migration-check
XDG_DATA_HOME=/tmp/cswap-migration-check-xdg uv run cswap list
```

Expected: the Codex account appears with a slot number after the Claude accounts.

- [ ] **Step 6: Commit**

```bash
git add src/claude_swap/pool_migration.py src/claude_swap/switcher.py tests/test_pool_migration.py
git commit -m "feat(pool): fold the codex store into the main registry"
```

---

## Task 4: Build the account list across providers

**Files:**
- Modify: `src/claude_swap/switcher.py:2723-2762` (`_build_accounts_info`)
- Test: `tests/test_pool_list.py`

`_build_accounts_info` currently returns a 7-tuple and reads Claude credentials for every slot. It must read each slot through its own provider, and detect the active slot per provider.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_list_holds_accounts_from_both_providers(pool_with_both):
    infos = pool_with_both._build_accounts_info()
    providers = {info[-1] for info in infos}
    assert providers == {"claude", "codex"}


def test_each_provider_marks_its_own_active_slot(pool_with_both):
    infos = pool_with_both._build_accounts_info()
    active = [i for i in infos if i[4]]
    assert len(active) == 2
    assert {i[-1] for i in active} == {"claude", "codex"}


def test_a_codex_slot_reads_its_credential_from_the_codex_store(pool_with_both):
    infos = pool_with_both._build_accounts_info()
    codex_row = next(i for i in infos if i[-1] == "codex")
    assert '"tokens"' in codex_row[5]


def test_a_missing_codex_credential_yields_an_empty_blob_not_a_crash(pool_missing_codex_cred):
    infos = pool_missing_codex_cred._build_accounts_info()
    codex_row = next(i for i in infos if i[-1] == "codex")
    assert codex_row[5] == ""
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_pool_list.py -q`
Expected: FAIL, tuple has no provider element

- [ ] **Step 3: Implement**

Append `provider` as an eighth tuple element — appending rather than inserting keeps every existing unpack site working, so this stays a small diff.

Replace the credential read inside the loop:

```python
            provider = account.get("provider") or "claude"
            ops = provider_ops.ops_for(provider)
            is_active = str(num) == active_for.get(provider)

            if is_active:
                if provider == "claude":
                    active = self._read_active_credentials()
                    creds = active.value or ""
                    self._active_keychain_unavailable = active.keychain_unavailable
                else:
                    creds = ops.read_live() or ""
            elif provider == "claude":
                creds = self._read_account_credentials(str(num), email)
            else:
                creds = self._read_provider_credentials(provider, str(num), email)
```

Build `active_for` before the loop: `{"claude": <existing detection>, "codex": <from activeProviderAccounts, confirmed against the live blob's identity>}`. Confirming against the live blob matters — a `codex login` run outside cswap changes the live file without touching the registry.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_pool_list.py -q`
Expected: 4 passed

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: no new failures

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat(pool): build the account list across both providers"
```

---

## Task 5: Collect usage across providers

**Files:**
- Modify: `src/claude_swap/switcher.py:3482-3594` (`_collect_usage_entries`), `:3386-3465` (`_fetch_account_usage`)
- Test: `tests/test_pool_usage.py`

Codex usage must flow through the same `UsageStore`, the same TTL, and the same pacing as Claude usage, so the TUI and `list` need no special case. `providers/codex.py` already normalizes to `{five_hour, seven_day, scoped, spend}`.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_codex_slot_fetches_through_the_codex_provider(pool_with_both, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "claude_swap.providers.codex.fetch_usage",
        lambda blob: calls.append(blob) or {"seven_day": {"pct": 12.0}},
    )
    entries = pool_with_both._collect_usage_entries(pool_with_both._build_accounts_info())
    codex_slot = ...  # the codex slot number from the fixture
    assert len(calls) == 1
    assert entries[codex_slot].last_good["seven_day"]["pct"] == 12.0


def test_codex_usage_is_written_to_the_shared_store(pool_with_both, monkeypatch):
    ...


def test_a_codex_fetch_failure_becomes_a_sentinel_not_an_exception(pool_with_both, monkeypatch):
    monkeypatch.setattr(
        "claude_swap.providers.codex.fetch_usage",
        lambda blob: (_ for _ in ()).throw(OSError("network down")),
    )
    entries = pool_with_both._collect_usage_entries(pool_with_both._build_accounts_info())
    codex_slot = ...
    assert entries[codex_slot].sentinel is not None


def test_an_expired_codex_token_refreshes_the_live_file_too(pool_with_both, monkeypatch):
    """The single-use refresh token rule from Phase 1 still holds here."""
    ...
```

**Warning for the implementer:** OpenAI refresh tokens are single use. When the refreshed slot is the active Codex slot, write the new blob to **both** the stored copy and `~/.codex/auth.json`, guarded by an identity check. Phase 1 shipped two Critical bugs from getting this wrong — see commits `c0b8fbe` and `66b35b7` and do not repeat them.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_pool_usage.py -q`

- [ ] **Step 3: Implement**

Branch inside `_fetch_account_usage` on the provider, calling `ops.fetch_usage`. Keep every store write, sentinel, pacing decision, and lock exactly as they are — only the fetch call changes.

- [ ] **Step 4: Run to verify it passes**

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(pool): collect codex usage through the shared store"
```

---

## Task 6: Active per provider

**Files:**
- Modify: `src/claude_swap/switcher.py:1657-1673` (`current_account_number`), `:1843-1866`
- Test: `tests/test_pool_active.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_current_account_number_defaults_to_claude(pool_with_both):
    assert pool_with_both.current_account_number() == "1"


def test_current_account_number_answers_per_provider(pool_with_both):
    assert pool_with_both.current_account_number(provider="codex") == "9"


def test_switching_claude_leaves_the_codex_active_pointer_alone(pool_with_both):
    before = pool_with_both.current_account_number(provider="codex")
    pool_with_both.switch_to("2")
    assert pool_with_both.current_account_number(provider="codex") == before


def test_a_codex_login_outside_cswap_is_detected(pool_with_both, foreign_codex_login):
    """The registry pointer must lose to the live file's real identity."""
    assert pool_with_both.current_account_number(provider="codex") is None
```

- [ ] **Step 2-6:** As the pattern above. `current_account_number` gains a keyword-only `provider: str = "claude"`. Claude behaviour is byte-identical when the argument is omitted.

```bash
git commit -m "feat(pool): track the active account per provider"
```

---

## Task 7: `list` renders the provider

**Files:**
- Modify: `src/claude_swap/switcher.py:3921-4020` (`list_accounts`)
- Test: `tests/test_pool_list.py`

Target output:

```
Accounts:
  1: you@example.com [.. Organization] (active)
     ├ 5h:  63%  resets 05:40         in 1h 57m
     ├ 7d:  52%  resets Aug 14 18:00  in 5d 14h  (ahead of pace)
     └ Fable: 15%  resets Aug 14 17:59  in 5d 14h

  9: user@example.com [Personal] (codex) (active)
     └ 7d:   0%  resets Aug 16 03:16  in 6d 23h
```

Rules:
- A Claude row is unchanged, to the byte. Anyone diffing against upstream output must see no difference when no Codex account exists.
- A Codex row carries a `(codex)` tag after the org tag.
- `(active)` appears once per provider.
- A Codex row omits the 5h line when the plan has no `secondary_window` — do not print an empty bar.

- [ ] **Steps:** test the exact strings, implement, full suite, commit.

```bash
git commit -m "feat(pool): show the provider in cswap list"
```

---

## Task 8: `status` reports both providers

**Files:**
- Modify: `src/claude_swap/switcher.py:4037-4129`
- Test: `tests/test_pool_list.py`

`cswap status` prints the active Claude account first, then one line per other provider that has an active account. With no Codex account, output is unchanged.

```bash
git commit -m "feat(pool): report every provider's active account in status"
```

---

## Task 9: `--json` carries the provider

**Files:**
- Modify: `src/claude_swap/switcher.py:3875-3920` (`_build_list_payload`), `src/claude_swap/json_output.py`
- Test: `tests/test_pool_json.py`

Keep `schemaVersion: 1`. Add a `"provider"` string to each account object, and `"activeProviderAccounts"` beside `"activeAccountNumber"`. Both are additive, so an existing consumer keeps working.

- [ ] Test that a pool with no Codex account produces a payload byte-identical to the pre-change one.

```bash
git commit -m "feat(pool): add the provider to the JSON payload"
```

---

## Task 10: `switch` dispatches by provider

**Files:**
- Modify: `src/claude_swap/switcher.py:4208-4598` (`switch`), `:5024-5536` (`_perform_switch`)
- Test: `tests/test_pool_switch.py`

The Claude switch path is 500 lines of keychain, session-dir, config-backup and rollback ceremony. None of it applies to Codex. Branch at the top of `_perform_switch` and hand a Codex target to a short dedicated path that reuses `CodexAccountStore.switch_to`.

Rules:
- `cswap switch 9` switches the Codex credential and leaves Claude alone.
- Bare `cswap switch` rotates within the *active Claude* provider, unchanged.
- `cswap switch --strategy best` considers only accounts of the same provider as the current active one. When it skips cross-provider candidates it says so once.
- Switching to the already-active Codex slot must recapture the live blob first. This is the Phase 1 Critical bug `c0b8fbe`; re-test it here.

- [ ] **Required regression test:**

```python
def test_switching_to_the_live_codex_slot_keeps_the_rotated_token(pool_with_both):
    """Regression for c0b8fbe: never write a stale blob over a rotated one."""
    ...
```

```bash
git commit -m "feat(pool): dispatch switch by provider"
```

---

## Task 11: `add` and `remove` across providers

**Files:**
- Modify: `src/claude_swap/switcher.py:2200-2428` (`add_account`), `:2636-2722` (`remove_account`), `src/claude_swap/cli.py`
- Test: `tests/test_pool_lifecycle.py`

- `cswap add --provider codex` captures `~/.codex/auth.json` into a new slot.
- Bare `cswap add` keeps meaning Claude.
- `cswap remove 9` deletes the Codex credential and its registry entry, and clears `activeProviderAccounts["codex"]` when it pointed there.
- Duplicate detection for Codex keys on the token's `account_id`, not the email — one person can hold two ChatGPT accounts on the same address.

```bash
git commit -m "feat(pool): add and remove codex accounts through the main commands"
```

---

## Task 12: `swap` and `move` relocate provider credentials

**Files:**
- Modify: `src/claude_swap/switcher.py:842-979` (`_swap_accounts_locked`), `:1265-1375` (`_relocate_locked`)
- Test: `tests/test_pool_lifecycle.py`

Both rewrite slot numbers and rename credential files. A Codex slot's file lives in the Codex store, so the rename must go through `CodexOps`. Cross-provider swaps are allowed — the slot number is just a label.

- [ ] Test a Claude/Codex swap, then read both credentials back and assert they followed their accounts.

```bash
git commit -m "feat(pool): move codex credentials with their slots"
```

---

## Task 13: `run` and `map` launch the right binary

**Files:**
- Modify: `src/claude_swap/cli.py:100-...` (`_run_command`)
- Test: `tests/test_pool_run.py`

`cswap run 9` must exec `codex`, not `claude`, with `CODEX_HOME` pointed at a per-slot directory so the session is isolated the way the Claude path isolates with `CLAUDE_CONFIG_DIR`. `cswap map` needs no change — it stores a slot number — but `cswap run` in a mapped directory must pick the binary from the mapped slot's provider.

- [ ] Test with a mocked `os.execvp` that asserts the binary name and the environment.

```bash
git commit -m "feat(pool): run codex accounts with the codex binary"
```

---

## Task 14: The TUI shows and switches Codex accounts

**Files:**
- Modify: `src/claude_swap/tui/widgets.py` (`account_card_text`, `mini_account_text`, `AccountItem`), `src/claude_swap/tui/dashboard.py` (switch action)
- Test: `tests/test_pool_tui.py`

- The row carries a dim `codex` tag where a Claude row carries nothing.
- The watch page shows a Codex row with only the windows it has.
- Enter on a Codex row switches that Codex account.
- The active marker appears on one row per provider.

**Warning:** `tests/test_tui.py::TestWatchScreen` is flaky under load on this repo, upstream included — measured 2 failures in `upstream/main`'s own suite at load average 97. Do not chase those failures as a regression. Confirm by running the same test file on `upstream/main` before reporting one.

```bash
git commit -m "feat(pool): show and switch codex accounts in the TUI"
```

---

## Task 15: `export` and `import` carry providers

**Files:**
- Modify: `src/claude_swap/transfer.py`
- Test: `tests/test_pool_lifecycle.py`

The archive must include each account's provider and its credential blob from the right store. Importing an archive that holds a Codex account onto a machine with no Codex login must work.

```bash
git commit -m "feat(pool): export and import codex accounts"
```

---

## Task 16: `auto` stays inside one provider

**Files:**
- Modify: `src/claude_swap/autoswitch.py:1702` (`_rank_candidates`)
- Test: `tests/test_pool_autoswitch.py`

Filter candidates to the active account's provider. When a higher-headroom candidate is excluded because it belongs to another provider, log one line naming it and the reason. Do not silently drop it — a silent drop looks like the ranking is wrong.

- [ ] Test that a Codex account with 0% used is not chosen over a Claude account at 90%.
- [ ] Test that the exclusion is logged once, not once per tick.

```bash
git commit -m "feat(pool): keep auto-rotation inside one provider"
```

---

## Task 17: Documentation

**Files:**
- Modify: `README.md`, `docs/design/2026-08-08-multi-provider-design.md`

Cover:
- The unified pool: one slot space, one set of commands.
- Active is per provider, and why.
- `auto` does not cross providers, and that Phase 2's router is what unblocks it.
- The downgrade hazard: remove Codex accounts before reverting to upstream cswap.
- `cswap codex ...` still works and is now an alias.

```bash
git commit -m "docs: describe the unified account pool"
```

---

## Verification gate

Before calling this done:

1. `uv run pytest -q` — no new failures against the recorded baseline.
2. `git diff upstream/main --numstat` — record the new deletion count. Phase 1's zero-deletion property is deliberately given up here; record the real number rather than claiming it held.
3. Against the real store, on a copy: `cswap list`, `cswap status`, `cswap list --json`, and the TUI all show slot 9 as Codex.
4. `cswap switch 9 && cswap switch 1 && cswap switch 9` then check `~/.codex/auth.json` still holds a working token. This is the Phase 1 Critical bug path.
5. Task 12 from the Phase 1 plan is **still open** — `codex.try_refresh` is verified against mocks only. Nothing in this plan closes it.
