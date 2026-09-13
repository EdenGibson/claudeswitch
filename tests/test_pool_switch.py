"""Switching a Codex account through the main commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_swap import paths
from claude_swap.codex_store import CodexAccountStore
from claude_swap.exceptions import ConfigError
from claude_swap.switcher import ClaudeAccountSwitcher
from tests.providers.conftest import make_codex_auth

A_EMAIL, A_ID = "one@example.com", "acc-one"
B_EMAIL, B_ID = "two@example.com", "acc-two"


@pytest.fixture
def pool(temp_home: Path) -> ClaudeAccountSwitcher:
    """Claude in slot 1, two Codex accounts in slots 2 and 3; 2 is live."""
    root = paths.get_backup_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "sequence.json").write_text(
        json.dumps(
            {
                "activeAccountNumber": 1,
                "activeProviderAccounts": {"codex": "2"},
                "sequence": [1, 2, 3],
                "accounts": {
                    "1": {"email": "claude@example.com", "uuid": "u-1",
                          "organizationUuid": "", "organizationName": ""},
                    "2": {"email": A_EMAIL, "uuid": A_ID, "organizationUuid": "",
                          "organizationName": "", "provider": "codex"},
                    "3": {"email": B_EMAIL, "uuid": B_ID, "organizationUuid": "",
                          "organizationName": "", "provider": "codex"},
                },
            }
        ),
        encoding="utf-8",
    )
    store = CodexAccountStore()
    store.write_credential("2", A_EMAIL, make_codex_auth(email=A_EMAIL, account_id=A_ID))
    store.write_credential("3", B_EMAIL, make_codex_auth(email=B_EMAIL, account_id=B_ID))
    store.write_live(make_codex_auth(email=A_EMAIL, account_id=A_ID))
    return ClaudeAccountSwitcher()


def _live_account_id() -> str:
    return json.loads(CodexAccountStore().read_live())["tokens"]["account_id"]


def test_switching_to_a_codex_slot_changes_the_codex_credential(pool):
    pool.switch_to("3")
    assert _live_account_id() == B_ID


def test_switching_codex_leaves_the_claude_active_account_alone(pool):
    before = json.loads((paths.get_backup_root() / "sequence.json").read_text())
    pool.switch_to("3")
    after = json.loads((paths.get_backup_root() / "sequence.json").read_text())
    assert after["activeAccountNumber"] == before["activeAccountNumber"]


def test_the_registry_records_the_new_active_codex_slot(pool):
    pool.switch_to("3")
    data = json.loads((paths.get_backup_root() / "sequence.json").read_text())
    assert data["activeProviderAccounts"]["codex"] == "3"


def test_switching_by_email_works(pool):
    pool.switch_to(B_EMAIL)
    assert _live_account_id() == B_ID


def test_json_output_names_the_provider(pool):
    result = pool.switch_to("3", json_output=True)
    assert result["switched"] is True
    assert result["provider"] == "codex"


def test_switch_reports_live_failures_without_undoing_file_switch(pool, monkeypatch):
    report = {'updated': 1, 'failed': [{'pid': 20, 'status': 'failed'}],
              'unsupported': [], 'warnings': ['One server rejected the account']}
    monkeypatch.setattr('claude_swap.codex_live.sync_live', lambda: report)
    result = pool.switch_to('3', json_output=True)
    assert _live_account_id() == B_ID
    assert result['liveSessions'] == report
    assert result['warnings'] == report['warnings']


def test_switch_to_selected_account_still_syncs_live_servers(pool, monkeypatch):
    calls = []

    def sync():
        calls.append(_live_account_id())
        return {'updated': 1, 'failed': [], 'unsupported': [], 'warnings': []}

    monkeypatch.setattr('claude_swap.codex_live.sync_live', sync)
    pool.switch_to('2')
    assert calls == [A_ID]


def test_a_codex_slot_with_no_stored_credential_errors(pool):
    CodexAccountStore().delete_credential("3", B_EMAIL)
    with pytest.raises(ConfigError, match="No stored credential"):
        pool.switch_to("3")


def test_switching_to_the_live_slot_keeps_the_rotated_token(pool):
    """Regression for c0b8fbe: never write a stale blob over a rotated one.

    The Codex CLI rotates the token in place. OpenAI refresh tokens are single
    use, so restoring the stored copy would hand back a spent token.
    """
    rotated = make_codex_auth(
        email=A_EMAIL, account_id=A_ID, refresh_token="ROTATED"
    )
    CodexAccountStore().write_live(rotated)

    pool.switch_to("2")

    store = CodexAccountStore()
    assert json.loads(store.read_live())["tokens"]["refresh_token"] == "ROTATED"
    assert json.loads(store.read_credential("2", A_EMAIL))["tokens"][
        "refresh_token"
    ] == "ROTATED"


def test_switching_away_and_back_preserves_the_rotation(pool):
    """The full c0b8fbe path: rotate, leave, return."""
    rotated = make_codex_auth(
        email=A_EMAIL, account_id=A_ID, refresh_token="ROTATED"
    )
    CodexAccountStore().write_live(rotated)

    pool.switch_to("3")
    pool.switch_to("2")

    assert json.loads(CodexAccountStore().read_live())["tokens"][
        "refresh_token"
    ] == "ROTATED"


def test_a_codex_slot_is_switchable(pool):
    assert pool._account_is_switchable("2") is True


def test_bare_rotation_never_lands_on_a_codex_slot(pool):
    """Claude Code cannot read a Codex credential, so rotation must not offer one."""
    assert "2" not in pool.switchable_account_numbers()
    assert "3" not in pool.switchable_account_numbers()
