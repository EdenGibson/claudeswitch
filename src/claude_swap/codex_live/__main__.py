"""Supervised auth helper. Its connections outlive the switch command."""

from __future__ import annotations

import asyncio
import json
import time

from claude_swap import paths
from claude_swap.locking import FileLock
from claude_swap.settings import atomic_write_json

from . import state_dir
from .credentials import CredentialSource
from .discovery import discover
from .rpc import AuthClient


async def watch() -> None:
    source = CredentialSource()
    clients: dict[int, AuthClient] = {}
    state = state_dir()
    last_request = None
    try:
        while (state / 'request.json').exists():
            request = json.loads((state / 'request.json').read_text())
            listeners, unsupported = await asyncio.to_thread(discover, paths.get_codex_home())
            wanted = {listener.pid for listener in listeners}
            for pid in list(clients):
                if pid not in wanted:
                    await clients.pop(pid).close()
            try:
                credential = await asyncio.to_thread(source.read)
                error = None
            except (OSError, ValueError) as exc:
                credential, error = None, str(exc)

            async def update(listener):
                row = {'pid': listener.pid, 'status': 'failed'}
                if error:
                    row['error'] = error
                    return row
                client = clients.get(listener.pid)
                if client and client.listener != listener:
                    await clients.pop(listener.pid).close()
                    client = None
                if client is None:
                    client = clients[listener.pid] = AuthClient(listener, source)
                try:
                    await client.apply(credential, verify=request['id'] != last_request)
                    row.update(status='updated', email=credential.email)
                except Exception as exc:
                    # Never persist server error bodies or token-bearing frames.
                    row['error'] = type(exc).__name__ + ': auth update was not confirmed'
                    await clients.pop(listener.pid).close()
                return row

            rows = await asyncio.gather(*(update(listener) for listener in listeners))
            atomic_write_json(state / 'status.json', {
                'requestId': request['id'], 'checkedAt': time.time(),
                'accountId': credential.account_id if credential else None,
                'servers': rows, 'unsupported': unsupported, 'error': error,
            })
            last_request = request['id']
            if not listeners:
                return  # No external-auth clients remain to serve.
            await asyncio.sleep(1)
    finally:
        await asyncio.gather(*(client.close() for client in clients.values()), return_exceptions=True)


def main() -> None:
    # Also excludes an accidental manual launch beside the supervised service.
    lock = FileLock(state_dir() / 'daemon.lock', timeout=0)
    if not lock.acquire():
        return
    try:
        asyncio.run(watch())
    finally:
        lock.release()


if __name__ == '__main__':
    main()
