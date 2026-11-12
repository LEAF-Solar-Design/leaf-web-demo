"""Keychain command transport and read-back verification without macOS."""
import shlex
import subprocess
import traceback
from types import SimpleNamespace

import pytest

from leaf_agent.packages.homeassistant import HAError, HomeAssistant, LABEL


class KeychainRunner:
    def __init__(self, *, write_code=0, truncate=False, read_code=0, failure=None):
        self.calls = []
        self.values = {}
        self.write_code = write_code
        self.truncate = truncate
        self.read_code = read_code
        self.failure = failure

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        assert kwargs["check"] is False
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["timeout"] == 15
        if argv == ["/usr/bin/security", "-i"]:
            assert kwargs["start_new_session"] is True
            command, quit_command, empty = kwargs["input"].split("\n")
            assert quit_command == "quit" and empty == ""
            args = shlex.split(command)
            assert args[:4] == ["add-generic-password", "-U", "-s", LABEL]
            assert args[4] == "-a" and args[6] == "-w" and len(args) == 8
            if self.failure == "write":
                raise subprocess.TimeoutExpired(argv, 15, output=kwargs["input"])
            self.values[args[5]] = args[7][:128] if self.truncate else args[7]
            return SimpleNamespace(returncode=self.write_code, stdout="", stderr="")
        assert argv == ["/usr/bin/security", "find-generic-password", "-s", LABEL,
                        "-a", "token", "-w"]
        if self.failure == "read":
            raise RuntimeError(self.values["token"])
        return SimpleNamespace(returncode=self.read_code,
                               stdout=self.values.get("token", "") + "\n", stderr="")


@pytest.mark.parametrize("write_code", [0, 1])
def test_long_keychain_value_round_trips_on_stdin(tmp_path, write_code):
    value = "long-lived-token-" + "x" * 166
    assert len(value) == 183
    runner = KeychainRunner(write_code=write_code)
    handler = HomeAssistant(tmp_path, platform="darwin", runner=runner)
    handler._save_credential("token", value)
    assert len(runner.calls) == 2
    assert runner.calls[0][1]["input"] == (
        f'add-generic-password -U -s "{LABEL}" -a "token" -w "{value}"\nquit\n'
    )
    assert all(value not in " ".join(argv) for argv, kwargs in runner.calls)
    assert handler._credential("token") == value


@pytest.mark.parametrize("options", [
    {"truncate": True}, {"read_code": 1}, {"read_code": 44},
    {"failure": "write"}, {"failure": "read"},
])
def test_failed_keychain_save_has_credential_free_error(tmp_path, caplog, options):
    value = "long-lived-token-" + "x" * 166
    runner = KeychainRunner(**options)
    handler = HomeAssistant(tmp_path, platform="darwin", runner=runner)
    with pytest.raises(HAError, match="^Credential write failed$") as raised:
        handler._save_credential("token", value)
    assert value not in str(raised.value) + caplog.text
    assert value not in "".join(traceback.format_exception(raised.value))


@pytest.mark.parametrize("value", ["", 'secret"value', "secret\\value", "secret\nvalue",
                                   "secret\rvalue", "secret\x00value"])
def test_invalid_keychain_value_is_rejected_before_runner(tmp_path, caplog, value):
    runner = KeychainRunner()
    handler = HomeAssistant(tmp_path, platform="darwin", runner=runner)
    with pytest.raises(HAError, match="^Invalid credential value$") as raised:
        handler._save_credential("token", value)
    assert runner.calls == []
    if value:
        assert value not in str(raised.value) + caplog.text
