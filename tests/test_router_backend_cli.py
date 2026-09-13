"""`cswap backend ...` — what it writes, and what it refuses to do."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_swap import paths, router_cli
from claude_swap.codex_store import CodexAccountStore
from claude_swap.router import cliproxy, install as router_install, switching
from claude_swap.router.mode import RouterMode, read_mode, write_mode
from claude_swap.settings import set_setting
from claude_swap.switcher import ClaudeAccountSwitcher
from tests.providers.conftest import make_codex_auth

CODEX_EMAIL = "codex@example.com"
CODEX_ID = "acc-codex"


def _write_registry(data: dict) -> None:
    root = paths.get_backup_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "sequence.json").write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_services(monkeypatch):
    """Never start a real service, and never ask a real port for health."""
    monkeypatch.setattr(cliproxy, "start", lambda: (True, "started (stubbed)"))
    monkeypatch.setattr(cliproxy, "stop", lambda: (True, "stopped (stubbed)"))
    monkeypatch.setattr(router_cli, "_health", lambda port: {"upstreamReachable": True})


@pytest.fixture
def pool(temp_home: Path) -> ClaudeAccountSwitcher:
    """Slot 1 Claude, slots 2 and 3 Codex, slot 2 live."""
    _write_registry(
        {
            "activeAccountNumber": 1,
            "sequence": [1, 2, 3],
            "accounts": {
                "1": {"email": "claude@example.com", "uuid": "u-1"},
                "2": {"email": CODEX_EMAIL, "uuid": CODEX_ID, "provider": "codex"},
                "3": {
                    "email": "other@example.com",
                    "uuid": "acc-other",
                    "provider": "codex",
                },
            },
            "activeProviderAccounts": {"codex": "2"},
        }
    )
    store = CodexAccountStore()
    live = make_codex_auth(email=CODEX_EMAIL, account_id=CODEX_ID)
    store.write_credential("2", CODEX_EMAIL, live)
    store.write_credential(
        "3",
        "other@example.com",
        make_codex_auth(email="other@example.com", account_id="acc-other"),
    )
    store.write_live(live)
    return ClaudeAccountSwitcher()


class TestBackendCodex:
    def test_it_writes_the_mode(self, pool, capsys):
        router_cli.backend_command(["codex"])

        mode = read_mode()
        assert mode.provider == "codex"
        assert mode.slot == "2"
        assert mode.pinned is True

    def test_it_publishes_the_credential(self, pool, capsys):
        router_cli.backend_command(["codex"])

        assert cliproxy.credential_email() == CODEX_EMAIL

    def test_it_defaults_to_the_live_codex_account(self, pool, capsys):
        router_cli.backend_command(["codex"])
        assert read_mode().slot == "2"

    def test_a_named_account_is_used(self, pool, capsys):
        router_cli.backend_command(["codex", "3"])

        assert read_mode().slot == "3"
        assert cliproxy.credential_email() == "other@example.com"

    def test_a_claude_account_is_refused(self, pool, capsys):
        with pytest.raises(SystemExit) as exc:
            router_cli.backend_command(["codex", "1"])

        assert exc.value.code == 1
        assert "not a Codex account" in capsys.readouterr().err
        assert read_mode().provider == "claude"

    def test_an_empty_pool_is_refused(self, temp_home: Path, capsys):
        _write_registry({"accounts": {}, "sequence": []})

        with pytest.raises(SystemExit):
            router_cli.backend_command(["codex"])

        assert "no Codex accounts" in capsys.readouterr().err


class TestBackendClaude:
    def test_it_writes_the_mode(self, pool, capsys):
        router_cli.backend_command(["codex"])
        router_cli.backend_command(["claude"])

        assert read_mode().provider == "claude"
        assert read_mode().pinned is True

    def test_it_takes_a_rotated_token_back(self, pool, capsys):
        router_cli.backend_command(["codex"])
        record = cliproxy.read_credential()
        record["refresh_token"] = "rotated-by-the-backend"
        path = next(cliproxy.auth_dir().glob("*.json"))
        path.write_text(json.dumps(record), encoding="utf-8")

        router_cli.backend_command(["claude"])

        stored = json.loads(CodexAccountStore().read_credential("2", CODEX_EMAIL))
        assert stored["tokens"]["refresh_token"] == "rotated-by-the-backend"

    def test_a_rotated_token_also_reaches_the_live_file(self, pool, capsys):
        router_cli.backend_command(["codex"])
        record = cliproxy.read_credential()
        record["refresh_token"] = "rotated-by-the-backend"
        next(cliproxy.auth_dir().glob("*.json")).write_text(
            json.dumps(record), encoding="utf-8"
        )

        router_cli.backend_command(["claude"])

        live = json.loads(CodexAccountStore().read_live())
        assert live["tokens"]["refresh_token"] == "rotated-by-the-backend"

    def test_an_unchanged_token_is_left_alone(self, pool, capsys):
        before = CodexAccountStore().read_credential("2", CODEX_EMAIL)
        router_cli.backend_command(["codex"])
        router_cli.backend_command(["claude"])

        assert CodexAccountStore().read_credential("2", CODEX_EMAIL) == before

    def test_it_stops_the_backend_before_reading_the_token(self, pool, monkeypatch):
        """Left running, CLIProxyAPI rotates a token nobody is watching."""
        order: list[str] = []
        real_sync = switching.sync_back
        monkeypatch.setattr(cliproxy, "stop", lambda: order.append("stop") or (True, ""))
        monkeypatch.setattr(
            switching,
            "sync_back",
            lambda *a, **kw: order.append("sync") or real_sync(*a, **kw),
        )

        router_cli.backend_command(["codex"])
        router_cli.backend_command(["claude"])

        assert order == ["stop", "sync"]


class TestBackendAuto:
    def test_it_clears_the_pin_and_keeps_the_provider(self, pool, capsys):
        router_cli.backend_command(["codex"])

        router_cli.backend_command(["auto"])

        mode = read_mode()
        assert mode.provider == "codex"
        assert mode.pinned is False

    def test_it_says_nothing_moves_the_backend_when_fallback_is_off(self, pool, capsys):
        router_cli.backend_command(["auto"])

        assert "fallbackProvider" in capsys.readouterr().out

    def test_it_names_the_fallback_when_it_is_on(self, pool, capsys):
        set_setting(paths.get_backup_root(), "autoswitch.fallbackProvider", "codex")

        router_cli.backend_command(["auto"])

        out = capsys.readouterr().out
        assert "every Claude" in out
        assert "fallbackProvider" not in out


class TestBackendReport:
    def test_no_argument_prints_the_mode(self, pool, capsys):
        write_mode(RouterMode(provider="codex", slot="2", pinned=True))

        router_cli.backend_command([])

        assert "codex" in capsys.readouterr().out

    def test_it_warns_when_the_router_is_not_installed(self, pool, capsys):
        router_cli.backend_command([])
        assert "not installed" in capsys.readouterr().out

    def test_an_unknown_backend_is_a_usage_error(self, pool, capsys):
        with pytest.raises(SystemExit) as exc:
            router_cli.backend_command(["gemini"])
        assert exc.value.code == 2


def _rotate_the_published_token(value: str) -> None:
    """Pretend CLIProxyAPI refreshed the credential it is serving."""
    record = cliproxy.read_credential()
    record["refresh_token"] = value
    next(cliproxy.auth_dir().glob("*.json")).write_text(
        json.dumps(record), encoding="utf-8"
    )


class TestReplacingOneCodexAccountWithAnother:
    """The account being replaced holds a rotated single-use token."""

    def test_it_takes_the_replaced_accounts_token_back(self, pool, capsys):
        router_cli.backend_command(["codex", "2"])
        _rotate_the_published_token("rotated-while-slot-2-served")

        router_cli.backend_command(["codex", "3"])

        stored = json.loads(CodexAccountStore().read_credential("2", CODEX_EMAIL))
        assert stored["tokens"]["refresh_token"] == "rotated-while-slot-2-served"

    def test_a_pool_switch_takes_it_back_too(self, pool, capsys):
        router_cli.backend_command(["codex", "2"])
        _rotate_the_published_token("rotated-before-the-switch")

        pool.switch_to("3")

        stored = json.loads(CodexAccountStore().read_credential("2", CODEX_EMAIL))
        assert stored["tokens"]["refresh_token"] == "rotated-before-the-switch"


class TestTheModeFileFollowsTheBackend:
    """In codex mode the router has nowhere else to send a request."""

    def test_a_backend_that_will_not_start_leaves_the_mode_alone(
        self, pool, monkeypatch
    ):
        monkeypatch.setattr(cliproxy, "unit_active", lambda: False)
        monkeypatch.setattr(cliproxy, "start", lambda: (False, "unit failed"))
        monkeypatch.setattr(cliproxy, "reachable", lambda timeout=1.0: False)

        ok, message = switching.activate_codex(pool, "2", CODEX_EMAIL, pinned=True)

        assert ok is False
        assert "unit failed" in message
        assert read_mode().provider == "claude"

    def test_a_hand_run_backend_still_counts(self, pool, monkeypatch):
        monkeypatch.setattr(cliproxy, "unit_active", lambda: False)
        monkeypatch.setattr(cliproxy, "start", lambda: (False, "no unit installed"))
        monkeypatch.setattr(cliproxy, "reachable", lambda timeout=1.0: True)

        ok, note = switching.activate_codex(pool, "2", CODEX_EMAIL, pinned=True)

        assert ok is True
        assert read_mode().provider == "codex"
        assert "no unit installed" in note


class TestRouterUninstall:
    def test_it_returns_to_claude_before_tearing_down(self, pool, capsys):
        router_cli.backend_command(["codex", "2"])
        _rotate_the_published_token("rotated-before-teardown")

        router_cli.router_command(["uninstall"])

        stored = json.loads(CodexAccountStore().read_credential("2", CODEX_EMAIL))
        assert stored["tokens"]["refresh_token"] == "rotated-before-teardown"
        assert read_mode().provider == "claude"


class TestTheSwitchSaysWhenTheRouterDidNotFollow:
    """Silence here means spending the account the user just left."""

    def test_the_warning_names_the_account_still_being_served(
        self, pool, monkeypatch, capsys
    ):
        router_cli.backend_command(["codex", "2"])
        monkeypatch.setattr(cliproxy, "ensure_running", lambda: (False, "unit failed"))
        capsys.readouterr()

        pool.switch_to("3")

        assert "still serving Account-2" in capsys.readouterr().out

    def test_json_output_carries_the_same_warning(self, pool, monkeypatch, capsys):
        router_cli.backend_command(["codex", "2"])
        monkeypatch.setattr(cliproxy, "ensure_running", lambda: (False, "unit failed"))

        result = pool.switch_to("3", json_output=True)

        assert result["warnings"] and "Account-2" in result["warnings"][0]

    def test_a_switch_the_router_followed_warns_about_nothing(self, pool, capsys):
        router_cli.backend_command(["codex", "2"])

        assert pool.switch_to("3", json_output=True)["warnings"] == []


class TestTearDown:
    def test_it_reports_nothing_when_the_router_was_never_installed(self, pool):
        assert switching.tear_down(pool) == ""

    def test_it_returns_to_claude_and_reports_the_removal(self, pool):
        router_install.install(write_unit=False)
        router_cli.backend_command(["codex", "2"])

        note = switching.tear_down(pool)

        assert "Router" in note
        assert read_mode().provider == "claude"
        assert router_install.is_installed() is False


class TestSwitchFollowsTheRouter:
    def test_switching_moves_a_codex_router(self, pool, capsys):
        router_cli.backend_command(["codex", "2"])

        pool.switch_to("3")

        assert read_mode().slot == "3"
        assert cliproxy.credential_email() == "other@example.com"

    def test_switching_does_not_turn_the_router_on(self, pool, capsys):
        assert read_mode().provider == "claude"

        pool.switch_to("3")

        assert read_mode().provider == "claude"
        assert cliproxy.credential_email() == ""
