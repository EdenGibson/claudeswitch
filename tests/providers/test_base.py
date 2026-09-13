"""Provider seam types."""

from __future__ import annotations

import dataclasses

import pytest

from claude_swap.providers import AccountIdentity


def test_identity_display_label_uses_org_name():
    ident = AccountIdentity(
        email="a@example.com",
        account_uuid="acc-1",
        org_uuid="org-1",
        org_name="Acme",
        plan="pro",
    )
    assert ident.display_label == "a@example.com [Acme]"


def test_identity_display_label_falls_back_to_personal():
    ident = AccountIdentity(email="a@example.com", account_uuid="acc-1")
    assert ident.display_label == "a@example.com [personal]"


def test_identity_is_hashable_and_frozen():
    ident = AccountIdentity(email="a@example.com", account_uuid="acc-1")
    assert {ident: 1}[ident] == 1
    with pytest.raises(dataclasses.FrozenInstanceError):
        ident.email = "b@example.com"
