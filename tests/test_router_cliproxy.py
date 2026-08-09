"""Turning a cswap Codex credential into a CLIProxyAPI one, and back."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from claude_swap.router import cliproxy, unit
from tests.providers.conftest import make_codex_auth


def test_the_record_matches_cliproxys_struct(temp_home: Path):
    record = cliproxy.to_storage(
        make_codex_auth(email="a@example.com", account_id="acc-a")
    )

    assert record["type"] == "codex"
    assert record["email"] == "a@example.com"
    assert record["account_id"] == "acc-a"
    assert set(record) == {
        "id_token",
        "access_token",
        "refresh_token",
        "account_id",
        "last_refresh",
        "email",
        "type",
        "expired",
    }


def test_the_expiry_is_rfc3339(temp_home: Path):
    record = cliproxy.to_storage(make_codex_auth(email="a@example.com"))
    assert record["expired"].endswith("Z")


def test_an_unreadable_blob_gives_nothing(temp_home: Path):
    assert cliproxy.to_storage("not json") is None
    assert cliproxy.to_storage(json.dumps({"tokens": "wrong"})) is None


def test_the_filename_carries_the_email_and_plan(temp_home: Path):
    name = cliproxy.credential_name("acc-a", "a@example.com", "Plus Team")
    assert name.startswith("codex-")
    assert "a@example.com" in name
    assert name.endswith("-plus-team.json")


def test_only_one_credential_survives_a_write(temp_home: Path):
    cliproxy.auth_dir().mkdir(parents=True, exist_ok=True)
    (cliproxy.auth_dir() / "stale.json").write_text("{}", encoding="utf-8")

    written = cliproxy.write_credential(
        make_codex_auth(email="a@example.com", account_id="acc-a")
    )

    assert written is not None
    assert [p.name for p in cliproxy.auth_dir().glob("*.json")] == [written.name]


def test_a_second_account_replaces_the_first(temp_home: Path):
    cliproxy.write_credential(make_codex_auth(email="a@example.com", account_id="acc-a"))
    cliproxy.write_credential(make_codex_auth(email="b@example.com", account_id="acc-b"))

    assert cliproxy.credential_email() == "b@example.com"
    assert len(list(cliproxy.auth_dir().glob("*.json"))) == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_the_credential_is_private(temp_home: Path):
    written = cliproxy.write_credential(make_codex_auth(email="a@example.com"))
    assert oct(written.stat().st_mode)[-3:] == "600"


def test_an_unreadable_blob_writes_nothing(temp_home: Path):
    assert cliproxy.write_credential("not json") is None
    assert not cliproxy.auth_dir().exists() or not list(
        cliproxy.auth_dir().glob("*.json")
    )


class TestApiKey:
    def test_it_is_generated_once(self, temp_home: Path):
        first = cliproxy.api_key()
        assert first.startswith("cswap-")
        assert cliproxy.api_key() == first

    def test_it_is_not_created_when_only_read(self, temp_home: Path):
        assert cliproxy.api_key(create=False) == ""
        assert not cliproxy.key_path().exists()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
    def test_it_is_private(self, temp_home: Path):
        cliproxy.api_key()
        assert oct(cliproxy.key_path().stat().st_mode)[-3:] == "600"


class TestConfig:
    def test_it_binds_loopback_only(self, temp_home: Path):
        text = cliproxy.config_text()
        assert 'host: "127.0.0.1"' in text
        assert 'host: ""' not in text

    def test_it_points_at_cswaps_own_auth_dir(self, temp_home: Path):
        assert str(cliproxy.auth_dir()) in cliproxy.config_text()

    def test_it_carries_the_api_key(self, temp_home: Path):
        key = cliproxy.api_key()
        assert key in cliproxy.config_text()

    def test_writing_it_leaves_a_file(self, temp_home: Path):
        path = cliproxy.write_config()
        assert path.exists()
        assert "port: 8317" in path.read_text(encoding="utf-8")


class TestMergeBack:
    def test_a_rotated_token_comes_back(self, temp_home: Path):
        original = make_codex_auth(
            email="a@example.com", account_id="acc-a", refresh_token="old"
        )
        cliproxy.write_credential(original)
        record = cliproxy.read_credential()
        path = next(cliproxy.auth_dir().glob("*.json"))
        record["refresh_token"] = "rotated"
        path.write_text(json.dumps(record), encoding="utf-8")

        merged = cliproxy.merge_back(original)

        assert merged is not None
        assert json.loads(merged)["tokens"]["refresh_token"] == "rotated"

    def test_an_unchanged_token_writes_nothing_back(self, temp_home: Path):
        original = make_codex_auth(email="a@example.com", account_id="acc-a")
        cliproxy.write_credential(original)
        assert cliproxy.merge_back(original) is None

    def test_another_account_is_ignored(self, temp_home: Path):
        cliproxy.write_credential(
            make_codex_auth(
                email="b@example.com", account_id="acc-b", refresh_token="theirs"
            )
        )
        mine = make_codex_auth(
            email="a@example.com", account_id="acc-a", refresh_token="mine"
        )
        assert cliproxy.merge_back(mine) is None

    def test_an_empty_directory_gives_nothing(self, temp_home: Path):
        assert cliproxy.merge_back(make_codex_auth(email="a@example.com")) is None


class TestTheUnitStartsOnDemandOnly:
    def test_it_has_no_install_section(self):
        text = cliproxy.unit_text("/usr/bin/cli-proxy-api", Path("/tmp/c.yaml"))

        assert "[Install]" not in text
        assert "WantedBy" not in text

    def test_install_disables_an_older_enabled_unit(self, temp_home, monkeypatch):
        calls: list[tuple[str, ...]] = []
        monkeypatch.setattr(cliproxy, "find_binary", lambda: "/usr/bin/cli-proxy-api")
        monkeypatch.setattr(unit, "have_systemd", lambda: True)
        monkeypatch.setattr(
            unit, "_systemctl", lambda *args: calls.append(args) or None
        )

        assert cliproxy.install_unit() is True
        assert ("disable", cliproxy.UNIT_NAME) in calls
        assert not any(call[0] == "enable" for call in calls)
