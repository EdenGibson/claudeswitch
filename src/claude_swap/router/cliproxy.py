"""CLIProxyAPI as the Codex half of the router.

CLIProxyAPI speaks Anthropic's ``/v1/messages`` on one side and a ChatGPT
subscription on the other. cswap does not reimplement that translation — it
supplies the process with a config file, exactly one Codex credential, and a
local API key, then starts and stops it.

Exactly one credential is written. CLIProxyAPI load-balances across every
credential in its auth directory, so a second file there would silently spread
traffic over accounts cswap did not choose. The directory is cswap's alone and
is emptied on every write.

The credential file matches CLIProxyAPI's ``CodexTokenStorage`` struct
(``internal/auth/codex/token.go``). The filename follows its
``CredentialFileName`` helper, minus the account hash, which is an internal
detail — CLIProxyAPI loads a credential by reading the file, not by parsing
its name.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

from claude_swap import paths
from claude_swap.providers import codex
from claude_swap.router.mode import CLIPROXY_PORT
from claude_swap.router.unit import Unit
from claude_swap.settings import atomic_write_json

#: Binary names a CLIProxyAPI release ships under, best first.
BINARY_NAMES = ("cli-proxy-api", "CLIProxyAPI", "cliproxyapi")

#: systemd unit cswap installs for the Codex backend.
UNIT_NAME = "cswap-cliproxy.service"

#: The ``type`` field every Codex credential carries.
AUTH_TYPE = "codex"

_UNSAFE = re.compile(r"[^A-Za-z0-9._@+-]")


# -- paths -------------------------------------------------------------------


def auth_dir() -> Path:
    """The one directory CLIProxyAPI reads credentials from."""
    return paths.get_router_root() / "cliproxy-auth"


def config_path() -> Path:
    return paths.get_router_root() / "cliproxy.yaml"


def key_path() -> Path:
    return paths.get_router_root() / "api-key"


def unit_path() -> Path:
    return codex_unit().path()


def find_binary() -> str | None:
    """The CLIProxyAPI executable, or None when it is not installed."""
    for name in BINARY_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


# -- api key -----------------------------------------------------------------


def api_key(create: bool = True) -> str:
    """The key the router sends to CLIProxyAPI, generated once.

    CLIProxyAPI rejects an unauthenticated request, and the key is what stops
    any other local process from spending the Codex quota.
    """
    path = key_path()
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except OSError:
        existing = ""
    if existing:
        return existing
    if not create:
        return ""
    key = "cswap-" + secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key + "\n", encoding="utf-8")
    if sys.platform != "win32":
        os.chmod(path, 0o600)
    return key


# -- credential --------------------------------------------------------------


def _safe(value: str) -> str:
    return _UNSAFE.sub("_", value.strip())


def _rfc3339(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def credential_name(account_id: str, email: str, plan: str) -> str:
    """CLIProxyAPI's filename for a Codex credential."""
    digest = hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:16]
    parts = ["codex", digest, _safe(email) or "unknown"]
    plan_part = "-".join(
        piece for piece in re.split(r"[^A-Za-z0-9]+", plan.lower()) if piece
    )
    if plan_part:
        parts.append(plan_part)
    return "-".join(parts) + ".json"


def to_storage(blob: str) -> dict | None:
    """A cswap Codex auth.json body as CLIProxyAPI's credential record."""
    try:
        parsed = json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    tokens = parsed.get("tokens")
    if not isinstance(tokens, dict):
        return None
    identity = codex.identity(blob)
    if identity is None:
        return None

    expires_at = codex.access_token_expires_at(blob)
    last_refresh = parsed.get("last_refresh")
    return {
        "id_token": str(tokens.get("id_token") or ""),
        "access_token": str(tokens.get("access_token") or ""),
        "refresh_token": str(tokens.get("refresh_token") or ""),
        "account_id": str(tokens.get("account_id") or identity.account_uuid),
        "last_refresh": (
            last_refresh
            if isinstance(last_refresh, str) and last_refresh
            else _rfc3339(datetime.now(tz=timezone.utc).timestamp())
        ),
        "email": identity.email,
        "type": AUTH_TYPE,
        "expired": _rfc3339(expires_at) if expires_at else "",
    }


def write_credential(blob: str) -> Path | None:
    """Put one Codex credential in the auth directory, replacing any others.

    Returns the file written, or None when the blob is not a readable Codex
    credential.
    """
    record = to_storage(blob)
    if record is None:
        return None
    identity = codex.identity(blob)
    assert identity is not None  # to_storage already proved it reads
    target_dir = auth_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        os.chmod(target_dir, stat.S_IRWXU)

    target = target_dir / credential_name(
        record["account_id"], identity.email, identity.plan
    )
    atomic_write_json(target, record)

    for stale in target_dir.glob("*.json"):
        if stale != target:
            stale.unlink(missing_ok=True)
    return target


def read_credential() -> dict | None:
    """The Codex credential record in the auth directory, if there is one."""
    for path in sorted(auth_dir().glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(record, dict) and record.get("type") == AUTH_TYPE:
            return record
    return None


def merge_back(original: str) -> str | None:
    """The cswap blob updated with CLIProxyAPI's rotated tokens.

    CLIProxyAPI refreshes the credential itself while it runs, and an OpenAI
    refresh token is single-use. Without this, cswap's stored copy is dead the
    first time the backend refreshes. Returns None when there is nothing newer
    or the record belongs to another account.
    """
    record = read_credential()
    if record is None:
        return None
    try:
        parsed = json.loads(original)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    tokens = parsed.get("tokens")
    if not isinstance(tokens, dict):
        return None

    mine = codex.identity(original)
    theirs = str(record.get("account_id") or "")
    if mine is None or (theirs and mine.account_uuid and theirs != mine.account_uuid):
        return None
    if str(record.get("refresh_token") or "") == str(tokens.get("refresh_token") or ""):
        return None  # unchanged; nothing to write back

    for field in ("id_token", "access_token", "refresh_token"):
        value = record.get(field)
        if isinstance(value, str) and value:
            tokens[field] = value
    last = record.get("last_refresh")
    if isinstance(last, str) and last:
        parsed["last_refresh"] = last
    parsed["tokens"] = tokens
    return json.dumps(parsed, indent=2)


def credential_email() -> str:
    """The email of the credential currently in the auth directory."""
    record = read_credential()
    return str(record.get("email") or "") if record else ""


# -- config ------------------------------------------------------------------


def config_text(port: int = CLIPROXY_PORT, key: str | None = None) -> str:
    """The CLIProxyAPI config cswap generates.

    Warning: ``host`` must stay loopback. CLIProxyAPI binds every interface
    when host is empty, and this box has a public one.
    """
    return f"""# Generated by cswap. Edits are overwritten on 'cswap router install'.
host: "127.0.0.1"
port: {port}
auth-dir: "{auth_dir()}"
api-keys:
  - "{key or api_key()}"
debug: false
remote-management:
  allow-remote: false
  secret-key: ""
  disable-control-panel: true
"""


def write_config(port: int = CLIPROXY_PORT) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config_text(port), encoding="utf-8")
    if sys.platform != "win32":
        os.chmod(path, 0o600)
    return path


# -- process -----------------------------------------------------------------


def codex_unit(binary: str | None = None, config: Path | None = None) -> Unit:
    """The Codex backend's systemd unit.

    Deliberately not enabled at login. cswap starts this process only while
    the backend is codex. Left enabled it would sit there in claude mode
    refreshing the Codex login every 15 minutes, rotating a single-use token
    cswap is not watching.
    """
    runner = binary or find_binary() or BINARY_NAMES[0]
    return Unit(
        name=UNIT_NAME,
        description="cswap Codex backend (CLIProxyAPI)",
        exec_start=f"{runner} --config {config or config_path()}",
        missing_message="no CLIProxyAPI unit installed — run 'cswap router install'",
    )


def unit_text(binary: str, config: Path) -> str:
    return codex_unit(binary, config).text()


def install_unit() -> bool:
    """Write the unit, started on demand only. False when nothing to run."""
    if find_binary() is None:
        return False
    return codex_unit().install()


def remove_unit() -> bool:
    return codex_unit().remove()


def start() -> tuple[bool, str]:
    if find_binary() is None:
        return False, (
            "CLIProxyAPI is not installed — the codex backend needs it. See "
            "https://github.com/router-for-me/CLIProxyAPI/releases"
        )
    return codex_unit().start()


def stop() -> tuple[bool, str]:
    return codex_unit().stop()


def unit_active() -> bool:
    return codex_unit().active()


def reachable(timeout: float = 1.0) -> bool:
    """Whether anything answers on the CLIProxyAPI port.

    The unit is one way to run CLIProxyAPI, not the only one. A box without
    systemd, or a user running the binary by hand, still has a usable backend.
    """
    import socket

    try:
        with socket.create_connection(("127.0.0.1", CLIPROXY_PORT), timeout):
            return True
    except OSError:
        return False


def ensure_running() -> tuple[bool, str]:
    """Make sure something answers on the CLIProxyAPI port.

    Returns (ok, note). A note alongside ok is not a failure: it means the
    unit would not start but the port answers anyway, because someone is
    running CLIProxyAPI by hand. That is a usable backend, and the note says
    why the unit did not start.
    """
    if unit_active():
        return True, ""
    started, message = start()
    if started:
        return True, ""
    if not reachable():
        return False, message
    return True, message
