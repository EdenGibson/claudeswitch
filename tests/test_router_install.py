"""Installing the router edits one settings key and can put it back."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_swap.router import install as router_install
from claude_swap.router import unit as router_unit
from claude_swap.router.mode import DEFAULT_PORT


@pytest.fixture(autouse=True)
def _no_systemd(monkeypatch):
    """Never touch the real user's systemd from a test."""
    monkeypatch.setattr(router_unit, "have_systemd", lambda: False)


def _settings(temp_home: Path) -> dict:
    path = router_install.settings_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_settings(payload: dict) -> None:
    path = router_install.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_install_writes_the_base_url(temp_home: Path):
    router_install.install(write_unit=False)

    env = _settings(temp_home)["env"]
    assert env["ANTHROPIC_BASE_URL"] == f"http://127.0.0.1:{DEFAULT_PORT}"


def test_install_writes_no_other_key(temp_home: Path):
    router_install.install(write_unit=False)
    assert list(_settings(temp_home)["env"]) == ["ANTHROPIC_BASE_URL"]


def test_install_keeps_the_rest_of_the_file(temp_home: Path):
    _write_settings({"model": "opus", "env": {"FOO": "bar"}})

    router_install.install(write_unit=False)

    settings = _settings(temp_home)
    assert settings["model"] == "opus"
    assert settings["env"]["FOO"] == "bar"


def test_uninstall_removes_the_key_it_added(temp_home: Path):
    _write_settings({"env": {"FOO": "bar"}})
    router_install.install(write_unit=False)

    assert router_install.uninstall() is True

    assert _settings(temp_home)["env"] == {"FOO": "bar"}


def test_uninstall_restores_a_base_url_the_user_had(temp_home: Path):
    _write_settings({"env": {"ANTHROPIC_BASE_URL": "https://gateway.example"}})
    router_install.install(write_unit=False)

    router_install.uninstall()

    assert (
        _settings(temp_home)["env"]["ANTHROPIC_BASE_URL"] == "https://gateway.example"
    )


def test_uninstall_drops_an_env_block_it_created(temp_home: Path):
    _write_settings({"model": "opus"})
    router_install.install(write_unit=False)

    router_install.uninstall()

    assert "env" not in _settings(temp_home)


def test_is_installed_tracks_the_settings_file(temp_home: Path):
    assert router_install.is_installed() is False
    router_install.install(write_unit=False)
    assert router_install.is_installed() is True
    router_install.uninstall()
    assert router_install.is_installed() is False


def test_a_remote_base_url_does_not_count_as_installed(temp_home: Path):
    _write_settings({"env": {"ANTHROPIC_BASE_URL": "https://gateway.example"}})
    assert router_install.is_installed() is False


def test_invalid_settings_json_stops_the_install(temp_home: Path):
    path = router_install.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")

    with pytest.raises(RuntimeError, match="not valid JSON"):
        router_install.install(write_unit=False)


def test_the_state_file_disappears_on_uninstall(temp_home: Path):
    router_install.install(write_unit=False)
    assert router_install.state_path().exists()

    router_install.uninstall()

    assert not router_install.state_path().exists()


def test_a_custom_port_is_remembered(temp_home: Path):
    router_install.install(port=9999, write_unit=False)
    assert router_install.read_state().port == 9999
    assert _settings(temp_home)["env"]["ANTHROPIC_BASE_URL"].endswith(":9999")


def test_the_unit_runs_the_serve_command(temp_home: Path):
    text = router_install.unit_text(9999)
    assert "router serve --port 9999" in text
    assert "Restart=always" in text


def test_a_second_install_still_knows_what_was_there_first(temp_home: Path):
    """Re-installing is normal: a new port, or a repaired unit."""
    _write_settings({"env": {"ANTHROPIC_BASE_URL": "https://gateway.example"}})
    router_install.install(write_unit=False)

    router_install.install(port=9999, write_unit=False)
    router_install.uninstall()

    assert (
        _settings(temp_home)["env"]["ANTHROPIC_BASE_URL"] == "https://gateway.example"
    )


def test_a_second_install_still_removes_a_key_it_added(temp_home: Path):
    _write_settings({"model": "opus"})
    router_install.install(write_unit=False)

    router_install.install(port=9999, write_unit=False)
    router_install.uninstall()

    assert "env" not in _settings(temp_home)


def test_purge_restores_the_base_url_before_it_deletes_the_state(
    temp_home: Path, monkeypatch
):
    """install-state.json lives in the backup dir purge is about to delete."""
    from claude_swap.switcher import ClaudeAccountSwitcher

    _write_settings({"env": {"ANTHROPIC_BASE_URL": "https://gateway.example"}})
    router_install.install(write_unit=False)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "y")

    ClaudeAccountSwitcher().purge()

    assert (
        _settings(temp_home)["env"]["ANTHROPIC_BASE_URL"] == "https://gateway.example"
    )
