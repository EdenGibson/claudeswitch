"""Codex usage normalization and fetch."""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import oauth
from claude_swap.providers import codex
from tests.providers.conftest import make_codex_auth

FIXTURE = Path(__file__).parent.parent / "fixtures" / "codex_usage_response.json"


def _response() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_windows_map_by_length_not_by_position():
    """A 604800s window is the 7-day one wherever the API puts it."""
    result = codex.build_usage_result(_response())
    assert result is not None
    assert result["seven_day"]["pct"] == 74.0
    assert result["five_hour"]["pct"] == 12.5


def test_windows_carry_an_iso_resets_at_the_shared_formatter_accepts():
    result = codex.build_usage_result(_response())
    resets_at = result["seven_day"]["resets_at"]
    countdown, clock = oauth.format_reset(resets_at)
    assert isinstance(countdown, str) and countdown
    assert isinstance(clock, str) and clock


def test_headroom_uses_the_binding_window():
    """The whole point of normalizing: the existing math must just work."""
    result = codex.build_usage_result(_response())
    assert oauth.account_headroom(result) == pytest.approx(26.0)


def test_a_null_secondary_window_is_omitted_not_zeroed():
    data = _response()
    data["rate_limit"]["secondary_window"] = None
    result = codex.build_usage_result(data)
    assert "five_hour" not in result
    assert result["seven_day"]["pct"] == 74.0


def test_an_unrecognised_window_length_becomes_a_scoped_entry():
    data = _response()
    data["rate_limit"]["secondary_window"]["limit_window_seconds"] = 86400
    result = codex.build_usage_result(data)
    assert "five_hour" not in result
    assert len(result["scoped"]) == 1
    assert result["scoped"][0]["name"] == "24h"
    assert result["scoped"][0]["pct"] == 12.5


def test_a_float_window_length_still_classifies():
    """JSON gives no integer guarantee. A dropped window reads as "unknown"
    headroom for an account that is in fact maxed out."""
    data = _response()
    data["rate_limit"]["primary_window"]["limit_window_seconds"] = 604800.0
    result = codex.build_usage_result(data)
    assert result is not None
    assert result["seven_day"]["pct"] == 74.0
    assert "scoped" not in result


def test_a_bool_window_length_is_rejected():
    """isinstance(True, int) is True, which would name the window "0h"."""
    data = _response()
    data["rate_limit"]["secondary_window"]["limit_window_seconds"] = True
    result = codex.build_usage_result(data)
    assert "five_hour" not in result
    assert "scoped" not in result


@pytest.mark.parametrize("literal", ["NaN", "Infinity"])
def test_a_non_finite_window_length_is_rejected(literal: str):
    """json.loads accepts both literals, and int() raises on either."""
    data = _response()
    data["rate_limit"]["secondary_window"]["limit_window_seconds"] = float(literal)
    result = codex.build_usage_result(data)
    assert "five_hour" not in result
    assert "scoped" not in result


def test_no_windows_at_all_returns_none():
    assert codex.build_usage_result({"rate_limit": {}}) is None
    assert codex.build_usage_result({}) is None


def test_fetch_usage_sends_the_bearer_and_account_header():
    blob = make_codex_auth(account_id="acc-hdr")
    captured = {}

    class _Resp:
        def read(self):
            return FIXTURE.read_bytes()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        return _Resp()

    with patch("urllib.request.urlopen", _fake_urlopen):
        result = codex.fetch_usage(blob)

    assert captured["url"] == codex.USAGE_URL
    assert captured["headers"]["Authorization"].startswith("Bearer ")
    assert captured["headers"]["Chatgpt-account-id"] == "acc-hdr"
    assert result["seven_day"]["pct"] == 74.0


def test_fetch_usage_propagates_http_errors():
    def _raise(req, timeout=None):
        raise urllib.error.HTTPError(codex.USAGE_URL, 429, "Too Many", {}, None)

    with patch("urllib.request.urlopen", _raise):
        with pytest.raises(urllib.error.HTTPError):
            codex.fetch_usage(make_codex_auth())
