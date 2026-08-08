"""The Codex account pool."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import paths
from claude_swap.codex_store import CodexAccountStore
from claude_swap.usage_store import UsageEntry
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


def test_a_credential_with_interleaved_junk_reads_as_empty(store: CodexAccountStore):
    """Junk must be rejected, not silently discarded into a partial blob."""
    store.ensure_dirs()
    path = store.credential_path("1", "junk@example.com")
    encoded = base64.b64encode(make_codex_auth().encode("utf-8")).decode("ascii")
    path.write_bytes((encoded[:20] + "!!!!" + encoded[20:]).encode("ascii"))
    assert store.read_credential("1", "junk@example.com") == ""


def test_a_non_dict_account_record_is_dropped(store: CodexAccountStore):
    """accounts() promises list[tuple[str, dict]]. A string record made the
    first record.get() raise AttributeError."""
    store.ensure_dirs()
    store.sequence_file.write_text(
        json.dumps({"accounts": {"1": "hello", "abc": {"email": "x@example.com"}}}),
        encoding="utf-8",
    )
    assert store.accounts() == []
    assert store.next_slot() == "1"


def test_a_valid_record_survives_alongside_a_dropped_one(store: CodexAccountStore):
    store.ensure_dirs()
    store.sequence_file.write_text(
        json.dumps({"accounts": {"1": "hello", "2": {"email": "keep@example.com"}}}),
        encoding="utf-8",
    )
    assert store.accounts() == [("2", {"email": "keep@example.com"})]
    assert store.next_slot() == "1"


def test_deleting_a_credential_is_idempotent(store: CodexAccountStore):
    store.write_credential("1", "d@example.com", make_codex_auth())
    store.delete_credential("1", "d@example.com")
    store.delete_credential("1", "d@example.com")
    assert store.read_credential("1", "d@example.com") == ""


def _write_live(temp_home: Path, blob: str) -> Path:
    live = temp_home / ".codex" / "auth.json"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text(blob, encoding="utf-8")
    return live


def test_add_captures_the_live_credential_into_slot_one(
    store: CodexAccountStore, temp_home: Path
):
    _write_live(temp_home, make_codex_auth(email="first@example.com", account_id="acc-1"))
    slot, ident = store.add_current()
    assert slot == "1"
    assert ident.email == "first@example.com"
    assert store.active_number() == "1"
    assert store.read_credential("1", "first@example.com")


def test_add_records_the_identity_fields(store: CodexAccountStore, temp_home: Path):
    _write_live(
        temp_home,
        make_codex_auth(
            email="rec@example.com", account_id="acc-9", plan="pro", org_title="Derive"
        ),
    )
    store.add_current()
    record = dict(store.accounts())["1"]
    assert record["email"] == "rec@example.com"
    assert record["accountId"] == "acc-9"
    assert record["plan"] == "pro"
    assert record["organizationName"] == "Derive"
    assert record["added"]


def test_add_refuses_a_duplicate_account(store: CodexAccountStore, temp_home: Path):
    _write_live(temp_home, make_codex_auth(email="dup@example.com", account_id="acc-d"))
    store.add_current()
    with pytest.raises(ValueError, match="already managed"):
        store.add_current()


def test_add_refuses_when_no_live_credential_exists(store: CodexAccountStore):
    with pytest.raises(ValueError, match="No Codex credential"):
        store.add_current()


def test_switch_writes_the_target_credential_live(
    store: CodexAccountStore, temp_home: Path
):
    _write_live(temp_home, make_codex_auth(email="a@example.com", account_id="acc-a"))
    store.add_current()
    _write_live(temp_home, make_codex_auth(email="b@example.com", account_id="acc-b"))
    store.add_current()

    store.switch("1")

    live = json.loads((temp_home / ".codex" / "auth.json").read_text())
    assert live["tokens"]["account_id"] == "acc-a"
    assert store.active_number() == "1"


def test_switch_recaptures_the_live_credential_before_replacing_it(
    store: CodexAccountStore, temp_home: Path
):
    """The Codex CLI rotates tokens in place. Losing that write would restore a
    stale token later, so switching away must save what is live first."""
    _write_live(temp_home, make_codex_auth(email="a@example.com", account_id="acc-a"))
    store.add_current()
    _write_live(temp_home, make_codex_auth(email="b@example.com", account_id="acc-b"))
    store.add_current()

    rotated = make_codex_auth(
        email="b@example.com", account_id="acc-b", refresh_token="rotated-by-codex"
    )
    _write_live(temp_home, rotated)

    store.switch("1")

    saved = store.read_credential("2", "b@example.com")
    assert json.loads(saved)["tokens"]["refresh_token"] == "rotated-by-codex"


def test_switch_to_the_already_live_slot_keeps_the_rotated_credential(
    store: CodexAccountStore, temp_home: Path
):
    """Re-switching to the live account must not roll its token back.

    OpenAI refresh tokens are single-use, so a rolled-back blob forces a
    browser re-login the next time it is used.
    """
    _write_live(temp_home, make_codex_auth(email="a@example.com", account_id="acc-a"))
    store.add_current()

    rotated = make_codex_auth(
        email="a@example.com", account_id="acc-a", refresh_token="a-v2-ROTATED"
    )
    _write_live(temp_home, rotated)

    store.switch("1")

    live = json.loads((temp_home / ".codex" / "auth.json").read_text())
    assert live["tokens"]["refresh_token"] == "a-v2-ROTATED"
    stored = json.loads(store.read_credential("1", "a@example.com"))
    assert stored["tokens"]["refresh_token"] == "a-v2-ROTATED"


def test_a_rotation_survives_switching_away_and_back(
    store: CodexAccountStore, temp_home: Path
):
    _write_live(temp_home, make_codex_auth(email="a@example.com", account_id="acc-a"))
    store.add_current()
    _write_live(temp_home, make_codex_auth(email="b@example.com", account_id="acc-b"))
    store.add_current()
    store.switch("1")

    rotated = make_codex_auth(
        email="a@example.com", account_id="acc-a", refresh_token="a-v2-ROTATED"
    )
    _write_live(temp_home, rotated)

    store.switch("1")
    store.switch("2")
    store.switch("1")

    live = json.loads((temp_home / ".codex" / "auth.json").read_text())
    assert live["tokens"]["refresh_token"] == "a-v2-ROTATED"


def test_switch_resolves_an_email_as_well_as_a_slot(
    store: CodexAccountStore, temp_home: Path
):
    _write_live(temp_home, make_codex_auth(email="by@example.com", account_id="acc-by"))
    store.add_current()
    _write_live(temp_home, make_codex_auth(email="other@example.com", account_id="acc-o"))
    store.add_current()

    store.switch("by@example.com")
    assert store.active_number() == "1"


def test_switch_to_an_unknown_account_raises(store: CodexAccountStore):
    with pytest.raises(ValueError, match="No Codex account"):
        store.switch("7")


def test_remove_drops_the_record_and_the_credential(
    store: CodexAccountStore, temp_home: Path
):
    _write_live(temp_home, make_codex_auth(email="gone@example.com", account_id="acc-g"))
    store.add_current()
    path = store.credential_path("1", "gone@example.com")

    store.remove("1")

    assert store.accounts() == []
    assert not path.exists()
    assert store.active_number() is None


def test_removing_a_non_active_account_leaves_the_active_pointer(
    store: CodexAccountStore, temp_home: Path
):
    _write_live(temp_home, make_codex_auth(email="keep@example.com", account_id="acc-k"))
    store.add_current()
    _write_live(temp_home, make_codex_auth(email="drop@example.com", account_id="acc-d"))
    store.add_current()
    store.switch("1")

    store.remove("2")

    assert store.active_number() == "1"


def _seed_two_accounts(store: CodexAccountStore, temp_home: Path) -> None:
    _write_live(temp_home, make_codex_auth(email="a@example.com", account_id="acc-a"))
    store.add_current()
    _write_live(temp_home, make_codex_auth(email="b@example.com", account_id="acc-b"))
    store.add_current()


def test_collect_usage_stores_a_row_per_account(
    store: CodexAccountStore, temp_home: Path
):
    _seed_two_accounts(store, temp_home)
    usage = {"five_hour": {"pct": 10.0}, "seven_day": {"pct": 40.0}}

    with patch("claude_swap.providers.codex.fetch_usage", return_value=usage):
        entries = store.collect_usage()

    assert set(entries) == {"1", "2"}
    assert entries["1"].last_good == usage
    assert entries["2"].last_good == usage


def test_collect_usage_records_a_failure_without_losing_the_last_good(
    store: CodexAccountStore, temp_home: Path
):
    _seed_two_accounts(store, temp_home)
    good = {"seven_day": {"pct": 40.0}}
    with patch("claude_swap.providers.codex.fetch_usage", return_value=good):
        store.collect_usage()

    with patch(
        "claude_swap.providers.codex.fetch_usage", side_effect=TimeoutError()
    ):
        entries = store.collect_usage(force=True)

    assert entries["1"].last_good == good
    assert entries["1"].last_error == "timeout"


def test_collect_usage_refreshes_an_expired_token_first(
    store: CodexAccountStore, temp_home: Path
):
    _write_live(
        temp_home,
        make_codex_auth(email="x@example.com", account_id="acc-x", access_expires_in=-5),
    )
    store.add_current()

    fresh = make_codex_auth(email="x@example.com", account_id="acc-x")
    from claude_swap.oauth import RefreshOutcome

    with patch(
        "claude_swap.providers.codex.try_refresh",
        return_value=RefreshOutcome(fresh, None),
    ) as refresh:
        with patch(
            "claude_swap.providers.codex.fetch_usage",
            return_value={"seven_day": {"pct": 1.0}},
        ):
            store.collect_usage(force=True)

    assert refresh.call_count == 1
    assert store.read_credential("1", "x@example.com") == fresh


def test_collect_usage_marks_a_dead_refresh_lineage(
    store: CodexAccountStore, temp_home: Path
):
    _write_live(
        temp_home,
        make_codex_auth(email="d@example.com", account_id="acc-d", access_expires_in=-5),
    )
    store.add_current()
    from claude_swap.oauth import RefreshOutcome

    with patch(
        "claude_swap.providers.codex.try_refresh",
        return_value=RefreshOutcome(None, "invalid_grant"),
    ):
        entries = store.collect_usage(force=True)

    assert entries["1"].sentinel == "token expired"


def test_usage_entries_are_returned_for_accounts_never_fetched(
    store: CodexAccountStore, temp_home: Path
):
    _seed_two_accounts(store, temp_home)
    entries = store.usage_entries()
    assert set(entries) == {"1", "2"}
    assert all(isinstance(e, UsageEntry) for e in entries.values())
    assert entries["1"].last_good is None
