"""Put Claude Code behind the router, and take it back out again.

Two things have to be true for a running session to follow a backend flip:
the session must have been started with ``ANTHROPIC_BASE_URL`` pointing at the
router, and the router must be running. This module owns both — the settings
edit and, on Linux, the systemd user unit.

Only ``ANTHROPIC_BASE_URL`` is written. No credential variable is set, which
is what keeps the saved claude.ai login active and voice dictation working.
Remote Control is lost while the base URL points at a non-Anthropic host.

Every key this module writes is recorded in ``install-state.json`` with its
previous value, so uninstall restores the file to what it was rather than
deleting a key the user set themselves.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from claude_swap import paths
from claude_swap.fsutil import replace_with_retry
from claude_swap.router.mode import DEFAULT_PORT
from claude_swap.router.unit import Unit
from claude_swap.settings import atomic_write_json

#: The single variable that puts a session behind the router.
BASE_URL_KEY = "ANTHROPIC_BASE_URL"

#: systemd unit name on Linux.
UNIT_NAME = "cswap-router.service"

#: Fix-ups some non-Claude upstreams need. Deliberately NOT written by
#: install: they are process-start variables, so setting them would apply to
#: Claude traffic too, and CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS in
#: particular turns off features that work fine on Anthropic. Reported by
#: `cswap router status` so they can be added by hand if codex mode misbehaves.
CODEX_COMPAT_HINTS = (
    "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1",
    "CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK=1",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW=<tokens>",
)


@dataclass(frozen=True)
class InstallState:
    """What install changed, so uninstall can put it back exactly."""

    #: env keys written, mapped to their previous value. None means the key
    #: was absent and must be removed again.
    previous_env: dict[str, str | None]
    port: int
    unit_installed: bool

    def to_dict(self) -> dict:
        return {
            "previousEnv": self.previous_env,
            "port": self.port,
            "unitInstalled": self.unit_installed,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "InstallState":
        raw = data.get("previousEnv")
        return cls(
            previous_env=raw if isinstance(raw, dict) else {},
            port=int(data.get("port") or DEFAULT_PORT),
            unit_installed=bool(data.get("unitInstalled")),
        )


def state_path() -> Path:
    return paths.get_router_root() / "install-state.json"


def settings_path() -> Path:
    """Claude Code's user settings file."""
    return paths.get_claude_config_home() / "settings.json"


def unit_path() -> Path:
    return router_unit().path()


def base_url(port: int = DEFAULT_PORT) -> str:
    return f"http://127.0.0.1:{port}"


def _write_settings(payload: dict) -> None:
    """Replace Claude Code's settings file atomically.

    Not :func:`settings.atomic_write_json`, which is otherwise the canonical
    writer here. That one chmods the parent directory to 0700, and this parent
    is ``~/.claude`` — Claude Code's, not cswap's. Narrowing another tool's
    directory is not something installing a router should do.
    """
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        replace_with_retry(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_settings() -> dict:
    try:
        raw = settings_path().read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{settings_path()} is not valid JSON ({exc}); fix it before "
            "installing the router"
        ) from exc
    return parsed if isinstance(parsed, dict) else {}


def read_state() -> InstallState | None:
    try:
        raw = state_path().read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return InstallState.from_dict(json.loads(raw))
    except (json.JSONDecodeError, ValueError, TypeError):
        return None


def is_installed() -> bool:
    """Whether Claude Code's settings currently point at the router."""
    env = _read_settings().get("env")
    if not isinstance(env, dict):
        return False
    return str(env.get(BASE_URL_KEY, "")).startswith("http://127.0.0.1:")


def install(port: int = DEFAULT_PORT, *, write_unit: bool = True) -> InstallState:
    """Point Claude Code at the router and, on Linux, install the unit."""
    settings = _read_settings()
    env = settings.get("env")
    if not isinstance(env, dict):
        env = {}
    # A second install must not record the router's own URL as the thing to
    # restore. Re-installing is normal (a new port, a repaired unit), and one
    # repeat would otherwise leave uninstall pointing Claude Code at a dead
    # local port with no record of what was there first.
    existing = read_state()
    if is_installed() and existing is not None:
        previous = dict(existing.previous_env)
    else:
        previous = {BASE_URL_KEY: env.get(BASE_URL_KEY)}
    env[BASE_URL_KEY] = base_url(port)
    settings["env"] = env
    _write_settings(settings)

    unit_installed = False
    if write_unit and sys.platform.startswith("linux"):
        unit_installed = router_unit(port).install()

    state = InstallState(
        previous_env=previous, port=port, unit_installed=unit_installed
    )
    atomic_write_json(state_path(), state.to_dict())
    return state


def uninstall() -> bool:
    """Undo install. Returns True when something was changed."""
    state = read_state()
    settings = _read_settings()
    env = settings.get("env")
    changed = False

    if isinstance(env, dict):
        keys = (
            state.previous_env.items()
            if state is not None
            else [(BASE_URL_KEY, None)]
        )
        for key, before in keys:
            if before is None:
                if key in env:
                    del env[key]
                    changed = True
            elif env.get(key) != before:
                env[key] = before
                changed = True
        if not env:
            settings.pop("env", None)
        if changed:
            _write_settings(settings)

    if state is not None and state.unit_installed:
        changed = router_unit().remove() or changed

    state_path().unlink(missing_ok=True)
    return changed


# -- systemd ----------------------------------------------------------------

def router_unit(port: int = DEFAULT_PORT) -> Unit:
    """The router's systemd unit.

    Enabled at login, unlike the Codex backend's: the router is the address
    every session talks to, so it has to answer before any session starts.
    """
    executable = shutil.which("cswap") or "cswap"
    return Unit(
        name=UNIT_NAME,
        description="cswap router (Claude Code backend switch)",
        exec_start=f"{executable} router serve --port {port}",
        enable_at_login=True,
        missing_message=(
            "no service unit installed — run 'cswap router install' first, or "
            "run 'cswap router serve' in a terminal"
        ),
    )


def unit_text(port: int) -> str:
    return router_unit(port).text()


def start() -> tuple[bool, str]:
    """Start the router. Returns (ok, message)."""
    return router_unit().start()


def stop() -> tuple[bool, str]:
    return router_unit().stop()


def unit_active() -> bool:
    return router_unit().active()
