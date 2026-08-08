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

    codex_command(["list", "--no-usage"])
    out = capsys.readouterr().out
    assert "cli@example.com" in out
    assert "pro" in out


def test_list_marks_the_active_account(temp_home: Path, capsys):
    _write_live(temp_home, make_codex_auth(email="one@example.com", account_id="acc-1"))
    codex_command(["add"])
    _write_live(temp_home, make_codex_auth(email="two@example.com", account_id="acc-2"))
    codex_command(["add"])
    capsys.readouterr()

    codex_command(["list", "--no-usage"])
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


def test_remove_with_yes_skips_the_confirmation(temp_home: Path, capsys):
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

    codex_command(["list", "--json", "--no-usage"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["provider"] == "codex"
    assert payload["accounts"][0]["email"] == "j@example.com"
    assert payload["accounts"][0]["slot"] == "1"
    assert payload["accounts"][0]["active"] is True


def test_list_json_fetches_usage_unless_no_usage_is_given(temp_home: Path, capsys):
    """A scripting caller asking for usage must not get a stale cache."""
    _write_live(temp_home, make_codex_auth(email="f@example.com", account_id="acc-f"))
    codex_command(["add"])
    capsys.readouterr()

    usage = {"seven_day": {"pct": 55.0}}
    with patch("claude_swap.providers.codex.fetch_usage", return_value=usage) as fetch:
        codex_command(["list", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert fetch.call_count == 1
    assert payload["accounts"][0]["usage"] == usage
    assert payload["accounts"][0]["headroom"] == pytest.approx(45.0)


def test_list_json_with_no_usage_makes_no_network_call(temp_home: Path, capsys):
    _write_live(temp_home, make_codex_auth(email="n@example.com", account_id="acc-n"))
    codex_command(["add"])
    capsys.readouterr()

    with patch("claude_swap.providers.codex.fetch_usage") as fetch:
        codex_command(["list", "--json", "--no-usage"])

    assert fetch.call_count == 0


def test_list_survives_a_usage_window_with_no_pct(temp_home: Path, capsys):
    """A stored row from an older format must not crash the renderer."""
    _write_live(temp_home, make_codex_auth(email="p@example.com", account_id="acc-p"))
    codex_command(["add"])
    capsys.readouterr()

    with patch(
        "claude_swap.providers.codex.fetch_usage",
        return_value={"seven_day": {"resets_at": "2026-08-14T12:16:26+00:00"}},
    ):
        codex_command(["list"])

    assert "p@example.com" in capsys.readouterr().out


def test_a_storage_failure_reports_the_reason_and_exits_nonzero(
    temp_home: Path, capsys
):
    _write_live(temp_home, make_codex_auth(email="e@example.com"))
    with patch(
        "claude_swap.codex_store.CodexAccountStore.write_credential",
        side_effect=OSError("No space left on device"),
    ):
        with pytest.raises(SystemExit) as exc:
            codex_command(["add"])

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "No space left on device" in err
    assert "Codex account storage failed" in err


def test_main_dispatches_the_codex_namespace(temp_home: Path, monkeypatch):
    called = {}
    monkeypatch.setattr(
        "claude_swap.codex_cli.codex_command", lambda argv: called.setdefault("argv", argv)
    )
    monkeypatch.setattr("sys.argv", ["cswap", "codex", "list"])
    cli.main()
    assert called["argv"] == ["list"]
