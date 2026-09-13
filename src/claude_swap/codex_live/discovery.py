"""Find local Codex listeners without crossing user or CODEX_HOME boundaries."""

from __future__ import annotations

import os
import socket
import stat
import struct
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Listener:
    pid: int
    path: str


def discover(home: Path, proc: Path = Path('/proc')) -> tuple[list[Listener], list[int]]:
    """Linux Unix listeners and unsupported standalone processes for this home.

    Remote TUIs are clients, not additional servers. Explicit API credentials
    and private session homes belong to their operator and are excluded.
    """
    listeners, unsupported = [], []
    if not proc.exists():
        return listeners, unsupported
    for process in proc.iterdir():
        if not process.name.isdecimal():
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            if process.joinpath('exe').resolve().name.removesuffix(' (deleted)') != 'codex':
                continue
            args = process.joinpath('cmdline').read_bytes().decode().split('\0')
            env = dict(item.split('=', 1) for item in
                       process.joinpath('environ').read_bytes().decode().split('\0')
                       if '=' in item)
            if any(env.get(key) for key in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'CODEX_ACCESS_TOKEN')):
                continue
            selected = Path(env.get('CODEX_HOME') or str(Path(env['HOME']) / '.codex'))
            if not selected.is_absolute():
                selected = process.joinpath('cwd').resolve() / selected
            if selected.resolve() != home.resolve():
                continue
            if '--remote' in args or any(arg.startswith('--remote=') for arg in args):
                continue
            if 'app-server' not in args:
                # Ignore one-shot commands such as login, exec, and schema generation.
                commands = {'exec', 'e', 'login', 'logout', 'features', 'mcp', 'mcp-server',
                            'help', 'completion', 'debug', 'sandbox', 'apply', 'review',
                            'cloud', 'app', 'archive', 'unarchive', 'delete'}
                if not commands.intersection(args[1:]) and not {'--help', '--version'}.intersection(args):
                    unsupported.append(int(process.name))
                continue
            endpoint = next((arg.split('=', 1)[1] for arg in args if arg.startswith('--listen=')), '')
            if '--listen' in args:
                endpoint = args[args.index('--listen') + 1]
            if not endpoint.startswith('unix:///'):
                unsupported.append(int(process.name))
                continue
            path = endpoint.removeprefix('unix://')
            info = Path(path).stat()
            if stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid():
                listeners.append(Listener(int(process.name), path))
        except (OSError, ValueError, KeyError, IndexError, UnicodeError):
            continue  # Process exited or is not inspectable by this user.
    return sorted(listeners, key=lambda row: row.pid), sorted(unsupported)


def connect_socket(listener: Listener) -> socket.socket:
    """Verify the peer process before any credential enters the connection."""
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.settimeout(3)
        sock.connect(listener.path)
        pid, uid, _gid = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if pid != listener.pid or uid != os.getuid():
            raise OSError('Codex socket owner changed')
        sock.setblocking(False)
        return sock
    except BaseException:
        sock.close()
        raise
