"""Provider identity on the shared account models."""

from __future__ import annotations

from claude_swap.models import AccountInfo, AccountSnapshot
from claude_swap.usage_store import UsageEntry


def test_account_info_defaults_to_claude():
    """An entry written by upstream carries no provider key."""
    info = AccountInfo.from_dict(1, {"email": "a@example.com"})
    assert info.provider == "claude"


def test_account_info_reads_the_provider_key():
    info = AccountInfo.from_dict(9, {"email": "a@example.com", "provider": "codex"})
    assert info.provider == "codex"


def test_a_null_provider_key_reads_as_claude():
    info = AccountInfo.from_dict(1, {"email": "a@example.com", "provider": None})
    assert info.provider == "claude"


def test_claude_accounts_round_trip_without_a_provider_key():
    """Never write the default back — upstream must still read our file."""
    info = AccountInfo.from_dict(1, {"email": "a@example.com"})
    assert "provider" not in info.to_dict()


def test_codex_accounts_round_trip_with_a_provider_key():
    info = AccountInfo.from_dict(9, {"email": "a@example.com", "provider": "codex"})
    assert info.to_dict()["provider"] == "codex"


def test_account_snapshot_defaults_to_claude():
    snap = AccountSnapshot(
        number="1",
        email="a@example.com",
        org_name="",
        org_uuid="",
        is_active=True,
        kind="oauth",
        switchable=True,
        usage=UsageEntry(),
    )
    assert snap.provider == "claude"
