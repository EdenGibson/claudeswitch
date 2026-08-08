"""The Codex account pool."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_swap import paths
from claude_swap.codex_store import CodexAccountStore
from tests.providers.conftest import make_codex_auth


@pytest.fixture
def store(temp_home: Path) -> CodexAccountStore:
    return CodexAccountStore()


def test_a_fresh_store_has_no_accounts(store: CodexAccountStore):
    assert store.accounts() == []
    assert store.active_number() is None


def test_the_state_tree_is_namespaced_and_leaves_claude_alone(
    store: CodexAccountStore, temp_home: Path
):
    store.ensure_dirs()
    root = paths.get_backup_root() / "providers" / "codex"
    assert (root / "credentials").is_dir()
    assert (root / "cache").is_dir()
    assert not (paths.get_backup_root() / "sequence.json").exists()


def test_the_sequence_file_records_its_schema_version(store: CodexAccountStore):
    store.ensure_dirs()
    store._write_sequence({"accounts": {}})
    written = json.loads(store.sequence_file.read_text(encoding="utf-8"))
    assert written["schemaVersion"] == 1
    assert "lastUpdated" in written


def test_next_slot_fills_the_lowest_free_number(store: CodexAccountStore):
    store.ensure_dirs()
    store._write_sequence({"accounts": {"1": {}, "3": {}}})
    assert store.next_slot() == "2"


def test_next_slot_on_an_empty_store_is_one(store: CodexAccountStore):
    assert store.next_slot() == "1"
