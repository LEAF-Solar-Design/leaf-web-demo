"""Transport integrity and workflow authority boundary for the main gate."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import gate_result_transport as transport


SOURCE = "a" * 40
RUN = "34259220117"
TRUSTED = ("github.ref == 'refs/heads/main' && (inputs.ref == '' || inputs.ref == github.sha) "
           "&& (github.event_name == 'push' || github.event_name == 'workflow_dispatch')")


class MemoryS3:
    def __init__(self):
        self.objects = {}

    def put(self, key, payload, meta):
        if key in self.objects:
            raise ValueError("immutable object already exists")
        self.objects[key] = (payload, transport.checksum(payload),
                             {k: str(v) for k, v in meta.items()})

    def list(self, prefix):
        return [{"Key": k, "Size": len(v[0])} for k, v in self.objects.items()
                if k.startswith(prefix)]

    def get(self, key):
        return self.objects[key]


class RoundtripTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.store = MemoryS3()
        self.bodies = {}
        for shard in range(8):
            self.add_shard(shard, 1)

    def add_shard(self, shard, attempt):
        # Whitespace, UTF-8, and a failing row must survive without conversion.
        body = ('{ "schema": 1, "shard_index": %d, "results": '
                '[{"status":"FAIL", "note":"é"}], "attempt": %d }\r\n'
                % (shard, attempt)).encode("utf-8")
        path = self.directory / "input.json"
        path.write_bytes(body)
        transport.upload(self.store, SOURCE, RUN, attempt, shard, path)
        self.bodies[shard] = body

    def retrieve(self, attempt=1):
        return transport.download(self.store, SOURCE, RUN, attempt, self.directory / "out")

    def rewrite(self, change, shard=0):
        meta = transport.identity(SOURCE, RUN, 1, shard)
        key = transport.object_key(meta)
        payload, _, metadata = self.store.objects[key]
        data = json.loads(payload)
        change(data)
        payload = json.dumps(data).encode()
        self.store.objects[key] = payload, transport.checksum(payload), metadata

    def test_all_eight_original_bodies_roundtrip(self):
        self.retrieve()
        paths = list((self.directory / "out").rglob("gate-result.json"))
        self.assertEqual(8, len(paths))
        for shard, body in self.bodies.items():
            path = self.directory / "out" / f"gate-shard-{shard}-{RUN}" / "gate-results/gate-result.json"
            self.assertEqual(body, path.read_bytes())

    def test_failed_only_retry_selects_latest_per_shard(self):
        self.add_shard(2, 2)
        self.add_shard(2, 3)
        self.add_shard(6, 2)
        selected = self.retrieve(3)
        self.assertEqual([1, 1, 3, 1, 1, 1, 2, 1], [selected[i][0] for i in range(8)])
        for shard, body in self.bodies.items():
            path = self.directory / "out" / f"gate-shard-{shard}-{RUN}" / "gate-results/gate-result.json"
            self.assertEqual(body, path.read_bytes())

    def test_wrong_envelope_identity_and_unknown_keys(self):
        for field, value in (("source", "b" * 40), ("run", "99"), ("shard", "7"),
                             ("attempt", 2), ("unexpected", True)):
            with self.subTest(field=field):
                saved = dict(self.store.objects)
                self.rewrite(lambda d: d.update({field: value}))
                with self.assertRaises(ValueError):
                    self.retrieve()
                self.assertFalse((self.directory / "out").exists())
                self.store.objects = saved

    def test_missing_result_and_future_attempt_fail(self):
        key = transport.object_key(transport.identity(SOURCE, RUN, 1, 7))
        old = self.store.objects.pop(key)
        with self.assertRaisesRegex(ValueError, "missing shard"):
            self.retrieve()
        self.store.objects[key] = old
        self.add_shard(7, 2)
        with self.assertRaisesRegex(ValueError, "future attempt"):
            self.retrieve()

    def test_corrupt_latest_never_falls_back(self):
        self.add_shard(3, 2)
        key = transport.object_key(transport.identity(SOURCE, RUN, 2, 3))
        payload, _, meta = self.store.objects[key]
        self.store.objects[key] = payload, "wrong", meta
        with self.assertRaisesRegex(ValueError, "S3 checksum"):
            self.retrieve(2)

    def test_body_mismatch_and_s3_metadata_fail(self):
        self.rewrite(lambda d: d.update(body='{"status":"PASS"}'))
        with self.assertRaisesRegex(ValueError, "body checksum"):
            self.retrieve()
        key = transport.object_key(transport.identity(SOURCE, RUN, 1, 0))
        payload, digest, meta = self.store.objects[key]
        self.store.objects[key] = payload, digest, {**meta, "source": "b" * 40}
        with self.assertRaisesRegex(ValueError, "S3 metadata"):
            self.retrieve()

    def test_malformed_or_missing_body_is_not_materialized(self):
        key = transport.object_key(transport.identity(SOURCE, RUN, 1, 0))
        original = self.store.objects[key]
        for body in (b"not json", b"[]"):
            self.store.objects[key] = body, transport.checksum(body), original[2]
            with self.assertRaises(ValueError):
                self.retrieve()
            self.assertFalse((self.directory / "out").exists())
        self.store.objects[key] = original
        self.rewrite(lambda d: d.pop("body"))
        with self.assertRaisesRegex(ValueError, "missing envelope keys"):
            self.retrieve()

    def test_listing_rejects_foreign_keys_and_oversize(self):
        entries = self.store.list(transport.root(SOURCE, RUN) + "shards/")
        for key in (entries[0]["Key"].replace(SOURCE, "b" * 40),
                    entries[0]["Key"].replace(RUN, "99"),
                    entries[0]["Key"].replace("shards/0", "shards/8"),
                    entries[0]["Key"] + ".extra"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                transport.select_attempts([{**entries[0], "Key": key}, *entries[1:]], SOURCE, RUN, 1)
        with self.assertRaisesRegex(ValueError, "bound"):
            transport.select_attempts([{**entries[0], "Size": transport.MAX_PAYLOAD + 1},
                                       *entries[1:]], SOURCE, RUN, 1)

    def test_payload_body_logs_and_duplicate_key_bounds(self):
        meta = transport.identity(SOURCE, RUN, 1, 0)
        with self.assertRaises(ValueError):
            transport.unpack(b" " * (transport.MAX_PAYLOAD + 1), meta)
        with self.assertRaises(ValueError):
            transport.pack(b"x" * (transport.MAX_BODY + 1), meta)
        with self.assertRaises(ValueError):
            transport.pack(b"{}", meta, {"failure.log": "x" * (transport.MAX_LOGS + 1)})
        with self.assertRaisesRegex(ValueError, "duplicate"):
            transport.unpack(b'{"schema":1,"schema":1}', meta)
        logs = self.directory / "logs"
        logs.mkdir()
        (logs / "failure.log").write_bytes(b"\xff" * (transport.MAX_LOGS + 1))
        texts, truncated = transport.collect_logs(logs)
        self.assertTrue(truncated)
        self.assertLessEqual(sum(len(v.encode()) for v in texts.values()), transport.MAX_LOGS)
        self.assertEqual(b"{}", transport.unpack(transport.pack(b"{}", meta, texts, truncated), meta))

    def test_proof_is_same_run_bound_and_does_not_join_shards(self):
        path = self.directory / "proof.json"
        path.write_bytes(b'{"tree":"original-proof"}\n')
        transport.upload(self.store, SOURCE, RUN, 2, "proof", path)
        key = transport.object_key(transport.identity(SOURCE, RUN, 2, "proof"))
        self.assertIn(f"/{SOURCE}/{RUN}/proof/attempt-2.json", key)
        self.assertEqual(path.read_bytes(), transport.unpack(self.store.get(key)[0],
                         transport.identity(SOURCE, RUN, 2, "proof")))
        self.retrieve()
        with self.assertRaises(ValueError):
            transport.upload(self.store, SOURCE, RUN, 2, "proof", path)


class CliBoundaryTests(unittest.TestCase):
    def test_oversized_head_prevents_transfer(self):
        operations = []

        def cli(command, **kwargs):
            operations.append(command[command.index("s3api") + 1])
            return subprocess.CompletedProcess(command, 0, json.dumps({
                "ContentLength": transport.MAX_PAYLOAD + 1, "ETag": '"etag"'
            }).encode(), b"")

        with self.assertRaisesRegex(ValueError, "payload exceeds"):
            transport.S3(cli).get("unused-key")
        self.assertEqual(["head-object"], operations)

    def test_aws_immutable_checksum_and_bounded_download(self):
        calls, objects = [], {}

        def cli(command, **kwargs):
            calls.append((command, kwargs))
            args = command[command.index("s3api") + 1:]
            operation = args[0]
            key = args[args.index("--key") + 1] if "--key" in args else None
            result = {}
            if operation == "put-object":
                self.assertEqual("*", args[args.index("--if-none-match") + 1])
                body = Path(args[args.index("--body") + 1]).read_bytes()
                digest = args[args.index("--checksum-sha256") + 1]
                self.assertEqual(transport.checksum(body), digest)
                objects[key] = body, digest, json.loads(args[args.index("--metadata") + 1])
            elif operation == "head-object":
                body, digest, meta = objects[key]
                result = {"ContentLength": len(body), "ChecksumSHA256": digest,
                          "Metadata": meta, "ETag": '"etag"'}
            elif operation == "get-object":
                self.assertIn("--if-match", args)
                index = args.index("--range")
                self.assertEqual(f"bytes=0-{len(objects[key][0]) - 1}", args[index + 1])
                Path(args[index + 2]).write_bytes(objects[key][0])
            elif operation == "list-objects-v2":
                self.assertIn("--no-paginate", args)
                result = {"IsTruncated": True}
            return subprocess.CompletedProcess(command, 0, json.dumps(result).encode(), b"")

        store = transport.S3(cli)
        meta = transport.identity(SOURCE, RUN, 1, 0)
        payload = transport.pack(b"{}\n", meta)
        key = transport.object_key(meta)
        store.put(key, payload, meta)
        self.assertEqual((payload, transport.checksum(payload),
                          {k: str(v) for k, v in meta.items()}), store.get(key))
        with self.assertRaisesRegex(ValueError, "listing exceeds"):
            store.list(transport.root(SOURCE, RUN))
        for command, kwargs in calls:
            self.assertEqual(60, kwargs["timeout"])
            self.assertEqual("2", kwargs["env"]["AWS_MAX_ATTEMPTS"])
            self.assertEqual(transport.BUCKET, command[command.index("--bucket") + 1])


class WorkflowBoundaryTests(unittest.TestCase):
    def test_authority_transport_and_original_verifier_contract(self):
        workflows = Path(__file__).resolve().parents[1] / ".github/workflows"
        gate = (workflows / "test-gate.yml").read_text(encoding="utf-8")
        build = (workflows / "build-platform-images.yml").read_text(encoding="utf-8")
        probe = gate.split("\n  probe:\n", 1)[1].split("\n  shards:\n", 1)[0]
        self.assertNotIn("id-token: write", probe)
        self.assertNotIn("environment:", probe)
        for job in ("shards", "gate"):
            header = gate.split(f"\n  {job}:\n", 1)[1].split("    steps:\n", 1)[0]
            self.assertIn("environment: ${{ " + TRUSTED + " && 'ecr-release' || '' }}", header)
        steps = gate.split("      - name: ")
        for name in ("Configure AWS for main gate transport", "Store main shard result",
                     "Configure AWS for main gate fan-in", "Retrieve all eight main"):
            step = next(s for s in steps if s.startswith(name))
            condition = next(l for l in step.splitlines() if l.strip().startswith("if:"))
            self.assertIn("!cancelled()", condition)
            self.assertIn(TRUSTED, condition)
        for name in ("Upload shard result and logs", "Download shard results"):
            step = next(s for s in steps if s.startswith(name))
            self.assertIn("!(" + TRUSTED + ")", step)
        proof = next(s for s in steps if s.startswith("Publish the tree-bound"))
        self.assertIn("continue-on-error: ${{ " + TRUSTED + " }}", proof)
        self.assertIn("--verify-shard-results \"$RUNNER_TEMP/shard-results\"", gate)
        self.assertIn('test "$SHARD_JOB_RESULT" = "success"', gate)
        self.assertIn("shard: [0, 1, 2, 3, 4, 5, 6, 7]", gate)
        self.assertIn("fail-fast: false", gate)
        call = build.split("\n  test:\n", 1)[1].split("\n  warm:\n", 1)[0]
        self.assertIn("id-token: write", call)
        self.assertIn("AWS_ECR_PUSH_ROLE: ${{ secrets.AWS_ECR_PUSH_ROLE }}", call)
        self.assertNotIn("secrets: inherit", call)
        self.assertIn("    secrets:\n      AWS_ECR_PUSH_ROLE:", gate)


if __name__ == "__main__":
    unittest.main()
