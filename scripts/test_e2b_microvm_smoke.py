from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SCRIPT = Path(__file__).with_name("e2b-microvm-smoke.py")
SPEC = importlib.util.spec_from_file_location("e2b_microvm_smoke", SCRIPT)
smoke = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = smoke
SPEC.loader.exec_module(smoke)


def _portable(path: Path) -> str:
    return str(path).replace("\\", "/")


def test_default_key_file_uses_the_per_user_grant_root():
    windows = smoke._default_key_file(
        environ={"LOCALAPPDATA": "C:/Users/alice/AppData/Local"},
        os_name="nt",
        home="C:/Users/alice",
    )
    posix = smoke._default_key_file(
        environ={},
        os_name="posix",
        home="/home/alice",
    )

    assert _portable(windows) == (
        "C:/Users/alice/AppData/Local/leaf-grants/e2b-api-key.txt"
    )
    assert _portable(posix) == "/home/alice/.leaf-grants/e2b-api-key.txt"


def test_explicit_key_file_override_wins_as_a_full_path():
    override = "D:/operator-grants/private/e2b.txt"

    resolved = smoke._key_file(
        environ={
            "LOCALAPPDATA": "C:/Users/alice/AppData/Local",
            "E2B_API_KEY_FILE": override,
        },
        os_name="nt",
        home="C:/Users/alice",
    )

    assert _portable(resolved) == override
