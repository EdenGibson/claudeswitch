"""Provider seam: the shape every account provider presents to cswap.

One provider owns one credential format and one vendor endpoint set. Nothing
here knows about slots, storage or rotation — that belongs to the store that
composes a provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from claude_swap.oauth import RefreshOutcome


@dataclass(frozen=True)
class AccountIdentity:
    """Who a credential belongs to, as read out of the credential itself.

    ``account_uuid`` is the provider's own stable account id. It is the second
    half of the identity tuple ``UsageStore`` guards rows with, so it must not
    change across a token refresh.
    """

    email: str
    account_uuid: str
    org_uuid: str = ""
    org_name: str = ""
    plan: str = ""

    @property
    def display_label(self) -> str:
        """``email [OrgName]``, or ``email [personal]`` with no org."""
        tag = self.org_name if self.org_name else "personal"
        return f"{self.email} [{tag}]"


@runtime_checkable
class Provider(Protocol):
    """What a provider must supply. Implemented by ``CodexProvider``.

    A ``ClaudeProvider`` is deliberately absent: Phase 1 never holds two
    providers at once, and writing one means moving code out of switcher.py,
    which is the most expensive file in the tree to keep merged with upstream.
    """

    name: str
    display_name: str

    def active_path(self) -> Path:
        """Where the vendor CLI reads its live credential."""
        ...

    def read_active(self) -> str | None:
        """The live credential blob, or None when absent or unreadable."""
        ...

    def write_active(self, blob: str) -> None:
        """Replace the live credential atomically, mode 0600."""
        ...

    def identity(self, blob: str) -> AccountIdentity | None:
        """Identity read out of the blob with no network call."""
        ...

    def fingerprint(self, blob: str) -> str | None:
        """Stable lineage id, unchanged by access-token rotation."""
        ...

    def is_expired(self, blob: str) -> bool:
        """Whether the blob's access token is expired or about to be."""
        ...

    def try_refresh(self, blob: str) -> RefreshOutcome:
        """Exchange the refresh token for a fresh blob."""
        ...

    def fetch_usage(self, blob: str) -> dict | None:
        """Normalized usage dict, in the shape ``oauth.build_usage_result``
        returns, or None when the response carried no window data."""
        ...
