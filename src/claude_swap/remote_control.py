"""Remote Control bridges: one ``claude remote-control`` per opted-in account.

Why this exists. A Remote Control bridge is a long-lived Claude Code process
registered under the account that was signed in when it started, and the
sessions it spawns keep working for that account. Switching the default login
underneath it (``cswap switch`` / ``cswap auto``) rewrites the credentials the
bridge and its sessions read from disk, so per-request work done on behalf of
the original account — downloading a file attached from the phone app, for
one — runs with the *other* account's token and fails. The phone shows the
message without its attachment.

So bridges never run in the rotated default profile. ``cswap rc enable N``
gives an account its own ``CLAUDE_CONFIG_DIR`` under
``<backup_dir>/remote-control/<email-slug>-<org-hash>/``, and
``cswap rc start`` runs one bridge per enabled account in its own directory.
Whichever account the phone app is signed into, it sees that account's
bridge.

Credentials. A remote-control directory holds its *own* OAuth login, made with
``claude auth login`` inside that directory — never a copy of cswap's stored
credentials. OAuth refresh tokens rotate on use: two directories holding one
token family would invalidate each other on the first refresh. A separate
login is a separate grant (the same as signing in on a second machine), so
Claude Code refreshes it in place and nothing in cswap ever reads or writes
it. Switching refuses to run against a remote-control directory.

Sharing. The user's customizations follow the account into the directory:
``settings.json``, ``keybindings.json`` and ``CLAUDE.md`` as symlinks (or, on
Windows without the symlink privilege, as copies the supervisor re-syncs every
few seconds), and ``skills/``, ``commands/``, ``agents/``, ``plugins/`` and
``projects/`` (transcripts and auto-memory) as directory symlinks — junctions
on Windows, which need no privilege. User-scope MCP servers are mirrored from
the default ``.claude.json``, and so are per-project trust and tool approvals,
so a bridge never stalls on a trust prompt nobody can answer. Anything
account- or instance-scoped (``uploads/``, ``sessions/``, ``history.jsonl``,
credentials) stays per directory.

Directories are keyed by account identity (email + organization), not slot
number, so renumbering never moves them. An account removed from cswap loses
its bridge and its directory on the supervisor's next pass.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from claude_swap.claude_locks import proper_lockfile
from claude_swap.exceptions import ClaudeCodeLockTimeout, SessionError
from claude_swap.locking import FileLock
from claude_swap.paths import get_default_global_config_path
from claude_swap.process_detection import is_pid_alive
from claude_swap.session import (
    AUTH_OVERRIDE_ENV_VARS,
    SessionManager,
    slugify_email,
)
from claude_swap.settings import atomic_write_json

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher

RC_DIRNAME = "remote-control"
RC_CONFIG = "rc.json"
SUPERVISOR_STATE = "supervisor.json"
SUPERVISOR_LOCK = ".supervisor.lock"
STOP_REQUEST = ".stop-requested"
# Written into every directory cswap creates, so pruning can never delete a
# directory it did not make.
DIR_MARKER = ".cswap-remote-control"
LINK_MANIFEST = ".cswap-rc-links.json"
BRIDGE_LOG = "bridge.log"

# Linked into the directory. Directories are live (symlink/junction) on every
# platform; files are symlinks where the OS allows it and re-synced copies
# otherwise.
SHARED_FILES = ("settings.json", "keybindings.json", "CLAUDE.md")
SHARED_DIRS = ("skills", "commands", "agents", "plugins", "projects")

# Per-project keys mirrored from the default config: trust and approvals only,
# never the per-project usage statistics Claude Code keeps next to them.
PROJECT_KEYS = (
    "hasTrustDialogAccepted",
    "allowedTools",
    "enabledMcpjsonServers",
    "disabledMcpjsonServers",
    "hasClaudeMdExternalIncludesApproved",
    "hasClaudeMdExternalIncludesWarningShown",
)

SUPERVISOR_TICK_S = 5.0
RESTART_BACKOFF_S = 15.0
LOGIN_RECHECK_S = 300.0
_AUTH_STATUS_TIMEOUT = 15.0


def rc_root(backup_dir: Path) -> Path:
    return backup_dir / RC_DIRNAME


def rc_dir_for(backup_dir: Path, email: str, org_uuid: str) -> Path:
    """Directory for an account identity. Stable across slot renumbering."""
    name = slugify_email(email)
    if org_uuid:
        name += "-" + hashlib.sha256(org_uuid.encode("utf-8")).hexdigest()[:8]
    return rc_root(backup_dir) / name


def is_rc_config_dir(backup_dir: Path, config_dir: Path) -> bool:
    """Whether ``config_dir`` is (inside) a remote-control directory."""
    try:
        config_dir.resolve().relative_to(rc_root(backup_dir).resolve())
        return True
    except (ValueError, OSError):
        return False


def identity_key(email: str, org_uuid: str) -> str:
    return f"{email}|{org_uuid}"


# -- links --------------------------------------------------------------------


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (sys.platform == "win32" and path.is_junction())


def _link_target(path: Path) -> Path | None:
    try:
        return Path(os.readlink(path))
    except OSError:
        return None


def _same_path(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(a)).rstrip("\\/").removeprefix(
        "\\\\?\\"
    ) == os.path.normcase(os.path.abspath(b)).rstrip("\\/").removeprefix("\\\\?\\")


def _unlink(path: Path) -> None:
    """Remove a link (symlink or junction) without touching its target."""
    if sys.platform == "win32" and path.is_junction():
        os.rmdir(path)  # removes the junction itself, never the target
    else:
        path.unlink()


def _make_dir_link(src: Path, dest: Path) -> None:
    if sys.platform == "win32":
        try:
            os.symlink(src, dest, target_is_directory=True)
            return
        except OSError:
            pass  # no symlink privilege: a junction needs none
        import _winapi

        _winapi.CreateJunction(str(src), str(dest))
    else:
        os.symlink(src, dest, target_is_directory=True)


def _try_file_symlink(src: Path, dest: Path) -> bool:
    try:
        os.symlink(src, dest)
        return True
    except OSError:
        return False


def _copy_if_changed(src: Path, dest: Path) -> None:
    try:
        if dest.exists() and dest.read_bytes() == src.read_bytes():
            return
    except OSError:
        pass
    tmp = dest.with_name(dest.name + ".cswap-tmp")
    shutil.copy2(src, tmp)
    os.replace(tmp, dest)


@dataclass
class BridgeState:
    pid: int | None = None
    started_at: float | None = None
    restarts: int = 0
    last_exit: int | None = None
    last_exit_at: float | None = None
    health: str = "stopped"  # running | starting | needs-login | stopped | error
    detail: str = ""

    def to_json(self) -> dict:
        return {
            "pid": self.pid,
            "startedAt": self.started_at,
            "restarts": self.restarts,
            "lastExit": self.last_exit,
            "lastExitAt": self.last_exit_at,
            "health": self.health,
            "detail": self.detail,
        }

    @classmethod
    def from_json(cls, raw: dict) -> "BridgeState":
        return cls(
            pid=raw.get("pid"),
            started_at=raw.get("startedAt"),
            restarts=int(raw.get("restarts") or 0),
            last_exit=raw.get("lastExit"),
            last_exit_at=raw.get("lastExitAt"),
            health=raw.get("health") or "stopped",
            detail=raw.get("detail") or "",
        )


@dataclass
class EnabledAccount:
    email: str
    org_uuid: str
    number: str | None  # current slot; None once the account left the pool
    rc_dir: Path

    @property
    def key(self) -> str:
        return identity_key(self.email, self.org_uuid)


@dataclass
class _Running:
    proc: subprocess.Popen
    log: object = field(default=None)


class RemoteControlManager:
    """Opt-in per-account Remote Control bridges and their supervisor."""

    def __init__(
        self,
        switcher: ClaudeAccountSwitcher,
        *,
        claude_bin: str | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.switcher = switcher
        self.root = rc_root(switcher.backup_dir)
        self.config_path = self.root / RC_CONFIG
        self.state_path = self.root / SUPERVISOR_STATE
        self._claude_bin = claude_bin
        self.clock = clock
        self._logger = switcher._logger
        self._running: dict[str, _Running] = {}
        self._states: dict[str, BridgeState] = {}
        self._login_checked: dict[str, tuple[float, bool, str]] = {}

    # -- config -----------------------------------------------------------

    def _read_config(self) -> dict:
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        raw.setdefault("schemaVersion", 1)
        if not isinstance(raw.get("accounts"), dict):
            raw["accounts"] = {}
        return raw

    def _write_config(self, config: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.config_path, config)

    def bridge_settings(
        self, cwd: str | None = None, args: list[str] | None = None
    ) -> tuple[Path, list[str]]:
        """Working directory and extra ``claude remote-control`` arguments.

        Values passed here are saved and become the defaults for later
        ``cswap rc start`` / ``run`` invocations (and the login autostart).
        """
        if cwd is not None or args is not None:
            with self._config_lock():
                config = self._read_config()
                bridge = config.get("bridge") if isinstance(config.get("bridge"), dict) else {}
                if cwd is not None:
                    bridge["cwd"] = str(Path(cwd).expanduser().resolve())
                if args is not None:
                    bridge["args"] = list(args)
                config["bridge"] = bridge
                self._write_config(config)
        bridge = self._read_config().get("bridge") or {}
        saved_cwd = bridge.get("cwd") if isinstance(bridge, dict) else None
        saved_args = bridge.get("args") if isinstance(bridge, dict) else None
        return (
            Path(saved_cwd) if saved_cwd else Path.home(),
            [str(a) for a in saved_args] if isinstance(saved_args, list) else [],
        )

    def _config_lock(self) -> FileLock:
        return FileLock(self.root / ".rc.lock")

    def claude_bin(self) -> str:
        found = self._claude_bin or shutil.which("claude")
        if not found:
            raise SessionError(
                "'claude' was not found on PATH. Install Claude Code first."
            )
        return found

    def enabled_accounts(self) -> list[EnabledAccount]:
        """Enabled identities, each resolved to its current slot (or None)."""
        config = self._read_config()
        data = self.switcher._get_sequence_data() or {}
        by_identity = {
            identity_key(a.get("email", ""), a.get("organizationUuid", "") or ""): num
            for num, a in data.get("accounts", {}).items()
        }
        out = []
        for key, entry in sorted(config["accounts"].items()):
            email = entry.get("email", "")
            org = entry.get("organizationUuid", "") or ""
            out.append(
                EnabledAccount(
                    email=email,
                    org_uuid=org,
                    number=by_identity.get(identity_key(email, org)),
                    rc_dir=rc_dir_for(self.switcher.backup_dir, email, org),
                )
            )
        out.sort(key=lambda a: (a.number is None, int(a.number or 0)))
        return out

    # -- enable / disable ---------------------------------------------------

    def enable(self, identifier: str, *, login: bool = True) -> EnabledAccount:
        num, email, org = self.switcher.resolve_account(identifier)
        if self.switcher.provider_of(num) != "claude":
            raise SessionError(
                f"Account-{num} is not a Claude account; Remote Control is "
                "Claude Code only."
            )
        if self.switcher.account_kind_for(num) == "api_key":
            raise SessionError(
                f"Account-{num} is an API-key account; Remote Control needs a "
                "claude.ai login."
            )
        rc_dir = rc_dir_for(self.switcher.backup_dir, email, org)
        self.prepare_dir(rc_dir)
        with self._config_lock():
            config = self._read_config()
            config["accounts"][identity_key(email, org)] = {
                "email": email,
                "organizationUuid": org,
                "enabledAt": self.clock(),
            }
            self._write_config(config)
        account = EnabledAccount(email, org, num, rc_dir)
        if login and not self.is_logged_in(account, fresh=True)[0]:
            self.login(account)
        return account

    def disable(self, identifier: str) -> EnabledAccount:
        """Opt an account out. The supervisor stops its bridge on its next
        pass; the directory (and its login) is kept for a later re-enable
        until the account leaves cswap or ``purge`` is used."""
        account = self.find_enabled(identifier)
        with self._config_lock():
            config = self._read_config()
            config["accounts"].pop(account.key, None)
            self._write_config(config)
        return account

    def find_enabled(self, identifier: str) -> EnabledAccount:
        enabled = self.enabled_accounts()
        for account in enabled:
            if identifier in (account.number, account.email):
                return account
        num, email, org = self.switcher.resolve_account(identifier)
        for account in enabled:
            if account.key == identity_key(email, org):
                return account
        raise SessionError(f"Account-{num} ({email}) is not enabled for Remote Control.")

    def login(self, account: EnabledAccount) -> None:
        """Interactive ``claude auth login`` inside the account's directory."""
        cmd = [self.claude_bin(), "auth", "login", "--claudeai", "--email", account.email]
        result = subprocess.run(cmd, env=self._env(account.rc_dir))
        ok, why = self.is_logged_in(account, fresh=True)
        if result.returncode != 0 or not ok:
            raise SessionError(
                f"Remote Control login for {account.email} did not complete"
                f"{f' ({why})' if why else ''}. Retry with: cswap rc login "
                f"{account.number or account.email}"
            )

    # -- directory preparation ------------------------------------------------

    def prepare_dir(self, rc_dir: Path) -> None:
        rc_dir.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            os.chmod(rc_dir, 0o700)
        (rc_dir / DIR_MARKER).touch()
        self.sync_shared(rc_dir)
        self._seed_config(rc_dir)
        SessionManager(self.switcher)._sync_mcp_servers(rc_dir, True)

    def sync_shared(self, rc_dir: Path) -> None:
        """Link (or re-sync) the shared customizations into ``rc_dir``.

        Idempotent and cheap on the steady state; the supervisor calls it every
        tick so copied files follow edits in the default profile. Never
        touches an entry it did not create (tracked in LINK_MANIFEST).
        """
        source_root = Path.home() / ".claude"
        manifest_path = rc_dir / LINK_MANIFEST
        try:
            managed = set(json.loads(manifest_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TypeError):
            managed = set()
        new_managed: set[str] = set()

        for name in SHARED_DIRS + SHARED_FILES:
            src = source_root / name
            dest = rc_dir / name
            is_dir_item = name in SHARED_DIRS
            if is_dir_item and not src.exists():
                # Create the shared side so both profiles write to one place
                # (projects/ and its memory, in particular).
                try:
                    src.mkdir(parents=True, exist_ok=True)
                except OSError:
                    continue
            if not src.exists():
                if name in managed and (dest.is_file() or _is_link(dest)):
                    _unlink(dest)
                continue
            target = src.resolve() if src.is_symlink() else src
            try:
                if _is_link(dest):
                    current = _link_target(dest)
                    if current is not None and _same_path(current, target):
                        new_managed.add(name)
                        continue
                    if name not in managed:
                        continue  # someone else's link
                    _unlink(dest)
                elif dest.exists():
                    if name not in managed:
                        self._logger.warning(
                            f"Not sharing {name} into {rc_dir}: it already has "
                            "its own copy."
                        )
                        continue
                    if is_dir_item:
                        continue  # never delete a real directory
                    # A managed copy: refresh it in place.
                    _copy_if_changed(target, dest)
                    new_managed.add(name)
                    continue
                if is_dir_item:
                    _make_dir_link(target, dest)
                elif not _try_file_symlink(target, dest):
                    _copy_if_changed(target, dest)
                new_managed.add(name)
            except OSError as e:
                self._logger.warning(f"Could not share {name} into {rc_dir}: {e}")

        if new_managed != managed:
            atomic_write_json(manifest_path, sorted(new_managed))

    def _seed_config(self, rc_dir: Path) -> None:
        """Mirror onboarding flags and per-project trust into the directory's
        ``.claude.json``. Never touches ``oauthAccount`` — the directory's own
        login owns that."""
        try:
            source = json.loads(
                get_default_global_config_path().read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            source = {}
        if not isinstance(source, dict):
            source = {}
        config_path = rc_dir / ".claude.json"
        lock_dir = rc_dir / ".claude.json.lock"
        try:
            with proper_lockfile(lock_dir):
                try:
                    existing = json.loads(config_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                    existing = {}
                if not isinstance(existing, dict):
                    existing = {}
                updated = json.loads(json.dumps(existing))
                updated["hasCompletedOnboarding"] = True
                updated.setdefault("theme", source.get("theme") or "dark")
                if source.get("remoteDialogSeen"):
                    updated["remoteDialogSeen"] = True
                projects = updated.get("projects")
                if not isinstance(projects, dict):
                    projects = {}
                for path, entry in (source.get("projects") or {}).items():
                    if not isinstance(entry, dict):
                        continue
                    mirrored = {k: entry[k] for k in PROJECT_KEYS if k in entry}
                    if not mirrored:
                        continue
                    target = projects.get(path)
                    if not isinstance(target, dict):
                        target = {}
                    target.update(mirrored)
                    projects[path] = target
                if projects:
                    updated["projects"] = projects
                if updated != existing:
                    atomic_write_json(config_path, updated)
        except (ClaudeCodeLockTimeout, OSError) as e:
            self._logger.warning(f"Could not seed {config_path}: {e}")

    # -- health -------------------------------------------------------------

    def _env(self, rc_dir: Path) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in AUTH_OVERRIDE_ENV_VARS}
        env["CLAUDE_CONFIG_DIR"] = str(rc_dir)
        return env

    def is_logged_in(
        self, account: EnabledAccount, *, fresh: bool = False
    ) -> tuple[bool, str]:
        """Local ``claude auth status`` in the directory, matched to the
        account's identity. Cached for LOGIN_RECHECK_S unless ``fresh``."""
        now = self.clock()
        cached = self._login_checked.get(account.key)
        if not fresh and cached and now - cached[0] < LOGIN_RECHECK_S:
            return cached[1], cached[2]
        ok, why = self._probe_login(account)
        self._login_checked[account.key] = (now, ok, why)
        return ok, why

    def _probe_login(self, account: EnabledAccount) -> tuple[bool, str]:
        if not account.rc_dir.is_dir():
            return False, "directory missing"
        try:
            result = subprocess.run(
                [self.claude_bin(), "auth", "status", "--json"],
                env=self._env(account.rc_dir),
                capture_output=True,
                text=True,
                timeout=_AUTH_STATUS_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return False, "auth status timed out"
        except OSError as e:
            return False, str(e)
        try:
            status = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return False, "unreadable auth status"
        if status.get("loggedIn") is not True:
            return False, "not logged in"
        if status.get("authMethod") not in (None, "claude.ai"):
            return False, f"auth method {status.get('authMethod')}"
        if status.get("email") and status.get("email") != account.email:
            return False, "logged in as a different account"
        org = status.get("orgId")
        if org and account.org_uuid and org != account.org_uuid:
            return False, "logged in to a different organization"
        return True, ""

    # -- supervisor ---------------------------------------------------------

    def supervisor_lock(self) -> FileLock:
        return FileLock(self.root / SUPERVISOR_LOCK)

    def read_state(self) -> dict:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def supervisor_pid(self) -> int | None:
        pid = self.read_state().get("pid")
        if isinstance(pid, int) and is_pid_alive(pid):
            return pid
        return None

    def _write_state(self) -> None:
        atomic_write_json(
            self.state_path,
            {
                "schemaVersion": 1,
                "pid": os.getpid(),
                "updatedAt": self.clock(),
                "bridges": {k: s.to_json() for k, s in self._states.items()},
            },
        )

    def bridge_command(self, account: EnabledAccount, args: list[str]) -> list[str]:
        cmd = [self.claude_bin(), "remote-control", *args]
        if not any(a == "--name" or a.startswith("--name=") for a in args):
            label = f"Account-{account.number}" if account.number else account.email
            cmd += ["--name", f"{socket.gethostname()} ({label})"]
        return cmd

    def _spawn(self, account: EnabledAccount, cwd: Path, args: list[str]) -> None:
        log = open(account.rc_dir / BRIDGE_LOG, "ab")
        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            self.bridge_command(account, args),
            cwd=str(cwd),
            env=self._env(account.rc_dir),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            **kwargs,
        )
        self._running[account.key] = _Running(proc, log)
        state = self._states.setdefault(account.key, BridgeState())
        state.pid = proc.pid
        state.started_at = self.clock()
        state.health = "running"
        state.detail = ""

    def _reap(self, key: str) -> None:
        running = self._running.get(key)
        if running is None:
            return
        code = running.proc.poll()
        if code is None:
            return
        self._running.pop(key)
        if running.log is not None:
            running.log.close()
        state = self._states.setdefault(key, BridgeState())
        state.pid = None
        state.last_exit = code
        state.last_exit_at = self.clock()
        state.restarts += 1
        state.health = "starting"
        state.detail = f"exited with code {code}; restarting"

    def _terminate(self, key: str) -> None:
        running = self._running.pop(key, None)
        if running is None:
            return
        kill_tree(running.proc.pid)
        try:
            running.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            running.proc.kill()
        if running.log is not None:
            running.log.close()
        state = self._states.setdefault(key, BridgeState())
        state.pid = None
        state.health = "stopped"

    def reconcile(self, cwd: Path, args: list[str]) -> None:
        """One supervisor pass: start, restart, stop and prune bridges."""
        enabled = self.enabled_accounts()
        wanted = {a.key: a for a in enabled if a.number is not None}

        # Accounts that left the pool: stop, forget, and delete the directory.
        orphaned = [a for a in enabled if a.number is None]
        if orphaned:
            with self._config_lock():
                config = self._read_config()
                for account in orphaned:
                    config["accounts"].pop(account.key, None)
                self._write_config(config)
            for account in orphaned:
                self._terminate(account.key)
                self._states.pop(account.key, None)
                self.purge_dir(account.rc_dir)

        for key in list(self._running):
            if key not in wanted:  # disabled
                self._terminate(key)
                self._states.pop(key, None)

        for key, account in wanted.items():
            self._reap(key)
            state = self._states.setdefault(key, BridgeState())
            if key in self._running:
                continue
            if (
                state.last_exit_at is not None
                and self.clock() - state.last_exit_at < RESTART_BACKOFF_S
            ):
                continue
            try:
                self.prepare_dir(account.rc_dir)
            except OSError as e:
                state.health, state.detail = "error", f"prepare failed: {e}"
                continue
            ok, why = self.is_logged_in(account)
            if not ok:
                state.health = "needs-login"
                state.detail = (
                    f"{why}; run: cswap rc login {account.number}"
                )
                continue
            try:
                self._spawn(account, cwd, args)
            except (OSError, SessionError) as e:
                state.health, state.detail = "error", f"spawn failed: {e}"
                state.last_exit_at = self.clock()

        # Running bridges: keep copied files fresh.
        for key in self._running:
            try:
                self.sync_shared(wanted[key].rc_dir)
            except (OSError, KeyError):
                pass
        self._write_state()

    def purge_dir(self, rc_dir: Path) -> None:
        """Delete a directory cswap created — links are unlinked first, so a
        junction's target (the shared ``~/.claude`` content) is never
        followed."""
        if not (rc_dir / DIR_MARKER).exists():
            return
        try:
            for child in rc_dir.iterdir():
                if _is_link(child):
                    _unlink(child)
            shutil.rmtree(rc_dir, ignore_errors=True)
        except OSError as e:
            self._logger.warning(f"Could not remove {rc_dir}: {e}")

    def purge_when_stopped(self, account: EnabledAccount, timeout: float = 30.0) -> None:
        """Delete a just-disabled account's directory once the supervisor has
        stopped its bridge (nothing may hold files open in it on Windows)."""
        deadline = time.monotonic() + timeout
        while self.supervisor_pid() is not None and time.monotonic() < deadline:
            raw = (self.read_state().get("bridges") or {}).get(account.key)
            pid = raw.get("pid") if isinstance(raw, dict) else None
            if not (isinstance(pid, int) and is_pid_alive(pid)):
                break
            time.sleep(0.5)
        self.purge_dir(account.rc_dir)

    def run_supervisor(self, cwd: Path, args: list[str]) -> int:
        """Foreground supervisor; single instance per backup root."""
        self.root.mkdir(parents=True, exist_ok=True)
        lock = self.supervisor_lock()
        if not lock.acquire(timeout=0):
            raise SessionError(
                "A Remote Control supervisor is already running "
                f"(PID {self.supervisor_pid() or 'unknown'})."
            )
        stop = {"flag": False}

        def _stop(*_):
            stop["flag"] = True

        signal.signal(signal.SIGTERM, _stop)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, _stop)
        stop_file = self.root / STOP_REQUEST
        stop_file.unlink(missing_ok=True)
        try:
            while not stop["flag"] and not stop_file.exists():
                try:
                    self.reconcile(cwd, args)
                except Exception as e:  # never let one bad pass kill the bridges
                    self._logger.warning(f"remote-control supervisor pass failed: {e}")
                deadline = self.clock() + SUPERVISOR_TICK_S
                while (
                    not stop["flag"]
                    and not stop_file.exists()
                    and self.clock() < deadline
                ):
                    time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            for key in list(self._running):
                self._terminate(key)
            try:
                self._write_state()
            except OSError:
                pass
            stop_file.unlink(missing_ok=True)
            lock.release()
        return 0

    # -- start / stop from another process ------------------------------------

    def start_detached(self, cswap_argv: list[str]) -> int:
        """Launch ``cswap rc run`` in the background; returns its PID."""
        if (pid := self.supervisor_pid()) is not None:
            raise SessionError(f"Remote Control supervisor already running (PID {pid}).")
        self.root.mkdir(parents=True, exist_ok=True)
        log = open(self.root / "supervisor.log", "ab")
        kwargs: dict = {}
        if sys.platform == "win32":
            # A hidden console rather than DETACHED_PROCESS: the bridges and
            # their sessions inherit it, so none of them pops a window.
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            cswap_argv,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            **kwargs,
        )
        log.close()
        return proc.pid

    def stop_supervisor(self, timeout: float = 30.0) -> bool:
        """Ask the supervisor to stop its bridges and exit. True if it did."""
        pid = self.supervisor_pid()
        if pid is None:
            return True
        (self.root / STOP_REQUEST).touch()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not is_pid_alive(pid):
                return True
            time.sleep(0.5)
        # Unresponsive: take the supervisor and every bridge down hard.
        for raw in (self.read_state().get("bridges") or {}).values():
            if isinstance(raw, dict) and isinstance(raw.get("pid"), int):
                kill_tree(raw["pid"])
        kill_tree(pid)
        (self.root / STOP_REQUEST).unlink(missing_ok=True)
        return not is_pid_alive(pid)

    def status(self) -> dict:
        """Snapshot for ``cswap rc status``; prunes nothing."""
        state = self.read_state()
        sup_pid = state.get("pid") if isinstance(state.get("pid"), int) else None
        sup_alive = bool(sup_pid and is_pid_alive(sup_pid))
        bridges = state.get("bridges") or {}
        rows = []
        for account in self.enabled_accounts():
            raw = bridges.get(account.key) if sup_alive else None
            b = BridgeState.from_json(raw) if isinstance(raw, dict) else BridgeState()
            alive = bool(b.pid and is_pid_alive(b.pid))
            logged_in, why = self.is_logged_in(account, fresh=True)
            if account.number is None:
                health = "removed"
            elif not logged_in:
                health = "needs-login"
            elif not sup_alive:
                health = "stopped"
            elif alive:
                health = "running"
            else:
                health = b.health if b.health != "running" else "starting"
            rows.append(
                {
                    "number": account.number,
                    "email": account.email,
                    "dir": str(account.rc_dir),
                    "loggedIn": logged_in,
                    "loginDetail": why,
                    "pid": b.pid if alive else None,
                    "startedAt": b.started_at if alive else None,
                    "restarts": b.restarts,
                    "lastExit": b.last_exit,
                    "health": health,
                    "detail": b.detail,
                }
            )
        return {
            "supervisor": {"pid": sup_pid if sup_alive else None, "running": sup_alive},
            "bridges": rows,
        }


def kill_tree(pid: int) -> None:
    """Terminate a process and its children (bridge sessions included)."""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
