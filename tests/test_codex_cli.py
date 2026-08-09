"""`cswap codex <sub>` is an alias over the unified account commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_swap import cli
from claude_swap.codex_cli import codex_command, translate
from claude_swap.codex_store import CodexAccountStore
from tests.providers.conftest import make_codex_auth


def _write_live(temp_home: Path, blob: str) -> None:
    live = temp_home / ".codex" / "auth.json"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text(blob, encoding="utf-8")


def _registry(temp_home: Path) -> dict:
    from claude_swap import paths

    return json.loads((paths.get_backup_root() / "sequence.json").read_text())


class TestTranslate:
    """The rewrite table, checked without running a command."""

    def test_add_names_the_provider(self):
        assert translate(["add"]) == ["--add-account", "--provider", "codex"]

    def test_list_becomes_the_pool_list(self):
        assert translate(["list"]) == ["--list"]
        assert translate(["ls"]) == ["--list"]

    def test_status_becomes_the_pool_status(self):
        assert translate(["status"]) == ["--status"]

    def test_switch_carries_the_account(self):
        assert translate(["switch", "3"]) == ["--switch-to", "3"]

    def test_remove_carries_the_account(self):
        assert translate(["remove", "a@b.com"]) == ["--remove-account", "a@b.com"]
        assert translate(["rm", "3"]) == ["--remove-account", "3"]

    def test_extra_flags_pass_through(self):
        assert translate(["list", "--json"]) == ["--list", "--json"]

    def test_an_unknown_command_is_a_usage_error(self, capsys):
        with pytest.raises(SystemExit) as exc:
            translate(["frobnicate"])
        assert exc.value.code == 2
        assert "unknown command" in capsys.readouterr().err

    def test_switch_without_an_account_is_a_usage_error(self, capsys):
        with pytest.raises(SystemExit) as exc:
            translate(["switch"])
        assert exc.value.code == 2
        assert "needs an account" in capsys.readouterr().err

    def test_no_command_is_a_usage_error(self, capsys):
        with pytest.raises(SystemExit) as exc:
            translate([])
        assert exc.value.code == 2

    def test_help_prints_the_mapping(self, capsys):
        with pytest.raises(SystemExit) as exc:
            translate(["--help"])
        assert exc.value.code == 0
        assert "cswap add --provider codex" in capsys.readouterr().out


def test_add_puts_the_account_in_the_unified_registry(temp_home: Path, capsys):
    _write_live(temp_home, make_codex_auth(email="cli@example.com", account_id="acc-1"))

    codex_command(["add"])

    data = _registry(temp_home)
    slots = {n for n, a in data["accounts"].items() if a.get("provider") == "codex"}
    assert len(slots) == 1
    assert "cli@example.com" in capsys.readouterr().out


def test_list_shows_the_added_account(temp_home: Path, capsys):
    _write_live(temp_home, make_codex_auth(email="cli@example.com", account_id="acc-1"))
    codex_command(["add"])
    capsys.readouterr()

    codex_command(["list"])

    out = capsys.readouterr().out
    assert "cli@example.com" in out
    assert "(codex)" in out


def test_switch_changes_the_live_credential(temp_home: Path, capsys):
    _write_live(temp_home, make_codex_auth(email="a@example.com", account_id="acc-a"))
    codex_command(["add"])
    _write_live(temp_home, make_codex_auth(email="b@example.com", account_id="acc-b"))
    codex_command(["add"])
    capsys.readouterr()

    codex_command(["switch", "1"])

    live = json.loads((temp_home / ".codex" / "auth.json").read_text())
    assert live["tokens"]["account_id"] == "acc-a"


def test_add_without_a_login_exits_nonzero(temp_home: Path, capsys):
    with pytest.raises(SystemExit) as exc:
        codex_command(["add"])

    assert exc.value.code == 1
    assert "codex login" in capsys.readouterr().err


def test_remove_drops_the_account(temp_home: Path, capsys, monkeypatch):
    _write_live(temp_home, make_codex_auth(email="rm@example.com", account_id="acc-r"))
    codex_command(["add"])
    slot = next(
        n for n, a in _registry(temp_home)["accounts"].items()
        if a.get("provider") == "codex"
    )
    capsys.readouterr()
    monkeypatch.setattr("builtins.input", lambda *_: "y")

    codex_command(["remove", slot])

    assert _registry(temp_home)["accounts"] == {}
    assert CodexAccountStore().read_credential(slot, "rm@example.com") == ""


def test_list_json_carries_the_provider(temp_home: Path, capsys):
    _write_live(temp_home, make_codex_auth(email="j@example.com", account_id="acc-j"))
    codex_command(["add"])
    capsys.readouterr()

    codex_command(["list", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["accounts"][0]["provider"] == "codex"
    assert payload["accounts"][0]["email"] == "j@example.com"


def test_main_dispatches_the_codex_namespace(temp_home: Path, monkeypatch):
    called = {}
    monkeypatch.setattr(
        "claude_swap.codex_cli.codex_command",
        lambda argv: called.setdefault("argv", argv),
    )
    monkeypatch.setattr("sys.argv", ["cswap", "codex", "list"])
    cli.main()
    assert called["argv"] == ["list"]
