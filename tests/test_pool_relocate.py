"""Swapping and moving slots that hold a Codex account."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_swap import paths
from claude_swap.codex_store import CodexAccountStore
from claude_swap.switcher import ClaudeAccountSwitcher
from tests.providers.conftest import make_codex_auth

A_EMAIL, A_ID = "one@example.com", "acc-one"
B_EMAIL, B_ID = "two@example.com", "acc-two"


def _registry() -> dict:
    return json.loads((paths.get_backup_root() / "sequence.json").read_text())


def _write_registry(data: dict) -> None:
    root = paths.get_backup_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "sequence.json").write_text(json.dumps(data), encoding="utf-8")


def _claude(email: str, uuid: str) -> dict:
    return {
        "email": email,
        "uuid": uuid,
        "organizationUuid": "",
        "organizationName": "",
    }


def _codex(email: str, uuid: str) -> dict:
    return {**_claude(email, uuid), "provider": "codex"}


@pytest.fixture
def pool(temp_home: Path) -> ClaudeAccountSwitcher:
    """Slot 1 Claude, slot 2 Codex (active for Codex)."""
    _write_registry(
        {
            "activeAccountNumber": 1,
            "activeProviderAccounts": {"codex": "2"},
            "sequence": [1, 2],
            "accounts": {
                "1": _claude("claude@example.com", "u-1"),
                "2": _codex(A_EMAIL, A_ID),
            },
        }
    )
    store = CodexAccountStore()
    store.write_credential("2", A_EMAIL, make_codex_auth(email=A_EMAIL, account_id=A_ID))
    return ClaudeAccountSwitcher()


def _cred(slot: str, email: str) -> str:
    return CodexAccountStore().read_credential(slot, email)


def test_move_carries_the_codex_credential_to_the_new_slot(pool):
    pool.move_account("2", "5")

    assert _cred("5", A_EMAIL) != ""
    assert _cred("2", A_EMAIL) == ""


def test_move_follows_the_active_codex_pointer(pool):
    pool.move_account("2", "5")

    assert _registry()["activeProviderAccounts"]["codex"] == "5"


def test_move_keeps_the_provider_on_the_record(pool):
    pool.move_account("2", "5")

    data = _registry()
    assert data["accounts"]["5"]["provider"] == "codex"
    assert data["sequence"] == [1, 5]


def test_swap_carries_the_codex_credential_with_its_account(pool):
    pool.swap_accounts("1", "2")

    assert _cred("1", A_EMAIL) != ""
    assert _cred("2", A_EMAIL) == ""


def test_swap_follows_the_active_codex_pointer(pool):
    pool.swap_accounts("1", "2")

    assert _registry()["activeProviderAccounts"]["codex"] == "1"


def test_swap_exchanges_two_codex_accounts(temp_home: Path):
    _write_registry(
        {
            "activeAccountNumber": None,
            "activeProviderAccounts": {"codex": "1"},
            "sequence": [1, 2],
            "accounts": {"1": _codex(A_EMAIL, A_ID), "2": _codex(B_EMAIL, B_ID)},
        }
    )
    store = CodexAccountStore()
    blob_a = make_codex_auth(email=A_EMAIL, account_id=A_ID)
    blob_b = make_codex_auth(email=B_EMAIL, account_id=B_ID)
    store.write_credential("1", A_EMAIL, blob_a)
    store.write_credential("2", B_EMAIL, blob_b)

    ClaudeAccountSwitcher().swap_accounts("1", "2")

    assert _cred("2", A_EMAIL) == blob_a
    assert _cred("1", B_EMAIL) == blob_b
    assert _registry()["activeProviderAccounts"]["codex"] == "2"


def test_a_same_email_swap_leaves_no_stale_codex_credential(temp_home: Path):
    """One email, two ChatGPT accounts: the keys overlap, so a clear is needed."""
    _write_registry(
        {
            "activeAccountNumber": None,
            "activeProviderAccounts": {"codex": "1"},
            "sequence": [1, 2],
            "accounts": {"1": _codex(A_EMAIL, A_ID), "2": _claude(A_EMAIL, "u-2")},
        }
    )
    blob = make_codex_auth(email=A_EMAIL, account_id=A_ID)
    CodexAccountStore().write_credential("1", A_EMAIL, blob)

    ClaudeAccountSwitcher().swap_accounts("1", "2")

    assert _cred("2", A_EMAIL) == blob
    assert _cred("1", A_EMAIL) == ""


def test_swapping_two_claude_slots_touches_no_codex_file(temp_home: Path):
    _write_registry(
        {
            "activeAccountNumber": 1,
            "sequence": [1, 2],
            "accounts": {"1": _claude("a@x.com", "u-1"), "2": _claude("b@x.com", "u-2")},
        }
    )

    ClaudeAccountSwitcher().swap_accounts("1", "2")

    assert "activeProviderAccounts" not in _registry()
