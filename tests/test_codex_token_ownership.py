"""Who owns a Codex token family, and who must keep their hands off it.

While CLIProxyAPI serves a Codex account it refreshes that token every 15
minutes and holds a copy cswap did not write. An OpenAI refresh token is
single use, so a refresh taken by cswap retires the copy the backend still
holds. The backend's next refresh then fails with ``refresh_token_reused``,
which costs a browser re-login.

A usage reading must never be able to do that, and every collector reaches the
same fetch path: ``cswap list``, the TUI, the menu bar, the auto engine.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap.router import cliproxy
from claude_swap.router.mode import RouterMode, write_mode
from tests.providers.conftest import make_codex_auth
from tests.test_autoswitch import EngineHarness
from tests.test_autoswitch_fallback import CODEX_A, _add_codex, _install_router


@pytest.fixture(autouse=True)
def _no_backend_process(monkeypatch):
    monkeypatch.setattr(cliproxy, "start", lambda: (True, "started (stubbed)"))
    monkeypatch.setattr(cliproxy, "stop", lambda: (True, "stopped (stubbed)"))
    monkeypatch.setattr(cliproxy, "unit_active", lambda: True)


@pytest.fixture
def served(temp_home: Path) -> EngineHarness:
    """One Codex account, with the router pointed at it and its token expired.

    Expired on purpose: a live token would take the no-refresh path anyway, so
    the test could not tell a guard from a coincidence.
    """
    harness = EngineHarness(temp_home)
    harness.seed(1, "a@example.com")
    harness.make_live("a@example.com", 1)
    _add_codex(harness, *CODEX_A)
    _install_router(temp_home)
    write_mode(RouterMode(provider="codex", slot=CODEX_A[0], pinned=False))
    from claude_swap.codex_store import CodexAccountStore

    CodexAccountStore().write_credential(
        CODEX_A[0],
        CODEX_A[1],
        make_codex_auth(
            email=CODEX_A[1], account_id=CODEX_A[2], access_expires_in=-3600
        ),
    )
    return harness


def _fetch(harness: EngineHarness, calls: list[str]):
    """Run one usage fetch for the Codex slot, recording refresh attempts."""
    from claude_swap.oauth import RefreshOutcome
    from claude_swap.providers import codex

    def fake_refresh(blob, *_args, **_kwargs):
        calls.append(blob)
        return RefreshOutcome(None, "invalid_grant")

    info = (
        int(CODEX_A[0]),
        CODEX_A[1],
        "",
        "",
        False,
        harness.switcher._read_provider_material(CODEX_A[0], CODEX_A[1]),
        "",
    )
    with patch.object(codex, "try_refresh", fake_refresh):
        return harness.switcher._fetch_account_usage(info)


class TestAUsageReadNeverSpendsTheBackendsToken:
    def test_it_does_not_refresh_a_slot_the_router_serves(self, served):
        calls: list[str] = []

        _fetch(served, calls)

        assert calls == []

    def test_it_reports_the_expiry_instead(self, served):
        """The backend renews on its own cadence, so the next poll sees a fresh
        token. Reporting beats refreshing, and beats reporting nothing.

        The refresh here is stubbed to *succeed*, which is what makes the
        assertion mean something: with the guard gone, the record would carry
        usage. A failing stub returns the same sentinel down either path, so
        the test could not tell a guard from a coincidence.
        """
        from claude_swap.json_output import USAGE_TOKEN_EXPIRED
        from claude_swap.oauth import RefreshOutcome
        from claude_swap.providers import codex

        fresh = make_codex_auth(email=CODEX_A[1], account_id=CODEX_A[2])
        info = (
            int(CODEX_A[0]),
            CODEX_A[1],
            "",
            "",
            False,
            served.switcher._read_provider_material(CODEX_A[0], CODEX_A[1]),
            "",
        )
        with (
            patch.object(
                codex, "try_refresh", lambda *_a, **_k: RefreshOutcome(fresh, None)
            ),
            patch.object(codex, "fetch_usage", lambda _blob: {"seven_day": {"pct": 1.0}}),
        ):
            record = served.switcher._fetch_account_usage(info)

        assert record.sentinel == USAGE_TOKEN_EXPIRED
        assert record.usage is None

    def test_it_does_refresh_a_slot_the_router_does_not_serve(self, served):
        """No backend holds the family, so cswap is free to renew it. Without
        this the guard could be a blanket refusal and still look correct."""
        write_mode(RouterMode(provider="claude", slot=None, pinned=False))
        calls: list[str] = []

        _fetch(served, calls)

        assert len(calls) == 1

    def test_it_does_refresh_when_the_router_serves_another_slot(self, served):
        write_mode(RouterMode(provider="codex", slot="7", pinned=False))
        calls: list[str] = []

        _fetch(served, calls)

        assert len(calls) == 1
