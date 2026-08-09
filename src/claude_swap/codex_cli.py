"""``cswap codex <sub>`` — an alias over the unified account commands.

Phase 1 kept a separate Codex pool with its own registry and its own command
surface. The pool is now one registry, so a second surface would be a second
truth: ``cswap codex list`` would read a store that migration already emptied.

Every subcommand here rewrites to the equivalent main command and hands over.
``list`` and ``status`` therefore show the whole pool, Claude accounts
included — one pool, one list.
"""

from __future__ import annotations

import sys

from claude_swap import printer

#: Subcommand → the main-command argv it becomes. The commands that take an
#: account identifier get it appended.
_ALIASES: dict[str, list[str]] = {
    "list": ["--list"],
    "ls": ["--list"],
    "status": ["--status"],
    "add": ["--add-account", "--provider", "codex"],
    "switch": ["--switch-to"],
    "remove": ["--remove-account"],
    "rm": ["--remove-account"],
}

#: Subcommands that need an account identifier.
_NEEDS_ACCOUNT = frozenset({"switch", "remove", "rm"})

_HELP = """usage: cswap codex <command> [args]

Manage OpenAI Codex (ChatGPT subscription) accounts. Every command is an
alias for the matching cswap command, which now holds accounts of both
providers in one pool.

  cswap codex list               ->  cswap list
  cswap codex status             ->  cswap status
  cswap codex add                ->  cswap add --provider codex
  cswap codex switch <num|email> ->  cswap switch <num|email>
  cswap codex remove <num|email> ->  cswap remove <num|email>

list and status show every account, Claude ones included.
"""


def translate(argv: list[str]) -> list[str]:
    """Rewrite ``codex <sub> [args]`` into the main command's argv.

    Raises SystemExit(2) on an unknown subcommand or a missing account, the
    same code argparse uses for a usage error.
    """
    if not argv:
        printer.error("cswap codex needs a command. Try 'cswap codex --help'.")
        raise SystemExit(2)

    verb, rest = argv[0], argv[1:]
    if verb in ("-h", "--help", "help"):
        print(_HELP, end="")
        raise SystemExit(0)

    flags = _ALIASES.get(verb)
    if flags is None:
        known = ", ".join(sorted(_ALIASES))
        printer.error(f"unknown command: {verb} (choose from {known})")
        raise SystemExit(2)

    flags = list(flags)
    if verb in _NEEDS_ACCOUNT:
        if not rest or rest[0].startswith("-"):
            printer.error(f"cswap codex {verb} needs an account (number or email)")
            raise SystemExit(2)
        flags.append(rest[0])
        rest = rest[1:]
    return flags + rest


def codex_command(argv: list[str]) -> None:
    """Entry point for ``cswap codex``: rewrite, then run the main command."""
    from claude_swap.cli import main

    sys.argv = [sys.argv[0], *translate(argv)]
    main()
