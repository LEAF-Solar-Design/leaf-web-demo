"""Focused contracts for pure PR family selection; no runner import or IO."""

import copy
import unittest

from web_family import (
    DEPENDENCY_ROOTS, SAFE_PATHS, filter_web_family, web_family_decision,
)


def suite(sid, cwd="web"):
    return {"id": sid, "cwd": cwd, "command": ["node", "check.mjs"],
            "kind": "script"}


class WebFamilyTests(unittest.TestCase):
    def setUp(self):
        self.rows = [suite("web-build"), suite("web-unit"),
                     suite("studio-walk-regressions", "."),
                     suite("web-demo-gate", "."), suite("server-unit", "server"),
                     suite("bridge", "."), suite("platform-unit", "..")]
        self.selected = {row["id"] for row in self.rows}
        self.nonweb = {"server-unit", "bridge", "platform-unit"}
        self.safe = ["server/tests/test_catalog_digest_boundary.py"]

    def assertRetained(self, paths, **kwargs):
        self.assertEqual(filter_web_family(self.selected, self.rows, paths, **kwargs),
                         self.selected)

    def test_exact_independent_docs_and_backend_test(self):
        for path in ("docs/adn-purchase-walkthrough.md",
                     "docs/adn-membership-reinstate-email-draft.md",
                     "server/tests/test_catalog_digest_boundary.py"):
            with self.subTest(path=path):
                self.assertEqual(filter_web_family(self.selected, self.rows, [path]),
                                 self.nonweb)
        self.assertEqual(filter_web_family(self.selected, self.rows, sorted(SAFE_PATHS)),
                         self.nonweb)

    def test_every_declared_dependency_root_retains(self):
        for root in DEPENDENCY_ROOTS:
            with self.subTest(root=root):
                self.assertRetained([root + "/changed.py"])
                self.assertRetained([*self.safe, root + "/changed.py"])

    def test_real_external_web_inputs_and_runtime_keep_family(self):
        for path in (
            "server/app.py", "server/runtime/worker.py", "platform/runtime/worker.py",
            "server/tests/fixtures/lab-placement-cases.v1.json",
            "server/tests/test_solar_solaredge_flow.py", "server/tests/conftest.py",
            "docs/DEMO-GOLDEN-PATH.md", "docs/CAD-ENGINE-LICENSE-REVIEW.md",
            "docs/parity/evidence/ground/generate/intake.json",
            "docker-compose.yml", "package.json", "package-lock.json",
            "pyproject.toml", "requirements.txt", "e2b.Dockerfile", "ASPECTS.yaml",
        ):
            with self.subTest(path=path):
                self.assertRetained([path])

    def test_unknown_paths_and_unreviewed_docs_tests_keep_family(self):
        for path in ("new-root/code.py", "docs/new-guide.md",
                     "server/tests/test_new.py", "tests/test_web.py",
                     "README.md", "WEB/src/app.js", "docs/adn-purchase-walkthrough.md/child"):
            with self.subTest(path=path):
                self.assertRetained([path])

    def test_malformed_and_unavailable_changed_paths(self):
        for paths in (None, [], (), set(), "docs/readme.md", {}, 7,
                      iter(self.safe), [None], [42], [False], [[]],
                      [""], ["/docs/readme.md"], ["C:/docs/readme.md"],
                      ["C:relative.md"], ["docs\\readme.md"],
                      ["../docs/readme.md"], ["docs/../readme.md"],
                      ["./docs/readme.md"], ["docs//readme.md"],
                      ["docs/readme.md/"], ["docs/readme\0.md"],
                      ["docs/readme\n.md"], [" docs/readme.md"],
                      ["docs/ readme.md"], ["docs/.env"], [".env"],
                      ["docs/.hidden/readme.md"]):
            with self.subTest(paths=repr(paths)):
                self.assertRetained(paths)

    def test_malformed_catalog_preserves_original(self):
        bad_row_values = (
            {"id": None}, {"cwd": None}, {"cwd": []}, {"cwd": "/web"},
            {"cwd": "web/../server"}, {"cwd": "web\\src"},
            {"command": None}, {"command": "node check.mjs"},
            {"command": []}, {"command": [None]}, {"kind": None},
            {"kind": ""}, {"id": []},
        )
        catalogs = [None, [], {}, "catalog", [None], self.rows + [self.rows[0]],
                    self.rows[:-1]]
        catalogs.extend([{**self.rows[0], **patch}, *self.rows[1:]]
                        for patch in bad_row_values)
        catalogs.extend([[{key: value for key, value in self.rows[0].items()
                            if key != missing}, *self.rows[1:]]
                         for missing in ("id", "cwd", "command", "kind")])
        for rows in catalogs:
            with self.subTest(rows=rows):
                self.assertEqual(filter_web_family(self.selected, rows, self.safe),
                                 self.selected)

    def test_malformed_dependencies_preserve_original(self):
        for deps in ([], "web-build", False,
                     {"server-unit": "web-build"}, {"server-unit": None},
                     {"server-unit": [None]}, {"server-unit": ["missing"]},
                     {"missing": ["web-build"]}, {1: []}):
            with self.subTest(deps=deps):
                self.assertRetained(self.safe, prerequisites=deps)

    def test_new_and_priority_suites_are_protected(self):
        rows = self.rows + [suite("brand-new-browser")]
        selected = self.selected | {"brand-new-browser"}
        self.assertEqual(filter_web_family(
            selected, rows, self.safe,
            protected_ids={"brand-new-browser", "web-unit", "web-demo-gate"}),
            self.nonweb | {"brand-new-browser", "web-unit", "web-demo-gate"})
        for protected in (None, "web-unit", [None]):
            self.assertRetained(self.safe, protected_ids=protected)

    def test_unknown_selected_id_is_retained_even_if_catalog_is_stale(self):
        selected = self.selected | {"unknown-new-suite"}
        self.assertEqual(filter_web_family(
            selected, self.rows, self.safe, protected_ids=["unknown-new-suite"]),
            selected)

    def test_unconditional_prerequisite_can_be_protected(self):
        self.assertEqual(filter_web_family(self.selected, self.rows, self.safe,
                                           protected_ids=["web-build"]),
                         self.nonweb | {"web-build"})

    def test_transitive_prerequisite_even_through_unselected_suite(self):
        selected = self.selected - {"bridge"}
        deps = {"server-unit": ["bridge"], "bridge": ["web-build"]}
        self.assertEqual(filter_web_family(selected, self.rows, self.safe,
                                           prerequisites=deps),
                         (self.nonweb - {"bridge"}) | {"web-build"})

    def test_protected_family_prerequisite_and_cycles(self):
        deps = {"web-unit": ["web-build"], "web-build": ["web-unit"]}
        self.assertEqual(filter_web_family(self.selected, self.rows, self.safe,
                                           protected_ids=["web-unit"], prerequisites=deps),
                         self.nonweb | {"web-unit", "web-build"})
        # A cycle solely among removable suites does not manufacture a consumer.
        self.assertEqual(filter_web_family(self.selected, self.rows, self.safe,
                                           prerequisites=deps), self.nonweb)

    def test_nonweb_ids_never_removed_even_with_web_prefix(self):
        rows = self.rows + [suite("web-named-root-check", "."),
                            suite("nested-web-check", "web/subdir")]
        selected = {row["id"] for row in rows}
        self.assertEqual(filter_web_family(selected, rows, self.safe),
                         self.nonweb | {"web-named-root-check", "nested-web-check"})

    def test_reason_and_no_mutation(self):
        before = copy.deepcopy((self.selected, self.rows, self.safe))
        filtered, reason = web_family_decision(self.selected, self.rows, self.safe)
        self.assertEqual(filtered, self.nonweb)
        self.assertIn("removed proven-unaffected", reason)
        self.assertIn("web-build", reason)
        self.assertEqual((self.selected, self.rows, self.safe), before)
        for paths, fragment in ((None, "unavailable"), (["web/src/a.js"], "dependency"),
                                (["new/a.py"], "unknown"), (["../a"], "malformed")):
            result, reason = web_family_decision(self.selected, self.rows, paths)
            self.assertEqual(result, self.selected)
            self.assertIn("retained web family", reason)
            self.assertIn(fragment, reason)


if __name__ == "__main__":
    unittest.main()
