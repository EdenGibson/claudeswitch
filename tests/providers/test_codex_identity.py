"""Codex identity, fingerprint and expiry."""

from __future__ import annotations

import json
import time

from claude_swap.providers import codex
from tests.providers.conftest import make_codex_auth, make_jwt


def test_identity_reads_every_field():
    blob = make_codex_auth(
        email="eden@example.com",
        account_id="acc-42",
        plan="pro",
        org_id="org-42",
        org_title="Derive",
    )
    ident = codex.identity(blob)
    assert ident is not None
    assert ident.email == "eden@example.com"
    assert ident.account_uuid == "acc-42"
    assert ident.org_uuid == "org-42"
    assert ident.org_name == "Derive"
    assert ident.plan == "pro"


def test_identity_survives_an_expired_id_token():
    """Claims are read, never validated, so expiry must not matter."""
    blob = make_codex_auth(now=time.time() - 86_400)
    assert codex.identity(blob) is not None


def test_identity_with_no_organizations_is_personal():
    raw = json.loads(make_codex_auth())
    payload_claims = {
        "email": "solo@example.com",
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "acc-solo",
            "chatgpt_plan_type": "plus",
            "organizations": [],
        },
    }
    raw["tokens"]["id_token"] = make_jwt(payload_claims)
    ident = codex.identity(json.dumps(raw))
    assert ident is not None
    assert ident.org_uuid == ""
    assert ident.display_label == "solo@example.com [personal]"


def test_identity_of_garbage_is_none():
    assert codex.identity("not json") is None
    assert codex.identity(json.dumps({"tokens": {}})) is None
    assert codex.identity(json.dumps({"tokens": {"id_token": "a.b"}})) is None


def test_fingerprint_tracks_the_refresh_token_only():
    a = make_codex_auth(refresh_token="same", access_expires_in=864000)
    b = make_codex_auth(refresh_token="same", access_expires_in=100)
    c = make_codex_auth(refresh_token="different")
    assert codex.fingerprint(a) == codex.fingerprint(b)
    assert codex.fingerprint(a) != codex.fingerprint(c)


def test_fingerprint_of_garbage_is_none():
    assert codex.fingerprint("") is None
    assert codex.fingerprint("not json") is None


def test_expiry_reads_the_access_token_exp():
    fresh = make_codex_auth(access_expires_in=864_000)
    assert codex.is_expired(fresh) is False

    stale = make_codex_auth(access_expires_in=-1)
    assert codex.is_expired(stale) is True


def test_expiry_buffer_treats_an_imminent_expiry_as_expired():
    """Five minutes of headroom, matching oauth.OAUTH_EXPIRY_BUFFER_MS."""
    soon = make_codex_auth(access_expires_in=60)
    assert codex.is_expired(soon) is True


def test_unreadable_blob_is_not_reported_expired():
    """Unknown must not read as expired, or a bad parse triggers a refresh
    storm against a credential that was fine."""
    assert codex.is_expired("not json") is False
