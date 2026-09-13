"""Making a backend change happen.

Three callers need the same steps — ``cswap backend``, ``cswap switch`` on a
Codex slot, and the autoswitch engine's fallback — so the steps live here
rather than in any one of them.

Going to Codex means: publish that account's credential where CLIProxyAPI
reads it, then name the account in the mode file. Coming back to Claude means
taking CLIProxyAPI's refreshed token back into cswap's own store first. An
OpenAI refresh token is single-use, so skipping that step kills the login.

The last three functions are what the switcher calls. They exist so that
``switcher.py`` knows one name per event — status, switch, uninstall — and
nothing about the router beyond that. Each returns text instead of raising:
the switcher's three callers all have to finish their own job whatever the
router does.
"""

from __future__ import annotations

import logging

from claude_swap.printer import accent, bolded, dimmed
from claude_swap.router import cliproxy
from claude_swap.router import install as router_install
from claude_swap.router.mode import RouterMode, read_mode, write_mode


def codex_slots(switcher) -> list[tuple[str, str]]:
    """(slot, email) for every Codex account in the pool, in slot order."""
    data = switcher._get_sequence_data() or {}
    found = [
        (str(num), account.get("email", ""))
        for num, account in data.get("accounts", {}).items()
        if account.get("provider") == "codex"
    ]
    return sorted(found, key=lambda pair: int(pair[0]) if pair[0].isdigit() else 0)


def router_serves(slot: str) -> bool:
    """Whether CLIProxyAPI is currently serving this Codex slot.

    While it is, the backend owns that token family: it refreshes on its own
    cadence and holds a copy cswap did not write. Callers use this to keep
    their hands off a refresh that would retire the backend's token.

    Reads the mode file, which is one small JSON read, so it is cheap enough
    to ask per fetch rather than caching an answer that a flip can invalidate
    at any moment.
    """
    mode = read_mode()
    return mode.provider == "codex" and mode.slot == str(slot)


def blob_for(switcher, slot: str, email: str) -> str:
    """A slot's Codex credential: the live file when it owns it, else the store.

    The live file is preferred because the Codex CLI refreshes in place, so it
    can hold a newer token than the stored copy.
    """
    from claude_swap import provider_ops

    if switcher.provider_active_number("codex") == slot:
        live = provider_ops.ops_for("codex").read_live()
        if live:
            return live
    return switcher._read_provider_material(slot, email)


def sync_back(switcher, slot: str | None = None) -> str:
    """Take CLIProxyAPI's rotated token back into cswap's store.

    Returns the slot written, or "" when there was nothing newer. Reads the
    mode file for the slot when none is given.
    """
    from claude_swap import provider_ops
    from claude_swap.codex_store import CodexAccountStore

    target = slot if slot is not None else read_mode().slot
    if target is None:
        return ""
    email = switcher.account_email(target)
    original = blob_for(switcher, target, email)
    if not original:
        return ""
    merged = cliproxy.merge_back(original)
    if merged is None:
        return ""
    CodexAccountStore().write_credential(target, email, merged)
    if switcher.provider_active_number("codex") == target:
        provider_ops.ops_for("codex").write_live(merged)
    return target


def activate_codex(
    switcher, slot: str, email: str, *, pinned: bool
) -> tuple[bool, str]:
    """Publish a Codex account and point the mode file at it.

    Returns (ok, message). The mode file is written last, and only once the
    backend answers. In codex mode the router has nowhere else to send a
    request, so a mode file pointing at a dead backend turns every running
    session into a 503 until something else moves it.

    Replacing a different Codex account takes that account's rotated token
    back first. ``write_credential`` deletes every other file in the auth
    directory, so the rotation is unreadable a moment later.
    """
    blob = blob_for(switcher, slot, email)
    if not blob:
        return False, f"account {slot} has no stored Codex credential"

    current = read_mode()
    if current.provider == "codex" and current.slot not in (None, str(slot)):
        cliproxy.stop()
        sync_back(switcher, current.slot)

    if cliproxy.write_credential(blob) is None:
        return False, f"account {slot}'s credential could not be read"

    ok, note = cliproxy.ensure_running()
    if not ok:
        return False, note
    write_mode(RouterMode(provider="codex", slot=str(slot), pinned=pinned))
    return True, note


def activate_claude(switcher, *, pinned: bool) -> str:
    """Return the router to Anthropic. Returns the slot synced back, or "".

    CLIProxyAPI stops first, and only then is its credential read back. Left
    running it refreshes the Codex login every 15 minutes, which rotates a
    single-use refresh token cswap is no longer watching. The next flip to
    Codex would then publish a spent token and kill the account.
    """
    slot = read_mode().slot
    cliproxy.stop()
    synced = sync_back(switcher, slot)
    write_mode(RouterMode(provider="claude", pinned=pinned))
    return synced


# -- what the switcher calls -------------------------------------------------


def backend_line() -> str | None:
    """A status line naming the backend, or None when it is Anthropic.

    Claude Code's own UI keeps naming a Claude model while a GPT model
    answers, so nothing else on screen tells the truth. Silent in the normal
    case: a router in claude mode, or none at all, says nothing.
    """
    try:
        mode = read_mode()
    except Exception:  # status must never fail on a cosmetic line
        return None
    if mode.provider == "claude":
        return None
    where = f"Account-{mode.slot}" if mode.slot else mode.provider
    return (
        f"{bolded('Backend:')} {accent(mode.provider)} ({where}) "
        f"{dimmed('— Claude Code sessions behind the router use it')}"
    )


def follow_switch(switcher, provider: str, account_num: str) -> str:
    """Point a codex-mode router at the slot that just went live.

    Only when the router is already serving that provider. Switching a Codex
    account must never turn the router on by itself — which backend Claude
    Code talks to is ``cswap backend``'s decision, not this one.

    Returns a warning for the user, or "" when there is nothing to say. A
    failure here is worth saying out loud: the router keeps serving the
    account the user just switched away from, and goes on spending it.
    """
    try:
        mode = read_mode()
        if mode.provider != provider or mode.slot == account_num:
            return ""
        # Through activate_codex, not a bare write_credential: the slot being
        # replaced has a rotated single-use token that only the shared path
        # takes back before the file is deleted.
        ok, message = activate_codex(
            switcher,
            account_num,
            switcher.account_email(account_num),
            pinned=mode.pinned,
        )
        if ok:
            return message
        return f"the router is still serving Account-{mode.slot}: {message}"
    except Exception as exc:  # the switch itself must still succeed
        return f"the router did not follow the switch: {exc}"


def detach_from(switcher, slot: str) -> str:
    """Return the backend to Claude when it is serving ``slot``.

    Called before a Codex slot's credential is deleted. CLIProxyAPI refreshes
    the login while it serves and an OpenAI refresh token is single use, so
    the rotation has to come back out of the backend first: afterwards the
    stored copy is gone and the live ``~/.codex/auth.json`` that ``remove``
    deliberately leaves behind would hold a token the server has retired.

    Returns a line for the caller's report, or "" when the router was serving
    something else. Never raises: removing an account must finish either way.
    """
    try:
        mode = read_mode()
        if mode.provider != "codex" or mode.slot != str(slot):
            return ""
        activate_claude(switcher, pinned=False)
        return f"the router returned to Claude: Account-{slot} was its backend"
    except Exception as exc:  # the removal itself must still finish
        return f"the router did not release Account-{slot}: {exc}"


def follow_renumber(moves: dict[str, str]) -> str:
    """Point the mode file at a Codex slot's new number after a swap or move.

    ``moves`` maps old slot to new slot. The account behind the slot has not
    changed, only its number, and its credential moved with it, so the backend
    keeps serving and only the name it is filed under changes. Returning to
    Claude would stop a backend that is still correct; leaving the old number
    is worse, because the next ``sync_back`` would then read a slot holding a
    different account.

    Returns the new slot when the mode file moved, else "". Never raises: the
    renumber it follows has already been committed, and undoing it is not on
    offer. A failure is logged rather than swallowed, because the mode file is
    then left naming a slot that now holds a different account, and the next
    ``sync_back`` would merge the backend's rotated token into the wrong one.
    """
    try:
        mode = read_mode()
        if mode.provider != "codex" or mode.slot is None:
            return ""
        new = moves.get(str(mode.slot))
        if new is None:
            return ""
        write_mode(RouterMode(provider="codex", slot=str(new), pinned=mode.pinned))
        return new
    except Exception as exc:
        logging.getLogger("claude-swap").warning(
            "the router's backend slot could not be updated (%s). Its mode "
            "file may still name a slot that now holds another account. Run "
            "'cswap backend claude', then pick the backend again.",
            exc,
        )
        return ""


def tear_down(switcher) -> str:
    """Take the router out. Returns a line for the removal report, or "".

    Must run before the backup directory goes. ``install-state.json`` lives
    in there and holds the ``ANTHROPIC_BASE_URL`` to restore; delete it first
    and Claude Code is left pointing at a dead local port with nothing left
    that knows what it used to point at.
    """
    try:
        if read_mode().provider == "codex":
            activate_claude(switcher, pinned=False)
        router_install.stop()
        cliproxy.stop()
        cliproxy.remove_unit()
        if router_install.uninstall():
            return "Router: settings.json and unit restored"
        return ""
    except Exception as exc:  # uninstall must finish regardless
        return f"Router: not fully removed ({exc})"
