"""What the TUI shows and does for a Codex account."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_swap import paths
from claude_swap.codex_store import CodexAccountStore
from claude_swap.models import UsageEntry
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.tui.theme import CSWAP_DARK, Palette
from claude_swap.tui.widgets import account_card_text, mini_account_text
from tests.providers.conftest import make_codex_auth

CODEX_EMAIL = "codex@example.com"
CODEX_ID = "acc-codex"


@pytest.fixture
def pool(temp_home: Path) -> ClaudeAccountSwitcher:
    """Slot 1 Claude, slot 2 Codex and live for Codex."""
    root = paths.get_backup_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "sequence.json").write_text(
        json.dumps(
            {
                "activeAccountNumber": 1,
                "activeProviderAccounts": {"codex": "2"},
                "sequence": [1, 2],
                "accounts": {
                    "1": {"email": "claude@example.com", "uuid": "u-1",
                          "organizationUuid": "", "organizationName": ""},
                    "2": {"email": CODEX_EMAIL, "uuid": CODEX_ID,
                          "organizationUuid": "", "organizationName": "",
                          "provider": "codex"},
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


def _snapshot(pool: ClaudeAccountSwitcher):
    return pool.accounts_snapshot(fetch=set())


def _by_number(snap):
    return {acc.number: acc for acc in snap.accounts}


def test_the_snapshot_names_each_row_s_provider(pool):
    rows = _by_number(_snapshot(pool))

    assert rows["1"].provider == "claude"
    assert rows["2"].provider == "codex"


def test_the_codex_row_is_marked_active(pool):
    """Each provider marks its own live account, independently of the other."""
    rows = _by_number(_snapshot(pool))

    assert rows["2"].is_active is True


def test_a_live_codex_account_never_claims_active_number(pool):
    """active_number drives the Claude switch UI, so only Claude may claim it.

    The fixture has no live Claude login, so nothing claims it here.
    """
    assert _snapshot(pool).active_number is None


def test_the_card_tags_a_codex_row(pool):
    rows = _by_number(_snapshot(pool))
    palette = Palette.from_theme(CSWAP_DARK)

    card = account_card_text(rows["2"], 80, threshold=None, now=0.0, palette=palette)

    assert "(codex)" in card.plain


def test_the_card_leaves_a_claude_row_untagged(pool):
    rows = _by_number(_snapshot(pool))
    palette = Palette.from_theme(CSWAP_DARK)

    card = account_card_text(rows["1"], 80, threshold=None, now=0.0, palette=palette)

    assert "(codex)" not in card.plain
    assert "(claude)" not in card.plain


def test_the_mini_line_tags_a_codex_row(pool):
    rows = _by_number(_snapshot(pool))
    palette = Palette.from_theme(CSWAP_DARK)

    line = mini_account_text(rows["2"], 0.0, palette=palette)

    assert "(codex)" in line.plain


def test_a_codex_row_is_a_switch_target(pool):
    rows = _by_number(_snapshot(pool))

    assert rows["2"].switchable is True


def test_the_switch_payload_the_tui_reads_names_the_account(pool):
    """do_switch renders payload['to']; a Codex switch must fill it."""
    payload = pool.switch_to("2", json_output=True)

    assert payload["switched"] is True
    assert payload["to"]["email"] == CODEX_EMAIL
    assert payload["provider"] == "codex"


def test_the_usage_entry_shape_is_unchanged_for_a_codex_row(pool):
    rows = _by_number(_snapshot(pool))

    assert isinstance(rows["2"].usage, UsageEntry)
