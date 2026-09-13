"""One refresh owner for the servers that follow the shared Codex login."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field

from claude_swap import paths
from claude_swap.codex_store import CodexAccountStore
from claude_swap.locking import FileLock
from claude_swap.providers import codex


@dataclass(frozen=True)
class Credential:
    account_id: str
    email: str
    plan: str
    access_token: str = field(repr=False)

    @property
    def revision(self) -> str:
        return hashlib.sha256((self.account_id + '\0' + self.access_token).encode()).hexdigest()

    def params(self) -> dict:
        return {'accessToken': self.access_token, 'chatgptAccountId': self.account_id,
                'chatgptPlanType': self.plan or None}


def parse(blob: str) -> Credential:
    identity = codex.identity(blob)
    tokens = codex._tokens(blob)
    if not identity or not identity.account_uuid or not tokens:
        raise ValueError('No ChatGPT login in auth.json')
    token = tokens.get('access_token')
    account_id = tokens.get('account_id') or identity.account_uuid
    if not isinstance(token, str) or not token or not isinstance(account_id, str):
        raise ValueError('Incomplete ChatGPT login in auth.json')
    return Credential(account_id, identity.email, identity.plan, token)


class CredentialSource:
    def __init__(self) -> None:
        self.store = CodexAccountStore()
        self._mutex = threading.Lock()
        self._failed_revision = ''
        self._retry_at = 0.0
        self._refreshed_revision = ''
        self._refreshed_at = 0.0

    def read(self, previous: Credential | None = None) -> Credential:
        """Refresh expired tokens, or a rejected token still in the live file.

        A callback for an earlier account must never undo an explicit switch.
        Concurrent servers reuse the first refresh result. The store lock is
        also held by the switch transaction, so the rotation cannot be lost.
        """
        with self._mutex, self.store._lock():
            blob = self.store.read_live()
            current = parse(blob)
            if previous and previous.account_id != current.account_id:
                raise ValueError('Account changed; retry with the selected account')
            force = previous is not None and previous.revision == current.revision
            if not force and not codex.is_expired(blob):
                return current
            # Several Codex subsystems can reject the same fresh token at once.
            # Give them the completed exchange instead of rotating it repeatedly.
            if current.revision == self._refreshed_revision and time.monotonic() - self._refreshed_at < 30:
                return current
            if current.revision == self._failed_revision and time.monotonic() < self._retry_at:
                raise ValueError('Token refresh is on cooldown; retry later')
            result = codex.try_refresh(blob, timeout_s=5)
            if result.error or not result.credentials:
                self._failed_revision = current.revision
                self._retry_at = time.monotonic() + 60
                raise ValueError('Codex token refresh failed; check the account login')
            refreshed = parse(result.credentials)
            if refreshed.account_id != current.account_id:
                raise ValueError('Token refresh returned a different account')
            # Codex itself does not take our lock. Preserve an external login.
            if self.store.read_live() != blob:
                latest = parse(self.store.read_live())
                if latest.account_id != refreshed.account_id:
                    self._save_account(result.credentials, refreshed.account_id)
                raise ValueError('Login changed during token refresh; retry')
            self.store.write_live(result.credentials)
            self._save_account(result.credentials, refreshed.account_id)
            self._refreshed_revision = refreshed.revision
            self._refreshed_at = time.monotonic()
            return refreshed

    def _save_account(self, blob: str, account_id: str) -> None:
        root = paths.get_backup_root()
        with FileLock(root / '.lock'):
            try:
                data = json.loads((root / 'sequence.json').read_text())
            except (OSError, ValueError):
                return
            for slot, account in data.get('accounts', {}).items():
                if account.get('provider') == 'codex' and account.get('uuid') == account_id:
                    self.store.write_credential(str(slot), account['email'], blob)
                    break
