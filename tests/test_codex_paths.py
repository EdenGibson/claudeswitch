"""Codex and provider path resolution."""

from __future__ import annotations

from pathlib import Path

from claude_swap import paths


def test_codex_home_defaults_under_home(temp_home: Path):
    assert paths.get_codex_home() == temp_home / ".codex"


def test_codex_home_honours_the_env_var(temp_home: Path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(temp_home / "elsewhere"))
    assert paths.get_codex_home() == temp_home / "elsewhere"


def test_codex_auth_path_sits_inside_codex_home(temp_home: Path):
    assert paths.get_codex_auth_path() == temp_home / ".codex" / "auth.json"


def test_provider_root_is_namespaced_under_the_backup_root(temp_home: Path):
    root = paths.get_provider_root("codex")
    assert root == paths.get_backup_root() / "providers" / "codex"


def test_provider_root_rejects_a_traversing_name(temp_home: Path):
    import pytest

    with pytest.raises(ValueError):
        paths.get_provider_root("../escape")
