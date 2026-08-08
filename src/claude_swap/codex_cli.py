"""``cswap codex <sub>`` — the Codex account pool's command surface.

A separate namespace, not new flags on the existing commands: the two pools are
independent in Phase 1, and a namespace keeps the diff against upstream's
``cli.py`` to the three dispatch lines in ``main()``.
"""

from __future__ import annotations

import argparse
import json
import sys

from claude_swap import printer
from claude_swap.codex_store import CodexAccountStore
from claude_swap.oauth import account_headroom, fresh_reset_strings


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cswap codex",
        usage="%(prog)s <command> [args]",
        description="""Manage OpenAI Codex (ChatGPT subscription) accounts.

Commands:
  %(prog)s list                 list managed Codex accounts
  %(prog)s status                show the active Codex account
  %(prog)s add                   add the account currently in ~/.codex/auth.json
  %(prog)s switch <num|email>    make an account live
  %(prog)s remove <num|email>    forget an account""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subs = parser.add_subparsers(dest="command", required=True)

    p_list = subs.add_parser("list", aliases=["ls"])
    p_list.add_argument("--json", action="store_true", help="machine-readable output")
    p_list.add_argument(
        "--no-usage", action="store_true", help="skip the usage fetch"
    )

    subs.add_parser("status")
    subs.add_parser("add")

    p_switch = subs.add_parser("switch")
    p_switch.add_argument("account", metavar="NUM|EMAIL")

    p_remove = subs.add_parser("remove", aliases=["rm"])
    p_remove.add_argument("account", metavar="NUM|EMAIL")
    p_remove.add_argument(
        "--yes", "-y", action="store_true", help="skip the confirmation"
    )
    return parser


def _usage_summary(entry) -> str:
    """One-line quota summary for a usage entry."""
    if entry is None:
        return "-"
    if entry.sentinel:
        return entry.sentinel
    usage = entry.last_good
    if not usage:
        return "-"
    parts = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        window = usage.get(key)
        if not isinstance(window, dict):
            continue
        pct = window.get("pct")
        if not isinstance(pct, (int, float)):
            continue
        text = f"{label} {pct:.0f}%"
        reset = fresh_reset_strings(window)
        if reset:
            text += f" (resets {reset[0]})"
        parts.append(text)
    return "  ".join(parts) if parts else "-"


def _cmd_list(store: CodexAccountStore, args: argparse.Namespace) -> None:
    rows = store.accounts()
    active = store.active_number()

    if args.json:
        # --json honours --no-usage exactly as the human-readable path does. A
        # scripting caller that asks for usage must not silently get whatever
        # happened to be cached.
        entries = store.usage_entries() if args.no_usage else store.collect_usage()
        print(json.dumps({
            "provider": "codex",
            "activeSlot": active,
            "accounts": [
                {
                    "slot": slot,
                    "email": record.get("email", ""),
                    "accountId": record.get("accountId", ""),
                    "organizationName": record.get("organizationName", ""),
                    "plan": record.get("plan", ""),
                    "active": slot == active,
                    "usage": entries[slot].last_good if slot in entries else None,
                    "headroom": account_headroom(
                        entries[slot].last_good if slot in entries else None
                    ),
                }
                for slot, record in rows
            ],
        }, indent=2))
        return

    if not rows:
        print("No Codex accounts yet.")
        print("Run 'codex login', then 'cswap codex add'.")
        return

    entries = {} if args.no_usage else store.collect_usage()
    print(printer.bolded("Codex accounts"))
    for slot, record in rows:
        marker = "*" if slot == active else " "
        plan = record.get("plan") or "?"
        org = record.get("organizationName") or "personal"
        label = f"{record.get('email', '')} [{org}]"
        print(
            f" {marker} {slot}. {label}  {printer.muted(plan)}  "
            f"{_usage_summary(entries.get(slot))}"
        )


def _cmd_status(store: CodexAccountStore) -> None:
    active = store.active_number()
    if active is None:
        print("No active Codex account.")
        return
    record = dict(store.accounts()).get(active, {})
    print(f"Active Codex account: {active}. {record.get('email', '')}")
    print(f"Credential file: {store.live_path()}")


def _cmd_add(store: CodexAccountStore) -> None:
    slot, ident = store.add_current()
    print(f"Added Codex account {slot}: {ident.display_label} ({ident.plan or '?'})")


def _cmd_switch(store: CodexAccountStore, args: argparse.Namespace) -> None:
    slot, email = store.switch(args.account)
    print(f"Switched to Codex account {slot}: {email}")


def _cmd_remove(store: CodexAccountStore, args: argparse.Namespace) -> None:
    slot, record = store.resolve(args.account)
    email = record.get("email", "")
    if not args.yes:
        answer = input(f"Remove Codex account {slot} ({email})? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Cancelled.")
            return
    store.remove(args.account)
    print(f"Removed Codex account {slot}: {email}")


def codex_command(argv: list[str]) -> None:
    """Entry point for ``cswap codex``. Exits 1 on any expected failure."""
    args = _build_parser().parse_args(argv)
    store = CodexAccountStore()
    try:
        if args.command in ("list", "ls"):
            _cmd_list(store, args)
        elif args.command == "status":
            _cmd_status(store)
        elif args.command == "add":
            _cmd_add(store)
        elif args.command == "switch":
            _cmd_switch(store, args)
        elif args.command in ("remove", "rm"):
            _cmd_remove(store, args)
    except ValueError as exc:
        printer.error(str(exc))
        sys.exit(1)
    except OSError as exc:
        # A read-only home, a full disk, a permission problem. The user needs
        # the reason, not a traceback.
        printer.error(f"Codex account storage failed: {exc}")
        sys.exit(1)
