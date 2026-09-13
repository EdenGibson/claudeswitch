"""Folding the standalone Codex store into the main registry."""

from __future__ import annotations

import json
from pathlib import Path

from claude_swap import paths
from claude_swap.codex_store import CodexAccountStore
from claude_swap.pool_migration import ACTIVE_KEY, migrate_codex_into_pool
from tests.providers.conftest import make_codex_auth


def _registry() -> Path:
    return paths.get_backup_root() / "sequence.json"


def _seed_claude(*emails: str) -> None:
    path = _registry()
    path.parent.mkdir(parents=True, exist_ok=True)
    accounts = {
        str(i): {"email": email, "uuid": f"u-{i}", "organizationUuid": ""}
        for i, email in enumerate(emails, start=1)
    }
    path.write_text(
        json.dumps(
            {
                "activeAccountNumber": 1,
                "sequence": [int(n) for n in accounts],
                "accounts": accounts,
            }
        ),
        encoding="utf-8",
    )


def _seed_codex(email: str = "c@example.com", account_id: str = "acc-c") -> str:
    store = CodexAccountStore()
    store.write_live(make_codex_auth(email=email, account_id=account_id))
    slot, _ = store.add_current()
    return slot


def test_a_codex_account_lands_at_a_fresh_slot(temp_home: Path):
    _seed_claude("a@example.com")
    _seed_codex()

    assert migrate_codex_into_pool() is True

    data = json.loads(_registry().read_text())
    assert data["sequence"] == [1, 2]
    assert data["accounts"]["2"]["email"] == "c@example.com"
    assert data["accounts"]["2"]["provider"] == "codex"
    # The Claude slot keeps its shape, including having no provider key.
    assert "provider" not in data["accounts"]["1"]


def test_the_migration_is_idempotent(temp_home: Path):
    _seed_claude("a@example.com")
    _seed_codex()

    migrate_codex_into_pool()
    first = json.loads(_registry().read_text())
    assert migrate_codex_into_pool() is False
    second = json.loads(_registry().read_text())

    assert first == second


def test_a_rerun_after_a_crash_does_not_duplicate(temp_home: Path):
    """The marker write is last, so the whole function may run twice."""
    _seed_claude("a@example.com")
    _seed_codex()

    migrate_codex_into_pool()
    store = CodexAccountStore()
    # Undo only the marker, exactly as a crash between the two writes would.
    data = store._read_sequence()
    data.pop("migrated", None)
    data["accounts"] = {
        "1": {"email": "c@example.com", "accountId": "acc-c", "added": ""}
    }
    store._write_sequence(data)

    migrate_codex_into_pool()

    registry = json.loads(_registry().read_text())
    codex_slots = [
        s for s, a in registry["accounts"].items() if a.get("provider") == "codex"
    ]
    assert codex_slots == ["2"]


def test_the_credential_survives_the_renumber(temp_home: Path):
    _seed_claude("a@example.com")
    store = CodexAccountStore()
    blob = make_codex_auth(email="c@example.com", account_id="acc-c")
    store.write_live(blob)
    store.add_current()

    migrate_codex_into_pool()

    registry = json.loads(_registry().read_text())
    slot = next(
        s for s, a in registry["accounts"].items() if a.get("provider") == "codex"
    )
    assert store.read_credential(slot, "c@example.com") == blob
    # The old slot's file is gone, not left as a second copy of the secret.
    assert store.read_credential("1", "c@example.com") == ""


def test_an_empty_codex_store_changes_nothing(temp_home: Path):
    _seed_claude("a@example.com")
    before = _registry().read_text()

    assert migrate_codex_into_pool() is False

    assert _registry().read_text() == before


def test_the_active_codex_account_becomes_the_active_provider_entry(temp_home: Path):
    _seed_claude("a@example.com")
    _seed_codex()

    migrate_codex_into_pool()

    data = json.loads(_registry().read_text())
    slot = next(
        s for s, a in data["accounts"].items() if a.get("provider") == "codex"
    )
    assert data[ACTIVE_KEY]["codex"] == slot


def test_the_claude_active_pointer_is_untouched(temp_home: Path):
    _seed_claude("a@example.com", "b@example.com")
    _seed_codex()

    migrate_codex_into_pool()

    assert json.loads(_registry().read_text())["activeAccountNumber"] == 1


def test_two_codex_accounts_keep_their_relative_order(temp_home: Path):
    _seed_claude("a@example.com")
    _seed_codex("one@example.com", "acc-1")
    store = CodexAccountStore()
    store.write_live(make_codex_auth(email="two@example.com", account_id="acc-2"))
    store.add_current()

    migrate_codex_into_pool()

    data = json.loads(_registry().read_text())
    assert data["accounts"]["2"]["email"] == "one@example.com"
    assert data["accounts"]["3"]["email"] == "two@example.com"


def test_no_registry_yet_leaves_the_codex_store_alone(temp_home: Path):
    """A first run with no Claude accounts must stay re-runnable."""
    _seed_codex()

    assert migrate_codex_into_pool() is False
    assert CodexAccountStore().accounts() != []
