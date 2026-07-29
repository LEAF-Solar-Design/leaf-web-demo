from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from executor.bootstrap import BootstrapError, CONTROL_FILES, EXECUTOR_FILES, materialize


def _pem(label: str) -> str:
    return f"-----BEGIN {label}-----\ntest\n-----END {label}-----\n"


class BootstrapTests(unittest.TestCase):
    def environment(self, profile: str) -> dict[str, str]:
        files = CONTROL_FILES if profile == "control" else EXECUTOR_FILES
        values = {}
        for variable, (_filename, kind) in files.items():
            if kind == "seed":
                values[variable] = base64.b64encode(b"s" * 32).decode("ascii")
            elif kind == "json":
                values[variable] = json.dumps({"keys": []})
            elif kind == "text":
                values[variable] = "s" * 40
            else:
                values[variable] = _pem("TEST")
        return values

    def test_profiles_write_only_allowlisted_private_files_and_scrub_raw_values(self) -> None:
        for profile, expected in (("control", CONTROL_FILES), ("executor", EXECUTOR_FILES)):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as temporary:
                environment = self.environment(profile)
                written = materialize(profile, environ=environment, output_directory=temporary)
                self.assertEqual(set(expected), set(written))
                self.assertFalse(set(expected) & set(environment))
                if os.name != "nt":
                    self.assertEqual(0o700, stat.S_IMODE(Path(temporary).stat().st_mode))
                for target in written.values():
                    if os.name != "nt":
                        self.assertEqual(0o600, stat.S_IMODE(target.stat().st_mode))

    def test_incomplete_or_invalid_profiles_fail_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment = self.environment("control")
            environment.pop("LEAF_INSTANT_CONTROL_TLS_KEY_PEM")
            with self.assertRaisesRegex(BootstrapError, "required"):
                materialize("control", environ=environment, output_directory=temporary)
            self.assertEqual([], list(Path(temporary).iterdir()))

        with tempfile.TemporaryDirectory() as temporary:
            environment = self.environment("executor")
            environment["LEAF_INSTANT_CONTROL_JWKS_JSON"] = "[]"
            with self.assertRaisesRegex(BootstrapError, "JSON object"):
                materialize("executor", environ=environment, output_directory=temporary)

    @unittest.skipIf(os.name == "nt", "Windows chmod semantics do not match Fargate mounts")
    def test_directory_chmod_permission_denial_is_tolerated_when_writable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real_chmod = os.chmod

            def chmod_side_effect(path: str | os.PathLike[str], mode: int) -> None:
                if Path(path) == root:
                    raise PermissionError(1, "Operation not permitted", str(path))
                real_chmod(path, mode)

            with mock.patch("executor.bootstrap.os.chmod", side_effect=chmod_side_effect):
                written = materialize("executor", environ=self.environment("executor"), output_directory=root)

            self.assertEqual(set(EXECUTOR_FILES), set(written))
            for target in written.values():
                self.assertEqual(0o600, stat.S_IMODE(target.stat().st_mode))

    def test_file_chmod_permission_denial_is_tolerated_after_private_create(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real_chmod = os.chmod

            def chmod_side_effect(path: str | os.PathLike[str], mode: int) -> None:
                candidate = Path(path)
                if candidate.parent == root and candidate.name.startswith("."):
                    raise PermissionError(1, "Operation not permitted", str(path))
                real_chmod(path, mode)

            with mock.patch("executor.bootstrap.os.chmod", side_effect=chmod_side_effect):
                written = materialize("executor", environ=self.environment("executor"), output_directory=root)

            self.assertEqual(set(EXECUTOR_FILES), set(written))
            for target in written.values():
                self.assertTrue(target.exists())

    def test_requested_owner_is_applied_to_directory_and_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self.environment("executor")
            environment["LEAF_INSTANT_SECRET_OWNER_UID"] = "65532"
            environment["LEAF_INSTANT_SECRET_OWNER_GID"] = "65532"

            with mock.patch("executor.bootstrap.os.chown", create=True) as chown:
                written = materialize("executor", environ=environment, output_directory=root)

            self.assertEqual(set(EXECUTOR_FILES), set(written))
            chown.assert_any_call(root, 65532, 65532)
            for target in written.values():
                chown.assert_any_call(mock.ANY, 65532, 65532)
                self.assertTrue(target.exists())

    def test_secret_owner_requires_uid_and_gid_together(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment = self.environment("executor")
            environment["LEAF_INSTANT_SECRET_OWNER_UID"] = "65532"
            with self.assertRaisesRegex(BootstrapError, "set together"):
                materialize("executor", environ=environment, output_directory=temporary)

    @unittest.skipIf(os.name == "nt", "Windows symlink creation can require elevated developer mode")
    def test_symlink_output_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "target"
            target.mkdir()
            link = Path(temporary) / "link"
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(BootstrapError, "symlink"):
                materialize("executor", environ=self.environment("executor"), output_directory=link)


if __name__ == "__main__":
    unittest.main()
