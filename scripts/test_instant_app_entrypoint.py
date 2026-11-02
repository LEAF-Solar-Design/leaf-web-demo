from pathlib import Path
import unittest


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
