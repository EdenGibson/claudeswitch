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


def test_a_stored_credential_round_trips(store: CodexAccountStore):
    blob = make_codex_auth(email="round@example.com")
    store.write_credential("1", "round@example.com", blob)
    assert store.read_credential("1", "round@example.com") == blob


def test_a_stored_credential_is_not_plain_text_on_disk(store: CodexAccountStore):
    blob = make_codex_auth(email="secret@example.com", refresh_token="TOPSECRET")
    store.write_credential("1", "secret@example.com", blob)
    on_disk = store.credential_path("1", "secret@example.com").read_text()
    assert "TOPSECRET" not in on_disk


@pytest.mark.skipif(
    __import__("sys").platform == "win32", reason="POSIX modes only"
)
def test_a_stored_credential_is_owner_only(store: CodexAccountStore):
    store.write_credential("1", "m@example.com", make_codex_auth())
    mode = store.credential_path("1", "m@example.com").stat().st_mode & 0o777
    assert mode == 0o600


def test_reading_a_missing_credential_returns_empty(store: CodexAccountStore):
    assert store.read_credential("9", "nobody@example.com") == ""


def test_deleting_a_credential_is_idempotent(store: CodexAccountStore):
    store.write_credential("1", "d@example.com", make_codex_auth())
    store.delete_credential("1", "d@example.com")
    store.delete_credential("1", "d@example.com")
    assert store.read_credential("1", "d@example.com") == ""
