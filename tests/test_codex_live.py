"""Account ownership, refresh serialization, and truthful live-swap reports."""

import json
import os
import socket
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest

from claude_swap import codex_live, paths
from claude_swap.codex_live.credentials import CredentialSource, parse
from claude_swap.codex_live.discovery import Listener, connect_socket, discover
from claude_swap.codex_store import CodexAccountStore
from claude_swap.oauth import RefreshOutcome
from tests.providers.conftest import make_codex_auth


def test_access_token_is_not_in_credential_repr():
    credential = parse(make_codex_auth())
    assert credential.access_token not in repr(credential)


@pytest.mark.parametrize('blob', ['{}', 'null', '[]', 'broken', '{"tokens":null}'])
def test_invalid_auth_is_rejected(blob):
    with pytest.raises(ValueError):
        parse(blob)


def test_workspace_selection_uses_explicit_account_id():
    blob = json.loads(make_codex_auth(account_id='personal'))
    blob['tokens']['account_id'] = 'workspace'
    assert parse(json.dumps(blob)).account_id == 'workspace'


def test_concurrent_refreshes_reuse_one_rotated_token(temp_home, monkeypatch):
    store = CodexAccountStore()
    old = make_codex_auth(access_expires_in=-1)
    fresh = make_codex_auth(refresh_token='rotated')
    store.write_live(old)
    refresh = Mock(return_value=RefreshOutcome(fresh, None))
    monkeypatch.setattr('claude_swap.providers.codex.try_refresh', refresh)
    source = CredentialSource()
    with ThreadPoolExecutor(4) as executor:
        results = list(executor.map(source.read, [parse(old)] * 4))
    assert results == [parse(fresh)] * 4
    assert refresh.call_count == 1
    assert store.read_live() == fresh


def test_refresh_preserves_rotation_in_own_slot(temp_home, monkeypatch):
    store = CodexAccountStore()
    old = make_codex_auth(access_expires_in=-1)
    fresh = make_codex_auth(refresh_token='rotated')
    store.write_live(old)
    root = paths.get_backup_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / 'sequence.json').write_text(json.dumps({'accounts': {
        '9': {'provider': 'codex', 'uuid': 'acc-0001', 'email': 'user@example.com'},
    }}))
    monkeypatch.setattr('claude_swap.providers.codex.try_refresh', lambda *_a, **_kw: RefreshOutcome(fresh, None))
    CredentialSource().read()
    assert store.read_credential('9', 'user@example.com') == fresh


def test_callback_cannot_restore_previous_account(temp_home, monkeypatch):
    store = CodexAccountStore()
    previous = parse(make_codex_auth(account_id='old'))
    selected = make_codex_auth(account_id='selected')
    store.write_live(selected)
    refresh = Mock()
    monkeypatch.setattr('claude_swap.providers.codex.try_refresh', refresh)
    with pytest.raises(ValueError, match='Account changed'):
        CredentialSource().read(previous)
    refresh.assert_not_called()
    assert store.read_live() == selected


def test_failed_refresh_has_backoff_and_keeps_login(temp_home, monkeypatch):
    store = CodexAccountStore()
    old = make_codex_auth(access_expires_in=-1)
    store.write_live(old)
    refresh = Mock(return_value=RefreshOutcome(None, 'invalid_grant'))
    monkeypatch.setattr('claude_swap.providers.codex.try_refresh', refresh)
    source = CredentialSource()
    with pytest.raises(ValueError, match='refresh failed'):
        source.read()
    with pytest.raises(ValueError, match='cooldown'):
        source.read()
    assert refresh.call_count == 1
    assert store.read_live() == old


def test_external_login_during_refresh_is_not_overwritten(temp_home, monkeypatch):
    store = CodexAccountStore()
    store.write_live(make_codex_auth(access_expires_in=-1))
    selected = make_codex_auth(account_id='new-account')

    def refresh(*_a, **_kw):
        store.write_live(selected)
        return RefreshOutcome(make_codex_auth(refresh_token='rotated'), None)

    monkeypatch.setattr('claude_swap.providers.codex.try_refresh', refresh)
    with pytest.raises(ValueError, match='Login changed'):
        CredentialSource().read()
    assert store.read_live() == selected


@pytest.mark.skipif(not hasattr(socket, 'SO_PEERCRED'), reason='Linux peer credentials')
def test_socket_peer_pid_must_match(tmp_path):
    with socket.socket(socket.AF_UNIX) as server:
        path = str(tmp_path / 'peer.sock')
        server.bind(path)
        server.listen()
        with pytest.raises(OSError, match='owner changed'):
            connect_socket(Listener(os.getpid() + 1, path))


@pytest.mark.skipif(not hasattr(socket, 'SO_PEERCRED'), reason='Linux discovery')
def test_discovery_excludes_other_homes_overrides_and_remote_clients(temp_home, tmp_path):
    proc = tmp_path / 'proc'
    proc.mkdir()
    home = temp_home / '.codex'
    with socket.socket(socket.AF_UNIX) as server:
        endpoint = str(tmp_path / 'a.sock')
        server.bind(endpoint)

        def process(pid, args, env):
            folder = proc / str(pid)
            folder.mkdir()
            (folder / 'exe').symlink_to(tmp_path / 'bin' / 'codex')
            (folder / 'cmdline').write_bytes(('codex\0' + '\0'.join(args) + '\0').encode())
            (folder / 'environ').write_bytes(('HOME=' + str(temp_home) + '\0' +
                '\0'.join(k + '=' + v for k, v in env.items())).encode())

        args = ['app-server', '--listen', 'unix://' + endpoint]
        process(1, args, {})
        process(2, args, {'CODEX_HOME': str(tmp_path / 'private')})
        process(3, args, {'OPENAI_API_KEY': 'private'})
        process(4, ['--remote', 'unix://' + endpoint], {})
        process(5, ['app-server', '--listen', 'stdio://'], {})
        process(6, [], {})
        rows, unsupported = discover(home, proc)
        assert rows == [Listener(1, endpoint)]
        assert unsupported == [5, 6]


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux live sync')
def test_sync_reports_partial_failure(temp_home, monkeypatch):
    store = CodexAccountStore()
    store.write_live(make_codex_auth())
    monkeypatch.setattr(codex_live, 'discover', lambda _home: ([Listener(1, '/a'), Listener(2, '/b')], [3]))
    monkeypatch.setattr(codex_live, '_start_service', lambda: None)

    def status():
        request = json.loads((codex_live.state_dir() / 'request.json').read_text())
        return {'requestId': request['id'], 'accountId': 'acc-0001', 'servers': [
            {'pid': 1, 'status': 'updated'}, {'pid': 2, 'status': 'failed'},
        ]}

    monkeypatch.setattr(codex_live, 'read_status', status)
    report = codex_live.sync_live()
    assert report['updated'] == 1
    assert report['failed'] == [{'pid': 2, 'status': 'failed'}]
    assert report['unsupported'] == [3]
    assert len(report['warnings']) == 2


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux live sync')
def test_stale_report_cannot_confirm_a_switch(temp_home, monkeypatch):
    CodexAccountStore().write_live(make_codex_auth())
    monkeypatch.setattr(codex_live, 'discover', lambda _home: ([Listener(1, '/a')], []))
    monkeypatch.setattr(codex_live, '_start_service', lambda: None)
    monkeypatch.setattr(codex_live, 'read_status', lambda: {
        'requestId': 'old', 'accountId': 'acc-0001', 'servers': [{'pid': 1, 'status': 'updated'}],
    })
    report = codex_live.sync_live(timeout=.01)
    assert report['updated'] == 0
    assert 'timeout' in report['warnings'][0]


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux live sync')
def test_service_failure_does_not_claim_live_success(temp_home, monkeypatch):
    CodexAccountStore().write_live(make_codex_auth())
    monkeypatch.setattr(codex_live, 'discover', lambda _home: ([Listener(1, '/a')], []))

    def fail():
        raise RuntimeError('No systemd user session')

    monkeypatch.setattr(codex_live, '_start_service', fail)
    report = codex_live.sync_live()
    assert report['updated'] == 0
    assert report['warnings'] == ['No systemd user session']


def test_no_listeners_does_not_start_helper(temp_home, monkeypatch):
    monkeypatch.setattr(codex_live, 'discover', lambda _home: ([], []))
    start = Mock()
    monkeypatch.setattr(codex_live, '_start_service', start)
    assert codex_live.sync_live()['updated'] == 0
    start.assert_not_called()
