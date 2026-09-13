"""The cswap router: which backend answers the next Claude Code request.

Claude Code fixes its endpoint when the process starts. A running session
therefore cannot be moved to another provider by rewriting a credential — the
only way is to give it a stable local address and change what stands behind
that address. The router is that address.

``mode.py`` holds the decision and is safe to import anywhere. ``server.py``
needs aiohttp and is imported only by the running proxy.
"""

from __future__ import annotations

from claude_swap.router.mode import (
    DEFAULT_PORT,
    RouterMode,
    mode_path,
    read_mode,
    write_mode,
)

__all__ = [
    "DEFAULT_PORT",
    "RouterMode",
    "mode_path",
    "read_mode",
    "write_mode",
]
