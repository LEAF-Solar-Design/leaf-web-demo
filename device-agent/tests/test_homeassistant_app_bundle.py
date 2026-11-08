"""macOS app lifecycle, using only local files and fake process operations."""
import os
from pathlib import Path
import plistlib
import re
import signal
from types import SimpleNamespace
import tomllib

import pytest

from leaf_agent import __version__
from leaf_agent.config import write_json
from leaf_agent.ops import Dispatcher
from leaf_agent.packages import Registry, homeassistant
from leaf_agent.packages.homeassistant import APP_IDENTIFIER, HAError, HomeAssistant, LABEL, PINNED_VERSION


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def make_handler(tmp_path, **kwargs):
    return HomeAssistant(tmp_path / "agent", platform="darwin",
                         user_home=tmp_path / "user [one]", uid=501, **kwargs)


def test_start_builds_signed_bundle_before_bootstrap(tmp_path, monkeypatch):
    calls = []
    chmod_calls = []
    real_chmod = Path.chmod
    handler = None

    def chmod(path, mode, **kwargs):
        chmod_calls.append((path, mode))
        return real_chmod(path, mode, **kwargs)

    monkeypatch.setattr(Path, "chmod", chmod)

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[:2] == ["launchctl", "bootstrap"]:
            assert not (handler.root / "lan.status").exists()
        return SimpleNamespace(returncode=0, stdout="")

    handler = make_handler(tmp_path, runner=runner)
    handler.root.mkdir(parents=True)
    (handler.root / "lan.status").write_text("denied\n")
    handler._start()
    app = handler.user_home / "Applications/Leaf Home Assistant.app"
    contents = app / "Contents"
    assert plistlib.loads((contents / "Info.plist").read_bytes()) == {
        "CFBundleIdentifier": "ai.leaf.homeassistant.app",
        "CFBundleName": "Leaf Home Assistant",
        "CFBundleDisplayName": "Leaf Home Assistant",
        "CFBundleExecutable": "leaf-ha-launcher",
        "CFBundlePackageType": "APPL",
        "CFBundleVersion": __version__,
        "CFBundleShortVersionString": __version__,
        "LSUIElement": True,
        "NSLocalNetworkUsageDescription": "Leaf Home Assistant controls the lights on your home network.",
        "NSBluetoothAlwaysUsageDescription": "Home Assistant can find Bluetooth devices near this computer.",
        "NSBonjourServices": ["_hue._tcp"],
    }
    args = contents / "Resources/run.args"
    assert args.read_text().splitlines() == [str(handler.root / "lan.status"),
        str(handler.root / "hass.log"), str(handler.root / "venv/bin/hass"), "-c", str(handler.root / "config")]
    launcher = contents / "MacOS/leaf-ha-launcher"
    assert chmod_calls == [(launcher, 0o755)]
    # Windows does not expose POSIX executable permission bits through stat.
    if os.name != "nt":
        assert launcher.stat().st_mode & 0o777 == 0o755
    assert launcher.read_bytes() == (Path(homeassistant.__file__).parent / "assets/leaf-ha-launcher").read_bytes()
    assert calls == [
        (["codesign", "--force", "--deep", "-s", "-", str(app)],
         {"check": True, "capture_output": True, "timeout": 30}),
        (["launchctl", "bootstrap", "gui/501", str(handler._service_path())],
         {"check": True, "capture_output": True, "timeout": 15}),
    ]
    service = plistlib.loads(handler._service_path().read_bytes())
    assert service["ProgramArguments"] == ["/usr/bin/open", "-W", str(app)]
    assert service["AssociatedBundleIdentifiers"] == [APP_IDENTIFIER]
    assert service["Label"] == LABEL
    assert service["KeepAlive"] and service["RunAtLoad"] and service["ThrottleInterval"] == 30
    assert service["StandardOutPath"] == service["StandardErrorPath"] == str(handler.root / "hass.log")
    before = args.read_bytes()
    handler._build_app()
    assert args.read_bytes() == before


@pytest.mark.parametrize("bad", ["\n", "\r"])
def test_newline_argument_refused_before_build(tmp_path, bad):
    calls = []
    handler = HomeAssistant(tmp_path / ("bad" + bad + "path"), platform="darwin",
                            user_home=tmp_path / "user", runner=lambda *a, **k: calls.append(a))
    with pytest.raises(HAError, match="launcher argument"):
        handler._start()
    assert not calls and not handler._app_path().exists()


def test_codesign_failure_does_not_bootstrap(tmp_path):
    import subprocess

    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        raise subprocess.CalledProcessError(1, argv)

    handler = make_handler(tmp_path, runner=runner)
    with pytest.raises(subprocess.CalledProcessError):
        handler._start()
    assert len(calls) == 1 and calls[0][0] == "codesign"


@pytest.mark.parametrize("remove", [False, True])
@pytest.mark.parametrize("has_plist", [False, True])
def test_stop_targets_exact_launcher_and_waits(tmp_path, monkeypatch, remove, has_plist):
    events = []
    clock = Clock()

    def runner(argv, **kwargs):
        events.append(argv)
        assert kwargs["check"] is False and kwargs["capture_output"] is True
        if argv[0] == "pgrep":
            assert kwargs["timeout"] == 10
            pattern = argv[-1]
            exact = str(handler._app_path() / "Contents/MacOS/leaf-ha-launcher")
            assert re.search(pattern, exact)
            assert not re.search(pattern, exact + "-other")
            assert not re.search(pattern, "unrelated " + exact)
            return SimpleNamespace(returncode=0, stdout="101\n102\n")
        assert kwargs["timeout"] == 15
        return SimpleNamespace(returncode=0)

    def kill(pid, sig):
        events.append((pid, sig))
        assert pid in {101, 102}
        if sig == 0 and (pid == 102 or clock() >= 0.2):
            raise ProcessLookupError

    monkeypatch.setattr(os, "kill", kill)
    handler = make_handler(tmp_path, runner=runner, clock=clock, sleep=clock.sleep)
    handler._app_path().mkdir(parents=True)
    if has_plist:
        handler._service_path().parent.mkdir(parents=True)
        handler._service_path().touch()
    handler._stop(remove=remove)
    assert events[0] == ["launchctl", "bootout", "gui/501", str(handler._service_path())]
    assert events[1][0] == "pgrep"
    assert set(events[2:4]) == {(101, signal.SIGTERM), (102, signal.SIGTERM)}
    assert clock() == pytest.approx(0.2)
    assert handler._app_path().exists() is (not remove)
    assert handler._service_path().exists() is (has_plist and not remove)


def test_stop_survivor_fails_closed_and_preserves_bundle(tmp_path, monkeypatch):
    clock = Clock()
    handler = make_handler(tmp_path, clock=clock, sleep=clock.sleep,
        runner=lambda argv, **kwargs: SimpleNamespace(returncode=0, stdout="101\n"))
    handler._app_path().mkdir(parents=True)
    handler._service_path().parent.mkdir(parents=True)
    handler._service_path().touch()
    monkeypatch.setattr(os, "kill", lambda pid, sig: None)
    with pytest.raises(HAError, match="launcher stop failed"):
        handler._stop(remove=True)
    assert clock() == pytest.approx(10)
    assert handler._app_path().exists() and handler._service_path().exists()


@pytest.mark.parametrize("returncode,stdout", [(1, ""), (0, "101\n"), (0, "0\n"), (2, "")])
def test_stop_lookup_and_exit_races(tmp_path, monkeypatch, returncode, stdout):
    handler = make_handler(tmp_path, runner=lambda argv, **kwargs: SimpleNamespace(
        returncode=returncode if argv[0] == "pgrep" else 0, stdout=stdout))
    handler._app_path().mkdir(parents=True)

    def gone(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", gone)
    if returncode == 2 or stdout == "0\n":
        with pytest.raises(HAError):
            handler._stop(remove=True)
        assert handler._app_path().exists()
    else:
        handler._stop(remove=True)
        assert not handler._app_path().exists()


@pytest.mark.parametrize("access", ["ok", "denied", None, "invalid"])
@pytest.mark.parametrize("installed", [False, True])
def test_health_lan_access_through_dispatcher(config, tmp_path, monkeypatch, access, installed):
    handler = make_handler(tmp_path)
    monkeypatch.setattr(handler, "_healthy", lambda: False)
    handler.root.mkdir(parents=True)
    if installed:
        write_json(handler.root / "state.json", {"package": "homeassistant", "version": PINNED_VERSION,
                   "status": "installed", "last_health_at": None})
    if access is not None:
        (handler.root / "lan.status").write_text(access + "\n")
    registry = Registry(config.home)
    registry.register("homeassistant", handler)
    result = Dispatcher(config, registry).dispatch("pkg.health", {"package": "homeassistant"})
    assert result["status"] == "ok"
    assert result["data"]["lan_access"] == (access if access in {"ok", "denied"} else "unknown")


def test_systemd_does_not_use_app_bundle(tmp_path):
    calls = []
    handler = HomeAssistant(tmp_path / "agent", platform="linux", user_home=tmp_path / "user",
        runner=lambda argv, **kwargs: calls.append(argv) or SimpleNamespace(returncode=0))
    handler._start()
    unit = handler._service_path().read_text()
    # A simulated systemd unit must escape Windows host path backslashes too.
    hass = str(handler.root / "venv/bin/hass").replace("\\", "\\\\")
    config = str(handler.root / "config").replace("\\", "\\\\")
    assert f'ExecStart="{hass}" -c "{config}"\n' in unit
    assert calls == [["systemctl", "--user", "daemon-reload"],
                     ["systemctl", "--user", "enable", "--now", LABEL + ".service"]]
    assert not handler._app_path().exists()
    assert "lan_access" not in handler.handle("pkg.health", {})
    handler._stop(remove=True)
    assert calls[-2:] == [["systemctl", "--user", "disable", "--now", LABEL + ".service"],
                          ["systemctl", "--user", "daemon-reload"]]


def test_packaged_asset_is_universal_macho():
    asset = Path(homeassistant.__file__).parent / "assets/leaf-ha-launcher"
    assert asset.is_file() and asset.read_bytes()[:4] == bytes.fromhex("cafebabe")
    project = Path(__file__).resolve().parents[1] / "pyproject.toml"
    metadata = tomllib.loads(project.read_text())
    assert metadata["tool"]["setuptools"]["package-data"]["leaf_agent.packages"] == ["assets/*"]
    assert metadata["project"]["version"] == __version__


def _launcher_min_macos(data):
    """Minimum macOS (major, minor) per architecture, from each slice's LC_BUILD_VERSION."""
    import struct
    nfat = struct.unpack(">I", data[4:8])[0]
    found = []
    for i in range(nfat):
        _, _, offset, _, _ = struct.unpack(">iiIII", data[8 + 20 * i:28 + 20 * i])
        magic, _, _, _, ncmds, _, _, _ = struct.unpack("<IiiIIIII", data[offset:offset + 32])
        assert magic == 0xFEEDFACF
        at = offset + 32
        for _ in range(ncmds):
            cmd, size = struct.unpack("<II", data[at:at + 8])
            if cmd == 0x32:  # LC_BUILD_VERSION
                minos = struct.unpack("<I", data[at + 12:at + 16])[0]
                found.append((minos >> 16, (minos >> 8) & 0xFF))
            at += size
    return found


def test_packaged_launcher_runs_on_older_macos():
    # F20: a launcher built without -mmacosx-version-min required macOS 27 and
    # LaunchServices refused it on a macOS 26.5 hub with error -10825.
    asset = Path(homeassistant.__file__).parent / "assets/leaf-ha-launcher"
    found = _launcher_min_macos(asset.read_bytes())
    assert len(found) == 2
    assert all(version <= (13, 0) for version in found), found
