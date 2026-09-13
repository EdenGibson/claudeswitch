"""The mode file, the watcher, and the model map."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from claude_swap.router.mode import (
    ModeWatcher,
    RouterMode,
    mode_path,
    read_mode,
    write_mode,
)
from claude_swap.router.modelmap import (
    DEFAULT_MAIN,
    DEFAULT_SMALL,
    ModelMap,
    read_map,
    reconcile,
    write_map,
)


class TestRouterMode:
    def test_the_default_is_claude(self):
        assert RouterMode().provider == "claude"
        assert RouterMode().slot is None
        assert RouterMode().pinned is False

    def test_an_unknown_provider_is_rejected(self):
        with pytest.raises(ValueError):
            RouterMode(provider="gemini")

    def test_a_round_trip_keeps_every_field(self):
        mode = RouterMode(provider="codex", slot="7", pinned=True)
        assert RouterMode.from_dict(mode.to_dict()) == mode

    def test_a_slot_is_stored_as_text(self):
        assert RouterMode.from_dict({"provider": "codex", "slot": 7}).slot == "7"

    def test_rubbish_reads_as_claude(self):
        assert RouterMode.from_dict("nonsense") == RouterMode()
        assert RouterMode.from_dict({"provider": "gemini"}) == RouterMode()


class TestModeFile:
    def test_a_missing_file_reads_as_claude(self, temp_home: Path):
        assert read_mode() == RouterMode()

    def test_a_written_mode_reads_back(self, temp_home: Path):
        write_mode(RouterMode(provider="codex", slot="3", pinned=True))
        assert read_mode() == RouterMode(provider="codex", slot="3", pinned=True)

    def test_broken_json_reads_as_claude(self, temp_home: Path):
        path = mode_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json", encoding="utf-8")
        assert read_mode() == RouterMode()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
    def test_the_file_is_private(self, temp_home: Path):
        write_mode(RouterMode(provider="codex"))
        assert oct(mode_path().stat().st_mode)[-3:] == "600"

    def test_no_temporary_file_is_left_behind(self, temp_home: Path):
        write_mode(RouterMode(provider="codex"))
        assert list(mode_path().parent.glob("*.tmp")) == []


class TestModeWatcher:
    def test_it_sees_a_change(self, tmp_path: Path):
        path = tmp_path / "mode.json"
        watcher = ModeWatcher(path)
        assert watcher.current().provider == "claude"

        write_mode(RouterMode(provider="codex", slot="2"), path)
        os.utime(path, (1_000_000, 1_000_000))  # force a distinct mtime
        assert watcher.current().provider == "codex"

    def test_it_does_not_reparse_an_unchanged_file(self, tmp_path: Path, monkeypatch):
        path = tmp_path / "mode.json"
        write_mode(RouterMode(provider="codex"), path)
        watcher = ModeWatcher(path)
        watcher.current()

        calls = []
        monkeypatch.setattr(
            "claude_swap.router.mode.read_mode",
            lambda p=None: calls.append(p) or RouterMode(),
        )
        watcher.current()
        watcher.current()
        assert calls == []

    def test_a_deleted_file_reads_as_claude(self, tmp_path: Path):
        path = tmp_path / "mode.json"
        write_mode(RouterMode(provider="codex"), path)
        watcher = ModeWatcher(path)
        assert watcher.current().provider == "codex"

        path.unlink()
        assert watcher.current().provider == "claude"


class TestModelMap:
    def test_haiku_gets_the_small_model(self):
        assert ModelMap().pick("claude-haiku-4-5-20251001") == DEFAULT_SMALL

    def test_everything_else_gets_the_main_model(self):
        assert ModelMap().pick("claude-opus-5") == DEFAULT_MAIN
        assert ModelMap().pick("claude-sonnet-5") == DEFAULT_MAIN

    def test_a_missing_name_gets_the_main_model(self):
        assert ModelMap().pick(None) == DEFAULT_MAIN
        assert ModelMap().pick(42) == DEFAULT_MAIN

    def test_a_written_map_reads_back(self, temp_home: Path):
        write_map(ModelMap(main="gpt-x", small="gpt-x-mini"))
        assert read_map() == ModelMap(main="gpt-x", small="gpt-x-mini")

    def test_a_missing_map_gives_the_defaults(self, temp_home: Path):
        assert read_map() == ModelMap()

    def test_a_half_written_map_fills_in_the_defaults(self, temp_home: Path):
        from claude_swap.router.modelmap import map_path

        map_path().parent.mkdir(parents=True, exist_ok=True)
        map_path().write_text(json.dumps({"main": "gpt-x"}), encoding="utf-8")
        assert read_map() == ModelMap(main="gpt-x", small=DEFAULT_SMALL)


class TestReconcile:
    def test_an_empty_list_changes_nothing(self):
        original = ModelMap(main="gone", small="also-gone")
        assert reconcile(original, []) is original

    def test_a_known_name_is_left_alone(self):
        original = ModelMap(main="gpt-a", small="gpt-b")
        assert reconcile(original, ["gpt-a", "gpt-b"]) == original

    def test_a_missing_main_is_replaced(self):
        result = reconcile(ModelMap(main="gone", small="gpt-b"), ["gpt-5-codex", "gpt-b"])
        assert result.main == "gpt-5-codex"
        assert result.small == "gpt-b"

    def test_a_missing_small_prefers_a_small_sounding_name(self):
        result = reconcile(
            ModelMap(main="gpt-5-codex", small="gone"),
            ["gpt-5-codex", "gpt-5-codex-mini"],
        )
        assert result.small == "gpt-5-codex-mini"

    def test_a_missing_small_falls_back_to_the_main_model(self):
        result = reconcile(ModelMap(main="gpt-a", small="gone"), ["gpt-a"])
        assert result.small == "gpt-a"
