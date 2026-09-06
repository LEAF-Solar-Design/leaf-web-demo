import copy
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).parent / "ci"


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


replay = load("replay_mq_review_app")
mq = load("mq_review")
HEAD = "a" * 40
BUILD_ID = replay.PROJECT + ":dc2c2b27-0c43-4adf-832d-94f49ce99eb0"


def build():
    return {"id": BUILD_ID, "arn": replay.PREFIX + BUILD_ID, "projectName": replay.PROJECT,
            "resolvedSourceVersion": HEAD, "buildComplete": True, "buildStatus": "FAILED",
            "serviceRole": "arn:aws:iam::807034087062:role/leaf-mq-codebuild",
            "source": {"location": f"https://github.com/{replay.REPO}.git"},
            "sourceVersion": "pr/1102", "initiator": "GitHub-Hookshot/test",
            "phases": [{"phaseType": "BUILD", "phaseStatus": "FAILED"}]}


MESSAGES = [f"loader: running .codebuild/mq.sh and scripts/ci/mq_review.py from {replay.LOADER} (ab949693)\n", replay.FAILURE + "\n"]


class App:
    def __init__(self):
        self.calls = []
        self.source = HEAD
        self.queued = False
        self.status_http = 201
        self.readback = True
        self.posted = None

    def installation_token(self, repo):
        assert repo == replay.REPO
        return "fixture-ephemeral"

    def _req(self, method, path, token, body=None):
        self.calls.append((method, path))
        if path.endswith("/pulls/1102"):
            return 200, {"state": "open", "head": {"sha": self.source, "repo": {"full_name": replay.REPO}},
                         "base": {"ref": "main", "repo": {"full_name": replay.REPO}}}
        if "branches-where-head" in path:
            return 200, [{"name": "gh-readonly-queue/main/pr-1102"}] if self.queued else []
        if path == "/graphql":
            return 200, {"data": {"repository": {"mergeQueue": None}}}
        if method == "POST":
            self.posted = dict(body, id=17)
            return self.status_http, self.posted
        return 200, [self.posted] if self.readback else []


class AdmissionTests(unittest.TestCase):
    def test_exact_transport_failure(self):
        self.assertEqual(replay.admit(build(), BUILD_ID, HEAD, MESSAGES), 1102)

    def test_identity_and_outcome_refusals(self):
        for field, value in {"id": "wrong", "arn": "wrong", "projectName": "other",
                             "resolvedSourceVersion": "b" * 40, "buildComplete": False,
                             "buildStatus": "SUCCEEDED", "serviceRole": "other",
                             "sourceVersion": "main", "initiator": "operator"}.items():
            with self.subTest(field=field):
                candidate = build()
                candidate[field] = value
                with self.assertRaises(ValueError):
                    replay.admit(candidate, BUILD_ID, HEAD, MESSAGES)

    def test_other_source_or_failure_refused(self):
        for field, value in [("source", {"location": "other"}),
                             ("phases", [{"phaseType": "DOWNLOAD_SOURCE", "phaseStatus": "FAILED"}])]:
            candidate = build()
            candidate[field] = value
            with self.assertRaises(ValueError):
                replay.admit(candidate, BUILD_ID, HEAD, MESSAGES)

    def test_log_receipt_must_be_exact_and_unique(self):
        for messages in [[], MESSAGES[:1], MESSAGES[1:], MESSAGES + [replay.FAILURE],
                         [MESSAGES[0], "arbitrary source failure"]]:
            with self.subTest(messages=messages), self.assertRaises(ValueError):
                replay.admit(build(), BUILD_ID, HEAD, messages)

    def test_invalid_inputs(self):
        for identity, head in [("other:abc", HEAD), (BUILD_ID, "short")]:
            with self.assertRaises(ValueError):
                replay.admit(build(), identity, head, MESSAGES)


class RecoveryTests(unittest.TestCase):
    def test_default_dry_run_never_posts_status_or_calls_legacy_auth(self):
        original = mq.github
        app = App()
        result = replay.recover(mq, app, build(), HEAD, 1102)
        self.assertFalse(result["publication_performed"])
        self.assertFalse(result["source_tests_passed"])
        self.assertEqual(result["native_build_outcome"], "FAILED")
        self.assertFalse(any(method == "POST" and path != "/graphql" for method, path in app.calls))
        self.assertIs(mq.github, original)

    def test_apply_posts_exact_context_and_reads_back(self):
        app = App()
        result = replay.recover(mq, app, build(), HEAD, 1102, True)
        self.assertTrue(result["publication_performed"])
        self.assertEqual(app.posted["context"], "mq-review")
        self.assertIn(BUILD_ID, app.posted["target_url"])
        self.assertEqual(sum(method == "POST" and path != "/graphql" for method, path in app.calls), 1)

    def test_drift_and_live_queue_refuse(self):
        for field, value in [("source", "b" * 40), ("queued", True)]:
            app = App()
            setattr(app, field, value)
            with self.assertRaises(ValueError):
                replay.recover(mq, app, build(), HEAD, 1102, True)
            self.assertIsNone(app.posted)

    def test_transport_or_readback_failure_is_not_success(self):
        for field, value in [("status_http", 404), ("readback", False)]:
            app = App()
            setattr(app, field, value)
            with self.assertRaises(ValueError):
                replay.recover(mq, app, build(), HEAD, 1102, True)

    def test_other_context_repo_or_payload_refused(self):
        for path, payload in [("repos/other/repo/statuses/" + HEAD, {}),
                              (f"repos/{replay.REPO}/statuses/{HEAD}", {"context": "contract"})]:
            request = replay.request_adapter(App(), HEAD, "target", True, [])
            with self.assertRaises(ValueError):
                request(path, payload)


if __name__ == "__main__":
    unittest.main()
