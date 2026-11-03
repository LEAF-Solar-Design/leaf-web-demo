"""Local identity and release-pinned configuration. Never accepts a remote catalog."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit


PINNED_CATALOG = [{
    "package": "homeassistant", "display_name": "Home Assistant",
    "version": "2026.10.4", "port": 8123, "health_path": "/api/",
    "relay_allow": [
        {"method": "GET", "path_prefix": "/api/states"},
        {"method": "POST", "path_prefix": "/api/services/light/"},
        {"method": "POST", "path_prefix": "/api/services/scene/"},
        {"method": "POST", "path_prefix": "/api/services/homeassistant/turn_off"},
        {"method": "POST", "path_prefix": "/api/services/homeassistant/turn_on"},
    ],
}]


def agent_home() -> Path:
    if os.environ.get("LEAF_AGENT_HOME"):
        return Path(os.environ["LEAF_AGENT_HOME"]).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/LeafAgent"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "leaf-agent"


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".leaf-", dir=path.parent)
    try:
        # mkstemp creates the file privately, before any secret is written.
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def write_json(path: Path, value) -> None:
    atomic_write(path, json.dumps(value, separators=(",", ":"), allow_nan=False).encode())


def validate_origin(origin: str) -> str:
    parsed = urlsplit(origin)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("Server must be an HTTPS origin")
    parsed.port  # Validate port syntax/range before saving configuration.
    return origin.rstrip("/")


class Config:
    def __init__(self, home: Path | None = None):
        self.home = Path(home) if home is not None else agent_home()

    @property
    def disabled(self) -> bool:
        return (self.home / "DISABLED").exists()

    def pause(self) -> None:
        atomic_write(self.home / "DISABLED", b"")

    def resume(self) -> None:
        (self.home / "DISABLED").unlink(missing_ok=True)

    def load(self) -> dict:
        value = json.loads((self.home / "config.json").read_text(encoding="utf-8"))
        if set(value) != {"server", "device_id"}:
            raise ValueError("Invalid local configuration")
        value["server"] = validate_origin(value["server"])
        return value

    def save(self, server: str, device_id: str) -> None:
        write_json(self.home / "config.json", {"server": validate_origin(server), "device_id": device_id})

    def catalog(self) -> dict:
        entries = json.loads((self.home / "catalog.json").read_text(encoding="utf-8"))
        return {entry["package"]: entry for entry in entries}

    def pin_catalog(self) -> None:
        write_json(self.home / "catalog.json", PINNED_CATALOG)

    def wipe_identity(self) -> None:
        for name in ("device.key", "config.json"):
            (self.home / name).unlink(missing_ok=True)
