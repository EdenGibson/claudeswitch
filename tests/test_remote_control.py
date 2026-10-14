"""Tests for per-account Remote Control bridges (claude_swap.remote_control)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_swap import remote_control as rc_mod
from claude_swap.exceptions import SessionError, SwitchError
from claude_swap.remote_control import (
    DIR_MARKER,
    RemoteControlManager,
    identity_key,
    is_rc_config_dir,
    rc_dir_for,
)
from claude_swap.switcher import ClaudeAccountSwitcher

ACCOUNTS = {
    "1": ("one@example.com", "org-1"),
    "2": ("two@example.com", "org-2"),
    "3": ("three@example.com", "org-3"),
}


def _seed(switcher: ClaudeAccountSwitcher, accounts: dict) -> None:
    switcher._setup_directories()
    switcher._write_json(
        switcher.sequence_file,
        {
            "activeAccountNumber": 1,
            "sequence": [int(n) for n in accounts],
            "accounts": {
                num: {"email": email, "organizationUuid": org, "uuid": f"u-{num}"}
                for num, (email, org) in accounts.items()
            },
        },
    )


@pytest.fixture
def switcher(temp_home: Path) -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher(debug=True)
    _seed(s, ACCOUNTS)
    claude = temp_home / ".claude"
    (claude / "settings.json").write_text('{"model": "x"}')
    (claude / "CLAUDE.md").write_text("shared memory")
    (claude / "skills" / "demo").mkdir(parents=True)
    (claude / "projects" / "proj" / "memory").mkdir(parents=True)
    (temp_home / ".claude.json").write_text(
        json.dumps(
            {
                "oauthAccount": {"emailAddress": "one@example.com"},
                "theme": "light",
                "mcpServers": {"srv": {"command": "x"}},
                "projects": {
                    "/work": {"hasTrustDialogAccepted": True, "lastCost": 3.5}
                },
            }
        )
    )
    return s


class FakeAuth:
    """Stands in for `claude auth status --json`: logged in as whoever the
    test says owns each directory."""

    def __init__(self) -> None:
        self.owner: dict[str, str] = {}

    def __call__(self, cmd, env=None, **kwargs):
        owner = self.owner.get(env["CLAUDE_CONFIG_DIR"])
        payload = (
            {"loggedIn": True, "authMethod": "claude.ai", "email": owner}
            if owner
            else {"loggedIn": False}
        )
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")


@pytest.fixture
def auth(monkeypatch) -> FakeAuth:
    fake = FakeAuth()
    monkeypatch.setattr(rc_mod.subprocess, "run", fake)
    return fake


class FakeProc:
    _next = 50000

    def __init__(self, cmd, env=None, cwd=None, **kwargs):
        FakeProc._next += 1
        self.pid = FakeProc._next
        self.cmd = cmd
        self.env = env
        self.cwd = cwd
        self.code: int | None = None

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        self.code = -9


@pytest.fixture
def spawned(monkeypatch) -> list[FakeProc]:
    procs: list[FakeProc] = []

    def fake_popen(cmd, **kwargs):
        p = FakeProc(cmd, **kwargs)
        procs.append(p)
        return p

    monkeypatch.setattr(rc_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(rc_mod, "kill_tree", lambda pid: None)
    return procs


def make_manager(switcher) -> RemoteControlManager:
    return RemoteControlManager(switcher, claude_bin="claude")


class TestDirectories:
    def test_dir_is_keyed_by_identity_not_slot(self, tmp_path):
        a = rc_dir_for(tmp_path, "one@example.com", "org-1")
        assert a == rc_dir_for(tmp_path, "one@example.com", "org-1")
        assert a != rc_dir_for(tmp_path, "one@example.com", "org-2")
        assert "1-" not in a.name  # no slot number in the name

    def test_is_rc_config_dir(self, tmp_path):
        inside = rc_dir_for(tmp_path, "one@example.com", "org-1")
        inside.mkdir(parents=True)
        assert is_rc_config_dir(tmp_path, inside)
        assert not is_rc_config_dir(tmp_path, tmp_path / "home" / ".claude")


class TestEnable:
    def test_default_off(self, switcher):
        assert make_manager(switcher).enabled_accounts() == []

    def test_enable_any_number_of_accounts(self, switcher, auth):
        rc = make_manager(switcher)
        for num in ACCOUNTS:
            account = rc.enable(num, login=False)
            assert account.number == num
            assert (account.rc_dir / DIR_MARKER).exists()
        assert [a.number for a in rc.enabled_accounts()] == ["1", "2", "3"]

    def test_enable_shares_customizations(self, switcher, auth, temp_home):
        rc = make_manager(switcher)
        account = rc.enable("2", login=False)
        d = account.rc_dir
        assert (d / "settings.json").read_text() == '{"model": "x"}'
        assert (d / "CLAUDE.md").read_text() == "shared memory"
        # Directories are live links, not copies: a write through the link
        # lands in the default profile (memory written by a bridge session).
        assert (d / "skills" / "demo").is_dir()
        (d / "projects" / "proj" / "memory" / "note.md").write_text("hi")
        assert (
            temp_home / ".claude" / "projects" / "proj" / "memory" / "note.md"
        ).read_text() == "hi"

    def test_enable_seeds_trust_and_mcp_but_no_identity(self, switcher, auth):
        account = make_manager(switcher).enable("2", login=False)
        cfg = json.loads((account.rc_dir / ".claude.json").read_text())
        assert cfg["projects"]["/work"] == {"hasTrustDialogAccepted": True}
        assert cfg["mcpServers"] == {"srv": {"command": "x"}}
        assert cfg["hasCompletedOnboarding"] is True
        assert "oauthAccount" not in cfg

    def test_enable_never_copies_stored_credentials(self, switcher, auth):
        account = make_manager(switcher).enable("2", login=False)
        assert not (account.rc_dir / ".credentials.json").exists()

    def test_enable_logs_in_when_not_logged_in(self, switcher, auth, monkeypatch):
        calls = []

        def fake_run(cmd, env=None, **kwargs):
            if cmd[1:3] == ["auth", "login"]:
                calls.append(cmd)
                auth.owner[env["CLAUDE_CONFIG_DIR"]] = "two@example.com"
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return auth(cmd, env=env)

        monkeypatch.setattr(rc_mod.subprocess, "run", fake_run)
        make_manager(switcher).enable("2")
        assert calls and calls[0][-2:] == ["--email", "two@example.com"]

    def test_login_as_wrong_account_fails(self, switcher, auth, monkeypatch):
        def fake_run(cmd, env=None, **kwargs):
            if cmd[1:3] == ["auth", "login"]:
                auth.owner[env["CLAUDE_CONFIG_DIR"]] = "one@example.com"
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return auth(cmd, env=env)

        monkeypatch.setattr(rc_mod.subprocess, "run", fake_run)
        with pytest.raises(SessionError, match="different account"):
            make_manager(switcher).enable("2")

    def test_disable(self, switcher, auth):
        rc = make_manager(switcher)
        rc.enable("1", login=False)
        rc.enable("2", login=False)
        rc.disable("1")
        assert [a.number for a in rc.enabled_accounts()] == ["2"]


class TestSupervisor:
    def _login_all(self, rc, auth):
        for account in rc.enabled_accounts():
            auth.owner[str(account.rc_dir)] = account.email

    def test_one_bridge_per_enabled_logged_in_account(
        self, switcher, auth, spawned, tmp_path
    ):
        rc = make_manager(switcher)
        rc.enable("1", login=False)
        rc.enable("3", login=False)
        self._login_all(rc, auth)
        rc.reconcile(tmp_path, ["--permission-mode", "acceptEdits"])
        assert len(spawned) == 2
        dirs = {p.env["CLAUDE_CONFIG_DIR"] for p in spawned}
        assert dirs == {str(a.rc_dir) for a in rc.enabled_accounts()}
        for p in spawned:
            assert p.cmd[1] == "remote-control"
            assert "--permission-mode" in p.cmd and "--name" in p.cmd
        # Steady state: nothing new spawned.
        rc.reconcile(tmp_path, [])
        assert len(spawned) == 2

    def test_auth_override_env_is_scrubbed(
        self, switcher, auth, spawned, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        rc = make_manager(switcher)
        rc.enable("1", login=False)
        self._login_all(rc, auth)
        rc.reconcile(tmp_path, [])
        assert "ANTHROPIC_API_KEY" not in spawned[0].env

    def test_not_logged_in_is_reported_not_spawned(
        self, switcher, auth, spawned, tmp_path
    ):
        rc = make_manager(switcher)
        rc.enable("2", login=False)
        rc.reconcile(tmp_path, [])
        assert spawned == []
        state = rc.read_state()["bridges"][identity_key("two@example.com", "org-2")]
        assert state["health"] == "needs-login"

    def test_exited_bridge_restarts_after_backoff(
        self, switcher, auth, spawned, tmp_path
    ):
        now = [1000.0]
        rc = RemoteControlManager(switcher, claude_bin="claude", clock=lambda: now[0])
        rc.enable("1", login=False)
        self._login_all(rc, auth)
        rc.reconcile(tmp_path, [])
        spawned[0].code = 1
        rc.reconcile(tmp_path, [])
        assert len(spawned) == 1  # inside the backoff
        now[0] += rc_mod.RESTART_BACKOFF_S + 1
        rc.reconcile(tmp_path, [])
        assert len(spawned) == 2
        key = identity_key("one@example.com", "org-1")
        assert rc.read_state()["bridges"][key]["restarts"] == 1

    def test_disabled_account_bridge_is_stopped(
        self, switcher, auth, spawned, tmp_path
    ):
        rc = make_manager(switcher)
        rc.enable("1", login=False)
        rc.enable("2", login=False)
        self._login_all(rc, auth)
        rc.reconcile(tmp_path, [])
        rc.disable("1")
        rc.reconcile(tmp_path, [])
        assert len(rc._running) == 1

    def test_account_removed_from_pool_is_pruned(
        self, switcher, auth, spawned, tmp_path
    ):
        rc = make_manager(switcher)
        account = rc.enable("2", login=False)
        self._login_all(rc, auth)
        rc.reconcile(tmp_path, [])
        _seed(switcher, {k: v for k, v in ACCOUNTS.items() if k != "2"})
        rc.reconcile(tmp_path, [])
        assert rc.enabled_accounts() == []
        assert not account.rc_dir.exists()

    def test_pruning_never_follows_links_into_the_default_profile(
        self, switcher, auth, spawned, tmp_path, temp_home
    ):
        rc = make_manager(switcher)
        account = rc.enable("2", login=False)
        rc.purge_dir(account.rc_dir)
        assert not account.rc_dir.exists()
        assert (temp_home / ".claude" / "skills" / "demo").is_dir()
        assert (temp_home / ".claude" / "settings.json").exists()
        assert (temp_home / ".claude" / "projects" / "proj" / "memory").is_dir()

    def test_renumbered_account_keeps_its_directory(
        self, switcher, auth, spawned, tmp_path
    ):
        rc = make_manager(switcher)
        before = rc.enable("2", login=False).rc_dir
        _seed(switcher, {"1": ACCOUNTS["1"], "7": ACCOUNTS["2"]})
        (account,) = rc.enabled_accounts()
        assert account.number == "7" and account.rc_dir == before

    def test_copied_file_follows_default_profile_edits(
        self, switcher, auth, temp_home, monkeypatch
    ):
        # Force the no-symlink-privilege path (Windows without Developer Mode).
        monkeypatch.setattr(rc_mod, "_try_file_symlink", lambda src, dest: False)
        rc = make_manager(switcher)
        account = rc.enable("1", login=False)
        (temp_home / ".claude" / "settings.json").write_text('{"model": "y"}')
        rc.sync_shared(account.rc_dir)
        assert (account.rc_dir / "settings.json").read_text() == '{"model": "y"}'

    def test_unmanaged_entries_are_left_alone(self, switcher, auth, temp_home):
        rc = make_manager(switcher)
        d = rc_dir_for(switcher.backup_dir, "one@example.com", "org-1")
        d.mkdir(parents=True)
        (d / "CLAUDE.md").write_text("mine")
        rc.enable("1", login=False)
        assert (d / "CLAUDE.md").read_text() == "mine"


class TestSupervisorSingleInstance:
    def test_second_supervisor_refuses(self, switcher, tmp_path):
        rc = make_manager(switcher)
        rc.root.mkdir(parents=True, exist_ok=True)
        held = rc.supervisor_lock()
        assert held.acquire(timeout=0)
        try:
            with pytest.raises(SessionError, match="already running"):
                make_manager(switcher).run_supervisor(tmp_path, [])
        finally:
            held.release()


class TestStatus:
    def test_status_rows(self, switcher, auth):
        rc = make_manager(switcher)
        rc.enable("1", login=False)
        rc.enable("2", login=False)
        auth.owner[str(rc.enabled_accounts()[0].rc_dir)] = "one@example.com"
        rows = {r["number"]: r for r in rc.status()["bridges"]}
        assert rows["1"]["health"] == "stopped"  # logged in, no supervisor
        assert rows["2"]["health"] == "needs-login"
        assert rc.status()["supervisor"]["running"] is False


class TestSwitchGuard:
    def test_switch_refused_inside_a_bridge_directory(self, switcher, monkeypatch):
        d = rc_dir_for(switcher.backup_dir, "one@example.com", "org-1")
        d.mkdir(parents=True)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(d))
        with pytest.raises(SwitchError, match="Remote Control"):
            switcher._ensure_not_remote_control_dir()

    def test_switch_allowed_in_default_profile(self, switcher, monkeypatch):
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        switcher._ensure_not_remote_control_dir()
