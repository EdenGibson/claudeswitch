"""Persistent Codex auth connection. Never subscribes to or controls turns."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from websockets.asyncio.client import unix_connect

from .credentials import Credential, CredentialSource
from .discovery import Listener, connect_socket


class AuthClient:
    def __init__(self, listener: Listener, source: CredentialSource) -> None:
        self.listener = listener
        self.source = source
        self.credential: Credential | None = None
        self.ws = None
        self.reader = None
        self.pending: dict[str, asyncio.Future] = {}
        self.callbacks: set[asyncio.Task] = set()
        self.serial = 0

    async def connect(self) -> None:
        # WebSocket debug logs include frames, which contain access tokens.
        logger = logging.getLogger('claude-swap.codex-auth-wire')
        logger.disabled = True
        sock = await asyncio.to_thread(connect_socket, self.listener)
        try:
            self.ws = await unix_connect(sock=sock, open_timeout=3, close_timeout=1,
                                         compression=None, max_size=2**20, logger=logger)
        except BaseException:
            sock.close()
            raise
        self.reader = asyncio.create_task(self._receive())
        await self.request('initialize', {
            'clientInfo': {'name': 'cswap-auth', 'version': '1'},
            'capabilities': {'experimentalApi': True},
        })
        await self.ws.send(json.dumps({'method': 'initialized'}))

    async def request(self, method: str, params: dict) -> dict:
        self.serial += 1
        key = f'cswap-{self.serial}'
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = future
        try:
            await self.ws.send(json.dumps({'id': key, 'method': method, 'params': params}))
            return await asyncio.wait_for(future, 8)
        finally:
            self.pending.pop(key, None)

    async def _receive(self) -> None:
        try:
            async for raw in self.ws:
                message = json.loads(raw)
                key = message.get('id')
                if message.get('method') == 'account/chatgptAuthTokens/refresh' and key is not None:
                    task = asyncio.create_task(self._refresh(message))
                    self.callbacks.add(task)
                    task.add_done_callback(self.callbacks.discard)
                elif not message.get('method') and key in self.pending:
                    future = self.pending[key]
                    if future.done():
                        continue
                    if 'error' in message:
                        # A remote error may contain credentials. Never echo it.
                        future.set_exception(ValueError('Codex rejected the auth request'))
                    else:
                        future.set_result(message.get('result', {}))
                # Other clients own approvals and turns. Leave their requests alone.
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError('Codex connection closed'))

    async def _refresh(self, message: dict) -> None:
        try:
            previous = self.credential
            account_id = message.get('params', {}).get('previousAccountId')
            if not previous or account_id and account_id != previous.account_id:
                raise ValueError('Refresh account does not match this connection')
            fresh = await asyncio.to_thread(self.source.read, previous)
            await self.ws.send(json.dumps({'id': message['id'], 'result': fresh.params()}))
            self.credential = fresh
        except Exception:
            with contextlib.suppress(Exception):
                await self.ws.send(json.dumps({'id': message['id'], 'error': {
                    'code': -32000, 'message': 'cswap could not refresh the selected account',
                }}))

    async def apply(self, credential: Credential, *, verify: bool = False) -> None:
        if not self.ws:
            await self.connect()
        if self.credential == credential and not verify:
            if self.reader.done():
                raise ConnectionError('Codex connection closed')
            return
        await self.request('account/login/start', {'type': 'chatgptAuthTokens', **credential.params()})
        self.credential = credential
        result = await self.request('account/read', {'refreshToken': False})
        account = result.get('account') or {}
        if account.get('type') != 'chatgpt' or account.get('email') != credential.email:
            self.credential = None
            raise ValueError('Codex did not confirm the selected account')

    async def close(self) -> None:
        if self.ws:
            await self.ws.close()
        if self.reader:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.reader
        for task in self.callbacks:
            task.cancel()
        if self.callbacks:
            await asyncio.gather(*self.callbacks, return_exceptions=True)
