"""Route credential work to the backend that owns it.

``sequence.json`` is one registry for accounts of every provider, but each
credential lives where its own CLI expects it: Claude Code reads
``~/.claude/.credentials.json``, the Codex CLI reads ``~/.codex/auth.json``.
This module is the one place that knows which is which. Callers ask for
``ops_for(account.provider)`` and never test the provider string themselves.

Nothing here holds state, so an ops object is cheap to build per call.

Two asymmetries are deliberate and must not be smoothed away:

* ``oauth.fetch_usage`` takes an access token, ``codex.fetch_usage`` takes the
  whole credential blob. The Codex call needs the blob so it can refresh a
  spent token; the Claude path refreshes elsewhere.
* ``ClaudeOps.write_live`` raises. Activating a Claude account carries
  keychain fallback, a config backup and a rollback record that only
  ``ClaudeAccountSwitcher`` owns. Every caller that activates a Claude
  account already holds a switcher, so the member exists for shape only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from claude_swap import paths
from claude_swap.providers import AccountIdentity


class ProviderOps(Protocol):
    """The operations a provider must supply to join the pool."""

    #: Registry key, as stored in sequence.json.
    name: str
    #: The executable ``cswap run`` launches for this provider.
    binary: str
    #: Short label for the provider column.
    label: str

    def live_path(self) -> Path:
        """Where this provider's CLI keeps the credential it is using now."""
        ...

    def read_live(self) -> str | None:
        """The live credential blob, or None when nobody is logged in."""
        ...

    def write_live(self, blob: str) -> None:
        """Make ``blob`` the credential this provider's CLI will use."""
        ...

    def identity(self, blob: str) -> AccountIdentity | None:
        """Who ``blob`` belongs to, or None when it cannot be resolved."""
        ...

    def fetch_usage(self, blob: str) -> dict | None:
        """Normalized usage: ``{five_hour, seven_day, scoped, spend}``."""
        ...


class ClaudeOps:
    """Claude Code credentials."""

    name = "claude"
    binary = "claude"
    label = "claude"

    def live_path(self) -> Path:
        return paths.get_credentials_path()

    def read_live(self) -> str | None:
        path = self.live_path()
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def write_live(self, blob: str) -> None:
        raise NotImplementedError(
            "Claude activation goes through ClaudeAccountSwitcher, which owns "
            "the keychain fallback and the rollback record"
        )

    def identity(self, blob: str) -> AccountIdentity | None:
        """Resolve ownership over the network; there is no offline reader.

        A Claude OAuth token carries no email claim, so
        ``oauth.fetch_oauth_profile`` is the only source. It answers uuid,
        email and organizationUuid — never the org name or the plan, which
        the registry entry holds instead. Never call this on a hot path, and
        never while a credential lock is held.
        """
        from claude_swap import oauth

        token = oauth.extract_access_token(blob)
        if not token:
            return None
        profile = oauth.fetch_oauth_profile(token)
        if not profile:
            return None
        return AccountIdentity(
            email=profile.get("email") or "",
            account_uuid=profile.get("uuid") or "",
            org_uuid=profile.get("organizationUuid") or "",
            org_name="",
            plan="",
        )

    def fetch_usage(self, blob: str) -> dict | None:
        from claude_swap import oauth

        token = oauth.extract_access_token(blob)
        return oauth.fetch_usage(token) if token else None


class CodexOps:
    """Codex (ChatGPT) credentials."""

    name = "codex"
    binary = "codex"
    label = "codex"

    def live_path(self) -> Path:
        return paths.get_codex_auth_path()

    def read_live(self) -> str | None:
        from claude_swap.codex_store import CodexAccountStore

        # The store answers "" for a missing file; the protocol says None, so
        # both providers report "not logged in" the same way.
        return CodexAccountStore().read_live() or None

    def write_live(self, blob: str) -> None:
        from claude_swap.codex_store import CodexAccountStore

        CodexAccountStore().write_live(blob)

    def identity(self, blob: str) -> AccountIdentity | None:
        from claude_swap.providers import codex

        return codex.identity(blob)

    def fetch_usage(self, blob: str) -> dict | None:
        from claude_swap.providers import codex

        return codex.fetch_usage(blob)


_OPS: dict[str, ProviderOps] = {"claude": ClaudeOps(), "codex": CodexOps()}

#: Registry default. An entry with no provider key is a Claude account.
DEFAULT_PROVIDER = "claude"


def ops_for(provider: str | None) -> ProviderOps:
    """The ops for ``provider``; None or empty means the Claude default."""
    key = provider or DEFAULT_PROVIDER
    try:
        return _OPS[key]
    except KeyError:
        raise ValueError(f"unknown provider: {key!r}") from None


def provider_names() -> tuple[str, ...]:
    """Every provider the pool can hold, Claude first."""
    return ("claude", "codex")


def is_known(provider: str | None) -> bool:
    """Whether ``provider`` names a backend this build can drive."""
    return (provider or DEFAULT_PROVIDER) in _OPS
