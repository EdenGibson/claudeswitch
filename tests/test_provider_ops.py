"""The provider dispatch layer."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap.provider_ops import (
    ClaudeOps,
    CodexOps,
    is_known,
    ops_for,
    provider_names,
)
from tests.providers.conftest import make_codex_auth


def test_ops_for_claude_returns_claude_ops():
    assert isinstance(ops_for("claude"), ClaudeOps)


def test_ops_for_codex_returns_codex_ops():
    assert isinstance(ops_for("codex"), CodexOps)


def test_ops_for_an_unknown_provider_raises():
    with pytest.raises(ValueError, match="unknown provider"):
        ops_for("gemini")


def test_ops_for_treats_none_as_claude():
    """A stored entry with no provider key is a Claude account."""
    assert isinstance(ops_for(None), ClaudeOps)


def test_ops_for_treats_an_empty_string_as_claude():
    assert isinstance(ops_for(""), ClaudeOps)


def test_is_known_agrees_with_ops_for():
    assert is_known("codex") is True
    assert is_known(None) is True
    assert is_known("gemini") is False


def test_provider_names_lists_claude_first():
    assert provider_names()[0] == "claude"
    assert "codex" in provider_names()


def test_each_ops_declares_its_live_path(temp_home: Path):
    assert ops_for("claude").live_path().name == ".credentials.json"
    assert ops_for("codex").live_path().name == "auth.json"


def test_each_ops_names_the_binary_it_launches():
    assert ops_for("claude").binary == "claude"
    assert ops_for("codex").binary == "codex"


def test_read_live_returns_none_when_nobody_is_logged_in(temp_home: Path):
    assert ops_for("claude").read_live() is None
    assert ops_for("codex").read_live() is None


def test_codex_read_live_returns_the_blob(temp_home: Path):
    blob = make_codex_auth(email="a@example.com")
    ops_for("codex").write_live(blob)
    assert ops_for("codex").read_live() == blob


def test_claude_write_live_refuses(temp_home: Path):
    """Claude activation must stay inside the switcher."""
    with pytest.raises(NotImplementedError, match="ClaudeAccountSwitcher"):
        ops_for("claude").write_live("{}")


def test_codex_identity_reads_the_token_offline(temp_home: Path):
    blob = make_codex_auth(email="who@example.com", account_id="acc-w")
    ident = ops_for("codex").identity(blob)
    assert ident is not None
    assert ident.email == "who@example.com"
    assert ident.account_uuid == "acc-w"


def test_claude_fetch_usage_passes_the_access_token_not_the_blob():
    """oauth.fetch_usage takes a token; codex.fetch_usage takes the blob."""
    with patch(
        "claude_swap.oauth.extract_access_token", return_value="tok-123"
    ), patch("claude_swap.oauth.fetch_usage", return_value={"seven_day": {}}) as fetch:
        ops_for("claude").fetch_usage('{"claudeAiOauth": {}}')
    fetch.assert_called_once_with("tok-123")


def test_claude_fetch_usage_is_none_without_a_token():
    with patch("claude_swap.oauth.extract_access_token", return_value=None):
        assert ops_for("claude").fetch_usage("{}") is None


def test_codex_fetch_usage_passes_the_whole_blob():
    blob = make_codex_auth(email="a@example.com")
    with patch(
        "claude_swap.providers.codex.fetch_usage", return_value={"seven_day": {}}
    ) as fetch:
        ops_for("codex").fetch_usage(blob)
    fetch.assert_called_once_with(blob)
