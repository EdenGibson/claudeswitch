"""Apply the selected Codex account to existing local app servers."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from claude_swap import paths
from claude_swap.codex_store import CodexAccountStore
from claude_swap.settings import atomic_write_json

from .credentials import parse
from .discovery import discover


def state_dir() -> Path:
    key = hashlib.sha256(str(paths.get_codex_home().resolve()).encode()).hexdigest()[:16]
    return paths.get_provider_root('codex') / 'live' / key


def unit_name() -> str:
    return f'cswap-codex-live-{state_dir().name}.service'


def read_status() -> dict:
    try:
        data = json.loads((state_dir() / 'status.json').read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _start_service() -> None:
    if not shutil.which('systemd-run'):
        raise RuntimeError('Live Codex swaps require a systemd user session')
    name = unit_name()
    active = subprocess.run(['systemctl', '--user', 'is-active', name],
                            capture_output=True, text=True, timeout=5)
    if active.stdout.strip() in ('active', 'activating'):
        return
    args = ['systemd-run', '--user', '--quiet', '--collect', f'--unit={name}',
            '--property=Restart=on-failure', '--property=RestartSec=1',
            f'--setenv=CODEX_HOME={paths.get_codex_home().resolve()}',
            f'--setenv=HOME={Path.home()}',
            f'--setenv=XDG_DATA_HOME={paths.get_backup_root().parent}']
    if 'PYTHONPATH' in os.environ:
        args.append(f'--setenv=PYTHONPATH={os.environ["PYTHONPATH"]}')
    args.extend([sys.executable, '-m', 'claude_swap.codex_live'])
    result = subprocess.run(args, capture_output=True, text=True, timeout=5)
    if result.returncode:
        # Another switch may have started the same unit concurrently.
        active = subprocess.run(['systemctl', '--user', 'is-active', name],
                                capture_output=True, text=True, timeout=5)
        if active.stdout.strip() not in ('active', 'activating'):
            raise RuntimeError('Could not start the Codex auth helper; check the systemd user session')


def sync_live(*, timeout: float = 12) -> dict:
    """Start supervised refresh support, then wait for per-server acknowledgments.

    The file switch already succeeded. Live failures are reported separately;
    undoing the file switch would strand servers that accepted the new account.
    """
    report = {'updated': 0, 'failed': [], 'unsupported': [], 'warnings': []}
    if not sys.platform.startswith('linux'):
        report['warnings'].append('Live Codex swaps currently support Linux Unix listeners only')
        return report
    listeners, unsupported = discover(paths.get_codex_home())
    report['unsupported'] = unsupported
    if unsupported:
        report['warnings'].append(f'{len(unsupported)} Codex processes have no supported auth socket; their login is unchanged')
    if not listeners:
        return report
    try:
        credential = parse(CodexAccountStore().read_live())
        request_id = uuid.uuid4().hex
        atomic_write_json(state_dir() / 'request.json', {'id': request_id})
        _start_service()
        deadline = time.monotonic() + timeout
        expected = {listener.pid for listener in listeners}
        while time.monotonic() < deadline:
            status = read_status()
            if status.get('requestId') == request_id and (
                status.get('accountId') == credential.account_id or status.get('error')
            ):
                rows = {row['pid']: row for row in status.get('servers', [])}
                if expected.issubset(rows):
                    report['updated'] = sum(rows[pid]['status'] == 'updated' for pid in expected)
                    report['failed'] = [rows[pid] for pid in sorted(expected) if rows[pid]['status'] != 'updated']
                    if report['failed']:
                        report['warnings'].append(f'{len(report["failed"])} Codex servers did not confirm the account; inspect cswap codex live status')
                    return report
            time.sleep(0.1)
        raise RuntimeError('Live Codex swap was not confirmed before timeout; inspect cswap codex live status')
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        report['warnings'].append(str(exc))
        return report


def command(argv: list[str]) -> None:
    """Small diagnostic command; ordinary switches call sync_live directly."""
    if argv == ['status']:
        status = read_status()
        if status:
            status['ageSeconds'] = round(time.time() - status.get('checkedAt', 0), 1)
        print(json.dumps(status, indent=2))
    elif argv == ['sync']:
        report = sync_live()
        print(json.dumps(report, indent=2))
        if report['warnings']:
            raise SystemExit(1)
    else:
        print('usage: cswap codex live {status|sync}', file=sys.stderr)
        raise SystemExit(2)
