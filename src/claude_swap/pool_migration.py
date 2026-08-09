"""Fold the standalone Codex store into the main account registry.

Phase 1 gave Codex its own pool with its own slot numbers starting at 1, which
collide with the Claude slots. The unified pool makes ``sequence.json`` the one
registry: every account gets a slot there, and a ``provider`` key names the
backend that owns its credential. Credential bytes do not move — Codex blobs
stay under ``providers/codex/credentials/`` — only the slot number changes, so
the file is renamed to match.

The migration runs on the first registry read of any command and is
idempotent. It marks the Codex sequence ``migrated`` last, so a crash between
the two writes leaves it re-runnable rather than half-applied.
"""

from __future__ import annotations

import json
import logging

from claude_swap import paths
from claude_swap.codex_store import CodexAccountStore
from claude_swap.locking import FileLock
from claude_swap.settings import atomic_write_json

_logger = logging.getLogger("claude-swap")

#: Key under which the registry records the active slot of each non-Claude
#: provider. ``activeAccountNumber`` keeps meaning "the active Claude slot",
#: because Claude Code and the Codex CLI read different credential files and
#: one global pointer cannot describe both.
ACTIVE_KEY = "activeProviderAccounts"


def _registry_path():
    return paths.get_backup_root() / "sequence.json"


def _read_registry() -> dict | None:
    """The main registry, or None when it does not exist or will not parse."""
    path = _registry_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _next_free_slot(data: dict, taken: set[int]) -> int:
    """The lowest slot number no account holds."""
    used = {int(n) for n in data.get("accounts", {}) if str(n).isdecimal()} | taken
    candidate = 1
    while candidate in used:
        candidate += 1
    return candidate


_ran_this_process = False


def ensure_migrated() -> bool:
    """Run the migration once per process, before any lock is taken.

    ``FileLock`` is not reentrant and the migration takes the same registry
    lock the switcher does, so this must be called from the command entry
    point, never from inside a locked region.
    """
    global _ran_this_process
    if _ran_this_process:
        return False
    _ran_this_process = True
    try:
        return migrate_codex_into_pool()
    except Exception:
        # A failed fold must never stop the command. The accounts stay in the
        # Codex store and the next run tries again.
        _logger.warning("Codex pool migration failed; retrying next run", exc_info=True)
        _ran_this_process = False
        return False


def migrate_codex_into_pool() -> bool:
    """Move every standalone Codex account into the registry.

    Returns True when the registry changed. Takes the registry lock, so never
    call it while holding that lock — use :func:`ensure_migrated`.
    """
    store = CodexAccountStore()
    codex_seq = store._read_sequence()
    if codex_seq.get("migrated"):
        return False
    rows = codex_seq.get("accounts") or {}
    if not rows:
        # Nothing to fold. Do not stamp the marker: a later `cswap codex add`
        # against an old build would then be skipped forever.
        return False

    registry = _read_registry()
    if registry is None:
        # No Claude accounts yet. add_account builds the file on first use and
        # the migration runs again then.
        return False

    with FileLock(paths.get_backup_root() / ".lock"):
        registry = _read_registry()
        if registry is None:
            return False
        registry.setdefault("accounts", {})
        registry.setdefault("sequence", [])

        # Already-folded accounts, by Codex account id. A crash between the
        # registry write and the marker write re-runs this whole function, so
        # every account must be recognised rather than added twice.
        already = {
            str(acc.get("uuid", "")): str(slot)
            for slot, acc in registry["accounts"].items()
            if acc.get("provider") == "codex" and acc.get("uuid")
        }

        moved: dict[str, str] = {}  # codex slot -> registry slot
        taken: set[int] = set()
        for codex_slot, record in sorted(rows.items(), key=lambda kv: int(kv[0])):
            email = str(record.get("email", ""))
            account_id = str(record.get("accountId", ""))
            if account_id and account_id in already:
                moved[codex_slot] = already[account_id]
                continue
            blob = store.read_credential(codex_slot, email)
            new_slot = _next_free_slot(registry, taken)
            taken.add(new_slot)
            registry["accounts"][str(new_slot)] = {
                "email": email,
                "uuid": str(record.get("accountId", "")),
                "organizationUuid": str(record.get("organizationUuid", "") or ""),
                "organizationName": str(record.get("organizationName", "") or ""),
                "added": str(record.get("added", "")),
                "provider": "codex",
                "plan": str(record.get("plan", "") or ""),
            }
            registry["sequence"].append(new_slot)
            moved[codex_slot] = str(new_slot)
            if blob:
                store.write_credential(str(new_slot), email, blob)
                if str(new_slot) != codex_slot:
                    store.delete_credential(codex_slot, email)

        registry["sequence"] = sorted({int(n) for n in registry["sequence"]})
        active = codex_seq.get("activeAccountNumber")
        if active is not None and str(active) in moved:
            registry.setdefault(ACTIVE_KEY, {})["codex"] = moved[str(active)]
        atomic_write_json(_registry_path(), registry)

    # Stamped last. A crash before this point re-runs the whole migration; the
    # duplicate check in add_account and the slot allocator both tolerate it.
    with store._lock():
        data = store._read_sequence()
        data["migrated"] = True
        data["accounts"] = {}
        store._write_sequence(data)

    _logger.info("Folded %d Codex account(s) into the main registry", len(moved))
    return True
