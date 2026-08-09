"""Building the account list across both providers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_swap import paths
from claude_swap.codex_store import CodexAccountStore
from claude_swap.switcher import ClaudeAccountSwitcher
from tests.providers.conftest import make_codex_auth

CODEX_EMAIL = "codex@example.com"
CODEX_ID = "acc-codex"


@pytest.fixture
def pool(temp_home: Path) -> ClaudeAccountSwitcher:
    """One Claude account in slot 1, one Codex account in slot 2."""
    root = paths.get_backup_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "sequence.json").write_text(
        json.dumps(
            {
                "activeAccountNumber": 1,
                "activeProviderAccounts": {"codex": "2"},
                "sequence": [1, 2],
                "accounts": {
                    "1": {
                        "email": "claude@example.com",
                        "uuid": "u-1",
                        "organizationUuid": "",
                        "organizationName": "",
                    },
                    "2": {
                        "email": CODEX_EMAIL,
                        "uuid": CODEX_ID,
                        "organizationUuid": "",
                        "organizationName": "",
                        "provider": "codex",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    blob = make_codex_auth(email=CODEX_EMAIL, account_id=CODEX_ID)
    store = CodexAccountStore()
    store.write_credential("2", CODEX_EMAIL, blob)
    store.write_live(blob)
    return ClaudeAccountSwitcher()


def test_the_list_holds_accounts_from_both_providers(pool: ClaudeAccountSwitcher):
    numbers = {str(info[0]) for info in pool._build_accounts_info()}
    assert numbers == {"1", "2"}
    assert pool.provider_of("1") == "claude"
    assert pool.provider_of("2") == "codex"


def test_an_unknown_slot_reads_as_claude(pool: ClaudeAccountSwitcher):
    assert pool.provider_of("99") == "claude"


def test_a_codex_slot_reads_its_credential_from_its_own_store(
    pool: ClaudeAccountSwitcher,
):
    info = {str(i[0]): i for i in pool._build_accounts_info()}
    assert '"tokens"' in info["2"][5]


def test_the_codex_slot_is_active_when_the_live_file_is_its_own(
    pool: ClaudeAccountSwitcher,
):
    assert pool.provider_active_number("codex") == "2"


def test_a_codex_login_outside_cswap_clears_the_active_slot(
    pool: ClaudeAccountSwitcher,
):
    """The registry pointer nominates; the live blob decides."""
    CodexAccountStore().write_live(
        make_codex_auth(email="stranger@example.com", account_id="acc-stranger")
    )
    assert pool.provider_active_number("codex") is None


def test_no_codex_login_at_all_is_not_an_error(temp_home: Path):
    assert ClaudeAccountSwitcher().provider_active_number("codex") is None


def test_active_by_provider_answers_every_provider(pool: ClaudeAccountSwitcher):
    active = pool.active_by_provider()
    assert set(active) == {"claude", "codex"}
    assert active["codex"] == "2"


def test_the_active_codex_slot_reads_the_live_file_not_the_stale_copy(
    pool: ClaudeAccountSwitcher,
):
    """The Codex CLI rotates its token in place; the stored copy goes stale.

    OpenAI refresh tokens are single use, so serving the stored copy for the
    active slot would hand out a spent token.
    """
    rotated = make_codex_auth(
        email=CODEX_EMAIL, account_id=CODEX_ID, refresh_token="ROTATED"
    )
    CodexAccountStore().write_live(rotated)
    info = {str(i[0]): i for i in pool._build_accounts_info()}
    assert info["2"][5] == rotated


def test_a_missing_codex_credential_yields_an_empty_blob_not_a_crash(
    pool: ClaudeAccountSwitcher,
):
    store = CodexAccountStore()
    store.delete_credential("2", CODEX_EMAIL)
    store.live_path().unlink()
    info = {str(i[0]): i for i in pool._build_accounts_info()}
    assert info["2"][5] == ""
