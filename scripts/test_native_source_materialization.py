"""Tiny real Git fixtures only; no provider, image build or dependency install."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "ci"))

import native_source_materialization as m


class Store:
    def __init__(self):
        self.objects = {}
        self.wrong_version = False

    def put(self, key, payload):
        ref = dict(bucket="fixture-only", key="source/" + key,
                   version_id="fixture-version", sha256=m.digest(payload))
        self.objects[ref["key"]] = payload
        return ref

    def get(self, bucket, key, version, limit):
        return self.objects[key], "wrong-version" if self.wrong_version else version


class MaterializationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        cls.base = Path(cls.temp.name)
        repo = cls.base / "original"
        repo.mkdir()
        m.git(repo, "init", "--template=", ".")
        (repo / "older.txt").write_bytes(b"retained history\n")
        m.git(repo, "add", ".")
        m.git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "older")
        cls.older = m.git(repo, "rev-parse", "HEAD").decode().strip()
        (repo / "current.txt").write_bytes(b"producer source\n")
        m.git(repo, "add", ".")
        m.git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "current")
        cls.commit = m.git(repo, "rev-parse", "HEAD").decode().strip()
        cls.tree = m.git(repo, "rev-parse", "HEAD^{tree}").decode().strip()
        m.git(repo, "update-ref", "refs/heads/snapshot", cls.commit)
        m.git(repo, "bundle", "create", str(cls.base / "fixture.bundle"), "refs/heads/snapshot")
        m.git(repo, "archive", "--format=zip", "--output=" + str(cls.base / "fixture.zip"), cls.commit)

    @classmethod
    def tearDownClass(cls):
        # Windows Git objects can be read-only. Keep fixture cleanup local.
        import shutil
        def writable_remove(func, path, exc):
            os.chmod(path, 0o700)
            func(path)
        shutil.rmtree(cls.base, onerror=writable_remove)
        cls.temp.cleanup()

    def setUp(self):
        self.store = Store()
        self.archive = self.store.put("fixture.zip", (self.base / "fixture.zip").read_bytes())
        self.bundle = self.store.put("fixture.bundle", (self.base / "fixture.bundle").read_bytes())
        self.manifest = dict(schema_version=1, commit=self.commit, archive=self.archive, bundle=self.bundle)
        self.ref = self.store.put("fixture.json", json.dumps(self.manifest).encode())
        self.admitted = {role: dict(commit=self.commit, tree=self.tree, manifest=self.ref,
                                   scope=dict(bucket="fixture-only", prefix="source/")) for role in m.ROLES}
        self.requested = {role: copy.deepcopy(self.ref) for role in m.ROLES}
        self.destination = self.base / self._testMethodName
        self.producer_request = dict(source_revision=self.commit, source_tree=self.tree, contract_revision=self.commit)

    def run_materializer(self):
        return m.materialize(self.store, self.requested, self.admitted, self.destination,
                             producer_request=self.producer_request)

    def test_three_clean_sources_preserve_commit_tree_history_and_producer_admission(self):
        result = self.run_materializer()
        for root in result["directories"].values():
            self.assertEqual(m.git(root, "rev-parse", "HEAD").decode().strip(), self.commit)
            self.assertEqual(m.git(root, "rev-parse", "HEAD^{tree}").decode().strip(), self.tree)
            self.assertEqual(m.git(root, "show", self.older + ":older.txt"), b"retained history\n")
            self.assertEqual(m.git(root, "status", "--porcelain"), b"")
            if path := os.environ.get("STUDIO_PRODUCER_SOURCE"):
                spec = importlib.util.spec_from_file_location("actual_producer", path)
                producer = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(producer)
                producer.admit_checkout(Path(root), self.commit, self.tree)
        self.assertEqual(set(result["sources"]), set(m.ROLES))

    def test_request_cannot_substitute_contract_generation(self):
        self.requested["provider_contract"]["version_id"] = "substitution"
        with self.assertRaisesRegex(ValueError, "request differs"):
            self.run_materializer()
        self.assertFalse(self.destination.exists())

    def test_actual_object_version_must_match(self):
        self.store.wrong_version = True
        with self.assertRaisesRegex(ValueError, "object integrity"):
            self.run_materializer()

    def test_payload_hash_must_match(self):
        self.store.objects[self.bundle["key"]] += b"tamper"
        with self.assertRaisesRegex(ValueError, "object integrity"):
            self.run_materializer()

    def test_tree_cannot_be_claimed_by_manifest(self):
        self.admitted["primary"]["tree"] = "0" * 40
        self.producer_request["source_tree"] = "0" * 40
        with self.assertRaisesRegex(ValueError, "commit/tree substitution"):
            self.run_materializer()

    def test_release_request_cannot_select_different_contract(self):
        self.producer_request["contract_revision"] = "0" * 40
        with self.assertRaisesRegex(ValueError, "producer contract binding"):
            self.run_materializer()
        self.assertFalse(self.destination.exists())

    def test_archive_must_match_bundle_even_when_its_object_hash_is_valid(self):
        import io
        import zipfile
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as z:
            z.writestr("current.txt", "substitution")
        self.manifest["archive"] = self.store.put("other.zip", out.getvalue())
        new_ref = self.store.put("other.json", json.dumps(self.manifest).encode())
        self.admitted["primary"]["manifest"] = new_ref
        self.requested["primary"] = new_ref
        with self.assertRaisesRegex(ValueError, "archive/bundle disagreement"):
            self.run_materializer()

    def test_missing_pin_and_arbitrary_command_rejected(self):
        del self.requested["autofill_solver"]
        with self.assertRaisesRegex(ValueError, "source roles"):
            self.run_materializer()
        self.requested["autofill_solver"] = self.ref
        self.admitted["primary"]["command"] = "do-not-run"
        with self.assertRaisesRegex(ValueError, "admission fields"):
            self.run_materializer()


if __name__ == "__main__":
    unittest.main()
