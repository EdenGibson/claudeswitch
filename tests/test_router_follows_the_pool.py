"""The router's mode file tracks the Codex slot it serves.

``cswap switch`` already has ``follow_switch``. Removing or renumbering an
account is the other half: the mode file names a slot number, and every path
that frees or moves that number has to say so, or the router goes on naming a
slot that is gone or now holds a different account.

The stakes are the same in both directions. CLIProxyAPI refreshes the Codex
login while it serves, and an OpenAI refresh token is single-use, so the copy
it rotated to is the only valid one. Losing track of it costs a browser
re-login.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from claude_swap import paths
from claude_swap.codex_store import CodexAccountStore
from claude_swap.router import cliproxy
from claude_swap.router import switching as router_switching
from claude_swap.router.mode import RouterMode, read_mode, write_mode
from claude_swap.switcher import ClaudeAccountSwitcher
from tests.providers.conftest import make_codex_auth

CODEX_EMAIL = "codex@example.com"
CODEX_ID = "acc-codex"
OTHER_EMAIL = "other@example.com"
OTHER_ID = "acc-other"


@pytest.fixture(autouse=True)
def _no_backend_process(monkeypatch) -> list[str]:
    """No systemctl, and a record of whether the backend was asked to stop."""
    calls: list[str] = []
    monkeypatch.setattr(cliproxy, "start", lambda: (True, "started (stubbed)"))
    monkeypatch.setattr(
        cliproxy, "stop", lambda: (calls.append("stop"), (True, "stopped"))[1]
    )
    monkeypatch.setattr(cliproxy, "unit_active", lambda: True)
    monkeypatch.setattr(cliproxy, "merge_back", lambda blob: None)
    return calls


@pytest.fixture
def pool(temp_home: Path) -> ClaudeAccountSwitcher:
    """Slot 1 Claude, slot 2 Codex (live), slot 3 a second Codex account."""
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
                    "2": {"email": CODEX_EMAIL, "uuid": CODEX_ID,
                          "organizationUuid": "", "organizationName": "",
                          "provider": "codex"},
                    "3": {"email": OTHER_EMAIL, "uuid": OTHER_ID,
                          "organizationUuid": "", "organizationName": "",
                          "provider": "codex"},
                },
            }
        ),
        encoding="utf-8",
    )
    store = CodexAccountStore()
    blob = make_codex_auth(email=CODEX_EMAIL, account_id=CODEX_ID)
    store.write_credential("2", CODEX_EMAIL, blob)
    store.write_live(blob)
    store.write_credential(
        "3", OTHER_EMAIL, make_codex_auth(email=OTHER_EMAIL, account_id=OTHER_ID)
    )
    return ClaudeAccountSwitcher()


def _serving(slot: str, *, pinned: bool = False) -> None:
    write_mode(RouterMode(provider="codex", slot=slot, pinned=pinned))


class TestRemove:
    def test_removing_the_served_slot_returns_the_backend_to_claude(self, pool):
        """The account is gone, so the router cannot keep naming it."""
        _serving("2")

        pool.remove_account("2", assume_yes=True)

        assert read_mode().provider == "claude"
        assert read_mode().slot is None

    def test_the_backend_is_stopped_before_the_credential_goes(
        self, pool, _no_backend_process
    ):
        """Left running, CLIProxyAPI rotates a token nothing is watching."""
        _serving("2")

        pool.remove_account("2", assume_yes=True)

        assert _no_backend_process == ["stop"]

    def test_removing_another_codex_slot_leaves_the_router_alone(self, pool):
        _serving("2")

        pool.remove_account("3", assume_yes=True)

        assert read_mode().provider == "codex"
        assert read_mode().slot == "2"

    def test_removing_a_claude_slot_leaves_the_router_alone(self, pool):
        _serving("2")

        pool.remove_account("1", assume_yes=True)

        assert read_mode().provider == "codex"
        assert read_mode().slot == "2"

    def test_a_claude_mode_router_is_untouched(self, pool, _no_backend_process):
        write_mode(RouterMode(provider="claude"))

        pool.remove_account("2", assume_yes=True)

        assert read_mode().provider == "claude"
        assert _no_backend_process == []

    def test_a_pin_cannot_outlive_the_account_it_pinned(self, pool):
        """'cswap backend codex 2' outranks the engine, but not a deletion.

        Slot 2 no longer exists, so a pin naming it would park the router on
        an account nothing can serve and refuse every later flip.
        """
        _serving("2", pinned=True)

        pool.remove_account("2", assume_yes=True)

        assert read_mode().provider == "claude"
        assert read_mode().pinned is False

    def test_the_report_says_the_router_moved(self, pool, capsys):
        _serving("2")

        pool.remove_account("2", assume_yes=True)

        assert "the router returned to Claude" in capsys.readouterr().out


class TestRenumber:
    def test_a_swap_takes_the_mode_file_to_the_new_number(self, pool):
        """The account did not change, only its number, and its credential
        moved with it. Following is the whole fix; falling back to Claude
        would be a heavier answer than the situation needs."""
        _serving("2")

        pool.swap_accounts("2", "3")

        assert read_mode().provider == "codex"
        assert read_mode().slot == "3"

    def test_a_swap_keeps_the_backend_running(self, pool, _no_backend_process):
        _serving("2")

        pool.swap_accounts("2", "3")

        assert _no_backend_process == []

    def test_a_swap_of_the_other_pair_leaves_the_mode_file_alone(self, pool):
        _serving("2")

        pool.swap_accounts("1", "3")

        assert read_mode().slot == "2"

    def test_a_swap_keeps_the_pin(self, pool):
        _serving("2", pinned=True)

        pool.swap_accounts("2", "3")

        assert read_mode().slot == "3"
        assert read_mode().pinned is True

    def test_a_move_to_an_empty_slot_takes_the_mode_file_with_it(self, pool):
        _serving("2")

        pool.move_account("2", "7")

        assert read_mode().provider == "codex"
        assert read_mode().slot == "7"

    def test_a_claude_mode_router_is_untouched_by_a_swap(self, pool):
        write_mode(RouterMode(provider="claude"))

        pool.swap_accounts("2", "3")

        assert read_mode().provider == "claude"
        assert read_mode().slot is None

    def test_the_mode_update_is_outside_the_swap_rollback(self, pool, monkeypatch):
        """sequence.json's write is the commit point, and nothing may undo it.

        Telling the router runs after that write. Inside the try it would sit
        under `except BaseException`, so a Ctrl-C between the two would roll
        both credentials back to a swap the registry had already published,
        leaving slot 2's record naming one account and its stored credential
        holding the other.
        """
        def interrupted(_moves):
            raise KeyboardInterrupt

        monkeypatch.setattr(router_switching, "follow_renumber", interrupted)
        _serving("2")

        with pytest.raises(KeyboardInterrupt):
            pool.swap_accounts("2", "3")

        registry = json.loads(
            (paths.get_backup_root() / "sequence.json").read_text()
        )
        assert registry["accounts"]["3"]["email"] == CODEX_EMAIL
        # The registry is committed either way. The credential is what the
        # rollback would undo, leaving slot 3's record naming an account whose
        # stored blob had been put back under slot 2.
        assert CodexAccountStore().read_credential("3", CODEX_EMAIL)

    def test_a_failed_mode_update_is_reported(self, pool, monkeypatch, caplog):
        """Silence here leaves the mode file naming another account's slot."""
        def broken(_mode, *_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(router_switching, "write_mode", broken)
        _serving("2")

        with caplog.at_level(logging.WARNING):
            pool.swap_accounts("2", "3")

        assert "router" in caplog.text.lower()
