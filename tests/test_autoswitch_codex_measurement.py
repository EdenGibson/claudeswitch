"""Measuring the Codex account the router is serving.

Rotation can only fire on a reading, and until now nothing took one. The
engine's scheduler builds its fetch plan from ``switchable_account_numbers``,
which excludes every non-Claude slot, so during an unattended ``cswap auto``
run a Codex row went stale the moment the fallback moved onto it.

Two rules are pinned here. The engine nominates the live Codex account on the
ordinary poll cadence, and it nominates every Codex account once that one
reads spent, so the target it picks is not chosen on a row from the last time
that account was live.

Reading the account the backend serves is only safe because of the rule in
``test_codex_token_ownership``: cswap takes the backend's own rotation rather
than refreshing a token family the backend still holds.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap.router import cliproxy
from claude_swap.router.mode import RouterMode, write_mode
from claude_swap.usage_store import UsageEntry
from tests.test_autoswitch_fallback import (
    CODEX_A,
    CODEX_B,
    _add_codex,
    _install_router,
)
from tests.test_autoswitch import EngineHarness


@pytest.fixture(autouse=True)
def _no_backend_process(monkeypatch):
    monkeypatch.setattr(cliproxy, "start", lambda: (True, "started (stubbed)"))
    monkeypatch.setattr(cliproxy, "stop", lambda: (True, "stopped (stubbed)"))
    monkeypatch.setattr(cliproxy, "unit_active", lambda: True)


def _usage(pct: float) -> dict:
    return {"five_hour": {"pct": pct}, "seven_day": {"pct": 0.0}}


def _entry(value: dict | None, now: float) -> UsageEntry:
    if value is None:
        return UsageEntry()
    return UsageEntry(last_good=value, fetched_at=now, age_s=0.0)


@pytest.fixture
def on_codex(temp_home: Path) -> EngineHarness:
    """Router pointed at Codex A, with Codex B beside it."""
    harness = EngineHarness(temp_home, fallback_provider="codex")
    harness.seed(1, "a@example.com")
    harness.make_live("a@example.com", 1)
    _add_codex(harness, *CODEX_A)
    _add_codex(harness, *CODEX_B)
    _install_router(temp_home)
    write_mode(RouterMode(provider="codex", slot=CODEX_A[0], pinned=False))
    return harness


def _tick_recording(harness: EngineHarness, entries: dict) -> list[set[str]]:
    """Tick, returning the ``fetch`` set of every usage collection it ran."""
    seen: list[set[str]] = []
    real = harness.switcher.usage_entries_by_account

    def spy(fetch=None, *, scheduled=False):
        seen.append(set(fetch) if fetch else set())
        return entries

    with patch.object(harness.switcher, "usage_entries_by_account", spy):
        harness.engine.tick()
    assert real is not None
    return seen


class TestItMeasuresTheLiveCodexAccount:
    def test_it_nominates_the_router_slot(self, on_codex):
        now = on_codex.clock.now
        entries = {
            "1": _entry(_usage(100.0), now),
            CODEX_A[0]: _entry(_usage(10.0), now),
            CODEX_B[0]: _entry(_usage(0.0), now),
        }

        seen = _tick_recording(on_codex, entries)

        assert any(CODEX_A[0] in fetch for fetch in seen)

    def test_it_leaves_codex_alone_in_claude_mode(self, on_codex):
        write_mode(RouterMode(provider="claude", slot=None, pinned=False))
        now = on_codex.clock.now
        entries = {
            "1": _entry(_usage(10.0), now),
            CODEX_A[0]: _entry(_usage(10.0), now),
            CODEX_B[0]: _entry(_usage(0.0), now),
        }

        seen = _tick_recording(on_codex, entries)

        assert not any(CODEX_A[0] in fetch for fetch in seen)

    def test_a_spent_live_account_nominates_every_codex_account(self, on_codex):
        """The target is about to be chosen, so its row must be current."""
        now = on_codex.clock.now
        entries = {
            "1": _entry(_usage(100.0), now),
            CODEX_A[0]: _entry(_usage(100.0), now),
            CODEX_B[0]: _entry(_usage(0.0), now),
        }

        seen = _tick_recording(on_codex, entries)

        assert any(CODEX_B[0] in fetch for fetch in seen)

    def test_a_healthy_live_account_does_not(self, on_codex):
        """One account's cadence, not the whole pool's, while nothing moves."""
        now = on_codex.clock.now
        entries = {
            "1": _entry(_usage(100.0), now),
            CODEX_A[0]: _entry(_usage(10.0), now),
            CODEX_B[0]: _entry(_usage(0.0), now),
        }

        seen = _tick_recording(on_codex, entries)

        assert not any(CODEX_B[0] in fetch for fetch in seen)
