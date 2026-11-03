"""User-session service installation, without shell command interpolation."""
from __future__ import annotations

import os
from pathlib import Path
import plistlib
import subprocess
import sys

from .config import Config, atomic_write

LABEL = "ai.leaf.device-agent"


def render_plist(home: Path, python: str) -> bytes:
    return plistlib.dumps({
        "Label": LABEL, "ProgramArguments": [str(python), "-m", "leaf_agent", "run"],
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30,
        "EnvironmentVariables": {"LEAF_AGENT_HOME": str(home), "PYTHONUNBUFFERED": "1"},
        "StandardOutPath": str(home / "stdout.log"), "StandardErrorPath": str(home / "stderr.log"),
    }, sort_keys=True)


def _unit_quote(value, *, executable=False) -> str:
    value = str(value)
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ValueError("Invalid service path")
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if executable:
        value = value.replace("$", "$$")
    return '"' + value + '"'


def _log_path(path) -> str:
    # StandardOutput=append: consumes a whole path, not an ExecStart word.
    value = str(path)
    if any(c in value for c in "\n\r\x00"):
        raise ValueError("Invalid log path")
    return value.replace("%", "%%")


def render_systemd(home: Path, python: str) -> str:
    return (
        "[Unit]\nDescription=Leaf device agent\nAfter=network-online.target\n\n"
        "[Service]\nType=simple\n"
        f"ExecStart={_unit_quote(python, executable=True)} -m leaf_agent run\n"
        f"Environment={_unit_quote('LEAF_AGENT_HOME=' + str(home))}\n"
        "Environment=PYTHONUNBUFFERED=1\nRestart=always\nRestartSec=30\n"
        f"StandardOutput=append:{_log_path(home / 'stdout.log')}\n"
        f"StandardError=append:{_log_path(home / 'stderr.log')}\n\n"
        "[Install]\nWantedBy=default.target\n"
    )


def service_path(platform=None, user_home=None) -> Path:
    platform = platform or sys.platform
    base = Path(user_home) if user_home is not None else Path.home()
    if platform == "darwin":
        return base / "Library/LaunchAgents" / (LABEL + ".plist")
    if platform.startswith("linux"):
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", str(base / ".config"))) if user_home is None else base / ".config"
        return config_home / "systemd/user" / (LABEL + ".service")
    raise ValueError("Service installation requires macOS or Linux")


def install(config: Config, *, python=None, platform=None, user_home=None, uid=None, runner=subprocess.run):
    platform = platform or sys.platform
    python = str(python or sys.executable)
    path = service_path(platform, user_home)
    config.home.mkdir(parents=True, exist_ok=True, mode=0o700)
    if platform == "darwin":
        uid = os.getuid() if uid is None else uid
        # bootout is intentionally idempotent: a first install has no service.
        runner(["launchctl", "bootout", f"gui/{uid}", str(path)], check=False, timeout=15, capture_output=True)
        atomic_write(path, render_plist(config.home, python))
        runner(["launchctl", "bootstrap", f"gui/{uid}", str(path)], check=True, timeout=15, capture_output=True)
    else:
        atomic_write(path, render_systemd(config.home, python).encode("utf-8"))
        runner(["systemctl", "--user", "daemon-reload"], check=True, timeout=15, capture_output=True)
        runner(["systemctl", "--user", "enable", "--now", LABEL + ".service"], check=True, timeout=15, capture_output=True)
        runner(["systemctl", "--user", "restart", LABEL + ".service"], check=True, timeout=15, capture_output=True)
    return path


def uninstall(*, platform=None, user_home=None, uid=None, runner=subprocess.run):
    platform = platform or sys.platform
    path = service_path(platform, user_home)
    if platform == "darwin":
        uid = os.getuid() if uid is None else uid
        runner(["launchctl", "bootout", f"gui/{uid}", str(path)], check=False, timeout=15, capture_output=True)
    else:
        runner(["systemctl", "--user", "disable", "--now", LABEL + ".service"], check=False, timeout=15, capture_output=True)
    path.unlink(missing_ok=True)
    if platform != "darwin":
        runner(["systemctl", "--user", "daemon-reload"], check=True, timeout=15, capture_output=True)
