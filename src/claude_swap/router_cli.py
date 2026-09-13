"""``cswap router ...`` and ``cswap backend ...``.

The router is what makes a backend switch reach a session that is already
running. Claude Code reads ``ANTHROPIC_BASE_URL`` once, when the process
starts, so nothing cswap writes afterwards can move a live session — unless
that address is a local one cswap owns. ``cswap router install`` makes it one.

Warning: routing a Claude Code session to a ChatGPT subscription is outside
both providers' terms of service. It is the user's decision, taken knowingly.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

from claude_swap.printer import accent, bolded, dimmed, error, warning
from claude_swap.router import cliproxy, install as router_install, switching
from claude_swap.router.mode import (
    CLIPROXY_PORT,
    DEFAULT_PORT,
    RouterMode,
    read_mode,
    write_mode,
)
from claude_swap.router.modelmap import read_map
from claude_swap.settings import load_settings

_ROUTER_USAGE = """cswap router <command>

  install [--port N]   point Claude Code at the router and install the services
  uninstall            restore Claude Code's settings and remove the services
  start                start the router and the Codex backend
  stop                 stop both
  status [--json]      what is installed, what is running, which backend
  serve [--port N]     run the router in this terminal (no service)
"""

_BACKEND_USAGE = """cswap backend [claude|codex [<account>]|auto]

  (no argument)        print the current backend
  claude               send requests to api.anthropic.com
  codex [<account>]    send requests to the Codex backend
  auto                 clear the pin
"""


def _fail(message: str, code: int = 1) -> None:
    error(message)
    raise SystemExit(code)


# -- shared helpers ----------------------------------------------------------


def _switcher():
    from claude_swap.switcher import ClaudeAccountSwitcher

    return ClaudeAccountSwitcher()


def _health(port: int) -> dict | None:
    """The router's own health answer, or None when it is not listening."""
    url = f"http://127.0.0.1:{port}/_cswap/health"
    try:
        with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:
            return None
    except Exception:
        return None


# -- router subcommands ------------------------------------------------------


def _install(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="cswap router install")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--cliproxy-port", type=int, default=CLIPROXY_PORT, dest="cliproxy_port"
    )
    args = parser.parse_args(argv)

    try:
        import aiohttp  # noqa: F401
    except ImportError:
        _fail(
            "the router needs aiohttp — install it with "
            "'uv tool install --force --with aiohttp --editable <cswap checkout>' "
            "or 'pip install cswap[router]'"
        )

    cliproxy.api_key()
    cliproxy.write_config(args.cliproxy_port)
    backend_ready = cliproxy.install_unit()

    state = router_install.install(args.port)
    print(f"{bolded('Claude Code')} now talks to {accent(router_install.base_url(args.port))}")
    if state.unit_installed:
        print(dimmed(f"  installed {router_install.UNIT_NAME}"))
    if backend_ready:
        print(dimmed(f"  installed {cliproxy.UNIT_NAME}"))
    elif cliproxy.find_binary() is None:
        warning(
            "CLIProxyAPI is not installed, so the codex backend cannot run yet. "
            "Get a release from https://github.com/router-for-me/CLIProxyAPI"
        )

    ok, message = router_install.start()
    print(dimmed(f"  {message}"))
    if not ok:
        warning("start the router yourself with 'cswap router serve'")

    print()
    print("Only sessions started from now on follow a backend switch.")
    print(dimmed("  A running session keeps the address it was started with."))


def _uninstall(_argv: list[str]) -> None:
    # Return to Claude first. Tearing down while the mode file still says
    # codex would throw away the token CLIProxyAPI rotated while it served.
    if read_mode().provider == "codex":
        slot = switching.activate_claude(_switcher(), pinned=False)
        if slot:
            print(dimmed(f"  took the refreshed Codex token back into account {slot}"))
    router_install.stop()
    cliproxy.stop()
    cliproxy.remove_unit()
    changed = router_install.uninstall()
    print("Claude Code talks to api.anthropic.com again.")
    if not changed:
        print(dimmed("  nothing to undo in settings.json"))


def _start(_argv: list[str]) -> None:
    mode = read_mode()
    if mode.provider == "codex":
        ok, message = cliproxy.start()
        print(dimmed(f"  {message}") if ok else f"  {message}")
    ok, message = router_install.start()
    print(message if ok else f"could not start: {message}")
    if not ok:
        raise SystemExit(1)


def _stop(_argv: list[str]) -> None:
    for _, message in (router_install.stop(), cliproxy.stop()):
        print(dimmed(f"  {message}"))


def _status(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="cswap router status")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    state = router_install.read_state()
    port = state.port if state else DEFAULT_PORT
    mode = read_mode()
    models = read_map()
    health = _health(port)
    payload = {
        "installed": router_install.is_installed(),
        "baseUrl": router_install.base_url(port),
        "port": port,
        "routerRunning": health is not None,
        "provider": mode.provider,
        "slot": mode.slot,
        "pinned": mode.pinned,
        "backendBinary": cliproxy.find_binary(),
        "backendRunning": cliproxy.unit_active(),
        "backendAccount": cliproxy.credential_email(),
        "models": models.to_dict(),
    }
    if health is not None:
        payload["upstreamReachable"] = health.get("upstreamReachable")

    if args.as_json:
        print(json.dumps(payload, indent=2))
        return

    print(f"{bolded('Backend')}     {accent(mode.provider)}"
          + (f" (account {mode.slot})" if mode.slot else "")
          + ("" if mode.pinned else dimmed("  [not pinned]")))
    print(f"{bolded('Router')}      "
          + ("running" if payload["routerRunning"] else "not running")
          + dimmed(f"  {payload['baseUrl']}"))
    print(f"{bolded('Settings')}    "
          + ("point at the router" if payload["installed"] else "point at Anthropic"))
    backend = payload["backendBinary"] or "not installed"
    print(f"{bolded('CLIProxyAPI')} "
          + ("running" if payload["backendRunning"] else "stopped")
          + dimmed(f"  {backend}"))
    if payload["backendAccount"]:
        print(dimmed(f"  serving {payload['backendAccount']}"))
    print(f"{bolded('Models')}      {models.main}"
          + dimmed(f"  (haiku -> {models.small})"))
    if mode.provider == "codex":
        print()
        print(dimmed("If codex mode misbehaves, these help — set them yourself,"))
        print(dimmed("they are process-start variables and would hurt Claude mode:"))
        for hint in router_install.CODEX_COMPAT_HINTS:
            print(dimmed(f"  {hint}"))


def _serve(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="cswap router serve")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    try:
        from claude_swap.router.server import serve
    except ImportError:
        _fail("the router needs aiohttp — 'pip install cswap[router]'")
    serve(args.port)


_ROUTER_COMMANDS = {
    "install": _install,
    "uninstall": _uninstall,
    "start": _start,
    "stop": _stop,
    "status": _status,
    "serve": _serve,
}


def router_command(argv: list[str]) -> None:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(_ROUTER_USAGE)
        raise SystemExit(0 if argv else 2)
    handler = _ROUTER_COMMANDS.get(argv[0])
    if handler is None:
        error(f"unknown router command: {argv[0]}")
        print(_ROUTER_USAGE, file=sys.stderr)
        raise SystemExit(2)
    handler(argv[1:])


# -- backend -----------------------------------------------------------------


def _print_backend() -> None:
    mode = read_mode()
    where = accent(mode.provider) + (f" (account {mode.slot})" if mode.slot else "")
    print(where + ("" if mode.pinned else dimmed("  [not pinned]")))
    if not router_install.is_installed():
        warning("the router is not installed, so nothing follows this setting")


def _to_claude() -> None:
    switcher = _switcher()
    slot = switching.activate_claude(switcher, pinned=True)
    if slot:
        print(dimmed(f"  took the refreshed Codex token back into account {slot}"))
    print(f"Backend is {accent('claude')}.")
    _report_reach()


def _to_codex(target: str | None) -> None:
    switcher = _switcher()
    slots = switching.codex_slots(switcher)
    if not slots:
        _fail("no Codex accounts in the pool — add one with 'cswap add --provider codex'")

    if target is None:
        chosen = switcher.provider_active_number("codex") or slots[0][0]
    else:
        chosen = target
    email = switcher.account_email(chosen)
    if switcher.provider_of(chosen) != "codex":
        _fail(f"account {chosen} is not a Codex account")

    ok, message = switching.activate_codex(switcher, chosen, email, pinned=True)
    if not ok:
        _fail(message)
    if message:
        warning(message)
    print(f"Backend is {accent('codex')} on account {chosen} ({email}).")
    _report_reach()


def _to_auto() -> None:
    mode = read_mode()
    write_mode(RouterMode(provider=mode.provider, slot=mode.slot, pinned=False))
    print(f"Pin cleared. The backend stays {accent(mode.provider)} for now.")
    if load_settings(_switcher().backup_dir).fallback_provider == "codex":
        print(dimmed("  'cswap auto' moves it to Codex when every Claude"))
        print(dimmed("  account is spent, and back when one recovers."))
    else:
        print(dimmed("  Nothing moves it automatically. Turn that on with"))
        print(dimmed("  'cswap config set autoswitch.fallbackProvider codex'."))


def _report_reach() -> None:
    state = router_install.read_state()
    port = state.port if state else DEFAULT_PORT
    health = _health(port)
    if health is None:
        warning(
            "the router is not answering — a running session will keep failing "
            "until it starts ('cswap router start')"
        )
        return
    if health.get("upstreamReachable") is False:
        warning("the router is up but its backend is not answering")
        return
    print(dimmed("  Sessions started behind the router follow on their next request."))


def backend_command(argv: list[str]) -> None:
    if argv and argv[0] in ("-h", "--help", "help"):
        print(_BACKEND_USAGE)
        raise SystemExit(0)
    if not argv:
        _print_backend()
        return
    choice = argv[0]
    if choice == "claude":
        _to_claude()
    elif choice == "codex":
        _to_codex(argv[1] if len(argv) > 1 else None)
    elif choice == "auto":
        _to_auto()
    else:
        error(f"unknown backend: {choice}")
        print(_BACKEND_USAGE, file=sys.stderr)
        raise SystemExit(2)
