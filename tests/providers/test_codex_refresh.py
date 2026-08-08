"""Codex refresh-token grant outcomes."""

from __future__ import annotations

import json
import urllib.error
from unittest.mock import patch

from claude_swap.providers import codex
from tests.providers.conftest import make_codex_auth, make_jwt


def _grant_response(body: dict):
    class _Resp:
        def read(self):
            return json.dumps(body).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _Resp()


def test_success_returns_a_whole_new_blob_with_the_rotated_tokens():
    original = make_codex_auth(refresh_token="old-refresh", account_id="acc-1")
    new_access = make_jwt({"exp": 9_999_999_999})
    new_id = make_jwt({
        "email": "user@example.com",
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "acc-1",
            "chatgpt_plan_type": "plus",
            "organizations": [],
        },
    })
    captured = {}

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode())
        return _grant_response({
            "access_token": new_access,
            "id_token": new_id,
            "refresh_token": "new-refresh",
        })

    with patch("urllib.request.urlopen", _fake_urlopen):
        outcome = codex.try_refresh(original)

    assert captured["url"] == codex.TOKEN_URL
    assert captured["body"]["grant_type"] == "refresh_token"
    assert captured["body"]["refresh_token"] == "old-refresh"
    assert captured["body"]["client_id"] == codex.CLIENT_ID

    assert outcome.error is None
    refreshed = json.loads(outcome.credentials)
    assert refreshed["tokens"]["refresh_token"] == "new-refresh"
    assert refreshed["tokens"]["access_token"] == new_access
    assert refreshed["tokens"]["account_id"] == "acc-1"
    assert outcome.token_account == {
        "uuid": "acc-1",
        "email": "user@example.com",
        "organizationUuid": "",
    }


def test_a_response_without_a_new_refresh_token_keeps_the_old_one():
    original = make_codex_auth(refresh_token="keep-me")
    with patch(
        "urllib.request.urlopen",
        lambda req, timeout=None: _grant_response({"access_token": make_jwt({"exp": 1})}),
    ):
        outcome = codex.try_refresh(original)
    assert json.loads(outcome.credentials)["tokens"]["refresh_token"] == "keep-me"


def test_no_refresh_token_is_a_permanent_failure():
    blob = json.dumps({"tokens": {"access_token": "x"}})
    outcome = codex.try_refresh(blob)
    assert outcome.credentials is None
    assert outcome.error == "no_refresh_token"


def test_a_400_is_invalid_grant_and_permanent():
    def _raise(req, timeout=None):
        raise urllib.error.HTTPError(codex.TOKEN_URL, 400, "Bad", {}, None)

    with patch("urllib.request.urlopen", _raise):
        outcome = codex.try_refresh(make_codex_auth())
    assert outcome.error == "invalid_grant"


def test_a_500_is_transient():
    def _raise(req, timeout=None):
        raise urllib.error.HTTPError(codex.TOKEN_URL, 503, "Down", {}, None)

    with patch("urllib.request.urlopen", _raise):
        outcome = codex.try_refresh(make_codex_auth())
    assert outcome.error == "transient"


def test_a_response_missing_an_access_token_is_transient_and_writes_nothing():
    with patch(
        "urllib.request.urlopen",
        lambda req, timeout=None: _grant_response({"token_type": "Bearer"}),
    ):
        outcome = codex.try_refresh(make_codex_auth())
    assert outcome.credentials is None
    assert outcome.error == "transient"
