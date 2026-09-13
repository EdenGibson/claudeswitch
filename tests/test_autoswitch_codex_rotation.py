"""Rotating between Codex accounts while the router is serving Codex.

The cross-provider fallback in ``test_autoswitch_fallback`` moves the router
onto Codex and then stops looking. These tests cover what happens next: the
Codex account it landed on burns down too, and a second Codex account is
sitting there with a full window.

Rotation reuses the fallback's two lines exactly. Leaving an account needs
utilization at or above ``threshold``; arriving at one needs utilization
below ``threshold - hysteresisPct``. Returning to Claude outranks it, because
a Claude account with headroom is what the user actually asked for.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_swap.autoswitch import CodexRotateEvent
from claude_swap.codex_store import CodexAccountStore
from claude_swap.router import cliproxy
from claude_swap.router.mode import RouterMode, read_mode, write_mode
from tests.test_autoswitch_fallback import (
    CODEX_A,
    CODEX_B,
    _add_codex,
    _install_router,
    _tick,
)
from tests.test_autoswitch import EngineHarness


@pytest.fixture(autouse=True)
def _no_backend_process(monkeypatch):
    monkeypatch.setattr(cliproxy, "start", lambda: (True, "started (stubbed)"))
    monkeypatch.setattr(cliproxy, "stop", lambda: (True, "stopped (stubbed)"))
    monkeypatch.setattr(cliproxy, "unit_active", lambda: True)


@pytest.fixture
def on_codex(temp_home: Path) -> EngineHarness:
    """Router already serving Codex A, with Codex B idle beside it.

    Built by driving the real fallback rather than writing the mode file by
    hand, so the starting state is one the engine can actually reach.
    """
    harness = EngineHarness(temp_home, fallback_provider="codex")
    harness.seed(1, "a@example.com")
    harness.seed(2, "b@example.com")
    harness.make_live("a@example.com", 1)
    _add_codex(harness, *CODEX_A)
    _add_codex(harness, *CODEX_B)
    _install_router(temp_home)

    # Codex A wins the first pick, so the rotation tests start where the
    # fallback leaves off.
    _tick(harness, {"1": 100.0, "2": 100.0}, codex={CODEX_A[0]: 0.0, CODEX_B[0]: 50.0})
    assert read_mode().slot == CODEX_A[0]
    harness.clock.advance(harness.settings.cooldown_seconds + 1)
    harness.events.clear()
    return harness


class TestItRotates:
    def test_it_moves_off_a_spent_codex_account(self, on_codex):
        _tick(
            on_codex,
            {"1": 100.0, "2": 100.0},
            codex={CODEX_A[0]: 100.0, CODEX_B[0]: 5.0},
        )

        assert read_mode().slot == CODEX_B[0]

    def test_it_stays_on_codex(self, on_codex):
        _tick(
            on_codex,
            {"1": 100.0, "2": 100.0},
            codex={CODEX_A[0]: 100.0, CODEX_B[0]: 5.0},
        )

        assert read_mode().provider == "codex"

    def test_it_publishes_the_new_credential(self, on_codex):
        _tick(
            on_codex,
            {"1": 100.0, "2": 100.0},
            codex={CODEX_A[0]: 100.0, CODEX_B[0]: 5.0},
        )

        assert cliproxy.credential_email() == CODEX_B[1]

    def test_the_rotation_is_not_pinned(self, on_codex):
        _tick(
            on_codex,
            {"1": 100.0, "2": 100.0},
            codex={CODEX_A[0]: 100.0, CODEX_B[0]: 5.0},
        )

        assert read_mode().pinned is False

    def test_it_names_both_accounts(self, on_codex):
        _tick(
            on_codex,
            {"1": 100.0, "2": 100.0},
            codex={CODEX_A[0]: 100.0, CODEX_B[0]: 5.0},
        )

        rotated = [e for e in on_codex.events if isinstance(e, CodexRotateEvent)]
        assert len(rotated) == 1
        assert rotated[0].leaving["email"] == CODEX_A[1]
        assert rotated[0].account["email"] == CODEX_B[1]

    def test_it_picks_the_codex_account_with_the_most_headroom(self, on_codex):
        _add_codex(on_codex, "7", "codex-c@example.com", "acc-c")

        _tick(
            on_codex,
            {"1": 100.0, "2": 100.0},
            codex={CODEX_A[0]: 100.0, CODEX_B[0]: 40.0, "7": 5.0},
        )

        assert read_mode().slot == "7"


class TestTheLinesItUses:
    def test_a_live_account_below_the_threshold_stays(self, on_codex):
        _tick(
            on_codex,
            {"1": 100.0, "2": 100.0},
            codex={CODEX_A[0]: 89.0, CODEX_B[0]: 0.0},
        )

        assert read_mode().slot == CODEX_A[0]

    def test_a_live_account_exactly_at_the_threshold_moves(self, on_codex):
        _tick(
            on_codex,
            {"1": 100.0, "2": 100.0},
            codex={CODEX_A[0]: 90.0, CODEX_B[0]: 0.0},
        )

        assert read_mode().slot == CODEX_B[0]

    def test_a_target_inside_the_hysteresis_band_is_refused(self, on_codex):
        # Leaving needs 90% used; arriving needs under 80%. 85% is in the band,
        # so moving there would just buy another rotation a few minutes later.
        _tick(
            on_codex,
            {"1": 100.0, "2": 100.0},
            codex={CODEX_A[0]: 100.0, CODEX_B[0]: 85.0},
        )

        assert read_mode().slot == CODEX_A[0]

    def test_a_target_past_the_band_is_taken(self, on_codex):
        _tick(
            on_codex,
            {"1": 100.0, "2": 100.0},
            codex={CODEX_A[0]: 100.0, CODEX_B[0]: 79.0},
        )

        assert read_mode().slot == CODEX_B[0]

    def test_an_unmeasured_target_is_refused(self, on_codex):
        _tick(
            on_codex,
            {"1": 100.0, "2": 100.0},
            codex={CODEX_A[0]: 100.0, CODEX_B[0]: None},
        )

        assert read_mode().slot == CODEX_A[0]

    def test_an_unmeasured_live_account_is_not_called_spent(self, on_codex):
        """A reading we could not take is not a reading of zero. The same rule
        the flip onto Codex follows, and the reason this asserts on the events
        too: dropping the guard raises rather than rotates, and a mode file
        left alone by a crash would read as correct here."""
        _tick(
            on_codex,
            {"1": 100.0, "2": 100.0},
            codex={CODEX_A[0]: None, CODEX_B[0]: 0.0},
        )

        assert read_mode().slot == CODEX_A[0]
        assert not [e for e in on_codex.events if isinstance(e, CodexRotateEvent)]
        assert not [
            e for e in on_codex.events if "backend check failed" in getattr(e, "message", "")
        ]

    def test_it_says_when_every_codex_account_is_spent(self, on_codex):
        _tick(
            on_codex,
            {"1": 100.0, "2": 100.0},
            codex={CODEX_A[0]: 100.0, CODEX_B[0]: 100.0},
        )

        assert read_mode().slot == CODEX_A[0]
        assert any(
            "no Codex account has headroom" in getattr(e, "message", "")
            for e in on_codex.events
        )

    def test_it_warns_once_not_every_tick(self, on_codex):
        for _ in range(3):
            _tick(
                on_codex,
                {"1": 100.0, "2": 100.0},
                codex={CODEX_A[0]: 100.0, CODEX_B[0]: 100.0},
            )
            on_codex.clock.advance(on_codex.settings.cooldown_seconds + 1)

        warnings = [
            e
            for e in on_codex.events
            if "no Codex account has headroom" in getattr(e, "message", "")
        ]
        assert len(warnings) == 1


class TestWhatOutranksIt:
    def test_returning_to_claude_wins(self, on_codex):
        """A free Claude account beats a fresh Codex one: it is what the user
        signed up for, and the router's Codex mode is the fallback."""
        _tick(
            on_codex,
            {"1": 10.0, "2": 100.0},
            codex={CODEX_A[0]: 100.0, CODEX_B[0]: 0.0},
        )

        assert read_mode().provider == "claude"

    def test_a_pinned_mode_is_left_alone(self, on_codex):
        write_mode(RouterMode(provider="codex", slot=CODEX_A[0], pinned=True))

        _tick(
            on_codex,
            {"1": 100.0, "2": 100.0},
            codex={CODEX_A[0]: 100.0, CODEX_B[0]: 0.0},
        )

        assert read_mode().slot == CODEX_A[0]

    def test_the_cooldown_holds_it(self, temp_home: Path):
        """The flip onto Codex A and the rotation off it are the same class of
        move, so the same cooldown covers both. No clock advance here."""
        harness = EngineHarness(temp_home, fallback_provider="codex")
        harness.seed(1, "a@example.com")
        harness.make_live("a@example.com", 1)
        _add_codex(harness, *CODEX_A)
        _add_codex(harness, *CODEX_B)
        _install_router(temp_home)

        _tick(harness, {"1": 100.0}, codex={CODEX_A[0]: 0.0, CODEX_B[0]: 50.0})
        assert read_mode().slot == CODEX_A[0]

        _tick(harness, {"1": 100.0}, codex={CODEX_A[0]: 100.0, CODEX_B[0]: 0.0})

        assert read_mode().slot == CODEX_A[0]

    def test_the_cooldown_expires(self, on_codex):
        _tick(
            on_codex,
            {"1": 100.0, "2": 100.0},
            codex={CODEX_A[0]: 100.0, CODEX_B[0]: 0.0},
        )

        assert read_mode().slot == CODEX_B[0]

    def test_dry_run_never_rotates(self, temp_home: Path, on_codex):
        on_codex.engine.dry_run = True

        _tick(
            on_codex,
            {"1": 100.0, "2": 100.0},
            codex={CODEX_A[0]: 100.0, CODEX_B[0]: 0.0},
        )

        assert read_mode().slot == CODEX_A[0]

    def test_dry_run_still_says_what_it_would_do(self, on_codex):
        on_codex.engine.dry_run = True

        _tick(
            on_codex,
            {"1": 100.0, "2": 100.0},
            codex={CODEX_A[0]: 100.0, CODEX_B[0]: 0.0},
        )

        rotated = [e for e in on_codex.events if isinstance(e, CodexRotateEvent)]
        assert len(rotated) == 1
        assert rotated[0].dry_run is True

    def test_it_is_off_when_the_fallback_is_off(self, temp_home: Path):
        """``fallbackProvider`` off means the engine does not steer the
        router at all, Codex mode included — that mode can then only come
        from ``cswap backend``, which pins."""
        harness = EngineHarness(temp_home)
        harness.seed(1, "a@example.com")
        harness.make_live("a@example.com", 1)
        _add_codex(harness, *CODEX_A)
        _add_codex(harness, *CODEX_B)
        _install_router(temp_home)
        write_mode(RouterMode(provider="codex", slot=CODEX_A[0], pinned=False))

        _tick(
            harness,
            {"1": 100.0},
            codex={CODEX_A[0]: 100.0, CODEX_B[0]: 0.0},
        )

        assert read_mode().slot == CODEX_A[0]


class TestTheTokenSurvives:
    def test_it_takes_the_leaving_account_rotated_token_back(self, on_codex):
        """CLIProxyAPI refreshes in place and an OpenAI refresh token is
        single use, so the account it is leaving must be read back before its
        credential file is overwritten."""
        record = cliproxy.read_credential()
        record["refresh_token"] = "rotated-by-the-backend"
        next(cliproxy.auth_dir().glob("*.json")).write_text(
            json.dumps(record), encoding="utf-8"
        )

        _tick(
            on_codex,
            {"1": 100.0, "2": 100.0},
            codex={CODEX_A[0]: 100.0, CODEX_B[0]: 0.0},
        )

        assert read_mode().slot == CODEX_B[0]
        stored = json.loads(
            CodexAccountStore().read_credential(CODEX_A[0], CODEX_A[1])
        )
        assert stored["tokens"]["refresh_token"] == "rotated-by-the-backend"


class TestABrokenRotationDoesNotStopTheTick:
    def test_the_engine_still_rotates_claude_accounts(self, on_codex, monkeypatch):
        from claude_swap.router import switching

        def explode(*_args, **_kwargs):
            raise RuntimeError("cliproxy is wedged")

        monkeypatch.setattr(switching, "activate_codex", explode)

        outcome = _tick(
            on_codex,
            {"1": 100.0, "2": 100.0},
            codex={CODEX_A[0]: 100.0, CODEX_B[0]: 0.0},
        )

        assert outcome is not None
        assert any(
            "the backend check failed" in getattr(e, "message", "")
            for e in on_codex.events
        )
