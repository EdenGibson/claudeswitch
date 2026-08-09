"""Which backend the router sends the next request to.

The mode lives in one small file that the server re-reads whenever its mtime
changes. That is the whole mechanism behind mid-session switching: a running
Claude Code session keeps talking to the same local address, and the next
request it makes lands on whichever backend this file names.

Writes are atomic, so a request that reads the file during a flip sees either
the old mode or the new one, never a half-written file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from claude_swap import paths
from claude_swap.settings import atomic_write_json

#: The router's own port. CLIProxyAPI keeps 8317, so 8318 avoids a clash.
DEFAULT_PORT = 8318

#: Where CLIProxyAPI listens when cswap starts it.
CLIPROXY_PORT = 8317

#: Anthropic's real endpoint, used verbatim in claude mode.
ANTHROPIC_UPSTREAM = "https://api.anthropic.com"

_PROVIDERS = ("claude", "codex")


@dataclass(frozen=True)
class RouterMode:
    """The backend for the next request, and which account it uses.

    ``pinned`` records whether a person chose this backend or the autoswitch
    engine did. ``cswap backend auto`` clears it, which is what hands control
    back to the engine.
    """

    provider: str = "claude"
    slot: str | None = None
    pinned: bool = False

    def __post_init__(self) -> None:
        if self.provider not in _PROVIDERS:
            raise ValueError(f"unknown router provider: {self.provider!r}")

    def to_dict(self) -> dict:
        payload: dict = {"provider": self.provider}
        if self.slot is not None:
            payload["slot"] = str(self.slot)
        if self.pinned:
            payload["pinned"] = True
        return payload

    @classmethod
    def from_dict(cls, data: object) -> "RouterMode":
        """Parse a mode file. Anything unreadable means Claude.

        Failing to Claude is the safe default: it is the passthrough that
        needs no extra process, and it is what an uninstalled router does.
        """
        if not isinstance(data, dict):
            return cls()
        provider = data.get("provider")
        if provider not in _PROVIDERS:
            return cls()
        slot = data.get("slot")
        return cls(
            provider=provider,
            slot=str(slot) if slot is not None else None,
            pinned=bool(data.get("pinned")),
        )


def mode_path() -> Path:
    """The mode file's location."""
    return paths.get_router_root() / "mode.json"


def read_mode(path: Path | None = None) -> RouterMode:
    """The current mode. A missing or broken file reads as Claude."""
    target = path or mode_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return RouterMode()
    try:
        return RouterMode.from_dict(json.loads(raw))
    except json.JSONDecodeError:
        return RouterMode()


def write_mode(mode: RouterMode, path: Path | None = None) -> None:
    """Replace the mode file atomically, mode 0600."""
    atomic_write_json(path or mode_path(), mode.to_dict())


class ModeWatcher:
    """Reads the mode file, re-reading only when its mtime changes.

    The server asks for the mode on every request, so the common case must not
    parse JSON. A missing file is cached the same way as a present one, so a
    router with no mode file does not stat-and-parse on every request either.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or mode_path()
        self._stamp: tuple[float, int] | None = None
        self._mode = RouterMode()
        self._loaded = False

    @property
    def path(self) -> Path:
        return self._path

    def current(self) -> RouterMode:
        """The mode now, re-read if the file changed since the last call."""
        try:
            stat = self._path.stat()
            stamp = (stat.st_mtime, stat.st_size)
        except OSError:
            stamp = None
        if self._loaded and stamp == self._stamp:
            return self._mode
        self._stamp = stamp
        self._mode = read_mode(self._path)
        self._loaded = True
        return self._mode
