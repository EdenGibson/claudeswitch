"""Real Codex, synthetic credentials, loopback Responses API. No paid calls.

Run explicitly: CSWAP_TEST_CODEX=1 uv run pytest tests/integration/test_codex_live.py -n 0
"""

import asyncio
import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from websockets.asyncio.client import unix_connect

from claude_swap.codex_live.credentials import CredentialSource, parse
from claude_swap.codex_live.discovery import Listener
from claude_swap.codex_live.rpc import AuthClient
from claude_swap.oauth import RefreshOutcome
from tests.providers.conftest import make_codex_auth

pytestmark = pytest.mark.skipif(
    os.environ.get('CSWAP_TEST_CODEX') != '1' or not shutil.which('codex'),
    reason='Opt-in real Codex integration test',
)


class Provider:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.accounts = []
        self.reject_once = False
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"models":[]}')

            def do_POST(self):
                self.rfile.read(int(self.headers.get('content-length', 0)))
                provider.accounts.append(self.headers.get('chatgpt-account-id'))
                if provider.reject_once:
                    provider.reject_once = False
                    self.send_response(401)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(b'{"error":{"message":"fixture expired token"}}')
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()

                def send(event):
                    self.wfile.write(('data: ' + json.dumps(event) + '\n\n').encode())
                    self.wfile.flush()

                send({'type': 'response.created', 'response': {'id': 'resp_fixture'}})
                provider.started.set()
                if not provider.release.wait(20):
                    return
                item = {'type': 'message', 'id': 'msg_fixture', 'role': 'assistant',
                        'status': 'completed', 'content': [{'type': 'output_text', 'text': 'ok'}]}
                send({'type': 'response.output_item.done', 'output_index': 0, 'item': item})
                send({'type': 'response.completed', 'response': {'id': 'resp_fixture',
                      'status': 'completed', 'output': [item],
                      'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}})

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


@pytest.fixture
def real_codex(temp_home, monkeypatch):
    provider = Provider()
    home = temp_home / '.codex'
    home.mkdir()
    monkeypatch.setenv('CODEX_HOME', str(home))
    for key in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'CODEX_ACCESS_TOKEN'):
        monkeypatch.delenv(key, raising=False)
    (home / 'auth.json').write_text(make_codex_auth(email='before@example.invalid', account_id='account-before'))
    (home / 'config.toml').write_text(f'''
model = "gpt-5.1-codex"
model_provider = "fixture"
approval_policy = "never"
sandbox_mode = "danger-full-access"
[features]
apps = false
plugins = false
remote_plugin = false
[model_providers.fixture]
name = "fixture"
base_url = "http://127.0.0.1:{provider.server.server_address[1]}/v1"
wire_api = "responses"
requires_openai_auth = true
supports_websockets = false
request_max_retries = 0
stream_max_retries = 0
''')
    # Unix paths must fit sockaddr_un even inside pytest's temporary tree.
    import tempfile
    with tempfile.TemporaryDirectory(prefix='cswap-socket-') as sockets:
        endpoint = Path(sockets) / 'codex.sock'
        log = (home / 'server.log').open('w')
        launcher = subprocess.Popen(['codex', 'app-server', '--listen', f'unix://{endpoint}'],
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        try:
            # The npm launcher is a parent process; peer verification wants the Rust PID.
            import time
            for _ in range(100):
                if endpoint.exists():
                    break
                time.sleep(.05)
            import socket, struct
            with socket.socket(socket.AF_UNIX) as sock:
                sock.connect(str(endpoint))
                pid = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[0]
            yield Listener(pid, str(endpoint)), home, provider
        finally:
            provider.release.set()
            if 'pid' in locals():
                import signal
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            launcher.terminate()
            launcher.wait(timeout=5)
            log.close()
            provider.close()


async def request(ws, method, params, key=1):
    await ws.send(json.dumps({'id': key, 'method': method, 'params': params}))
    while True:
        message = json.loads(await asyncio.wait_for(ws.recv(), 15))
        if message.get('id') == key:
            assert 'error' not in message, message
            return message['result']


async def completed(ws):
    while True:
        message = json.loads(await asyncio.wait_for(ws.recv(), 15))
        if message.get('method') == 'turn/completed':
            assert message['params']['turn']['status'] == 'completed', message
            return


@pytest.mark.asyncio
async def test_account_swap_preserves_active_response(real_codex):
    listener, home, provider = real_codex
    helper = AuthClient(listener, CredentialSource())
    try:
        async with unix_connect(listener.path, compression=None) as ws:
            await request(ws, 'initialize', {'clientInfo': {'name': 'turn-owner', 'version': '1'}})
            await ws.send('{"method":"initialized"}')
            thread = await request(ws, 'thread/start', {'cwd': str(home.parent)})
            thread_id = thread['thread']['id']
            await request(ws, 'turn/start', {'threadId': thread_id, 'input': [{'type': 'text', 'text': 'Say ok'}]})
            assert await asyncio.to_thread(provider.started.wait, 10)
            after = make_codex_auth(email='after@example.invalid', account_id='account-after')
            (home / 'auth.json').write_text(after)
            await helper.apply(parse(after))
            os.kill(listener.pid, 0)
            provider.release.set()
            await completed(ws)
            await request(ws, 'turn/start', {'threadId': thread_id, 'input': [{'type': 'text', 'text': 'Again'}]})
            await completed(ws)
            assert provider.accounts == ['account-before', 'account-after']
    finally:
        await helper.close()


@pytest.mark.skipif(os.environ.get('CSWAP_TEST_SYSTEMD') != '1', reason='Opt-in systemd user service test')
@pytest.mark.asyncio
async def test_supervised_helper_recovers_without_restarting_codex(real_codex):
    from claude_swap.codex_live import read_status, sync_live, unit_name
    import signal
    import time

    listener, home, provider = real_codex
    unit = unit_name()

    def helper_pid():
        result = subprocess.run(['systemctl', '--user', 'show', unit, '--property=MainPID', '--value'],
                                capture_output=True, text=True, check=True)
        return int(result.stdout.strip() or '0')

    try:
        report = await asyncio.to_thread(sync_live)
        assert report == {'updated': 1, 'failed': [], 'unsupported': [], 'warnings': []}
        first_pid = helper_pid()
        assert first_pid > 0
        async with unix_connect(listener.path, compression=None) as ws:
            await request(ws, 'initialize', {'clientInfo': {'name': 'turn-owner', 'version': '1'}})
            await ws.send('{"method":"initialized"}')
            thread = await request(ws, 'thread/start', {'cwd': str(home.parent)})
            thread_id = thread['thread']['id']
            await request(ws, 'turn/start', {'threadId': thread_id, 'input': [{'type': 'text', 'text': 'Say ok'}]})
            assert await asyncio.to_thread(provider.started.wait, 10)
            os.kill(first_pid, signal.SIGKILL)
            killed_at = time.time()
            for _ in range(80):
                status = read_status()
                pid = helper_pid()
                if pid not in (0, first_pid) and status.get('checkedAt', 0) > killed_at and status['servers'][0]['status'] == 'updated':
                    break
                await asyncio.sleep(.1)
            else:
                pytest.fail('systemd did not restore the auth helper')
            os.kill(listener.pid, 0)
            after = make_codex_auth(email='after@example.invalid', account_id='account-after')
            (home / 'auth.json').write_text(after)
            report = await asyncio.to_thread(sync_live)
            assert report['updated'] == 1 and not report['warnings']
            provider.release.set()
            await completed(ws)
            await request(ws, 'turn/start', {'threadId': thread_id, 'input': [{'type': 'text', 'text': 'Again'}]})
            await completed(ws)
            assert provider.accounts == ['account-before', 'account-after']
    finally:
        subprocess.run(['systemctl', '--user', 'stop', unit], capture_output=True, timeout=5)


@pytest.mark.asyncio
async def test_real_codex_requests_refresh_from_helper(real_codex, monkeypatch):
    listener, home, provider = real_codex
    source = CredentialSource()
    helper = AuthClient(listener, source)
    calls = []
    import time
    rotated = make_codex_auth(email='before@example.invalid', account_id='account-before',
                              refresh_token='rotated', now=time.time() + 1)

    def refresh(blob, **_kwargs):
        calls.append(blob)
        return RefreshOutcome(rotated, None)

    monkeypatch.setattr('claude_swap.providers.codex.try_refresh', refresh)
    provider.reject_once = True
    provider.release.set()
    try:
        async with unix_connect(listener.path, compression=None) as ws:
            await request(ws, 'initialize', {'clientInfo': {'name': 'turn-owner', 'version': '1'}})
            await ws.send('{"method":"initialized"}')
            await helper.apply(source.read())
            # A later observer must not steal refresh ownership from the helper.
            async with unix_connect(listener.path, compression=None) as observer:
                await request(observer, 'initialize', {'clientInfo': {'name': 'observer', 'version': '1'}})
                await observer.send('{"method":"initialized"}')
                thread = await request(ws, 'thread/start', {'cwd': str(home.parent)})
                await request(ws, 'turn/start', {'threadId': thread['thread']['id'],
                              'input': [{'type': 'text', 'text': 'Say ok'}]})
                await completed(ws)
            assert len(calls) == 1
            assert (home / 'auth.json').read_text() == rotated
            assert provider.accounts == ['account-before', 'account-before']
    finally:
        await helper.close()
