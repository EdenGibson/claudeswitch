"""One systemd user unit, described as a value.

The router and the Codex backend are both long-running local processes that
cswap installs, starts and stops. Everything about managing them is the same
except three things: the unit's name, the command it runs, and whether it
belongs at login. Those are fields here, not a second copy of the module.

Nothing in this module is Linux-only by accident. ``have_systemd`` is false on
every other platform, and each method returns the same "not installed" answer
it would give for a missing unit file, so callers need no platform test.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True, check=False
    )


def have_systemd() -> bool:
    return sys.platform.startswith("linux") and shutil.which("systemctl") is not None


@dataclass(frozen=True)
class Unit:
    """A systemd user unit cswap owns.

    ``enable_at_login`` is the whole difference between the two units cswap
    installs. The router belongs at login: it is the address Claude Code
    talks to, and it must answer before any session starts. The Codex backend
    must not be, because it refreshes the Codex login every 15 minutes while
    it runs, rotating a single-use token cswap is not watching in claude mode.
    """

    name: str
    description: str
    exec_start: str
    enable_at_login: bool = False
    #: What to tell the user when there is no unit file to act on.
    missing_message: str = "no service unit installed"

    def path(self) -> Path:
        return Path.home() / ".config" / "systemd" / "user" / self.name

    def text(self) -> str:
        install = "\n[Install]\nWantedBy=default.target\n" if self.enable_at_login else ""
        return f"""[Unit]
Description={self.description}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={self.exec_start}
Restart=always
RestartSec=3
{install}"""

    def install(self) -> bool:
        """Write the unit file. False when systemd is not available."""
        if not have_systemd():
            return False
        target = self.path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.text(), encoding="utf-8")
        _systemctl("daemon-reload")
        # Always stated, never left to whatever an earlier install did: a unit
        # that changes this flag between versions would otherwise keep the old
        # answer forever.
        _systemctl("enable" if self.enable_at_login else "disable", self.name)
        return True

    def remove(self) -> bool:
        """Delete the unit file. False when there was none."""
        target = self.path()
        if not target.exists():
            return False
        if have_systemd():
            _systemctl("disable", "--now", self.name)
        target.unlink(missing_ok=True)
        if have_systemd():
            _systemctl("daemon-reload")
        return True

    def _installed(self) -> bool:
        return have_systemd() and self.path().exists()

    def start(self) -> tuple[bool, str]:
        if not self._installed():
            return False, self.missing_message
        result = _systemctl("start", self.name)
        if result.returncode == 0:
            return True, f"started {self.name}"
        return False, result.stderr.strip() or "systemctl start failed"

    def stop(self) -> tuple[bool, str]:
        if not self._installed():
            return False, self.missing_message
        result = _systemctl("stop", self.name)
        if result.returncode == 0:
            return True, f"stopped {self.name}"
        return False, result.stderr.strip() or "systemctl stop failed"

    def active(self) -> bool:
        if not self._installed():
            return False
        return _systemctl("is-active", self.name).stdout.strip() == "active"
