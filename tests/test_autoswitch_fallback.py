"""Cross-provider fallback: the engine moves the router when Claude runs dry.

The flip is deliberately hard to trigger. Every Claude account must be
*measured* and at zero headroom, the router must be installed, the mode must
not be pinned, and a Codex account must exist. Each of those is a test here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_swap.autoswitch import BackendSwitchEvent
from claude_swap.codex_store import CodexAccountStore
from claude_swap.router import cliproxy, install as router_install
from claude_swap.router.mode import RouterMode, read_mode, write_mode
from claude_swap.settings import AutoSwitchSettings
from claude_swap.usage_store import UsageEntry
from tests.providers.conftest import make_codex_auth
from tests.test_autoswitch import EngineHarness

CODEX_A = ("9", "codex-a@example.com", "acc-a")
CODEX_B = ("8", "codex-b@example.com", "acc-b")


def _usage(pct: float) -> dict:
    return {"five_hour": {"pct": pct}, "seven_day": {"pct": 0.0}}


def _entry(value: dict | None, now: float) -> UsageEntry:
    if value is None:
        return UsageEntry()
    return UsageEntry(last_good=value, fetched_at=now, age_s=0.0)


def _add_codex(harness: EngineHarness, slot: str, email: str, account_id: str) -> None:
    data = harness.switcher._get_sequence_data()
    data["accounts"][slot] = {
        "email": email,
        "uuid": account_id,
        "organizationUuid": "",
        "organizationName": "",
        "provider": "codex",
    }
    harness.switcher._write_json(harness.switcher.sequence_file, data)
    CodexAccountStore().write_credential(
        slot, email, make_codex_auth(email=email, account_id=account_id)
    )


def _install_router(temp_home: Path) -> None:
    path = temp_home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8318"}}),
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _no_backend_process(monkeypatch):
    monkeypatch.setattr(cliproxy, "start", lambda: (True, "started (stubbed)"))
    monkeypatch.setattr(cliproxy, "stop", lambda: (True, "stopped (stubbed)"))
    monkeypatch.setattr(cliproxy, "unit_active", lambda: True)


@pytest.fixture
def spent(temp_home: Path) -> EngineHarness:
    """Two Claude accounts at 100%, two Codex accounts, router installed."""
    harness = EngineHarness(temp_home, fallback_provider="codex")
    harness.seed(1, "a@example.com")
    harness.seed(2, "b@example.com")
    harness.make_live("a@example.com", 1)
    _add_codex(harness, *CODEX_A)
    _install_router(temp_home)
    return harness


def _tick(harness: EngineHarness, claude: dict[str, float | None], codex=None):
    now = harness.clock.now
    entries = {
        num: _entry(_usage(pct) if pct is not None else None, now)
        for num, pct in claude.items()
    }
    for slot, pct in (codex or {}).items():
        entries[slot] = _entry(_usage(pct) if pct is not None else None, now)
    return harness.tick_with_entries(entries)


class TestFlipToCodex:
    def test_it_flips_when_every_claude_account_is_spent(self, spent):
        _tick(spent, {"1": 100.0, "2": 100.0})

        mode = read_mode()
        assert mode.provider == "codex"
        assert mode.slot == CODEX_A[0]

    def test_it_publishes_the_credential(self, spent):
        _tick(spent, {"1": 100.0, "2": 100.0})
        assert cliproxy.credential_email() == CODEX_A[1]

    def test_the_flip_is_not_pinned(self, spent):
        _tick(spent, {"1": 100.0, "2": 100.0})
        assert read_mode().pinned is False

    def test_it_says_so_loudly(self, spent):
        _tick(spent, {"1": 100.0, "2": 100.0})

        event = next(e for e in spent.events if isinstance(e, BackendSwitchEvent))
        assert event.to_provider == "codex"
        assert event.account["email"] == CODEX_A[1]
        assert "GPT model answers" in event.human()

    def test_one_account_with_headroom_stops_it(self, spent):
        _tick(spent, {"1": 100.0, "2": 40.0})
        assert read_mode().provider == "claude"

    def test_an_account_over_the_threshold_does_not_stop_it(self, spent):
        # 97% used is past the 90% switch threshold, so its 3 points of
        # headroom are not somewhere the engine would ever send a session.
        # Counting them as "free" is what kept the backend on Claude while
        # every account was over the line.
        _tick(spent, {"1": 100.0, "2": 97.0})
        assert read_mode().provider == "codex"

    def test_an_account_below_the_threshold_stops_it(self, spent):
        # At the threshold the engine already wants off the account, so the
        # boundary sits just under it: 89% used is somewhere to land, 90% is
        # not. Same comparison rotation uses.
        _tick(spent, {"1": 100.0, "2": 89.0})
        assert read_mode().provider == "claude"

    def test_an_account_exactly_at_the_threshold_does_not_stop_it(self, spent):
        _tick(spent, {"1": 100.0, "2": 90.0})
        assert read_mode().provider == "codex"

    def test_an_unmeasured_account_stops_it(self, spent):
        _tick(spent, {"1": 100.0, "2": None})
        assert read_mode().provider == "claude"

    def test_it_picks_the_codex_account_with_the_most_headroom(self, spent):
        _add_codex(spent, *CODEX_B)

        _tick(spent, {"1": 100.0, "2": 100.0}, codex={CODEX_A[0]: 90.0, CODEX_B[0]: 5.0})

        assert read_mode().slot == CODEX_B[0]

    def test_it_stays_on_claude_when_codex_is_spent_too(self, spent):
        _tick(spent, {"1": 100.0, "2": 100.0}, codex={CODEX_A[0]: 100.0})

        assert read_mode().provider == "claude"
        assert any(
            "Codex account is exhausted" in getattr(e, "message", "")
            for e in spent.events
        )


class TestItRefusesWithoutTheParts:
    def test_off_by_default(self, temp_home: Path):
        harness = EngineHarness(temp_home)  # fallback_provider defaults to off
        harness.seed(1, "a@example.com")
        harness.make_live("a@example.com", 1)
        _add_codex(harness, *CODEX_A)
        _install_router(temp_home)

        _tick(harness, {"1": 100.0})

        assert read_mode().provider == "claude"

    def test_it_needs_the_router(self, temp_home: Path):
        harness = EngineHarness(temp_home, fallback_provider="codex")
        harness.seed(1, "a@example.com")
        harness.make_live("a@example.com", 1)
        _add_codex(harness, *CODEX_A)

        _tick(harness, {"1": 100.0})

        assert read_mode().provider == "claude"
        assert any(
            "router is not installed" in getattr(e, "message", "")
            for e in harness.events
        )

    def test_it_needs_a_codex_account(self, temp_home: Path):
        harness = EngineHarness(temp_home, fallback_provider="codex")
        harness.seed(1, "a@example.com")
        harness.make_live("a@example.com", 1)
        _install_router(temp_home)

        _tick(harness, {"1": 100.0})

        assert read_mode().provider == "claude"
        assert any(
            "no Codex account" in getattr(e, "message", "") for e in harness.events
        )

    def test_it_warns_once_not_every_tick(self, temp_home: Path):
        harness = EngineHarness(temp_home, fallback_provider="codex")
        harness.seed(1, "a@example.com")
        harness.make_live("a@example.com", 1)
        _add_codex(harness, *CODEX_A)

        _tick(harness, {"1": 100.0})
        _tick(harness, {"1": 100.0})

        warnings = [e for e in harness.events if e.kind == "config-warning"]
        assert len(warnings) == 1

    def test_dry_run_never_flips(self, spent):
        spent.engine.dry_run = True

        _tick(spent, {"1": 100.0, "2": 100.0})

        assert read_mode().provider == "claude"

    def test_dry_run_still_says_what_it_would_do(self, spent):
        spent.engine.dry_run = True

        _tick(spent, {"1": 100.0, "2": 100.0})

        event = next(e for e in spent.events if isinstance(e, BackendSwitchEvent))
        assert event.dry_run is True
        assert event.human().startswith("[dry-run]")

    def test_dry_run_still_reports_a_missing_router(self, temp_home: Path):
        harness = EngineHarness(temp_home, fallback_provider="codex")
        harness.seed(1, "a@example.com")
        harness.make_live("a@example.com", 1)
        _add_codex(harness, *CODEX_A)
        harness.engine.dry_run = True

        _tick(harness, {"1": 100.0})

        assert any(
            "router is not installed" in getattr(e, "message", "")
            for e in harness.events
        )

    def test_a_pinned_mode_outranks_the_engine(self, spent):
        write_mode(RouterMode(provider="claude", pinned=True))

        _tick(spent, {"1": 100.0, "2": 100.0})

        assert read_mode().provider == "claude"


class TestFlipBack:
    def test_it_returns_to_claude_when_an_account_recovers(self, spent):
        _tick(spent, {"1": 100.0, "2": 100.0})
        assert read_mode().provider == "codex"
        spent.clock.advance(spent.settings.cooldown_seconds + 1)

        _tick(spent, {"1": 10.0, "2": 100.0})

        assert read_mode().provider == "claude"

    def test_a_recovery_inside_the_hysteresis_band_is_not_enough(self, spent):
        # Leaving needs utilization over the threshold (90); returning needs it
        # under threshold - hysteresis (80). 85% used sits in the band: past
        # the leave line, short of the return line, so the backend stays put
        # rather than flipping on an account grazing the threshold.
        _tick(spent, {"1": 100.0, "2": 100.0})
        assert read_mode().provider == "codex"
        spent.clock.advance(spent.settings.cooldown_seconds + 1)

        _tick(spent, {"1": 85.0, "2": 100.0})

        assert read_mode().provider == "codex"

    def test_a_recovery_past_the_hysteresis_band_returns(self, spent):
        _tick(spent, {"1": 100.0, "2": 100.0})
        spent.clock.advance(spent.settings.cooldown_seconds + 1)

        _tick(spent, {"1": 79.0, "2": 100.0})

        assert read_mode().provider == "claude"

    def test_the_widest_band_the_settings_allow_still_returns(self, temp_home: Path):
        """threshold 50 with hysteresisPct 50 puts the return line at zero.

        Both values pass their own SETTING_SPECS bounds (settings.py:111 and
        :123), and subtracting one from the other leaves a line no utilization
        can be under. Without a floor the fallback becomes a one-way door: the
        backend goes to Codex once and no Claude recovery, not even a window
        that has fully reset, can bring the sessions back.
        """
        harness = EngineHarness(
            temp_home,
            fallback_provider="codex",
            threshold=50.0,
            hysteresis_pct=50.0,
        )
        harness.seed(1, "a@example.com")
        harness.make_live("a@example.com", 1)
        _add_codex(harness, *CODEX_A)
        _install_router(temp_home)

        _tick(harness, {"1": 100.0})
        assert read_mode().provider == "codex"
        harness.clock.advance(harness.settings.cooldown_seconds + 1)

        _tick(harness, {"1": 0.0})

        assert read_mode().provider == "claude"

    def test_it_says_so(self, spent):
        _tick(spent, {"1": 100.0, "2": 100.0})
        spent.clock.advance(spent.settings.cooldown_seconds + 1)
        spent.events.clear()

        _tick(spent, {"1": 10.0, "2": 100.0})

        event = next(e for e in spent.events if isinstance(e, BackendSwitchEvent))
        assert event.to_provider == "claude"

    def test_it_takes_the_rotated_token_back(self, spent):
        _tick(spent, {"1": 100.0, "2": 100.0})
        record = cliproxy.read_credential()
        record["refresh_token"] = "rotated-by-the-backend"
        next(cliproxy.auth_dir().glob("*.json")).write_text(
            json.dumps(record), encoding="utf-8"
        )
        spent.clock.advance(spent.settings.cooldown_seconds + 1)

        _tick(spent, {"1": 10.0, "2": 100.0})

        stored = json.loads(
            CodexAccountStore().read_credential(CODEX_A[0], CODEX_A[1])
        )
        assert stored["tokens"]["refresh_token"] == "rotated-by-the-backend"

    def test_a_pinned_codex_mode_is_left_alone(self, spent):
        write_mode(RouterMode(provider="codex", slot=CODEX_A[0], pinned=True))

        _tick(spent, {"1": 10.0, "2": 10.0})

        assert read_mode().provider == "codex"


class TestCooldown:
    def test_it_will_not_flip_straight_back(self, spent):
        _tick(spent, {"1": 100.0, "2": 100.0})
        assert read_mode().provider == "codex"

        _tick(spent, {"1": 10.0, "2": 100.0})  # no clock advance

        assert read_mode().provider == "codex"

    def test_the_cooldown_expires(self, spent):
        _tick(spent, {"1": 100.0, "2": 100.0})
        spent.clock.advance(spent.settings.cooldown_seconds + 1)

        _tick(spent, {"1": 10.0, "2": 100.0})

        assert read_mode().provider == "claude"


class TestSettings:
    def test_the_key_round_trips(self):
        assert AutoSwitchSettings().fallback_provider == "off"
        assert AutoSwitchSettings(fallback_provider="codex").fallback_provider == "codex"


class TestStatusNamesTheBackend:
    def test_it_is_silent_in_claude_mode(self, spent, capsys):
        spent.switcher.status()
        assert "Backend:" not in capsys.readouterr().out

    def test_it_names_codex(self, spent, capsys):
        write_mode(RouterMode(provider="codex", slot=CODEX_A[0]))

        spent.switcher.status()

        out = capsys.readouterr().out
        assert "Backend:" in out
        assert "codex" in out


def _mark_api_key(harness: EngineHarness, num: int) -> None:
    data = harness.switcher._get_sequence_data()
    data["accounts"][str(num)]["kind"] = "api_key"
    harness.switcher._write_json(harness.switcher.sequence_file, data)


class TestWhichAccountsCountAsClaude:
    """Only slots the engine could actually switch to."""

    def test_an_api_key_account_in_rotation_stops_the_flip(self, temp_home: Path):
        harness = EngineHarness(
            temp_home, fallback_provider="codex", include_api_key_accounts=True
        )
        harness.seed(1, "a@example.com")
        harness.seed(2, "key@example.com")
        harness.make_live("a@example.com", 1)
        _mark_api_key(harness, 2)
        _add_codex(harness, *CODEX_A)
        _install_router(temp_home)

        _tick(harness, {"1": 100.0})

        # The API-key account has no quota to run out of, so Claude still has
        # somewhere to go.
        assert read_mode().provider == "claude"

    def test_an_excluded_api_key_account_does_not_block_the_flip(
        self, temp_home: Path
    ):
        harness = EngineHarness(temp_home, fallback_provider="codex")
        harness.seed(1, "a@example.com")
        harness.seed(2, "key@example.com")
        harness.make_live("a@example.com", 1)
        _mark_api_key(harness, 2)
        _add_codex(harness, *CODEX_A)
        _install_router(temp_home)

        _tick(harness, {"1": 100.0})

        # Its headroom is permanently None. Counting it would mean "every
        # account measured" never holds, and the fallback never fires.
        assert read_mode().provider == "codex"

    def test_a_disabled_account_does_not_block_the_flip(self, temp_home: Path):
        harness = EngineHarness(temp_home, fallback_provider="codex")
        harness.seed(1, "a@example.com")
        harness.seed(2, "off@example.com")
        harness.make_live("a@example.com", 1)
        data = harness.switcher._get_sequence_data()
        data["accounts"]["2"]["disabled"] = True
        harness.switcher._write_json(harness.switcher.sequence_file, data)
        _add_codex(harness, *CODEX_A)
        _install_router(temp_home)

        _tick(harness, {"1": 100.0, "2": None})

        assert read_mode().provider == "codex"


class TestABrokenBackendCheckDoesNotStopTheTick:
    def test_the_engine_still_rotates_accounts(self, spent, monkeypatch):
        """Which backend answers is a side question, not the main job."""
        from claude_swap import autoswitch as autoswitch_module

        def explode(*_args, **_kwargs):
            raise RuntimeError("settings.json is not valid JSON")

        monkeypatch.setattr(
            autoswitch_module.AutoSwitchEngine,
            "_maybe_flip_backend",
            explode,
        )

        outcome = _tick(spent, {"1": 100.0, "2": 40.0})

        active = spent.switcher._get_sequence_data()["activeAccountNumber"]
        assert str(active) == "2"
        assert outcome is not None
        assert any(
            "the backend check failed" in getattr(e, "message", "")
            for e in spent.events
        )
