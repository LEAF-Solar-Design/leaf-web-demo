"""The operator CLI exercises the same verifier as staging, with an in-memory SSM."""
from datetime import datetime, time, timedelta, timezone
import importlib.util
import os
from pathlib import Path
import re
import stat
import subprocess
from unittest.mock import Mock

import pytest


spec = importlib.util.spec_from_file_location("investor_demo_link", Path(__file__).with_name("investor_demo_link.py"))
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class FakeSSM:
    class exceptions:
        class ParameterAlreadyExists(Exception):
            pass

    def __init__(self):
        self.values = {}

    def put_parameter(self, *, Name, Value, Type, Overwrite):
        assert Type == "SecureString"
        version = self.values.get(Name, (0, None))[0]
        if version and not Overwrite:
            raise self.exceptions.ParameterAlreadyExists()
        self.values[Name] = (version + 1, Value)
        return {"Version": version + 1}

    def get_parameter(self, *, Name, WithDecryption):
        assert WithDecryption is True
        return {"Parameter": {"Value": self.values[Name][1], "Type": "SecureString"}}


@pytest.fixture
def ssm():
    client = FakeSSM()
    assert cli.main(["put-secret"], client=client) == 0
    return client


def day_after(days):
    return datetime.now(timezone.utc).date() + timedelta(days=days)


def test_put_is_create_only_until_rotated(capsys):
    ssm = FakeSSM()
    assert cli.main(["put-secret"], client=ssm) == 0
    original = ssm.values[cli.PARAMETER]
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", original[1])
    with cli._configured(original[1]):
        assert cli.demo.enabled()
        assert capsys.readouterr().out.split() == [cli.PARAMETER, "1", cli.demo.key_id()]
    assert cli.main(["put-secret"], client=ssm) == 1
    assert ssm.values[cli.PARAMETER] == original
    assert cli.main(["put-secret", "--rotate"], client=ssm) == 0
    version, rotated = ssm.values[cli.PARAMETER]
    assert version == 2 and rotated != original[1]
    out, err = capsys.readouterr()
    assert original[1] not in out + err and rotated not in out + err


def test_mint_verify_and_revoke(ssm, tmp_path, capsys):
    day = day_after(2)
    path = tmp_path / "link.txt"
    mint = ["mint", "--expires", day.isoformat(), "--out", str(path)]
    assert cli.main(mint, client=ssm) == 0
    raw = path.read_bytes()
    assert raw.startswith(cli.LINK_PREFIX.encode()) and raw.endswith(b"\n")
    token = raw[len(cli.LINK_PREFIX):-1].decode("ascii")
    expected = datetime.combine(day, time(23, 59, 59), tzinfo=timezone.utc)
    secret = ssm.values[cli.PARAMETER][1]
    with cli._configured(secret):
        claims = cli.demo.verify_link_token(token, now=int(datetime.now(timezone.utc).timestamp()))
        assert claims.expires_at == int(expected.timestamp())
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
    assert cli.main(mint, client=ssm) == 1
    assert path.read_bytes() == raw
    check = ["check", "--file", str(path)]
    assert cli.main(check, client=ssm) == 0
    assert cli.main(["put-secret", "--rotate"], client=ssm) == 0
    assert cli.main(check, client=ssm) == 1
    out, err = capsys.readouterr()
    assert f"valid until {expected.isoformat()}" in out.splitlines()
    assert out.splitlines()[-1] == "invalid"
    for sensitive in (token, secret, ssm.values[cli.PARAMETER][1]):
        assert sensitive not in out + err


def test_mint_refuses_existing_output(ssm, tmp_path, capsys):
    path = tmp_path / "existing.txt"
    path.write_bytes(b"keep this file")
    assert cli.main(["mint", "--expires", day_after(1).isoformat(), "--out", str(path)], client=ssm) == 1
    assert capsys.readouterr().err.splitlines() == ["Output file exists. Move it away first."]
    assert path.read_bytes() == b"keep this file"


@pytest.mark.parametrize("offset", [-1, 32])
def test_refuse_invalid_expiry(ssm, tmp_path, offset):
    path = tmp_path / "link.txt"
    assert cli.main(["mint", "--expires", day_after(offset).isoformat(), "--out", str(path)], client=ssm) == 1
    assert not path.exists()


def test_refuse_git_worktree(ssm, tmp_path):
    (tmp_path / ".git").write_text("gitdir: /private/worktrees/staging\n")
    nested = tmp_path / "nested"
    nested.mkdir()
    path = nested / "link.txt"
    assert cli.main(["mint", "--expires", day_after(1).isoformat(), "--out", str(path)], client=ssm) == 1
    assert not path.exists()


def test_check_distinguishes_unavailable_ssm_from_invalid_link(tmp_path, capsys):
    assert cli.main(["check", "--file", str(tmp_path / "link.txt")], client=FakeSSM()) == 2
    assert capsys.readouterr() == ("cannot check: secret unavailable\n", "")


@pytest.mark.parametrize("windows", [True, False])
def test_write_restricts_empty_file_only_on_windows(tmp_path, monkeypatch, windows):
    path = tmp_path / "link.txt"
    fdopen = os.fdopen
    events = []

    def observe_stream(*args, **kwargs):
        stream = fdopen(*args, **kwargs)
        events.append("create")
        write = stream.write

        def record_write(value):
            events.append("write")
            return write(value)

        stream.write = record_write
        return stream

    def restrict(argv, **kwargs):
        assert path.is_file() and path.stat().st_size == 0
        events.append("restrict")

    runner = Mock(side_effect=restrict)
    monkeypatch.setattr(cli, "_WINDOWS", windows)
    monkeypatch.setattr(cli, "_windows_account", lambda: r"DOMAIN\visitor")
    monkeypatch.setattr(cli.os, "fdopen", observe_stream)
    monkeypatch.setattr(cli.subprocess, "run", runner)
    cli._write_link(path, "sample")
    if windows:
        runner.assert_called_once_with(["icacls", str(path), "/inheritance:r", "/grant:r", r"DOMAIN\visitor:F"],
                                       capture_output=True, timeout=15, check=True)
        assert events == ["create", "restrict", "write"]
    else:
        runner.assert_not_called()
        assert events == ["create", "write"]
    assert path.read_text() == cli.LINK_PREFIX + "sample\n"


def test_failed_acl_removes_empty_file(tmp_path, monkeypatch):
    path = tmp_path / "link.txt"

    def refuse(argv, **kwargs):
        assert path.is_file() and path.stat().st_size == 0
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(cli, "_WINDOWS", True)
    monkeypatch.setattr(cli, "_windows_account", lambda: r"DOMAIN\visitor")
    monkeypatch.setattr(cli.subprocess, "run", refuse)
    with pytest.raises(cli.Refused, match=r"^Could not restrict the link file to the current user\.$"):
        cli._write_link(path, "sample")
    assert not path.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows file ACLs require Windows")
def test_minted_file_acl_has_only_current_account(ssm, tmp_path):
    path = tmp_path / "link.txt"
    assert cli.main(["mint", "--expires", day_after(1).isoformat(), "--out", str(path)], client=ssm) == 0
    listing = subprocess.run(["icacls", str(path)], capture_output=True, check=True, timeout=15,
                             text=True, encoding="oem").stdout
    acl = listing.split("\n\n", 1)[0].rstrip()
    assert acl.casefold() == f"{path} {cli._windows_account()}:(F)".casefold()
