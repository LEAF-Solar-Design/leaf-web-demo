from pathlib import PurePosixPath, PureWindowsPath
import plistlib

from leaf_agent import launchd


def test_plist_rendering_is_platform_independent():
    # Literal platform paths catch bugs that host-native tmp_path would hide.
    home = PurePosixPath("/Users/Leaf Owner/Library/Application Support/LeafAgent")
    python = "/Users/Leaf Owner/Library/Application Support/LeafAgent/venv/bin/python"
    plist = plistlib.loads(launchd.render_plist(home, python))
    assert plist == {
        "Label": "ai.leaf.device-agent", "ProgramArguments": [python, "-m", "leaf_agent", "run"],
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30,
        "EnvironmentVariables": {"LEAF_AGENT_HOME": str(home), "PYTHONUNBUFFERED": "1"},
        "StandardOutPath": str(home / "stdout.log"), "StandardErrorPath": str(home / "stderr.log"),
    }
    windows_home = PureWindowsPath(r"C:\Users\Leaf Owner\Agent")
    assert plistlib.loads(launchd.render_plist(windows_home, r"C:\Python311\python.exe"))["EnvironmentVariables"]["LEAF_AGENT_HOME"] == str(windows_home)


def test_launchctl_argv_with_stub_on_every_platform(config, tmp_path):
    calls = []
    def runner(argv, **options):
        calls.append((argv, options))
    python = "/Users/Leaf Owner/agent/venv/bin/python"
    path = launchd.install(config, python=python, platform="darwin", user_home=tmp_path, uid=501, runner=runner)
    assert path == tmp_path / "Library/LaunchAgents/ai.leaf.device-agent.plist"
    assert [call[0] for call in calls] == [["launchctl", "bootout", "gui/501", str(path)],
                                         ["launchctl", "bootstrap", "gui/501", str(path)]]
    assert calls[0][1] == {"check": False, "timeout": 15, "capture_output": True}
    assert calls[1][1] == {"check": True, "timeout": 15, "capture_output": True}
    assert plistlib.loads(path.read_bytes())["ProgramArguments"][0] == python
    launchd.uninstall(platform="darwin", user_home=tmp_path, uid=501, runner=runner)
    assert calls[-1][0] == ["launchctl", "bootout", "gui/501", str(path)]
    assert not path.exists()


def test_systemd_render_and_install_argv(config, tmp_path):
    home = PurePosixPath("/home/Leaf Owner/100%data")
    unit = launchd.render_systemd(home, str(home / "venv/bin/python"))
    assert 'ExecStart="/home/Leaf Owner/100%%data/venv/bin/python" -m leaf_agent run\n' in unit
    assert 'Environment="LEAF_AGENT_HOME=/home/Leaf Owner/100%%data"\n' in unit
    assert "Restart=always\nRestartSec=30\n" in unit
    assert "StandardOutput=append:/home/Leaf Owner/100%%data/stdout.log\n" in unit
    special = launchd.render_systemd(PurePosixPath("/home/Leaf $Owner"), "/home/Leaf $Owner/python")
    assert 'ExecStart="/home/Leaf $$Owner/python"' in special
    assert 'Environment="LEAF_AGENT_HOME=/home/Leaf $Owner"' in special
    calls = []
    def runner(argv, **options):
        calls.append(argv)
    path = launchd.install(config, platform="linux", user_home=tmp_path, runner=runner)
    assert path == tmp_path / ".config/systemd/user/ai.leaf.device-agent.service"
    assert calls == [["systemctl", "--user", "daemon-reload"],
                     ["systemctl", "--user", "enable", "--now", "ai.leaf.device-agent.service"],
                     ["systemctl", "--user", "restart", "ai.leaf.device-agent.service"]]
    assert "[Install]" in path.read_text(encoding="utf-8")
    launchd.uninstall(platform="linux", user_home=tmp_path, runner=runner)
    assert calls[-2:] == [["systemctl", "--user", "disable", "--now", "ai.leaf.device-agent.service"],
                          ["systemctl", "--user", "daemon-reload"]]
    assert not path.exists()
