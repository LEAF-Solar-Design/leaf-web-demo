from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class InstantHarnessEntrypointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = (ROOT / "harness/scripts/start-harness.sh").read_text(encoding="utf-8")
        cls.dockerfile = (ROOT / "deploy/Dockerfile.harness").read_text(encoding="utf-8")

    def test_entrypoint_materializes_bounded_files_and_execs_harness(self) -> None:
        for name in (
            "LEAF_INSTANT_EXECUTOR_CA_PEM",
            "LEAF_INSTANT_EXECUTOR_CLIENT_CERT_PEM",
            "LEAF_INSTANT_EXECUTOR_CLIENT_KEY_PEM",
        ):
            self.assertIn(f'unset {name}', self.script)
        self.assertIn("umask 077", self.script)
        self.assertIn("chmod 0600", self.script)
        self.assertIn("LEAF_INSTANT_EXECUTOR_CA_FILE", self.script)
        self.assertIn("LEAF_INSTANT_EXECUTOR_CERT_FILE", self.script)
        self.assertIn("LEAF_INSTANT_EXECUTOR_KEY_FILE", self.script)
        self.assertIn("exec node dist/scripts/serve.js", self.script)

    def test_image_uses_non_root_bootstrap_and_starts_disabled(self) -> None:
        self.assertLess(self.dockerfile.index("USER 10002:10002"), self.dockerfile.index('CMD ["/app/scripts/start-harness.sh"]'))
        self.assertIn("LEAF_INSTANT_EXECUTION_ENABLED=0", self.dockerfile)
        self.assertIn("chmod 0555 /app/scripts/start-harness.sh", self.dockerfile)


if __name__ == "__main__":
    unittest.main()
