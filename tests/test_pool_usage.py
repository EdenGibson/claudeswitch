"""Codex usage flows through the shared collect pass."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import paths
from claude_swap.codex_store import CodexAccountStore
from claude_swap.providers.base import RefreshOutcome
from claude_swap.switcher import ClaudeAccountSwitcher
from tests.providers.conftest import make_codex_auth

CODEX_EMAIL = "codex@example.com"
CODEX_ID = "acc-codex"


@pytest.fixture
def pool(temp_home: Path) -> ClaudeAccountSwitcher:
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


def _collect(pool: ClaudeAccountSwitcher):
    return pool._collect_usage_entries(pool._build_accounts_info())


def test_a_codex_slot_fetches_through_the_codex_provider(pool):
    usage = {"seven_day": {"pct": 12.0}}
    with patch(
        "claude_swap.providers.codex.fetch_usage", return_value=usage
    ) as fetch:
        entries = _collect(pool)
    assert fetch.call_count == 1
    assert entries["2"].last_good["seven_day"]["pct"] == 12.0


def test_a_codex_fetch_failure_becomes_an_error_not_an_exception(pool):
    with patch(
        "claude_swap.providers.codex.fetch_usage", side_effect=OSError("network down")
    ):
        entries = _collect(pool)
    assert entries["2"].last_good is None


def test_a_codex_slot_never_reports_a_claude_credential_sentinel(pool):
    """A Codex blob has no claudeAiOauth key; that is not "no credentials"."""
    with patch("claude_swap.providers.codex.fetch_usage", return_value={}):
        entries = _collect(pool)
    assert entries["2"].sentinel is None


def test_an_expired_codex_token_refreshes_both_copies(pool):
    """OpenAI refresh tokens are single use, so every holder must be updated."""
    rotated = make_codex_auth(
        email=CODEX_EMAIL, account_id=CODEX_ID, refresh_token="ROTATED"
    )
    with patch("claude_swap.providers.codex.is_expired", return_value=True), patch(
        "claude_swap.providers.codex.try_refresh",
        return_value=RefreshOutcome(rotated, None),
    ), patch("claude_swap.providers.codex.fetch_usage", return_value={}):
        _collect(pool)

    store = CodexAccountStore()
    assert json.loads(store.read_credential("2", CODEX_EMAIL))["tokens"][
        "refresh_token"
    ] == "ROTATED"
    assert json.loads(store.read_live())["tokens"]["refresh_token"] == "ROTATED"


def test_a_refresh_never_overwrites_a_foreign_login(pool):
    """A `codex login` for another account must survive our refresh."""
    stranger = make_codex_auth(email="other@example.com", account_id="acc-other")
    CodexAccountStore().write_live(stranger)
    rotated = make_codex_auth(
        email=CODEX_EMAIL, account_id=CODEX_ID, refresh_token="ROTATED"
    )
    with patch("claude_swap.providers.codex.is_expired", return_value=True), patch(
        "claude_swap.providers.codex.try_refresh",
        return_value=RefreshOutcome(rotated, None),
    ), patch("claude_swap.providers.codex.fetch_usage", return_value={}):
        _collect(pool)

    assert CodexAccountStore().read_live() == stranger


def test_a_dead_refresh_lineage_becomes_the_token_expired_sentinel(pool):
    with patch("claude_swap.providers.codex.is_expired", return_value=True), patch(
        "claude_swap.providers.codex.try_refresh",
        return_value=RefreshOutcome(None, "invalid_grant"),
    ):
        entries = _collect(pool)
    assert entries["2"].sentinel is not None


def test_the_claude_slot_is_unaffected(pool):
    """Adding a Codex slot must not change how a Claude slot is collected."""
    with patch("claude_swap.providers.codex.fetch_usage", return_value={}), patch(
        "claude_swap.providers.codex.is_expired", return_value=False
    ):
        entries = _collect(pool)
    assert "1" in entries
