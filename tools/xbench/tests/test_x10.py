"""Synthetic first-blocker coverage; no browser, customer files or network."""

from contextlib import contextmanager
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from decimal import Decimal

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benches import x10
from xbench_core import BenchContext, aggregate


class ClassificationTests(unittest.TestCase):
    def test_pending_catalog_is_never_a_product_failure(self):
        observation = self.observation(ui_text="Checking Solar steps for the selected project drawing version.")
        self.assertEqual(aggregate(x10.classify(observation)), "UNREADABLE")

    def observation(self, **changes):
        value = dict(fresh_project=True, imported_version_selected=True, evidence_saved=True,
                     project_id="00000000-0000-4000-8000-000000000001",
                     ground_option="Ground Mount Physical (preview, unavailable)",
                     attachment_status="Attached [primary drawing] as version 1 to [fresh project]",
                     version_selection_status="Project drawing version selected.")
        value.update(changes)
        return value

    def test_rendered_blocker_requires_canonical_imported_drawing(self):
        rows = x10.classify(self.observation())
        self.assertEqual([row.n for row in rows], list(range(1, 7)))
        self.assertEqual([row.verdict for row in rows], ["FAIL"] + ["UNREADABLE"] * 5)
        self.assertEqual(aggregate(rows), "FAIL")
        for row in rows[1:]:
            self.assertIn("Prerequisite", row.evidence)
            self.assertIn("unimplemented", row.evidence)
        for row, label in zip(rows[1:], ("cut/fill", "BOM reconciliation plus comparison drawing",
                                         "UI rate edit with exact cents", "missing quantities/rates remain unpriced",
                                         "save/reopen/export hashes")):
            self.assertIn(label, row.evidence)

    def test_blank_project_unavailability_is_not_product_failure(self):
        for selected in (False, None, "true"):
            with self.subTest(selected=selected):
                observation = self.observation(imported_version_selected=selected,
                                               import_status="Import attachment timed out")
                self.assertEqual([row.verdict for row in x10.classify(observation)], ["UNREADABLE"] * 6)
        observation.pop("imported_version_selected")
        self.assertEqual(aggregate(x10.classify(observation)), "UNREADABLE")

    def test_infrastructure_absence_and_available_option_never_pass(self):
        for changes in ({"fresh_project": False}, {"evidence_saved": False},
                        {"ground_option": "Ground Mount Physical (preview)"},
                        {"ground_option": ""}, {"ground_option": "Rooftop (unavailable)"}):
            with self.subTest(changes=changes):
                rows = x10.classify(self.observation(**changes))
                self.assertEqual([row.verdict for row in rows], ["UNREADABLE"] * 6)
        for issue in ("auth failed", "selector missing", "timeout", "no fresh project"):
            self.assertEqual(aggregate(x10.classify({"issue": issue})), "UNREADABLE")

    def test_explicit_import_rejection_requires_fresh_project_and_evidence(self):
        observation = self.observation(ground_option="", import_rejection="Drawing extraction failed.")
        self.assertEqual(x10.classify(observation)[0].verdict, "FAIL")
        observation["fresh_project"] = False
        self.assertEqual(x10.classify(observation)[0].verdict, "UNREADABLE")

    def test_evidence_scrubs_credentials_urls_and_markup(self):
        text = x10.safe_text("Ground Mount Physical (unavailable)\nBearer abc123\n"
                             "https://example.test/?secret=value\n<pre>private</pre>")
        self.assertIn("Ground Mount Physical (unavailable)", text)
        for forbidden in ("abc123", "https://", "<pre>", "secret=value"):
            self.assertNotIn(forbidden, text)


class FixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        fixtures = []
        for role in x10.ROLES:
            path = root / (role + ".json")
            data = {"synthetic": True}
            if role == "oracle":
                data = {"expected": {"synthetic_count": 7, "rate_total": "0.07"}}
            elif role == "rates":
                data = {"items": [{"quantity": "0.1", "unit_rate": "0.2"},
                                  {"quantity": "0.5", "unit_rate": "0.1"}]}
            path.write_text(json.dumps(data), encoding="utf-8")
            fixtures.append(dict(id="synthetic-" + role, role=role, path=str(path),
                                 bytes=path.stat().st_size, sha256="a" * 64))
        manifest = {"fixtures": fixtures, "x10": {
            "project_name": "Synthetic probe",
            "fixtures": {row["role"]: row["id"] for row in fixtures},
        }}
        # Core verification is a prerequisite; these are synthetic core receipts.
        self.ctx = BenchContext("X10", manifest, "https://example.test", root / "out",
                                "b" * 40, [dict(row, ok=True) for row in fixtures])

    def test_fixture_oracle_and_exact_decimal_arithmetic(self):
        _, bound, expected, total = x10.bindings(self.ctx)
        self.assertEqual(set(bound), set(x10.ROLES))
        self.assertEqual(expected, {"synthetic_count": 7, "rate_total": "0.07"})
        self.assertEqual(total, Decimal("0.07"))
        self.assertIsInstance(total, Decimal)

    def test_bad_binding_or_receipt_prevents_browser(self):
        for mode in ("config", "missing", "role", "hash", "unverified", "duplicate"):
            with self.subTest(mode=mode):
                original = json.loads(json.dumps(self.ctx.manifest))
                receipts = [dict(row) for row in self.ctx.fixtures]
                if mode == "config":
                    del self.ctx.manifest["x10"]
                elif mode == "missing":
                    self.ctx.manifest["x10"]["fixtures"].pop("terrain")
                elif mode == "role":
                    self.ctx.manifest["fixtures"][0]["role"] = "terrain"
                elif mode == "hash":
                    self.ctx.fixtures[0]["sha256"] = "c" * 64
                elif mode == "unverified":
                    self.ctx.fixtures[0]["ok"] = False
                else:
                    self.ctx.manifest["x10"]["fixtures"]["terrain"] = "synthetic-drawing"
                with patch.object(x10, "signed_in") as session:
                    self.assertEqual([row.verdict for row in x10.run(self.ctx)], ["UNREADABLE"] * 6)
                    session.assert_not_called()
                self.ctx.manifest = original
                self.ctx.fixtures = receipts

    def test_observer_result_and_session_cleanup(self):
        closed = []
        page = object()

        @contextmanager
        def session(repo, source, *, origin):
            self.assertEqual(source, self.ctx.source_sha)
            self.assertEqual(origin, self.ctx.base)
            try:
                yield page, {}
            finally:
                closed.append(True)

        observed = dict(fresh_project=True, imported_version_selected=True, evidence_saved=True,
                        ground_option="Ground Mount Physical (preview, unavailable)")
        with patch.object(x10, "signed_in", session), patch.object(x10, "observe", return_value=observed):
            rows = x10.run(self.ctx)
        self.assertEqual(rows[0].verdict, "FAIL")
        self.assertEqual(rows[0].observed["fixture_rate_total"], "0.07")
        self.assertEqual(closed, [True])
        with patch.object(x10, "signed_in", session), patch.object(x10, "observe", side_effect=TimeoutError):
            self.assertEqual(aggregate(x10.run(self.ctx)), "UNREADABLE")
        self.assertEqual(closed, [True, True])

    def test_invalid_rate_inputs_and_oracle_do_not_open_browser(self):
        for value in (float("nan"), "NaN", "-1", 0.1, "Infinity"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                x10.rate_total({"items": [{"quantity": "1", "unit_rate": value}]})
        oracle = Path(self.ctx.manifest["fixtures"][-1]["path"])
        oracle.write_text('{"expected": {}}', encoding="utf-8")
        with patch.object(x10, "signed_in") as session:
            self.assertEqual(aggregate(x10.run(self.ctx)), "UNREADABLE")
            session.assert_not_called()

    def test_import_failure_never_probes_or_preserves_blank_project_blocker(self):
        def create(page, ctx, config, observation):
            observation.update(fresh_project=True, project_id="synthetic-project")

        config, bound, _, _ = x10.bindings(self.ctx)
        with patch.object(x10, "_prepare_workspace"), patch.object(x10, "_create_project", create), \
                patch.object(x10, "_import_primary", side_effect=TimeoutError("synthetic private-provider-body")), \
                patch.object(x10, "_ground") as ground:
            observed = x10.observe(object(), self.ctx, config, bound)
        ground.assert_not_called()
        self.assertEqual(x10.classify(observed)[0].verdict, "UNREADABLE")
        diagnostic = (self.ctx.out_dir / "x10-ui.json").read_text()
        self.assertIn("TimeoutError", diagnostic)
        self.assertIn("primary drawing attachment and version selection", diagnostic)
        self.assertNotIn("private-provider-body", diagnostic)

    def test_observer_captures_only_after_imported_version_selection(self):
        events = []

        def create(page, ctx, config, observation):
            events.append("create")
            observation["fresh_project"] = True

        def attach(page, ctx, config, bound, observation):
            events.append("select imported version")
            observation["imported_version_selected"] = True

        def ground(page, ctx, observation):
            events.append("ground")
            self.assertTrue(observation["imported_version_selected"])
            self.assertEqual(observation["source"], "d" * 40)
            observation.update(ground_option="Ground Mount Physical (unavailable)", evidence_saved=True)

        config, bound, _, _ = x10.bindings(self.ctx)
        with patch.object(x10, "_prepare_workspace", side_effect=lambda page: events.append("profile")), \
                patch.object(x10, "_create_project", create), patch.object(x10, "_import_primary", attach), \
                patch.object(x10, "_open_workflow", side_effect=lambda *args: events.append("workflow")), \
                patch.object(x10, "_ground", ground):
            observed = x10.observe(object(), self.ctx, config, bound,
                                   session_info={"source": "d" * 40, "app_header_names": ["private"]})
        self.assertEqual(events, ["profile", "create", "select imported version", "workflow", "ground"])
        self.assertNotIn("app_header_names", observed)
        self.assertEqual(x10.classify(observed)[0].verdict, "FAIL")

    def test_material_upload_and_board_preview_use_normal_controls(self):
        page = MagicMock()
        project, material, preview, attachment, marker, upload = [MagicMock() for _ in range(6)]
        project.get_attribute.return_value = "true"
        attachment.inner_text.return_value = "Attached synthetic.dwg as version 1 to Synthetic probe"
        marker.inner_text.return_value = "Project drawing version selected."
        statuses = MagicMock()
        statuses.filter.side_effect = [attachment, marker]

        def role(kind, **kwargs):
            if kind == "status":
                return statuses
            return {"Project": project, "Material": material, "Preview v1 in the drawing": preview}[kwargs["name"]]

        page.get_by_role.side_effect = role
        page.get_by_label.return_value = upload
        config, bound, _, _ = x10.bindings(self.ctx)
        observation = {}
        x10._import_primary(page, self.ctx, config, bound, observation)
        page.get_by_label.assert_called_once_with("Drawing file", exact=True)
        page.locator.assert_not_called()
        upload.set_input_files.assert_called_once_with(str(bound["drawing"]), timeout=15_000)
        attachment.wait_for.assert_called_once_with(state="visible", timeout=180_000)
        preview.click.assert_called_once()
        marker.wait_for.assert_called_once()
        self.assertTrue(observation["imported_version_selected"])
        self.assertEqual(observation["version_selection_status"], "Project drawing version selected.")
        self.assertNotIn("synthetic.dwg", observation["attachment_status"])
        self.assertNotIn("Synthetic probe", observation["attachment_status"])

    def test_screenshot_failure_does_not_leave_scorable_evidence(self):
        def create(page, ctx, config, observation):
            observation["fresh_project"] = True

        def attach(page, ctx, config, bound, observation):
            observation["imported_version_selected"] = True

        page = MagicMock()
        picker = page.get_by_role.return_value
        picker.locator.return_value.inner_text.return_value = "Ground Mount Physical (unavailable)"
        picker.locator.return_value.all_text_contents.return_value = ["Ground Mount Physical (unavailable)"]
        picker.inner_text.return_value = "Ground Mount Physical (unavailable)"
        picker.screenshot.side_effect = TimeoutError("private diagnostic")
        config, bound, _, _ = x10.bindings(self.ctx)
        with patch.object(x10, "_prepare_workspace"), patch.object(x10, "_create_project", create), \
                patch.object(x10, "_import_primary", attach), patch.object(x10, "_open_workflow"):
            observed = x10.observe(page, self.ctx, config, bound)
        self.assertEqual(observed["ground_option"], "Ground Mount Physical (unavailable)")
        self.assertFalse(observed["evidence_saved"])
        self.assertEqual(x10.classify(observed)[0].verdict, "UNREADABLE")
        diagnostic = json.loads((self.ctx.out_dir / "x10-ui.json").read_text())
        self.assertEqual(diagnostic["stage"], "rendered Ground Physical evidence")
        self.assertNotIn("private diagnostic", json.dumps(diagnostic))

    def test_workflow_toggle_only_expands_closed_seat(self):
        for expanded in ("false", "true"):
            with self.subTest(expanded=expanded):
                page = MagicMock()
                button = page.get_by_role.return_value
                button.is_visible.return_value = False
                toggle = MagicMock()
                toggle.is_visible.return_value = True
                toggle.get_attribute.return_value = expanded
                page.get_by_role.side_effect = lambda role, **kw: toggle if kw.get("name") == "Solar flow tools" else button
                x10._open_workflow(page, {})
                self.assertEqual(toggle.click.call_count, 1 if expanded == "false" else 0)


if __name__ == "__main__":
    unittest.main()
