from pathlib import Path
import os
import shutil
import subprocess
import unittest

import pytest


ROOT = Path(__file__).resolve().parents[1]


class InstantAppEntrypointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = (ROOT / "server/start-app.sh").read_text(encoding="utf-8")
        cls.dockerfile = (ROOT / "deploy/Dockerfile.app").read_text(encoding="utf-8")
        cls.dockerfile_code = "\n".join(
            line for line in cls.dockerfile.splitlines()
            if not line.lstrip().startswith("#")
        )

    def test_entrypoint_materializes_scoped_control_files_and_scrubs_raw_values(self) -> None:
        for name in (
            "LEAF_INSTANT_CONTROL_SECRET",
            "LEAF_INSTANT_CONTROL_CA_PEM",
            "LEAF_INSTANT_CONTROL_CLIENT_CERT_PEM",
            "LEAF_INSTANT_CONTROL_CLIENT_KEY_PEM",
        ):
            self.assertIn(f"unset {name}", self.script)
        self.assertIn("umask 077", self.script)
        self.assertIn("chmod 0600", self.script)
        self.assertIn("LEAF_INSTANT_CONTROL_SECRET_FILE", self.script)
        self.assertIn("LEAF_INSTANT_CONTROL_CA_FILE", self.script)
        self.assertIn("LEAF_INSTANT_CONTROL_CLIENT_CERT_FILE", self.script)
        self.assertIn("LEAF_INSTANT_CONTROL_CLIENT_KEY_FILE", self.script)
        self.assertIn("exec uvicorn app:app", self.script)

    def test_image_starts_disabled_through_the_bootstrap(self) -> None:
        self.assertIn("LEAF_INSTANT_EXECUTION_ENABLED=0", self.dockerfile_code)
        # The image drops to uid 10003 before CMD. A root-owned 0500 script is
        # not executable there, while 0555 permits execution without writes.
        self.assertIn("chmod 0555 /app/server/start-app.sh", self.dockerfile_code)
        self.assertIn('CMD ["/app/server/start-app.sh"]', self.dockerfile_code)


if __name__ == "__main__":
    unittest.main()


@pytest.mark.parametrize("install_exit", [0, 1])
def test_source_bootstrap_selects_deployed_ref_even_when_install_fails(tmp_path, install_exit):
    shell = shutil.which("sh")
    if not shell:
        pytest.skip("POSIX shell unavailable")
    commands = tmp_path / "bin"
    commands.mkdir()
    for name, text in {
        "python": '#!/bin/sh\nprintf "%s" "$LEAF_PLATFORM_REPO_SOURCE_REF" > "$PROBE_INSTALL"\nexit "$PROBE_EXIT"\n',
        "uvicorn": '#!/bin/sh\nprintf "%s" "$LEAF_PLATFORM_REPO_SOURCE_REF" > "$PROBE_SERVE"\n',
    }.items():
        path = commands / name
        path.write_text(text, encoding="utf-8", newline="\n")
        path.chmod(0o755)
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "COMSPEC", "TEMP", "TMP") if key in os.environ}
    env.update(PATH=str(commands) + os.pathsep + env.get("PATH", ""),
               LEAF_PLATFORM_SOURCE_SNAPSHOT_DIR=str(tmp_path / "snapshot"),
               LEAF_SOURCE_SHA="a" * 40, LEAF_INSTANT_EXECUTION_ENABLED="0",
               PROBE_INSTALL=str(tmp_path / "install"), PROBE_SERVE=str(tmp_path / "serve"),
               PROBE_EXIT=str(install_exit))
    result = subprocess.run([shell, str(ROOT / "server/start-app.sh")], env=env,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0
    expected = "refs/leaf/deployed/" + "a" * 40
    assert (tmp_path / "install").read_text() == expected
    assert (tmp_path / "serve").read_text() == expected
    assert result.stderr.strip() == ("platform_source_snapshot_unavailable" if install_exit else "")


def test_source_bootstrap_preserves_explicit_authoring_override(tmp_path):
    shell = shutil.which("sh")
    if not shell:
        pytest.skip("POSIX shell unavailable")
    commands = tmp_path / "bin"
    commands.mkdir()
    python = commands / "python"
    python.write_text("#!/bin/sh\nexit 42\n", newline="\n")
    python.chmod(0o755)
    uvicorn = commands / "uvicorn"
    uvicorn.write_text('#!/bin/sh\nprintf "%s" "$LEAF_PLATFORM_REPO_SOURCE_REF"\n', newline="\n")
    uvicorn.chmod(0o755)
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "COMSPEC", "TEMP", "TMP") if key in os.environ}
    env.update(PATH=str(commands) + os.pathsep + env.get("PATH", ""),
               LEAF_PLATFORM_SOURCE_SNAPSHOT_DIR=str(tmp_path / "snapshot"),
               LEAF_PLATFORM_REPO_SOURCE_REF="refs/heads/operator-pin",
               LEAF_INSTANT_EXECUTION_ENABLED="0")
    result = subprocess.run([shell, str(ROOT / "server/start-app.sh")], env=env,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0 and result.stdout == "refs/heads/operator-pin"
    assert result.stderr == ""
