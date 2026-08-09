"""Export and import carry a Codex account with its provider."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_swap import paths, transfer
from claude_swap.codex_store import CodexAccountStore
from claude_swap.exceptions import TransferError
from claude_swap.switcher import ClaudeAccountSwitcher
from tests.providers.conftest import make_codex_auth

CODEX_EMAIL = "codex@example.com"
CODEX_ID = "acc-codex"


def _write_registry(data: dict) -> None:
    root = paths.get_backup_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "sequence.json").write_text(json.dumps(data), encoding="utf-8")


def _registry() -> dict:
    return json.loads((paths.get_backup_root() / "sequence.json").read_text())


@pytest.fixture
def pool(temp_home: Path) -> ClaudeAccountSwitcher:
    """One Codex account in slot 2, active, with a stored credential."""
    _write_registry(
        {
            "activeAccountNumber": None,
            "activeProviderAccounts": {"codex": "2"},
            "sequence": [2],
            "accounts": {
                "2": {
                    "email": CODEX_EMAIL,
                    "uuid": CODEX_ID,
                    "organizationUuid": "",
                    "organizationName": "",
                    "provider": "codex",
                },
            },
        }
    )
    blob = make_codex_auth(email=CODEX_EMAIL, account_id=CODEX_ID)
    store = CodexAccountStore()
    store.write_credential("2", CODEX_EMAIL, blob)
    store.write_live(blob)
    return ClaudeAccountSwitcher()


def _export(pool: ClaudeAccountSwitcher, tmp_path: Path) -> dict:
    out = tmp_path / "export.json"
    transfer.export_accounts(pool, str(out))
    return json.loads(out.read_text())


def test_export_names_the_provider(pool, tmp_path):
    entry = _export(pool, tmp_path)["accounts"][0]

    assert entry["provider"] == "codex"
    assert entry["credentials"]["tokens"]["account_id"] == CODEX_ID


def test_export_carries_no_claude_config(pool, tmp_path):
    """An older cswap requires config to be an object, so it aborts loudly."""
    entry = _export(pool, tmp_path)["accounts"][0]

    assert "config" not in entry


def test_export_prefers_the_live_credential_for_the_active_slot(pool, tmp_path):
    """The stored copy can hold a spent refresh token; the live one cannot."""
    CodexAccountStore().write_live(
        make_codex_auth(
            email=CODEX_EMAIL, account_id=CODEX_ID, refresh_token="ROTATED"
        )
    )

    entry = _export(pool, tmp_path)["accounts"][0]

    assert entry["credentials"]["tokens"]["refresh_token"] == "ROTATED"


def test_export_skips_a_codex_slot_with_no_credential(pool, tmp_path):
    CodexAccountStore().delete_credential("2", CODEX_EMAIL)
    CodexAccountStore().live_path().unlink()

    with pytest.raises(TransferError, match="no exportable accounts"):
        _export(pool, tmp_path)


def test_import_restores_the_codex_account(pool, tmp_path):
    payload = _export(pool, tmp_path)
    # Start from an empty registry, as a fresh machine would.
    _write_registry({"activeAccountNumber": None, "sequence": [], "accounts": {}})
    CodexAccountStore().delete_credential("2", CODEX_EMAIL)
    source = tmp_path / "in.json"
    source.write_text(json.dumps(payload), encoding="utf-8")

    transfer.import_accounts(ClaudeAccountSwitcher(), str(source))

    data = _registry()
    assert data["accounts"]["2"]["provider"] == "codex"
    restored = CodexAccountStore().read_credential("2", CODEX_EMAIL)
    assert json.loads(restored)["tokens"]["account_id"] == CODEX_ID


def test_import_writes_no_claude_credential_for_a_codex_account(pool, tmp_path):
    payload = _export(pool, tmp_path)
    _write_registry({"activeAccountNumber": None, "sequence": [], "accounts": {}})
    CodexAccountStore().delete_credential("2", CODEX_EMAIL)
    source = tmp_path / "in.json"
    source.write_text(json.dumps(payload), encoding="utf-8")

    switcher = ClaudeAccountSwitcher()
    transfer.import_accounts(switcher, str(source))

    assert switcher._read_account_credentials("2", CODEX_EMAIL) == ""


def test_import_rejects_an_unknown_provider(pool, tmp_path):
    payload = _export(pool, tmp_path)
    payload["accounts"][0]["provider"] = "gemini"
    source = tmp_path / "in.json"
    source.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(TransferError, match="unknown provider"):
        transfer.import_accounts(ClaudeAccountSwitcher(), str(source))


def test_a_codex_import_never_overwrites_a_claude_account_on_one_email(
    pool, tmp_path
):
    """One email can hold both a Claude login and a ChatGPT account."""
    payload = _export(pool, tmp_path)
    _write_registry(
        {
            "activeAccountNumber": 2,
            "sequence": [2],
            "accounts": {
                "2": {
                    "email": CODEX_EMAIL,
                    "uuid": "u-claude",
                    "organizationUuid": "",
                    "organizationName": "",
                },
            },
        }
    )
    source = tmp_path / "in.json"
    source.write_text(json.dumps(payload), encoding="utf-8")

    switcher = ClaudeAccountSwitcher()
    transfer.import_accounts(switcher, str(source))

    data = _registry()
    assert data["accounts"]["2"].get("provider") is None
    codex_slots = [
        num for num, acc in data["accounts"].items() if acc.get("provider") == "codex"
    ]
    assert len(codex_slots) == 1
    assert codex_slots[0] != "2"


def test_a_claude_only_export_is_unchanged(temp_home: Path, tmp_path):
    """Nothing about a Claude-only export may shift."""
    _write_registry(
        {
            "activeAccountNumber": 1,
            "sequence": [1],
            "accounts": {
                "1": {
                    "email": "claude@example.com",
                    "uuid": "u-1",
                    "organizationUuid": "",
                    "organizationName": "",
                },
            },
        }
    )
    switcher = ClaudeAccountSwitcher()
    switcher._setup_directories()
    switcher._write_account_credentials(
        "1", "claude@example.com", json.dumps({"claudeAiOauth": {"accessToken": "t"}})
    )
    switcher._write_account_config("1", "claude@example.com", json.dumps({"oauthAccount": {}}))

    out = tmp_path / "export.json"
    transfer.export_accounts(switcher, str(out))
    entry = json.loads(out.read_text())["accounts"][0]

    assert "provider" not in entry
    assert "config" in entry
