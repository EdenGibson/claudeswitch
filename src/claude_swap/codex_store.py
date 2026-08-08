"""The Codex account pool.

An independent pool: it shares no state with the Claude accounts and lives
entirely under ``<backup_root>/providers/codex/``. Storage mirrors the Claude
layout so the two read the same way, but nothing is shared except the generic
helpers (``FileLock``, ``atomic_write_json``, ``UsageStore``).

Credentials are stored base64-encoded with mode 0600, which is exactly what the
Claude ``.enc`` files already are on Linux. macOS Keychain storage is not
implemented for this provider.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from claude_swap import paths
from claude_swap.fsutil import replace_with_retry
from claude_swap.locking import FileLock
from claude_swap.settings import atomic_write_json

PROVIDER = "codex"
SCHEMA_VERSION = 1

_logger = logging.getLogger("claude-swap")


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CodexAccountStore:
    """Slots, credentials and the active pointer for the Codex pool."""

    def __init__(self) -> None:
        self.root = paths.get_provider_root(PROVIDER)
        self.credentials_dir = self.root / "credentials"
        self.cache_dir = self.root / "cache"
        self.sequence_file = self.root / "sequence.json"
        self._lock_path = self.root / ".lock"

    # -- directories and raw I/O -------------------------------------------

    def ensure_dirs(self) -> None:
        """Create the provider state tree with 0700 directories."""
        for directory in (self.root, self.credentials_dir, self.cache_dir):
            directory.mkdir(parents=True, exist_ok=True)
            if sys.platform != "win32":
                os.chmod(directory, 0o700)

    def _lock(self) -> FileLock:
        self.ensure_dirs()
        return FileLock(self._lock_path)

    def _read_sequence(self) -> dict:
        """The sequence file, or an empty shape when absent or unreadable."""
        try:
            data = json.loads(self.sequence_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {"schemaVersion": SCHEMA_VERSION, "accounts": {}}
        if not isinstance(data, dict) or not isinstance(data.get("accounts"), dict):
            return {"schemaVersion": SCHEMA_VERSION, "accounts": {}}
        return data

    def _write_sequence(self, data: dict) -> None:
        """Stamp and atomically write the sequence file."""
        self.ensure_dirs()
        data["schemaVersion"] = SCHEMA_VERSION
        data["lastUpdated"] = _timestamp()
        atomic_write_json(self.sequence_file, data)

    # -- slots --------------------------------------------------------------

    def accounts(self) -> list[tuple[str, dict]]:
        """``(slot, record)`` pairs in numeric slot order."""
        rows = self._read_sequence()["accounts"]
        return sorted(rows.items(), key=lambda kv: int(kv[0]))

    def active_number(self) -> str | None:
        """The slot recorded as active, or None."""
        value = self._read_sequence().get("activeAccountNumber")
        return str(value) if value is not None else None

    def next_slot(self) -> str:
        """The lowest unused slot number, as a string."""
        taken = {int(n) for n in self._read_sequence()["accounts"]}
        candidate = 1
        while candidate in taken:
            candidate += 1
        return str(candidate)

    # -- credential backups -------------------------------------------------

    def credential_path(self, slot: str, email: str) -> Path:
        """Backup file for one slot. Named like the Claude ones."""
        return self.credentials_dir / f".creds-{slot}-{email}.enc"

    def write_credential(self, slot: str, email: str, blob: str) -> None:
        """Atomically store a credential, base64-encoded, mode 0600."""
        self.ensure_dirs()
        target = self.credential_path(slot, email)
        encoded = base64.b64encode(blob.encode("utf-8"))
        fd, tmp_path = tempfile.mkstemp(dir=str(self.credentials_dir), suffix=".tmp")
        try:
            os.write(fd, encoded)
            os.close(fd)
            fd = -1
            replace_with_retry(tmp_path, str(target))
            if sys.platform != "win32":
                os.chmod(str(target), 0o600)
        except BaseException:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def read_credential(self, slot: str, email: str) -> str:
        """The stored credential, or an empty string when absent or corrupt."""
        try:
            encoded = self.credential_path(slot, email).read_bytes()
        except OSError:
            return ""
        try:
            return base64.b64decode(encoded).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            _logger.warning("Codex credential for slot %s is unreadable", slot)
            return ""

    def delete_credential(self, slot: str, email: str) -> None:
        """Remove a stored credential. Absent is not an error."""
        self.credential_path(slot, email).unlink(missing_ok=True)
