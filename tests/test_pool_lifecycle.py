"""Adding and removing a Codex account through the main commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_swap import paths
from claude_swap.codex_store import CodexAccountStore
from claude_swap.exceptions import ConfigError
from claude_swap.switcher import ClaudeAccountSwitcher
from tests.providers.conftest import make_codex_auth

CODEX_EMAIL = "codex@example.com"
CODEX_ID = "acc-codex"


def _registry() -> dict:
    return json.loads((paths.get_backup_root() / "sequence.json").read_text())


def _write_registry(data: dict) -> None:
    root = paths.get_backup_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "sequence.json").write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def claude_only(temp_home: Path) -> ClaudeAccountSwitcher:
    """One Claude account in slot 1 and nothing else."""
    _write_registry(
        {
            "activeAccountNumber": 1,
            "sequence": [1],
            "accounts": {
                "1": {
                    "email": "claude@example.com",
                    "uuid": "u-1",
                    "organizationUuid": "",
                    "organizationName": "",
                },
            },
        }
    )
    return ClaudeAccountSwitcher()


@pytest.fixture
def with_codex(claude_only: ClaudeAccountSwitcher) -> ClaudeAccountSwitcher:
    """Slot 1 Claude, slot 2 Codex and live."""
    data = _registry()
    data["sequence"] = [1, 2]
    data["accounts"]["2"] = {
        "email": CODEX_EMAIL,
        "uuid": CODEX_ID,
        "organizationUuid": "",
        "organizationName": "",
        "provider": "codex",
    }
    data["activeProviderAccounts"] = {"codex": "2"}
    _write_registry(data)
    blob = make_codex_auth(email=CODEX_EMAIL, account_id=CODEX_ID)
    store = CodexAccountStore()
    store.write_credential("2", CODEX_EMAIL, blob)
    store.write_live(blob)
    return ClaudeAccountSwitcher()


def test_add_captures_the_live_codex_login(claude_only: ClaudeAccountSwitcher):
    blob = make_codex_auth(email=CODEX_EMAIL, account_id=CODEX_ID)
    CodexAccountStore().write_live(blob)

    slot, label = claude_only.add_provider_account("codex")

    assert slot == "2"
    assert label.startswith(CODEX_EMAIL)
    assert CodexAccountStore().read_credential("2", CODEX_EMAIL) == blob


def test_add_records_the_provider_in_the_registry(claude_only: ClaudeAccountSwitcher):
    CodexAccountStore().write_live(
        make_codex_auth(email=CODEX_EMAIL, account_id=CODEX_ID)
    )

    claude_only.add_provider_account("codex")

    data = _registry()
    assert data["accounts"]["2"]["provider"] == "codex"
    assert data["sequence"] == [1, 2]
    assert data["activeProviderAccounts"]["codex"] == "2"


def test_add_leaves_the_claude_active_account_alone(
    claude_only: ClaudeAccountSwitcher,
):
    CodexAccountStore().write_live(
        make_codex_auth(email=CODEX_EMAIL, account_id=CODEX_ID)
    )

    claude_only.add_provider_account("codex")

    assert _registry()["activeAccountNumber"] == 1


def test_add_with_no_codex_login_errors(claude_only: ClaudeAccountSwitcher):
    with pytest.raises(ConfigError, match="No codex credential"):
        claude_only.add_provider_account("codex")


def test_add_refuses_a_duplicate_codex_account(with_codex: ClaudeAccountSwitcher):
    with pytest.raises(ConfigError, match="already managed"):
        with_codex.add_provider_account("codex")


def test_two_codex_accounts_on_one_email_are_both_addable(
    with_codex: ClaudeAccountSwitcher,
):
    """Duplicate detection keys on the account id, never the email.

    One person can hold two ChatGPT accounts on the same address.
    """
    CodexAccountStore().write_live(
        make_codex_auth(email=CODEX_EMAIL, account_id="acc-second")
    )

    slot, _ = with_codex.add_provider_account("codex")

    assert _registry()["accounts"][slot]["uuid"] == "acc-second"


def test_add_honours_an_explicit_slot(claude_only: ClaudeAccountSwitcher):
    CodexAccountStore().write_live(
        make_codex_auth(email=CODEX_EMAIL, account_id=CODEX_ID)
    )

    slot, _ = claude_only.add_provider_account("codex", slot=7)

    assert slot == "7"
    assert _registry()["sequence"] == [1, 7]


def test_add_refuses_a_taken_slot(claude_only: ClaudeAccountSwitcher):
    CodexAccountStore().write_live(
        make_codex_auth(email=CODEX_EMAIL, account_id=CODEX_ID)
    )

    with pytest.raises(ConfigError, match="already taken"):
        claude_only.add_provider_account("codex", slot=1)


def test_remove_deletes_the_codex_credential(with_codex: ClaudeAccountSwitcher):
    path = CodexAccountStore().credential_path("2", CODEX_EMAIL)
    assert path.exists()

    with_codex.remove_account("2", assume_yes=True)

    assert not path.exists()
    assert "2" not in _registry()["accounts"]


def test_remove_clears_the_active_codex_pointer(with_codex: ClaudeAccountSwitcher):
    with_codex.remove_account("2", assume_yes=True)

    assert "codex" not in _registry().get("activeProviderAccounts", {})


def test_remove_leaves_the_live_codex_login_alone(with_codex: ClaudeAccountSwitcher):
    """Removing a slot drops cswap's copy, never the user's current login."""
    live = CodexAccountStore().read_live()

    with_codex.remove_account("2", assume_yes=True)

    assert CodexAccountStore().read_live() == live


def test_remove_by_email_works(with_codex: ClaudeAccountSwitcher):
    with_codex.remove_account(CODEX_EMAIL, assume_yes=True)

    assert "2" not in _registry()["accounts"]


def test_removing_a_codex_slot_leaves_the_claude_slot_intact(
    with_codex: ClaudeAccountSwitcher,
):
    with_codex.remove_account("2", assume_yes=True)

    data = _registry()
    assert "1" in data["accounts"]
    assert data["activeAccountNumber"] == 1
