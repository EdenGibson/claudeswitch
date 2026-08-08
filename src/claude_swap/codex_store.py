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
import time
from datetime import datetime, timezone
from pathlib import Path

from claude_swap import paths
from claude_swap.fsutil import replace_with_retry
from claude_swap.locking import FileLock
from claude_swap.oauth import _classify_usage_error as classify_usage_error
from claude_swap.providers import codex
from claude_swap.providers.base import AccountIdentity
from claude_swap.settings import atomic_write_json
from claude_swap.usage_store import FetchRecord, UsageEntry, UsageStore, with_sentinel

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

    # -- the live credential ------------------------------------------------

    def live_path(self) -> Path:
        """Where the Codex CLI reads its credential."""
        return paths.get_codex_auth_path()

    def read_live(self) -> str:
        """The live credential, or an empty string when absent."""
        try:
            return self.live_path().read_text(encoding="utf-8")
        except OSError:
            return ""

    def write_live(self, blob: str) -> None:
        """Atomically replace the live credential, mode 0600."""
        target = self.live_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        try:
            os.write(fd, blob.encode("utf-8"))
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

    # -- resolution ---------------------------------------------------------

    def resolve(self, identifier: str) -> tuple[str, dict]:
        """``(slot, record)`` for a slot number or an email address.

        Raises ValueError naming the identifier when nothing matches.
        """
        rows = self._read_sequence()["accounts"]
        if identifier in rows:
            return identifier, rows[identifier]
        wanted = identifier.strip().lower()
        for slot, record in sorted(rows.items(), key=lambda kv: int(kv[0])):
            if str(record.get("email", "")).lower() == wanted:
                return slot, record
        raise ValueError(f"No Codex account matches '{identifier}'")

    # -- mutations ----------------------------------------------------------

    def _recapture_active(self, data: dict) -> None:
        """Save the live credential back into the slot that owns it.

        The Codex CLI refreshes tokens in place. Without this, switching away
        discards that rotation and a later switch back restores a stale token.
        Mutates nothing when the live credential belongs to no known slot.
        """
        live = self.read_live()
        if not live:
            return
        ident = codex.identity(live)
        if ident is None:
            return
        for slot, record in data["accounts"].items():
            if record.get("accountId") == ident.account_uuid:
                self.write_credential(slot, str(record.get("email", "")), live)
                return

    def add_current(self) -> tuple[str, AccountIdentity]:
        """Capture the live credential into a new slot and make it active.

        Raises ValueError when nothing is logged in, when the credential is
        unreadable, or when the account is already managed.
        """
        live = self.read_live()
        if not live:
            raise ValueError(
                f"No Codex credential at {self.live_path()}. Run 'codex login' first."
            )
        ident = codex.identity(live)
        if ident is None:
            raise ValueError(f"Could not read a Codex account from {self.live_path()}")

        with self._lock():
            data = self._read_sequence()
            for slot, record in data["accounts"].items():
                if record.get("accountId") == ident.account_uuid:
                    raise ValueError(
                        f"{ident.email} is already managed in slot {slot}"
                    )
            slot = self.next_slot()
            data["accounts"][slot] = {
                "email": ident.email,
                "accountId": ident.account_uuid,
                "organizationUuid": ident.org_uuid,
                "organizationName": ident.org_name,
                "plan": ident.plan,
                "added": _timestamp(),
            }
            data["activeAccountNumber"] = int(slot)
            self.write_credential(slot, ident.email, live)
            self._write_sequence(data)
        return slot, ident

    def switch(self, identifier: str) -> tuple[str, str]:
        """Make an account live. Returns ``(slot, email)``.

        Raises ValueError when the account is unknown or its stored credential
        is missing.
        """
        with self._lock():
            data = self._read_sequence()
            slot, record = self.resolve(identifier)
            email = str(record.get("email", ""))
            blob = self.read_credential(slot, email)
            if not blob:
                raise ValueError(
                    f"No stored credential for slot {slot} ({email}). "
                    f"Re-add it with 'cswap codex add'."
                )
            self._recapture_active(data)
            self.write_live(blob)
            data["activeAccountNumber"] = int(slot)
            self._write_sequence(data)
        return slot, email

    def remove(self, identifier: str) -> tuple[str, str]:
        """Forget an account and delete its stored credential.

        Returns ``(slot, email)``. The live ``auth.json`` is left alone: this
        removes cswap's copy, never the user's current login.
        """
        with self._lock():
            data = self._read_sequence()
            slot, record = self.resolve(identifier)
            email = str(record.get("email", ""))
            del data["accounts"][slot]
            if str(data.get("activeAccountNumber")) == slot:
                data.pop("activeAccountNumber", None)
            self.delete_credential(slot, email)
            self._write_sequence(data)
        return slot, email

    # -- usage --------------------------------------------------------------

    def _identities(self) -> dict[str, tuple[str, str]]:
        """Slot -> ``(email, accountId)``, the identity ``UsageStore`` guards on."""
        return {
            slot: (str(record.get("email", "")), str(record.get("accountId", "")))
            for slot, record in self.accounts()
        }

    def _usage_store(self) -> UsageStore:
        self.ensure_dirs()
        return UsageStore(self.cache_dir)

    def usage_entries(self) -> dict[str, UsageEntry]:
        """Stored usage per slot, with no network call."""
        return self._usage_store().entries(self._identities())

    def collect_usage(self, force: bool = False) -> dict[str, UsageEntry]:
        """Fetch usage for every account whose row is due, then return them all.

        Refreshes an expired access token first and persists the rotated
        credential, because a refresh that is not written back is lost work and
        the next poll would repeat it. A dead refresh lineage is surfaced as the
        ``token expired`` sentinel rather than a fetch error, so the UI can say
        what the user must actually do.
        """
        usage_store = self._usage_store()
        identities = self._identities()
        entries = usage_store.entries(identities)
        now = time.time()

        outcomes: dict[str, FetchRecord] = {}
        sentinels: dict[str, str] = {}

        for slot, (email, _account_id) in identities.items():
            entry = entries.get(slot)
            if not force and entry is not None and entry.fresh(now):
                continue

            blob = self.read_credential(slot, email)
            if not blob:
                continue

            if codex.is_expired(blob):
                refreshed = codex.try_refresh(blob)
                if refreshed.error is not None:
                    if refreshed.error in ("invalid_grant", "no_refresh_token"):
                        sentinels[slot] = "token expired"
                    continue
                blob = refreshed.credentials
                self.write_credential(slot, email, blob)

            try:
                usage = codex.fetch_usage(blob)
            except Exception as exc:  # noqa: BLE001 - classified just below
                kind, retry_after = classify_usage_error(exc)
                outcomes[slot] = FetchRecord(error=kind, retry_after_s=retry_after)
                continue
            outcomes[slot] = FetchRecord(usage=usage)

        if outcomes:
            usage_store.record(outcomes, identities)

        merged = usage_store.entries(identities)
        for slot, sentinel in sentinels.items():
            merged[slot] = with_sentinel(merged.get(slot, UsageEntry()), sentinel)
        return merged
