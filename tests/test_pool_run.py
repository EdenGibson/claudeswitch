"""`cswap run` on a Codex slot launches codex, not claude."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import codex_session, paths
from claude_swap.codex_store import CodexAccountStore
from claude_swap.exceptions import SessionError
from claude_swap.session import SessionManager
from claude_swap.switcher import ClaudeAccountSwitcher
from tests.providers.conftest import make_codex_auth

CODEX_EMAIL = "codex@example.com"
CODEX_ID = "acc-codex"


@pytest.fixture
def pool(temp_home: Path) -> ClaudeAccountSwitcher:
    root = paths.get_backup_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "sequence.json").write_text(
        json.dumps(
            {
                "activeAccountNumber": 1,
                "activeProviderAccounts": {"codex": "2"},
                "sequence": [1, 2],
                "accounts": {
                    "1": {"email": "claude@example.com", "uuid": "u-1",
                          "organizationUuid": "", "organizationName": ""},
                    "2": {"email": CODEX_EMAIL, "uuid": CODEX_ID,
                          "organizationUuid": "", "organizationName": "",
                          "provider": "codex"},
                },
            }
        ),
        encoding="utf-8",
    )
    CodexAccountStore().write_credential(
        "2", CODEX_EMAIL, make_codex_auth(email=CODEX_EMAIL, account_id=CODEX_ID)
    )
    return ClaudeAccountSwitcher()


class _Completed:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode


def test_run_launches_codex_with_a_private_home(pool: ClaudeAccountSwitcher):
    seen: dict = {}

    def fake_run(argv, env=None):
        seen["argv"] = argv
        seen["env"] = env
        return _Completed()

    with patch("shutil.which", return_value="/usr/bin/codex"), patch(
        "subprocess.run", side_effect=fake_run
    ):
        rc = codex_session.run("2", CODEX_EMAIL, ["--help"])

    assert rc == 0
    assert seen["argv"] == ["/usr/bin/codex", "--help"]
    assert seen["env"]["CODEX_HOME"] == str(
        codex_session.session_dir("2", CODEX_EMAIL)
    )


def test_the_session_home_holds_the_stored_credential(pool: ClaudeAccountSwitcher):
    with patch("shutil.which", return_value="/usr/bin/codex"), patch(
        "subprocess.run", return_value=_Completed()
    ):
        codex_session.run("2", CODEX_EMAIL, [])

    auth = codex_session.session_dir("2", CODEX_EMAIL) / "auth.json"
    assert json.loads(auth.read_text())["tokens"]["account_id"] == CODEX_ID


def test_an_api_key_in_the_environment_is_scrubbed(
    pool: ClaudeAccountSwitcher, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-reach-codex")
    seen: dict = {}

    with patch("shutil.which", return_value="/usr/bin/codex"), patch(
        "subprocess.run", side_effect=lambda argv, env=None: seen.update(env=env)
        or _Completed()
    ):
        codex_session.run("2", CODEX_EMAIL, [])

    assert "OPENAI_API_KEY" not in seen["env"]


def test_a_rotated_token_is_copied_back_on_exit(pool: ClaudeAccountSwitcher):
    """OpenAI refresh tokens are single use, so the session copy must win."""
    rotated = make_codex_auth(
        email=CODEX_EMAIL, account_id=CODEX_ID, refresh_token="ROTATED"
    )

    def rotate(argv, env=None):
        (Path(env["CODEX_HOME"]) / "auth.json").write_text(rotated, encoding="utf-8")
        return _Completed()

    with patch("shutil.which", return_value="/usr/bin/codex"), patch(
        "subprocess.run", side_effect=rotate
    ):
        codex_session.run("2", CODEX_EMAIL, [])

    stored = CodexAccountStore().read_credential("2", CODEX_EMAIL)
    assert json.loads(stored)["tokens"]["refresh_token"] == "ROTATED"


def test_a_foreign_login_inside_the_session_is_not_copied_back(
    pool: ClaudeAccountSwitcher,
):
    """A `codex login` for another account must not overwrite this slot."""
    before = CodexAccountStore().read_credential("2", CODEX_EMAIL)
    stranger = make_codex_auth(email="other@example.com", account_id="acc-other")

    def relogin(argv, env=None):
        (Path(env["CODEX_HOME"]) / "auth.json").write_text(stranger, encoding="utf-8")
        return _Completed()

    with patch("shutil.which", return_value="/usr/bin/codex"), patch(
        "subprocess.run", side_effect=relogin
    ):
        codex_session.run("2", CODEX_EMAIL, [])

    assert CodexAccountStore().read_credential("2", CODEX_EMAIL) == before


def test_ctrl_c_still_copies_the_credential_back(pool: ClaudeAccountSwitcher):
    rotated = make_codex_auth(
        email=CODEX_EMAIL, account_id=CODEX_ID, refresh_token="ROTATED"
    )

    def interrupt(argv, env=None):
        (Path(env["CODEX_HOME"]) / "auth.json").write_text(rotated, encoding="utf-8")
        raise KeyboardInterrupt

    with patch("shutil.which", return_value="/usr/bin/codex"), patch(
        "subprocess.run", side_effect=interrupt
    ):
        rc = codex_session.run("2", CODEX_EMAIL, [])

    assert rc == 130
    stored = CodexAccountStore().read_credential("2", CODEX_EMAIL)
    assert json.loads(stored)["tokens"]["refresh_token"] == "ROTATED"


def test_a_missing_codex_binary_is_a_clear_error(pool: ClaudeAccountSwitcher):
    with patch("shutil.which", return_value=None):
        with pytest.raises(SessionError, match="not found on PATH"):
            codex_session.run("2", CODEX_EMAIL, [])


def test_a_slot_with_no_stored_credential_errors(pool: ClaudeAccountSwitcher):
    CodexAccountStore().delete_credential("2", CODEX_EMAIL)
    with patch("shutil.which", return_value="/usr/bin/codex"):
        with pytest.raises(SessionError, match="no stored Codex credential"):
            codex_session.run("2", CODEX_EMAIL, [])


def test_the_user_config_seeds_a_fresh_session(pool: ClaudeAccountSwitcher):
    config = paths.get_codex_auth_path().parent
    config.mkdir(parents=True, exist_ok=True)
    (config / "config.toml").write_text('model = "gpt-5"\n', encoding="utf-8")

    with patch("shutil.which", return_value="/usr/bin/codex"), patch(
        "subprocess.run", return_value=_Completed()
    ):
        codex_session.run("2", CODEX_EMAIL, [])

    seeded = codex_session.session_dir("2", CODEX_EMAIL) / "config.toml"
    assert seeded.read_text() == 'model = "gpt-5"\n'


def test_session_manager_routes_a_codex_slot_to_codex(
    pool: ClaudeAccountSwitcher,
):
    manager = SessionManager(pool)
    with patch("shutil.which", return_value="/usr/bin/codex"), patch(
        "subprocess.run", return_value=_Completed(3)
    ):
        with pytest.raises(SystemExit) as exit_info:
            manager.run("2", [])

    assert exit_info.value.code == 3


def test_session_manager_rejects_the_claude_sharing_flags(
    pool: ClaudeAccountSwitcher,
):
    manager = SessionManager(pool)
    with patch("shutil.which", return_value="/usr/bin/codex"):
        with pytest.raises(SessionError, match="Claude session"):
            manager.run("2", [], share_history=True)


def test_a_codex_slot_runs_without_claude_on_path(pool: ClaudeAccountSwitcher):
    """Only the codex binary is needed to run a codex account."""

    def which(name):
        return "/usr/bin/codex" if name == "codex" else None

    manager = SessionManager(pool)
    with patch("shutil.which", side_effect=which), patch(
        "subprocess.run", return_value=_Completed()
    ):
        with pytest.raises(SystemExit) as exit_info:
            manager.run("2", [])

    assert exit_info.value.code == 0
