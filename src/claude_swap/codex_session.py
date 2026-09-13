"""Run the Codex CLI as a stored account, in this terminal only.

The Claude session mode in ``session.py`` copies a credential into a private
``CLAUDE_CONFIG_DIR``. Codex reads ``CODEX_HOME`` the same way, so the shape is
the same: seed a per-slot directory, point the CLI at it, leave the default
login alone.

One difference drives the whole design. Codex rotates its refresh token in
place, and an OpenAI refresh token is single use, so the copy inside the
session directory becomes the only valid one the moment Codex refreshes.
cswap therefore stays resident and copies the credential back into the store
when Codex exits. It does not ``exec``, which the Claude path does.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from claude_swap import paths
from claude_swap.codex_store import CodexAccountStore
from claude_swap.exceptions import SessionError
from claude_swap.providers import codex

#: Environment variables that would override the stored ChatGPT login.
AUTH_OVERRIDE_ENV_VARS = ("OPENAI_API_KEY", "CODEX_API_KEY")


def session_dir(slot: str, email: str) -> Path:
    """The private CODEX_HOME for one slot."""
    return paths.get_provider_root("codex") / "sessions" / f"{slot}-{email}"


def _seed_config(home: Path) -> None:
    """Copy the user's own Codex config into a fresh session directory.

    Only on first use, and only when the user has one: a session that starts
    with no model, no approval policy and no MCP servers is a session nobody
    wants. Later edits to the real config are deliberately not tracked — the
    session directory belongs to the account from then on.
    """
    for name in ("config.toml", "AGENTS.md"):
        source = paths.get_codex_auth_path().parent / name
        target = home / name
        if source.exists() and not target.exists():
            shutil.copy2(source, target)


def _write_auth(home: Path, blob: str) -> None:
    """Put ``blob`` in the session directory as auth.json, mode 0600."""
    target = home / "auth.json"
    target.write_text(blob, encoding="utf-8")
    if sys.platform != "win32":
        os.chmod(target, 0o600)


def sync_back(slot: str, email: str) -> bool:
    """Copy a session's credential into the store. True when it changed.

    Guarded on the account id: a ``codex login`` inside the session for some
    other account must not overwrite this slot's credential.
    """
    home = session_dir(slot, email)
    auth = home / "auth.json"
    try:
        blob = auth.read_text(encoding="utf-8")
    except OSError:
        return False
    if not blob:
        return False

    store = CodexAccountStore()
    stored = store.read_credential(slot, email)
    if blob == stored:
        return False

    session_identity = codex.identity(blob)
    stored_identity = codex.identity(stored) if stored else None
    if (
        session_identity is None
        or stored_identity is not None
        and session_identity.account_uuid != stored_identity.account_uuid
    ):
        return False

    store.write_credential(slot, email, blob)
    return True


def run(slot: str, email: str, codex_args: list[str]) -> int:
    """Launch Codex as the stored account. Returns its exit code."""
    codex_bin = shutil.which("codex")
    if not codex_bin:
        raise SessionError(
            "'codex' was not found on PATH. Install the Codex CLI first."
        )

    blob = CodexAccountStore().read_credential(slot, email)
    if not blob:
        raise SessionError(
            f"Account-{slot} ({email}) has no stored Codex credential."
        )

    home = session_dir(slot, email)
    home.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        os.chmod(home, 0o700)
    _seed_config(home)
    _write_auth(home, blob)

    env = {k: v for k, v in os.environ.items() if k not in AUTH_OVERRIDE_ENV_VARS}
    env["CODEX_HOME"] = str(home)

    try:
        return subprocess.run([codex_bin, *codex_args], env=env).returncode
    except KeyboardInterrupt:
        return 130  # Ctrl+C went to codex; mirror the exit
    finally:
        # Codex may have rotated the refresh token, and the old one is spent.
        # Losing this copy costs a browser re-login, so it runs on every exit
        # path, including Ctrl+C.
        sync_back(slot, email)
