"""Synthetic first-blocker coverage; no browser, customer files or network."""

from contextlib import contextmanager, nullcontext
import hashlib
import json
import re
import stat
from pathlib import Path
import sys
import tempfile
import struct
import unittest
from unittest.mock import MagicMock, patch
from decimal import Decimal
from types import SimpleNamespace
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benches import x10
from xbench_core import BenchContext, aggregate

PROJECT = "00000000-0000-4000-8000-000000000001"
VERSION = "00000000-0000-4000-8000-000000000002"
OTHER = "00000000-0000-4000-8000-000000000003"
DRAWING = "00000000-0000-4000-8000-000000000004"


def tiny_png(width=1, height=1, depth=8, color=2, compression=0, filtering=0,
             interlace=0, raw=b"\x00\x00\x00\x00", compressed=None, palette=None):
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height,
                depth, color, compression, filtering, interlace))
            + (chunk(b"PLTE", palette) if palette is not None else b"")
            + chunk(b"IDAT", zlib.compress(raw) if compressed is None else compressed)
            + chunk(b"IEND", b""))


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
        observation = self.observation(ground_option="", imported_version_selected=False,
                                       import_rejection=x10.IMPORT_REFUSALS[0])
        self.assertEqual(x10.classify(observation)[0].verdict, "FAIL")
        observation["fresh_project"] = False
        self.assertEqual(x10.classify(observation)[0].verdict, "UNREADABLE")

    def test_contradictory_import_evidence_is_always_unreadable(self):
        for fresh in (True, False):
            for saved in (True, False):
                for label in ("Ground Physical unavailable", "Ground Physical available", ""):
                    with self.subTest(fresh=fresh, saved=saved, label=label):
                        rows = x10.classify(self.observation(fresh_project=fresh,
                            evidence_saved=saved, ground_option=label, import_rejection="Rejected"))
                        self.assertEqual([row.verdict for row in rows], ["UNREADABLE"] * 6)

    def test_evidence_scrubs_credentials_urls_and_markup(self):
        text = x10.safe_text("Ground Mount Physical (unavailable)\nBearer abc123\n"
                             "https://example.test/?secret=value\n<pre>private</pre>")
        self.assertIn("Ground Mount Physical (unavailable)", text)
        for forbidden in ("abc123", "https://", "<pre>", "secret=value"):
            self.assertNotIn(forbidden, text)

    def test_wrong_typed_observations_and_unrecognized_refusals_are_unreadable(self):
        for value in (None, [], 4, "observation", object()):
            self.assertEqual([r.verdict for r in x10.classify(value)], ["UNREADABLE"] * 6)
        for field in ("ui_text", "ground_option", "issue", "evidence_path", "attachment_status",
                      "version_selection_status", "import_status", "import_rejection"):
            for value in (None, [], {}, 4, True):
                with self.subTest(field=field, value=value):
                    rows = x10.classify(self.observation(**{field: value}))
                    self.assertEqual([r.verdict for r in rows], ["UNREADABLE"] * 6)
        for field in ("fresh_project", "evidence_saved", "imported_version_selected"):
            for value in (None, [], 1, "true"):
                self.assertEqual([r.verdict for r in x10.classify(self.observation(**{field: value}))],
                                 ["UNREADABLE"] * 6)
        for hint in ("Rejected", "Drawing extraction failed.", "arbitrary sentence", 42, []):
            rows = x10.classify(self.observation(import_rejection=hint, imported_version_selected=False))
            self.assertEqual([r.verdict for r in rows], ["UNREADABLE"] * 6)
        for hint in x10.IMPORT_REFUSALS:
            observation = self.observation(import_rejection=hint, imported_version_selected=False)
            self.assertEqual(x10.classify(observation)[0].verdict, "FAIL")
            for field in ("fresh_project", "evidence_saved"):
                self.assertEqual(aggregate(x10.classify(dict(observation, **{field: False}))), "UNREADABLE")
        self.assertEqual(x10.classify(self.observation(import_rejection=""))[0].verdict, "FAIL")


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
                                 bytes=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
        manifest = {"fixtures": fixtures, "x10": {
            "project_name": "Synthetic probe",
            "fixtures": {row["role"]: row["id"] for row in fixtures},
        }}
        # Core verification is a prerequisite; these are synthetic core receipts.
        self.ctx = BenchContext("X10", manifest, "https://example.test", root / "out",
                                "b" * 40, [dict(row, ok=True) for row in fixtures])
        self.session_info = {key: self.ctx.source_sha for key in ("source", "web_build")}

    def replace_fixture(self, role, data):
        row = next(row for row in self.ctx.manifest["fixtures"] if row["role"] == role)
        Path(row["path"]).write_bytes(data)
        row.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        proof = next(proof for proof in self.ctx.fixtures if proof["id"] == row["id"])
        proof.update(bytes=row["bytes"], sha256=row["sha256"])

    def catalog_request(self, project=PROJECT, version=VERSION, origin=None, drawing=DRAWING, method="GET"):
        return SimpleNamespace(method=method, url=(origin or self.ctx.base) + "/api/capabilities?drawing_id="
                               + drawing + "&project_id=" + project + "&input_version_id=" + version)

    def complete_project(self, listener, project=PROJECT, sequence=1, versions=None):
        request = SimpleNamespace(method="GET", url=self.ctx.base + "/api/projects/" + project)
        listener(request)
        response = SimpleNamespace(request=request, url=request.url, status=200,
            json=lambda: {"project": {"id": project}, "drawing_versions": versions if versions is not None else [
                {"version_id": VERSION, "drawing_id": DRAWING, "seq": sequence}]})
        listener.response(response)

    def emit_project(self, page, project=PROJECT):
        callbacks = dict(call.args for call in page.on.call_args_list)
        listener = lambda request: callbacks["request"](request)
        listener.response = callbacks["response"]
        self.complete_project(listener, project)

    def project_identity(self, sequence=1):
        observation = dict(fresh_project=True, project_id=PROJECT)
        self.complete_project(x10._project_observer(self.ctx.base, observation), sequence=sequence)
        return observation

    def identity(self, page):
        observation = self.project_identity()
        observation.update(imported_version_id=VERSION, imported_drawing_id=DRAWING)
        listener = x10._catalog_observer(self.ctx.base, observation)
        page.on("request", listener)
        self.complete_catalog(listener)
        page.locator.return_value.is_visible.return_value = True
        page.locator.return_value.inner_text.return_value = VERSION
        return observation, listener

    def complete_catalog(self, listener, request=None, status=200):
        request = request or self.catalog_request()
        listener(request)
        listener.response(SimpleNamespace(request=request, url=request.url, status=status))

    def emit_catalog(self, page):
        listeners = dict(call.args for call in page.on.call_args_list)
        request = self.catalog_request()
        listeners["request"](request)
        listeners["response"](SimpleNamespace(request=request, url=request.url, status=200))

    def test_post_verification_mutations_do_not_open_browser(self):
        for row in self.ctx.manifest["fixtures"]:
            path = Path(row["path"])
            original = path.read_bytes()
            for replacement in (bytes([original[0] ^ 1]) + original[1:], original + b" "):
                with self.subTest(role=row["role"], size=len(replacement)):
                    path.write_bytes(replacement)
                    with patch.object(x10, "signed_in") as session:
                        self.assertEqual([r.verdict for r in x10.run(self.ctx)], ["UNREADABLE"] * 6)
                        session.assert_not_called()
                    path.write_bytes(original)

    def test_invalid_utf8_with_matching_receipts_does_not_open_browser(self):
        for role in ("oracle", "rates"):
            row = next(row for row in self.ctx.manifest["fixtures"] if row["role"] == role)
            original = Path(row["path"]).read_bytes()
            with self.subTest(role=role):
                self.replace_fixture(role, b'{"invalid": "\xff"}')
                with patch.object(x10, "signed_in") as session:
                    self.assertEqual([r.verdict for r in x10.run(self.ctx)], ["UNREADABLE"] * 6)
                    session.assert_not_called()
                self.replace_fixture(role, original)

    def test_provenance_rejection_precedes_all_ui_mutations(self):
        config, bound, _, _ = x10.bindings(self.ctx)
        for key in ("source", "web_build"):
            for value in (None, "", 123, "B" * 40, "c" * 40, "b" * 39,
                          "b" * 40 + "\n", "secret customer-name"):
                info = dict(self.session_info)
                if value is None:
                    del info[key]
                else:
                    info[key] = value
                with self.subTest(key=key, value=value), \
                        patch.object(x10, "_prepare_workspace") as prepare, \
                        patch.object(x10, "_create_project") as create, \
                        patch.object(x10, "_import_primary") as attach, \
                        patch.object(x10, "_open_workflow") as workflow, \
                        patch.object(x10, "_ground") as ground:
                    observed = x10.observe(object(), self.ctx, config, bound, info)
                    self.assertEqual([r.verdict for r in x10.classify(observed)], ["UNREADABLE"] * 6)
                    for action in (prepare, create, attach, workflow, ground):
                        action.assert_not_called()
                    saved = (self.ctx.out_dir / "x10-ui.json").read_text()
                    self.assertEqual(observed["stage"], "session provenance")
                    self.assertNotIn("source", observed)
                    self.assertNotIn("web_build", observed)
                    if isinstance(value, str) and value:
                        self.assertNotIn(value, saved)

    def test_fixture_oracle_and_exact_decimal_arithmetic(self):
        _, bound, expected, total = x10.bindings(self.ctx)
        self.assertEqual(set(bound), set(x10.ROLES))
        self.assertEqual(expected, {"synthetic_count": 7, "rate_total": "0.07"})
        self.assertEqual(total, Decimal("0.07"))
        self.assertIsInstance(total, Decimal)

    def test_bad_binding_or_receipt_prevents_browser(self):
        for mode in ("config", "missing", "role", "hash", "bytes", "unverified", "duplicate",
                     "manifest-identical", "manifest-conflict", "receipt-identical", "receipt-conflict"):
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
                elif mode == "bytes":
                    self.ctx.fixtures[0]["bytes"] += 1
                elif mode == "unverified":
                    self.ctx.fixtures[0]["ok"] = False
                elif mode == "duplicate":
                    self.ctx.manifest["x10"]["fixtures"]["terrain"] = "synthetic-drawing"
                else:
                    rows = self.ctx.manifest["fixtures"] if mode.startswith("manifest") else self.ctx.fixtures
                    duplicate = dict(rows[0])
                    if mode.endswith("conflict"):
                        duplicate["sha256"] = "c" * 64
                    rows.append(duplicate)
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
                yield page, self.session_info
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
        self.replace_fixture("oracle", b'{"expected": {}}')
        with patch.object(x10, "signed_in") as session:
            self.assertEqual(aggregate(x10.run(self.ctx)), "UNREADABLE")
            session.assert_not_called()

    def assert_binding_refusal(self):
        with patch.object(x10, "signed_in") as session:
            self.assertEqual([row.verdict for row in x10.run(self.ctx)], ["UNREADABLE"] * 6)
            session.assert_not_called()

    def test_strict_json_rejects_duplicates_and_constants_at_every_depth(self):
        for role in ("oracle", "rates"):
            row = next(row for row in self.ctx.manifest["fixtures"] if row["role"] == role)
            original = Path(row["path"]).read_bytes()
            prefix = original[:-1] + b', "ignored": '
            invalid = [b'{"a": 1, "a": 2}', b'[{"deep": {"a": 1, "a": 2}}]']
            invalid += [value for token in (b"NaN", b"Infinity", b"-Infinity")
                        for value in (token, b'[{"deep": ' + token + b'}]')]
            for value in invalid:
                with self.subTest(role=role, value=value):
                    self.replace_fixture(role, prefix + value + b'}')
                    self.assert_binding_refusal()
            self.replace_fixture(role, original)

    def test_fixture_row_containers_and_fields_are_validated_before_indexing(self):
        manifest = json.loads(json.dumps(self.ctx.manifest))
        receipts = [dict(row) for row in self.ctx.fixtures]
        bad_fields = {"id": [None, "", " ", "a" * 201, [], {}, 4],
                      "path": [None, "", [], 5], "bytes": [None, True, -1, 1.0, "1"],
                      "sha256": [None, [], "A" * 64, "a" * 63, "a" * 64 + "\n"]}
        for target in ("manifest", "receipt"):
            for value in (None, {}, "rows", 1):
                with self.subTest(target=target, container=value):
                    if target == "manifest":
                        self.ctx.manifest["fixtures"] = value
                    else:
                        self.ctx.fixtures = value
                    self.assert_binding_refusal()
                    self.ctx.manifest = json.loads(json.dumps(manifest))
                    self.ctx.fixtures = [dict(row) for row in receipts]
            for value in (None, [], "row", 1):
                rows = self.ctx.manifest["fixtures"] if target == "manifest" else self.ctx.fixtures
                original = rows[0]
                rows[0] = value
                self.assert_binding_refusal()
                rows[0] = original
            fields = dict(bad_fields, **({"role": [None, [], 1]} if target == "manifest" else {}))
            for key, values in fields.items():
                for missing, value in [(True, None)] + [(False, value) for value in values]:
                    with self.subTest(target=target, key=key, missing=missing, value=value):
                        rows = self.ctx.manifest["fixtures"] if target == "manifest" else self.ctx.fixtures
                        original = dict(rows[0])
                        if missing:
                            rows[0].pop(key)
                        else:
                            rows[0][key] = value
                        self.assert_binding_refusal()
                        rows[0] = original

    def test_json_byte_depth_and_string_boundaries(self):
        path = Path(self.temp.name) / "bounded.json"
        data = b'{"expected":{"ok":1}}'
        path.write_bytes(data + b" " * (x10.MAX_JSON_BYTES - len(data)))
        self.assertEqual(x10._strict_json(path), {"expected": {"ok": 1}})
        path.write_bytes(data + b" " * (x10.MAX_JSON_BYTES + 1 - len(data)))
        with patch.object(x10.json, "loads") as parser, self.assertRaises(ValueError):
            x10._strict_json(path)
        parser.assert_not_called()
        path.write_bytes(b"[" * x10.MAX_JSON_DEPTH + b"0" + b"]" * x10.MAX_JSON_DEPTH)
        x10._strict_json(path)
        path.write_bytes(b"[" * (x10.MAX_JSON_DEPTH + 1) + b"0" + b"]" * (x10.MAX_JSON_DEPTH + 1))
        with patch.object(x10.json, "loads") as parser, self.assertRaises(ValueError):
            x10._strict_json(path)
        parser.assert_not_called()
        control = {"escaped": '[{]}' * 100 + '\\"[{]}' + '\\\\', "nested": [{"ok": True}]}
        path.write_text(json.dumps(control), encoding="utf-8")
        self.assertEqual(x10._strict_json(path), control)

    def test_budget_refusals_precede_browser_and_decimal_accumulation(self):
        for data in (b" " * (x10.MAX_JSON_BYTES + 1),
                     b"[" * (x10.MAX_JSON_DEPTH + 1) + b"0" + b"]" * (x10.MAX_JSON_DEPTH + 1)):
            self.replace_fixture("oracle", data)
            self.assert_binding_refusal()
        items = [{"quantity": "1", "unit_rate": "0.01"}] * x10.MAX_RATE_ITEMS
        self.assertEqual(x10.rate_total({"items": items}), Decimal(x10.MAX_RATE_ITEMS) / 100)
        with patch.object(x10, "Decimal") as decimal, self.assertRaises(ValueError):
            x10.rate_total({"items": items + [items[0]]})
        decimal.assert_not_called()
        self.replace_fixture("oracle", b'{"expected":{"ok":1}}')
        self.replace_fixture("rates", json.dumps({"items": items + [items[0]]}).encode())
        self.assert_binding_refusal()

    def test_import_failure_never_probes_or_preserves_blank_project_blocker(self):
        def create(page, ctx, config, observation):
            observation.update(fresh_project=True, project_id="synthetic-project")

        config, bound, _, _ = x10.bindings(self.ctx)
        with patch.object(x10, "_prepare_workspace"), patch.object(x10, "_create_project", create), \
                patch.object(x10, "_import_primary", side_effect=TimeoutError("synthetic private-provider-body")), \
                patch.object(x10, "_ground") as ground:
            observed = x10.observe(MagicMock(), self.ctx, config, bound, self.session_info)
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
            observation.update(fresh_project=True, project_id=PROJECT)

        def attach(page, ctx, config, bound, observation):
            events.append("select imported version")
            observation.update(imported_version_selected=True, imported_version_id=VERSION, imported_drawing_id=DRAWING)
            self.emit_project(page)
            self.emit_catalog(page)

        def ground(page, ctx, observation):
            events.append("ground")
            x10._current_identity(page, observation)
            self.assertTrue(observation["imported_version_selected"])
            self.assertEqual(observation["source"], self.ctx.source_sha)
            self.assertEqual(observation["web_build"], self.ctx.source_sha)
            observation.update(ground_option="Ground Mount Physical (unavailable)", evidence_saved=True)

        config, bound, _, _ = x10.bindings(self.ctx)
        page = MagicMock()
        page.locator.return_value.is_visible.return_value = True
        page.locator.return_value.inner_text.return_value = VERSION
        with patch.object(x10, "_prepare_workspace", side_effect=lambda page: events.append("profile")), \
                patch.object(x10, "_create_project", create), patch.object(x10, "_import_primary", attach), \
                patch.object(x10, "_open_workflow", side_effect=lambda *args: events.append("workflow")), \
                patch.object(x10, "_ground", ground):
            observed = x10.observe(page, self.ctx, config, bound,
                                   session_info=dict(self.session_info, app_header_names=["private"]))
        self.assertEqual(events, ["profile", "create", "select imported version", "workflow", "ground"])
        self.assertNotIn("app_header_names", observed)
        self.assertEqual(x10.classify(observed)[0].verdict, "FAIL")

    def import_page(self, sentence):
        page = MagicMock()
        project, material, preview, attachment, marker, upload = [MagicMock() for _ in range(6)]
        project.get_attribute.return_value = "true"
        attachment.inner_text.return_value = sentence
        attachment.is_visible.side_effect = lambda: upload.set_input_files.called
        marker.inner_text.return_value = "Project drawing version selected."
        marker.is_visible.side_effect = [False, True]
        preview.locator.return_value.get_attribute.return_value = "version:" + VERSION
        statuses = MagicMock()
        statuses.filter.return_value = marker
        intake = page.locator.return_value
        intake.get_by_role.return_value.filter.return_value = attachment
        intake.get_by_text.return_value.is_visible.return_value = False
        intake.filter.return_value.inner_text.return_value = VERSION

        def role(kind, **kwargs):
            if kind == "status":
                return statuses
            if isinstance(kwargs.get("name"), re.Pattern):
                return project
            if kwargs["name"].startswith("Preview v"):
                return preview
            return {"Project": project, "Material": material}[kwargs["name"]]

        page.get_by_role.side_effect = role
        page.get_by_label.return_value = upload
        return page, preview, attachment, marker, upload

    def test_material_upload_and_board_preview_use_normal_controls(self):
        config, bound, _, _ = x10.bindings(self.ctx)
        for sequence in (1, 2):
            with self.subTest(sequence=sequence):
                page, preview, attachment, marker, upload = self.import_page(
                    "Attached synthetic.dwg as version " + str(sequence) + " to Synthetic probe")
                observation = self.project_identity(sequence)
                x10._import_primary(page, self.ctx, config, bound, observation)
                page.get_by_label.assert_called_once_with("Drawing file", exact=True)
                page.get_by_role.assert_any_call("button", name="Preview v" + str(sequence) + " in the drawing", exact=True)
                upload.set_input_files.assert_called_once_with(str(bound["drawing"]), timeout=15_000)
                attachment.or_.return_value.first.wait_for.assert_called_once_with(state="visible", timeout=180_000)
                preview.click.assert_called_once()
                marker.wait_for.assert_called_once()
                self.assertTrue(observation["imported_version_selected"])
                self.assertEqual(observation["selected_sequence"], sequence)
                self.assertNotIn("synthetic.dwg", observation["attachment_status"])
                self.assertNotIn("Synthetic probe", observation["attachment_status"])

    def test_malformed_versions_refuse_before_selection(self):
        config, bound, _, _ = x10.bindings(self.ctx)
        sentences = ["Attached synthetic.dwg to Synthetic probe"]
        sentences += ["Attached synthetic.dwg as version " + value + " to Synthetic probe"
                      for value in ("0", "-1", "+1", "1.2", "1e2", "1junk", "1234567890", "01", "")]
        for sentence in sentences:
            with self.subTest(sentence=sentence):
                page, preview, _, _, _ = self.import_page(sentence)
                observation = dict(fresh_project=True, evidence_saved=True)
                with self.assertRaises(ValueError):
                    x10._import_primary(page, self.ctx, config, bound, observation)
                preview.click.assert_not_called()
                self.assertEqual(aggregate(x10.classify(observation)), "UNREADABLE")

    def test_stale_marker_and_wrong_viewer_cannot_prove_selection(self):
        config, bound, _, _ = x10.bindings(self.ctx)
        for mode in ("stale", "missing-after-click", "wrong-viewer", "invalid-preview"):
            with self.subTest(mode=mode):
                page, preview, _, marker, _ = self.import_page(
                    "Attached synthetic.dwg as version 2 to Synthetic probe")
                if mode == "stale":
                    marker.is_visible.side_effect = [True]
                elif mode == "missing-after-click":
                    marker.is_visible.side_effect = [False, False]
                elif mode == "wrong-viewer":
                    page.locator.return_value.filter.return_value.inner_text.return_value = OTHER
                else:
                    preview.locator.return_value.get_attribute.return_value = "version:invalid"
                observation = self.project_identity(2)
                with self.assertRaises(ValueError):
                    x10._import_primary(page, self.ctx, config, bound, observation)
                self.assertFalse(observation["imported_version_selected"])
                if mode in ("stale", "invalid-preview"):
                    preview.click.assert_not_called()

    def test_catalog_listener_binds_real_preview_and_rejects_switches(self):
        config, bound, _, _ = x10.bindings(self.ctx)
        for mode in ("matching", "project-switch", "version-switch", "drawing-switch", "switch-back", "viewer-switch", "missing",
                     "capture-project", "capture-version", "capture-switch-back", "capture-flow",
                     "capture-label", "capture-pending", "capture-failed", "capture-request"):
            with self.subTest(mode=mode):
                page, preview, _, _, _ = self.import_page(
                    "Attached synthetic.dwg as version 1 to Synthetic probe")
                callbacks = {}
                page.on.side_effect = lambda event, callback: callbacks.update({event: callback})
                page.remove_listener.side_effect = lambda event, callback: callbacks.pop(event)
                viewer = page.locator.return_value
                viewer.is_visible.return_value = True
                viewer.inner_text.return_value = VERSION
                def emit(project=PROJECT, version=VERSION, drawing=DRAWING):
                    request = self.catalog_request(project, version, drawing=drawing)
                    callbacks["request"](request)
                    callbacks["response"](SimpleNamespace(request=request, url=request.url, status=200))
                preview.click.side_effect = lambda **kw: emit() if mode != "missing" else None
                rail, picker = MagicMock(), MagicMock()
                picker.input_value.return_value = "ground-physical"
                picker.locator.return_value.inner_text.return_value = "Ground Physical unavailable"
                rail.inner_text.return_value = "Ready"
                def capture(**kwargs):
                    Path(kwargs["path"]).write_bytes(tiny_png())
                    if mode == "capture-project":
                        emit(project=OTHER)
                    elif mode == "capture-version":
                        emit(version=OTHER)
                    elif mode == "capture-switch-back":
                        emit(project=OTHER)
                        emit()
                    elif mode == "capture-flow":
                        picker.input_value.return_value = "rooftop"
                    elif mode == "capture-label":
                        picker.locator.return_value.inner_text.return_value = "Ground Physical available"
                    elif mode == "capture-pending":
                        rail.inner_text.side_effect = None
                        rail.inner_text.return_value = "Checking Solar steps for the selected project drawing version."
                    elif mode in ("capture-failed", "capture-request"):
                        request = self.catalog_request()
                        callbacks["request"](request)
                        if mode == "capture-failed":
                            callbacks["requestfailed"](request)
                rail.screenshot.side_effect = capture

                def workflow(page, config):
                    page.get_by_role.side_effect = lambda role, **kw: rail if role == "navigation" else picker

                def selected(*args, **kwargs):
                    if mode == "project-switch":
                        emit(project=OTHER)
                    elif mode == "version-switch":
                        emit(version=OTHER)
                    elif mode == "drawing-switch":
                        emit(drawing=OTHER)
                    elif mode == "viewer-switch":
                        viewer.inner_text.return_value = OTHER

                picker.select_option.side_effect = selected
                samples = []
                def sampled(**kwargs):
                    samples.append(1)
                    if mode == "switch-back" and len(samples) == 2:
                        emit(project=OTHER)
                        emit()
                    return "Ready"
                rail.inner_text.side_effect = sampled
                def create(page, ctx, config, observation):
                    observation.update(fresh_project=True, project_id=PROJECT)
                    self.emit_project(page)
                with patch.object(x10, "_prepare_workspace"), patch.object(x10, "_create_project", create), \
                        patch.object(x10, "_open_workflow", workflow):
                    observed = x10.observe(page, self.ctx, config, bound, self.session_info)
                self.assertEqual(callbacks, {})
                self.assertEqual(page.on.call_count, 3)
                self.assertEqual(page.remove_listener.call_args_list, page.on.call_args_list)
                self.assertEqual(observed["imported_version_id"], VERSION)
                self.assertEqual(aggregate(x10.classify(observed)), "FAIL" if mode == "matching" else "UNREADABLE")
                self.assertEqual(observed["evidence_saved"], mode == "matching")
                for forbidden in ("https://", "headers", "body"):
                    self.assertNotIn(forbidden, json.dumps(observed))

    def test_catalog_listener_ignores_foreign_requests_and_invalidates_bad_identity(self):
        observation = {}
        listener = x10._catalog_observer(self.ctx.base, observation)
        self.complete_catalog(listener)
        epoch = observation["catalog_epoch"]
        listener(self.catalog_request(project=OTHER, origin="https://foreign.test"))
        listener(SimpleNamespace(url=self.ctx.base + "/api/capabilities-extra?project_id=" + OTHER))
        self.assertEqual(observation["catalog_binding"]["project_id"], PROJECT)
        self.assertEqual(observation["catalog_epoch"], epoch)
        for query in ("&project_id=" + OTHER, "&input_version_id=invalid"):
            listener(SimpleNamespace(method="GET", url=self.catalog_request().url + query))
            self.assertIsNone(observation["catalog_binding"])
            self.complete_catalog(listener)
        self.assertGreater(observation["catalog_epoch"], epoch)

    def test_catalog_transport_requires_latest_matching_success(self):
        observation = {}
        listener = x10._catalog_observer(self.ctx.base, observation)
        first, latest = self.catalog_request(), self.catalog_request()
        listener(first)
        self.assertIsNone(observation["catalog_binding"])
        listener(latest)
        listener.response(SimpleNamespace(request=first, url=first.url, status=200))
        self.assertIsNone(observation["catalog_binding"])
        listener.response(SimpleNamespace(request=latest, url=latest.url, status=204))
        self.assertEqual(observation["catalog_binding"]["project_id"], PROJECT)
        epoch = observation["catalog_epoch"]
        listener.failed(first)
        listener.response(SimpleNamespace(request=first, url=first.url, status=503))
        for url in ("https://foreign.test/api/capabilities", self.ctx.base + "/api/capabilities-extra"):
            listener.response(SimpleNamespace(request=latest, url=url, status=500))
            listener.failed(SimpleNamespace(url=url))
        self.assertEqual(observation["catalog_epoch"], epoch)
        self.assertEqual(observation["catalog_binding"]["project_id"], PROJECT)
        for mode in ("non-2xx", "failed", "failure-before-response"):
            with self.subTest(mode=mode):
                request = self.catalog_request()
                self.complete_catalog(listener, request)
                epoch = observation["catalog_epoch"]
                if mode == "non-2xx":
                    listener.response(SimpleNamespace(request=request, url=request.url, status=500))
                else:
                    listener.failed(request)
                    if mode == "failure-before-response":
                        listener.response(SimpleNamespace(request=request, url=request.url, status=200))
                self.assertIsNone(observation["catalog_binding"])
                self.assertGreater(observation["catalog_epoch"], epoch)
        self.assertNotIn("https://", json.dumps(observation))

    def test_stale_attachment_terminals_refuse_before_new_upload(self):
        config, bound, _, _ = x10.bindings(self.ctx)
        for terminal in ("success", "refusal"):
            page, preview, attachment, _, upload = self.import_page(
                "Attached old.dwg as version 1 to Old project")
            attachment.is_visible.side_effect = None
            attachment.is_visible.return_value = terminal == "success"
            page.locator.return_value.get_by_text.return_value.is_visible.return_value = terminal == "refusal"
            observation = dict(fresh_project=True)
            with self.assertRaises(ValueError):
                x10._import_primary(page, self.ctx, config, bound, observation)
            upload.set_input_files.assert_not_called()
            preview.click.assert_not_called()
            self.assertEqual([r.verdict for r in x10.classify(observation)], ["UNREADABLE"] * 6)

    def test_fixture_nonregular_types_are_rejected_before_open(self):
        for mode in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFDIR, stat.S_IFCHR, stat.S_IFBLK):
            with self.subTest(mode=mode), patch.object(Path, "lstat", return_value=SimpleNamespace(st_mode=mode)), \
                    patch.object(x10.os, "open") as opened:
                self.assert_binding_refusal()
                opened.assert_not_called()

    def test_fixture_growing_stream_is_bounded_by_declared_size(self):
        declared = self.ctx.manifest["fixtures"][0]["bytes"]
        source = MagicMock()
        source.read.side_effect = lambda count: b"x" * count
        manager = MagicMock()
        manager.__enter__.return_value = source
        with patch.object(x10.os, "open", return_value=123), \
                patch.object(x10.os, "fdopen", return_value=manager), \
                patch.object(x10.os, "fstat", return_value=SimpleNamespace(st_mode=stat.S_IFREG)):
            self.assert_binding_refusal()
        source.read.assert_called_once_with(declared + 1)
        source.read.reset_mock()
        with patch.object(x10.os, "open", return_value=123), \
                patch.object(x10.os, "fdopen", return_value=manager), \
                patch.object(x10.os, "fstat", return_value=SimpleNamespace(st_mode=stat.S_IFIFO)):
            self.assert_binding_refusal()
        source.read.assert_not_called()

    def test_growing_screenshot_read_is_explicitly_bounded_and_unscorable(self):
        page = MagicMock()
        real_open = Path.open
        def screenshot(**kwargs):
            with real_open(Path(kwargs["path"]), "wb") as target:
                target.write(tiny_png())
        page.get_by_role.return_value.screenshot.side_effect = screenshot
        source = MagicMock()
        source.read.return_value = b"x" * (x10.MAX_SCREENSHOT_BYTES + 1)
        manager = MagicMock()
        manager.__enter__.return_value = source
        observation = dict(fresh_project=True, imported_version_selected=True,
                           ground_option="Ground Physical unavailable")
        with patch.object(Path, "open", return_value=manager), \
                patch.object(x10.zlib, "decompressobj") as decoder, self.assertRaises(ValueError):
            x10._save(page, self.ctx, observation)
        source.read.assert_called_once_with(x10.MAX_SCREENSHOT_BYTES + 1)
        decoder.assert_not_called()
        self.assertFalse(observation["evidence_saved"])
        self.assertEqual(list(self.ctx.out_dir.iterdir()), [])
        self.assertEqual([r.verdict for r in x10.classify(observation)], ["UNREADABLE"] * 6)

    def test_import_rejection_and_timeout_are_distinct(self):
        config, bound, _, _ = x10.bindings(self.ctx)
        for hint in ("Choose the drawing again to retry the upload.",
                     "Retry to attach this drawing to the project."):
            page, preview, attachment, _, upload = self.import_page("")
            attachment.is_visible.side_effect = None
            attachment.is_visible.return_value = False
            intake = page.locator.return_value
            failure = intake.get_by_text.return_value
            failure.is_visible.side_effect = lambda: upload.set_input_files.called
            failure.inner_text.return_value = hint
            secrets = (bound["drawing"].name, str(bound["drawing"]),
                       config["project_name"] + "-customer-project-sentinel")
            intake.get_by_role.return_value.first.inner_text.return_value = "Rejected " + " / ".join(secrets)
            failure.screenshot.side_effect = lambda **kw: Path(kw["path"]).write_bytes(tiny_png())
            observation = self.project_identity()
            self.assertFalse(x10._import_primary(page, self.ctx, config, bound, observation))
            self.assertEqual(observation["import_rejection"], hint)
            for secret in secrets:
                self.assertNotIn(secret, json.dumps(observation))
                saved = next(self.ctx.out_dir.glob(observation["evidence_path"].split("/")[0]
                                                    + "/x10-ui.json")).read_text()
                self.assertNotIn(secret, saved)
                self.assertIn(hint, saved)
            intake.get_by_role.return_value.first.inner_text.assert_not_called()
            failure.screenshot.assert_called_once()
            self.assertEqual(aggregate(x10.classify(observation)), "FAIL")
            preview.click.assert_not_called()
        page, preview, attachment, _, _ = self.import_page("Uploading synthetic.dwg")
        attachment.or_.return_value.first.wait_for.side_effect = TimeoutError
        observation = dict(fresh_project=True)
        with self.assertRaises(TimeoutError):
            x10._import_primary(page, self.ctx, config, bound, observation)
        self.assertNotIn("import_rejection", observation)
        self.assertEqual(aggregate(x10.classify(observation)), "UNREADABLE")

    def test_screenshot_failure_does_not_leave_scorable_evidence(self):
        def create(page, ctx, config, observation):
            observation["fresh_project"] = True

        def attach(page, ctx, config, bound, observation):
            observation["imported_version_selected"] = True

        page = MagicMock()
        identity, listener = self.identity(page)
        picker = page.get_by_role.return_value
        picker.locator.return_value.inner_text.return_value = "Ground Mount Physical (unavailable)"
        picker.input_value.return_value = "ground-physical"
        picker.locator.return_value.all_text_contents.return_value = ["Ground Mount Physical (unavailable)"]
        picker.inner_text.return_value = "Ground Mount Physical (unavailable)"
        picker.screenshot.side_effect = TimeoutError("private diagnostic")
        config, bound, _, _ = x10.bindings(self.ctx)
        def create_identity(page, ctx, config, observation):
            create(page, ctx, config, observation)
            observation.update(project_id=PROJECT, imported_version_id=VERSION, imported_drawing_id=DRAWING)
            self.emit_project(page)
            self.emit_catalog(page)

        with patch.object(x10, "_prepare_workspace"), patch.object(x10, "_create_project", create_identity), \
                patch.object(x10, "_import_primary", attach), patch.object(x10, "_open_workflow"):
            observed = x10.observe(page, self.ctx, config, bound, self.session_info)
        self.assertEqual(observed["ground_option"], "Ground Mount Physical (unavailable)")
        self.assertFalse(observed["evidence_saved"])
        self.assertEqual(x10.classify(observed)[0].verdict, "UNREADABLE")
        diagnostic = json.loads((self.ctx.out_dir / "x10-ui.json").read_text())
        self.assertEqual(diagnostic["stage"], "rendered Ground Physical evidence")
        self.assertNotIn("private diagnostic", json.dumps(diagnostic))

    def test_observed_refusal_excludes_customer_status_and_diagnostics(self):
        config, bound, _, _ = x10.bindings(self.ctx)
        hint = "Choose the drawing again to retry the upload."
        page, preview, attachment, _, upload = self.import_page("")
        attachment.is_visible.side_effect = None
        attachment.is_visible.return_value = False
        intake = page.locator.return_value
        failure = intake.get_by_text.return_value
        failure.is_visible.side_effect = lambda: upload.set_input_files.called
        failure.inner_text.return_value = hint
        customer = config["project_name"] + "-actual-creation-name"
        intake.get_by_role.return_value.first.inner_text.return_value = (
            "Error for " + str(bound["drawing"]) + " in " + customer + " Diagnostic: secret body")
        failure.screenshot.side_effect = lambda **kw: Path(kw["path"]).write_bytes(tiny_png())
        with patch.object(x10, "_prepare_workspace"), \
                patch.object(x10, "_create_project", side_effect=lambda p, c, cfg, o: (
                    o.update(fresh_project=True, project_id=PROJECT), self.emit_project(p))), \
                patch.object(x10, "_ground") as ground:
            observed = x10.observe(page, self.ctx, config, bound, self.session_info)
        ground.assert_not_called()
        preview.click.assert_not_called()
        self.assertEqual(observed["import_rejection"], hint)
        self.assertEqual(aggregate(x10.classify(observed)), "FAIL")
        saved = next(self.ctx.out_dir.glob("x10-evidence-*/x10-ui.json")).read_text()
        for text in (json.dumps(observed), saved):
            self.assertIn(hint, text)
            for secret in (bound["drawing"].name, str(bound["drawing"]), customer,
                           config["project_name"], "secret body"):
                self.assertNotIn(secret, text)
        intake.get_by_role.return_value.first.inner_text.assert_not_called()
        failure.screenshot.assert_called_once()

    def test_save_rejects_crc_correct_invalid_png_content(self):
        invalid = [
            {"width": 0}, {"height": 0}, {"width": 0x80000000}, {"height": 0x80000000},
            {"depth": 0}, {"depth": 3}, {"depth": 4, "color": 2},
            {"depth": 16, "color": 3}, {"depth": 4, "color": 4}, {"depth": 1, "color": 6},
            {"color": 1}, {"color": 5}, {"compression": 1}, {"filtering": 1}, {"interlace": 2},
            {"compressed": b"not zlib"},
            {"compressed": zlib.compress(b"\x00\x00\x00\x00")[:-1] + b"\xff"},
            {"compressed": zlib.compress(b"\x00\x00\x00\x00")[:-1]},
            {"compressed": zlib.compress(b"\x00\x00\x00\x00") + b"garbage"},
            {"compressed": zlib.compress(b"\x00\x00\x00\x00") + zlib.compress(b"")},
            {"raw": b"\x00\x00\x00"}, {"raw": b"\x00" * 5},
            {"raw": b"\x05\x00\x00\x00"},
            {"height": 2, "raw": b"\x00\x00\x00\x00\x05\x00\x00\x00"},
            {"raw": b"\x00" * 100000},
            {"width": 10000, "height": 10000},
            {"color": 3, "raw": b"\x00\x00"},
            {"interlace": 1, "width": 8, "height": 8, "raw": b"\x00" * (25 * 8)},
        ]
        page = MagicMock()
        surface = page.get_by_role.return_value
        for options in invalid:
            with self.subTest(options={key: value if not isinstance(value, bytes) else len(value)
                                      for key, value in options.items()}):
                data = tiny_png(**options)
                surface.screenshot.side_effect = lambda **kw: Path(kw["path"]).write_bytes(data)
                observation = dict(fresh_project=True, imported_version_selected=True,
                                   ground_option="Ground Physical unavailable")
                with self.assertRaises(ValueError):
                    x10._save(page, self.ctx, observation)
                self.assertFalse(observation["evidence_saved"])
                self.assertNotIn("evidence_path", observation)
                self.assertEqual([r.verdict for r in x10.classify(observation)], ["UNREADABLE"] * 6)
                self.assertEqual(list(self.ctx.out_dir.iterdir()), [])

    def test_png_decoded_limit_is_checked_before_decompression(self):
        page = MagicMock()
        page.get_by_role.return_value.screenshot.side_effect = lambda **kw: Path(kw["path"]).write_bytes(
            tiny_png(width=10000, height=10000))
        with patch.object(x10.zlib, "decompressobj") as decoder, self.assertRaises(ValueError):
            x10._save(page, self.ctx, {})
        decoder.assert_not_called()

    def test_valid_screenshot_types_commit_scorable_evidence(self):
        valid = [
            {}, {"color": 6, "raw": b"\x00" * 5},
            {"color": 0, "depth": 1, "raw": b"\x00\x00"},
            {"color": 0, "depth": 16, "raw": b"\x01\x00\x00"},
            {"color": 4, "depth": 16, "raw": b"\x04" + b"\x00" * 4},
            {"color": 2, "depth": 16, "raw": b"\x02" + b"\x00" * 6},
            {"color": 3, "depth": 1, "palette": b"\x00\x00\x00", "raw": b"\x00\x00"},
            {"interlace": 1},
            # Adam7 8x8 RGB: pass (row bytes including filter, row count).
            {"interlace": 1, "width": 8, "height": 8,
             "raw": b"".join(b"\x00" * (stride * count)
                             for stride, count in ((4, 1), (4, 1), (7, 1), (7, 2),
                                                   (13, 2), (13, 4), (25, 4)))},
        ]
        page = MagicMock()
        for options in valid:
            with self.subTest(options=options):
                data = tiny_png(**options)
                page.get_by_role.return_value.screenshot.side_effect = lambda **kw: Path(kw["path"]).write_bytes(data)
                observation = dict(fresh_project=True, imported_version_selected=True,
                                   ground_option="Ground Physical unavailable")
                x10._save(page, self.ctx, observation)
                self.assertTrue(observation["evidence_saved"])
                self.assertEqual(aggregate(x10.classify(observation)), "FAIL")

    def test_creation_requires_post_route_and_open_response_identity(self):
        identity = "00000000-0000-4000-8000-000000000001"
        other = "00000000-0000-4000-8000-000000000002"
        for route_id, active_id in ((identity, identity), (other, identity), (identity, other)):
            with self.subTest(route_id=route_id, active_id=active_id):
                page = MagicMock()
                name = "Synthetic probe-" + "a" * 12
                creation = SimpleNamespace(status=201, request=SimpleNamespace(method="POST"),
                    url=self.ctx.base + "/api/projects", json=lambda: {"project": {"id": identity, "name": name}})
                opened = SimpleNamespace(status=200, request=SimpleNamespace(method="GET"),
                    url=self.ctx.base + "/api/projects/" + route_id,
                    json=lambda: {"project": {"id": active_id, "name": name}, "private": "not retained"})

                def expect(predicate, **kwargs):
                    response = opened if predicate(opened) else creation
                    self.assertTrue(predicate(response))
                    manager = MagicMock()
                    manager.__enter__.return_value.value = response
                    return manager

                page.expect_response.side_effect = expect
                observation = {}
                with patch.object(x10, "uuid4", return_value=SimpleNamespace(hex="a" * 32)):
                    if route_id == active_id == identity:
                        x10._create_project(page, self.ctx, {"project_name": "Synthetic probe"}, observation)
                        self.assertTrue(observation["fresh_project"])
                    else:
                        with self.assertRaises(ValueError):
                            x10._create_project(page, self.ctx, {"project_name": "Synthetic probe"}, observation)
                        self.assertFalse(observation["fresh_project"])
                self.assertNotIn("not retained", json.dumps(observation))
                self.assertNotIn("project_name", observation)

    def test_atomic_evidence_preserves_completed_generations_on_failure(self):
        page = MagicMock()
        surface = page.get_by_role.return_value
        surface.screenshot.side_effect = lambda **kw: Path(kw["path"]).write_bytes(tiny_png())
        observation = dict(fresh_project=True, imported_version_selected=True,
                           ground_option="Ground Mount Physical (unavailable)")
        x10._save(page, self.ctx, observation)
        old = observation["evidence_path"]
        generations = list(self.ctx.out_dir.iterdir())
        self.assertEqual(len(generations), 1)
        saved = json.loads((generations[0] / "x10-ui.json").read_text())
        self.assertTrue(saved["evidence_saved"])
        self.assertIn(generations[0].name, x10.classify(observation)[0].evidence)
        for mode in ("screenshot", "json", "invalid-png"):
            with self.subTest(mode=mode):
                observation.update(evidence_saved=True, evidence_path=old)
                if mode == "screenshot":
                    surface.screenshot.side_effect = OSError("synthetic")
                else:
                    data = tiny_png() if mode == "json" else b"not a PNG"
                    surface.screenshot.side_effect = lambda **kw: Path(kw["path"]).write_bytes(data)
                failure = patch.object(Path, "write_text", side_effect=OSError("synthetic")) if mode == "json" else nullcontext()
                with failure:
                    with self.assertRaises((OSError, ValueError)):
                        x10._save(page, self.ctx, observation)
                self.assertFalse(observation["evidence_saved"])
                self.assertNotIn("evidence_path", observation)
                self.assertEqual(list(self.ctx.out_dir.iterdir()), generations)
                self.assertEqual(aggregate(x10.classify(observation)), "UNREADABLE")

    def test_screenshot_pin_rejects_replacement_before_and_after_rename(self):
        page = MagicMock()
        page.get_by_role.return_value.screenshot.side_effect = lambda **kw: Path(kw["path"]).write_bytes(tiny_png())
        observation = dict(fresh_project=True, imported_version_selected=True,
                           ground_option="Ground Physical unavailable")
        x10._save(page, self.ctx, observation)
        old = list(self.ctx.out_dir.iterdir())
        saved = json.loads((old[0] / "x10-ui.json").read_text())
        self.assertEqual(saved["screenshot_bytes"], len(tiny_png()))
        self.assertEqual(saved["screenshot_sha256"], hashlib.sha256(tiny_png()).hexdigest())
        write_text, rename = Path.write_text, Path.rename
        for phase in ("json-write", "rename"):
            for data in (tiny_png(raw=b"\x00\xff\x00\x00"), b"tampered bytes"):
                with self.subTest(phase=phase, data=data):
                    def written(path, *args, **kwargs):
                        result = write_text(path, *args, **kwargs)
                        (path.parent / "x10-ground.png").write_bytes(data)
                        return result
                    def renamed(path, target):
                        result = rename(path, target)
                        (Path(target) / "x10-ground.png").write_bytes(data)
                        return result
                    failure = (patch.object(Path, "write_text", written) if phase == "json-write"
                               else patch.object(Path, "rename", renamed))
                    with failure, self.assertRaises(ValueError):
                        x10._save(page, self.ctx, observation)
                    self.assertFalse(observation["evidence_saved"])
                    self.assertNotIn("evidence_path", observation)
                    self.assertEqual(list(self.ctx.out_dir.iterdir()), old)
                    self.assertEqual([row.verdict for row in x10.classify(observation)], ["UNREADABLE"] * 6)

    def test_ground_resamples_after_selection_and_catalog_refresh(self):
        unavailable = "Ground Mount Physical (unavailable)"
        available = "Ground Mount Physical (preview)"
        for labels, texts, verdict in (([unavailable, available, available], ["Ready"] * 3, "UNREADABLE"),
                                      ([unavailable] * 2, ["Ready"] * 2, "FAIL")):
            page = MagicMock()
            rail, picker = MagicMock(), MagicMock()
            events = []
            picker.select_option.side_effect = lambda *a, **kw: events.append("select")
            rail.get_by_text.return_value.first.wait_for.side_effect = lambda **kw: events.append("ready")
            page.get_by_role.side_effect = lambda role, **kw: rail if role == "navigation" else picker
            picker.input_value.return_value = "ground-physical"
            picker.locator.return_value.inner_text.side_effect = lambda **kw: labels.pop(0) if len(labels) > 1 else labels[0]
            rail.inner_text.side_effect = lambda **kw: texts.pop(0) if len(texts) > 1 else texts[0]
            observation, listener = self.identity(page)
            observation.update(fresh_project=True, imported_version_selected=True)
            with patch.object(x10, "_save", side_effect=lambda p, c, o, **kw: o.update(evidence_saved=True)):
                x10._ground(page, self.ctx, observation)
            self.assertEqual(observation["ground_option"], labels[-1])
            self.assertEqual(aggregate(x10.classify(observation)), verdict)
            picker.locator.return_value.inner_text.assert_called()
            self.assertGreaterEqual(rail.get_by_text.return_value.first.wait_for.call_count, 3)
            self.assertEqual(events[:3], ["ready", "select", "ready"])
        page = MagicMock()
        picker = page.get_by_role.return_value
        picker.input_value.return_value = "ground-physical"
        picker.locator.return_value.inner_text.return_value = unavailable
        picker.inner_text.return_value = "Checking Solar steps for the selected project drawing version."
        with patch.object(x10, "_save") as save, self.assertRaises(ValueError):
            x10._ground(page, self.ctx, {})
        save.assert_not_called()

    def test_capture_revalidates_ui_and_identity_and_rolls_back_only_new_generation(self):
        page = MagicMock()
        rail, picker = MagicMock(), MagicMock()
        page.get_by_role.side_effect = lambda role, **kw: rail if role == "navigation" else picker
        observation, listener = self.identity(page)
        observation.update(fresh_project=True, imported_version_selected=True)
        rail.screenshot.side_effect = lambda **kw: Path(kw["path"]).write_bytes(tiny_png())
        picker.input_value.return_value = "ground-physical"
        picker.locator.return_value.inner_text.return_value = "Ground Physical unavailable"
        rail.inner_text.return_value = "Ready"
        x10._ground(page, self.ctx, observation)
        old = list(self.ctx.out_dir.iterdir())
        self.assertEqual(aggregate(x10.classify(observation)), "FAIL")
        original_rename = Path.rename
        for phase in ("screenshot", "rename"):
            for mode in ("project", "version", "switch-back", "flow", "label", "pending"):
                with self.subTest(phase=phase, mode=mode):
                    picker.input_value.return_value = "ground-physical"
                    picker.locator.return_value.inner_text.return_value = "Ground Physical unavailable"
                    rail.inner_text.return_value = "Ready"
                    self.complete_catalog(listener)
                    def mutate():
                        if mode == "project":
                            self.complete_catalog(listener, self.catalog_request(project=OTHER))
                        elif mode == "version":
                            self.complete_catalog(listener, self.catalog_request(version=OTHER))
                        elif mode == "switch-back":
                            self.complete_catalog(listener, self.catalog_request(project=OTHER))
                            self.complete_catalog(listener)
                        elif mode == "flow":
                            picker.input_value.return_value = "rooftop"
                        elif mode == "label":
                            picker.locator.return_value.inner_text.return_value = "Ground Physical available"
                        else:
                            rail.inner_text.return_value = "Checking Solar steps for the selected project drawing version."
                    def capture(**kwargs):
                        Path(kwargs["path"]).write_bytes(tiny_png())
                        if phase == "screenshot":
                            mutate()
                    def rename(path, target):
                        result = original_rename(path, target)
                        mutate()
                        return result
                    rail.screenshot.side_effect = capture
                    with (patch.object(Path, "rename", rename) if phase == "rename" else nullcontext()), \
                            self.assertRaises(ValueError):
                        x10._ground(page, self.ctx, observation)
                    self.assertFalse(observation["evidence_saved"])
                    self.assertNotIn("evidence_path", observation)
                    self.assertEqual(list(self.ctx.out_dir.iterdir()), old)
                    self.assertEqual([r.verdict for r in x10.classify(observation)], ["UNREADABLE"] * 6)

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

    def test_verified_json_bytes_are_parsed_even_when_path_changes(self):
        parser = x10._strict_json_bytes
        for role in ("oracle", "rates"):
            row = next(row for row in self.ctx.manifest["fixtures"] if row["role"] == role)
            path = Path(row["path"])
            original = path.read_bytes()
            for mode in ("replace", "mutate"):
                parsed = []
                def parse(data):
                    parsed.append(data)
                    if data == original:
                        changed = original.replace(b"7", b"8") if role == "oracle" else original.replace(b"0.1", b"0.9")
                        self.assertEqual(len(changed), len(original))
                        if mode == "replace":
                            replacement = path.with_suffix(".replacement")
                            replacement.write_bytes(changed)
                            replacement.replace(path)
                        else:
                            path.write_bytes(changed)
                    return parser(data)
                with self.subTest(role=role, mode=mode), patch.object(x10, "_strict_json_bytes", parse), \
                        patch.object(x10, "signed_in") as session:
                    self.assertEqual([r.verdict for r in x10.run(self.ctx)], ["UNREADABLE"] * 6)
                    session.assert_not_called()
                self.assertIn(original, parsed)
                self.assertNotIn(path.read_bytes(), parsed)
                path.write_bytes(original)
        with patch.object(x10, "_strict_json", side_effect=AssertionError("path reopened")), \
                patch.object(x10, "_strict_json_bytes", wraps=parser) as parsed:
            _, _, expected, total = x10.bindings(self.ctx)
        self.assertEqual(expected["synthetic_count"], 7)
        self.assertEqual(total, Decimal("0.07"))
        self.assertEqual({call.args[0] for call in parsed.call_args_list}, {
            Path(row["path"]).read_bytes() for row in self.ctx.manifest["fixtures"]
            if row["role"] in ("oracle", "rates")})

    def test_catalog_wrong_methods_invalidate_and_cannot_restore_proof(self):
        observation = {}
        listener = x10._catalog_observer(self.ctx.base, observation)
        for method in ("POST", "HEAD"):
            self.complete_catalog(listener)
            request = self.catalog_request(method=method)
            listener(request)
            self.assertIsNone(observation["catalog_binding"])
            listener.response(SimpleNamespace(request=request, url=request.url, status=200))
            self.assertIsNone(observation["catalog_binding"])
            latest = self.catalog_request()
            self.complete_catalog(listener, latest)
            listener.response(SimpleNamespace(request=request, url=request.url, status=200))
            self.assertEqual(observation["catalog_binding"]["drawing_id"], DRAWING)

    def test_project_observer_latest_get_and_safe_version_metadata(self):
        observation = {}
        listener = x10._project_observer(self.ctx.base, observation)
        self.complete_project(listener)
        initial_epoch = observation["project_epoch"]
        first = SimpleNamespace(method="GET", url=self.ctx.base + "/api/projects/" + OTHER)
        latest = SimpleNamespace(method="GET", url=self.ctx.base + "/api/projects/" + PROJECT)
        listener(first)
        listener(latest)
        def response(request, identity):
            return SimpleNamespace(request=request, url=request.url, status=200,
                json=lambda: {"project": {"id": identity, "name": "private-name"},
                              "drawing_versions": [], "diagnostic": "private-body"})
        listener.response(response(first, OTHER))
        self.assertIsNone(observation["project_binding"])
        listener.response(response(latest, PROJECT))
        self.assertEqual(observation["project_binding"], PROJECT)
        self.assertGreater(observation["project_epoch"], initial_epoch)
        listener.failed(latest)
        listener.response(response(latest, PROJECT))
        self.assertIsNone(observation["project_binding"])
        self.complete_project(listener)
        self.assertEqual(observation["project_versions"][0], dict(
            project_id=PROJECT, version_id=VERSION, drawing_id=DRAWING, seq=1))
        for secret in ("private-name", "private-body", "https://"):
            self.assertNotIn(secret, json.dumps(observation))
        listener(latest)
        listener.response(response(latest, OTHER))
        self.assertIsNone(observation["project_binding"])

    def test_primary_drawing_identity_is_independent_of_catalog(self):
        config, bound, _, _ = x10.bindings(self.ctx)
        for versions in ([], [{"version_id": VERSION, "drawing_id": DRAWING, "seq": 2}]):
            page, preview, _, _, _ = self.import_page("Attached synthetic.dwg as version 1 to Synthetic probe")
            observation = dict(fresh_project=True, project_id=PROJECT)
            self.complete_project(x10._project_observer(self.ctx.base, observation), versions=versions)
            with self.assertRaises(ValueError):
                x10._import_primary(page, self.ctx, config, bound, observation)
            preview.click.assert_not_called()
        page = MagicMock()
        observation, listener = self.identity(page)
        self.complete_catalog(listener, self.catalog_request(drawing=OTHER))
        with self.assertRaises(ValueError):
            x10._current_identity(page, observation)
        self.complete_catalog(listener)
        x10._current_identity(page, observation)

    def integration_page(self, refusal=False):
        page, preview, attachment, _, upload = self.import_page(
            "Attached synthetic.dwg as version 1 to Synthetic probe")
        original_role = page.get_by_role.side_effect
        field, chip, rail, picker, inactive = [MagicMock() for _ in range(5)]
        inactive.is_visible.return_value = False
        picker.input_value.return_value = "ground-physical"
        picker.locator.return_value.inner_text.return_value = "Ground Physical unavailable"
        rail.inner_text.return_value = "Ready"
        rail.screenshot.side_effect = lambda **kw: Path(kw["path"]).write_bytes(tiny_png())
        page.locator.return_value.is_visible.return_value = True
        page.locator.return_value.inner_text.return_value = VERSION

        def role(kind, **kwargs):
            name = kwargs.get("name")
            if kind == "textbox":
                return field
            if kind == "navigation":
                return rail
            if kind == "combobox":
                return picker
            if isinstance(name, re.Pattern) or (isinstance(name, str) and name.startswith("Projects:")):
                return chip
            if name in ("Continue workspace", "Return to drawing", "Solar", "Solar flow tools"):
                return inactive
            return original_role(kind, **kwargs)

        page.get_by_role.side_effect = role
        created = SimpleNamespace(status=201, request=SimpleNamespace(method="POST"),
            url=self.ctx.base + "/api/projects", json=lambda: {"project": {
                "id": PROJECT, "name": field.fill.call_args.args[0]}})
        opened = SimpleNamespace(status=200, request=SimpleNamespace(method="GET"),
            url=self.ctx.base + "/api/projects/" + PROJECT,
            json=lambda: {"project": {"id": PROJECT}, "drawing_versions": []})
        def expect(predicate, **kwargs):
            manager = MagicMock()
            manager.__enter__.return_value.value = opened if predicate(opened) else created
            return manager
        page.expect_response.side_effect = expect
        field.locator.return_value.get_by_role.return_value.click.side_effect = lambda **kw: self.emit_project(page)
        upload.set_input_files.side_effect = lambda *a, **kw: self.emit_project(page)
        preview.click.side_effect = lambda **kw: self.emit_catalog(page)
        failure = page.locator.return_value.get_by_text.return_value
        if refusal:
            attachment.is_visible.side_effect = None
            attachment.is_visible.return_value = False
            failure.is_visible.side_effect = lambda: upload.set_input_files.called
            failure.inner_text.return_value = x10.IMPORT_REFUSALS[0]
            failure.screenshot.side_effect = lambda **kw: Path(kw["path"]).write_bytes(tiny_png())
        return page, failure if refusal else rail, chip

    def run_page(self, page, info=None):
        closed = []
        @contextmanager
        def session(*args, **kwargs):
            try:
                yield page, self.session_info if info is None else info
            finally:
                closed.append(True)
        with patch.object(x10, "signed_in", session):
            rows = x10.run(self.ctx)
        self.assertEqual(closed, [True])
        self.assertEqual([row.n for row in rows], list(range(1, 7)))
        self.assertEqual([row.verdict for row in rows[1:]], ["UNREADABLE"] * 5)
        self.assertNotIn("PASS", [row.verdict for row in rows])
        return rows

    def test_offline_run_observe_classify_real_refusal_and_settled_ground(self):
        for refusal in (True, False):
            with self.subTest(refusal=refusal):
                page, _, _ = self.integration_page(refusal)
                rows = self.run_page(page)
                self.assertEqual(rows[0].verdict, "FAIL")
                self.assertEqual(rows[0].observed["project_binding"], PROJECT)
                if refusal:
                    self.assertEqual(rows[0].observed["import_rejection"], x10.IMPORT_REFUSALS[0])
                else:
                    self.assertEqual(rows[0].observed["imported_drawing_id"], DRAWING)
                    self.assertEqual(rows[0].observed["catalog_binding"]["drawing_id"], DRAWING)
                self.assertEqual(page.remove_listener.call_args_list, page.on.call_args_list)

    def test_offline_run_fixture_and_session_failures_never_score(self):
        with patch.object(x10, "signed_in", side_effect=RuntimeError("private session diagnostic")):
            self.assertEqual([row.verdict for row in x10.run(self.ctx)], ["UNREADABLE"] * 6)
        page, _, _ = self.integration_page()
        self.assertEqual(self.run_page(page, info={})[0].verdict, "UNREADABLE")
        page.on.assert_not_called()
        self.replace_fixture("oracle", b"invalid JSON")
        with patch.object(x10, "signed_in") as session:
            rows = x10.run(self.ctx)
        session.assert_not_called()
        self.assertEqual([row.n for row in rows], list(range(1, 7)))
        self.assertEqual([row.verdict for row in rows], ["UNREADABLE"] * 6)

    def test_refusal_project_switches_and_close_during_capture_never_score(self):
        original_rename = Path.rename
        for phase in ("screenshot", "rename"):
            for mode in ("switch", "switch-back", "closed"):
                with self.subTest(phase=phase, mode=mode):
                    page, surface, chip = self.integration_page(True)
                    def mutate():
                        if mode == "closed":
                            chip.is_visible.return_value = False
                        else:
                            self.emit_project(page, OTHER)
                            if mode == "switch-back":
                                self.emit_project(page)
                    def capture(**kwargs):
                        Path(kwargs["path"]).write_bytes(tiny_png())
                        if phase == "screenshot":
                            mutate()
                    def rename(path, target):
                        result = original_rename(path, target)
                        mutate()
                        return result
                    surface.screenshot.side_effect = capture
                    with patch.object(Path, "rename", rename) if phase == "rename" else nullcontext():
                        rows = self.run_page(page)
                    self.assertEqual(rows[0].verdict, "UNREADABLE")
                    self.assertEqual(list(self.ctx.out_dir.glob("x10-evidence-*")), [])

    def test_cleanup_failure_attempts_every_listener_and_invalidates_only_new_evidence(self):
        old_page, _, _ = self.integration_page(True)
        old_rows = self.run_page(old_page)
        old_pointer = old_rows[0].observed["evidence_path"]
        old = list(self.ctx.out_dir.glob("x10-evidence-*"))
        for fail_at, refusal, refresh in ((0, True, False), (1, True, False),
                                         (0, False, False), (1, False, False),
                                         (0, True, True), (1, False, True)):
            page, _, _ = self.integration_page(refusal)
            attempts = []
            def remove(event, callback):
                attempts.append(event)
                if len(attempts) - 1 == fail_at:
                    if refresh:
                        self.emit_project(page)
                    raise RuntimeError("private cleanup diagnostic")
            page.remove_listener.side_effect = remove
            rows = self.run_page(page)
            self.assertEqual(attempts, ["request", "response", "requestfailed"])
            self.assertEqual(rows[0].verdict, "UNREADABLE")
            self.assertEqual(list(self.ctx.out_dir.glob("x10-evidence-*")), old)
            self.assertTrue(json.loads((old[0] / "x10-ui.json").read_text())["evidence_saved"])
            self.assertEqual(json.loads((old[0] / "x10-ui.json").read_text())["evidence_path"], old_pointer)
            diagnostic = (self.ctx.out_dir / "x10-ui.json").read_text()
            self.assertNotIn("private cleanup diagnostic", diagnostic)
            self.assertIn("RuntimeError", diagnostic)


if __name__ == "__main__":
    unittest.main()
