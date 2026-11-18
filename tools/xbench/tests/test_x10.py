"""Synthetic first-blocker coverage; no browser, customer files or network."""

from contextlib import contextmanager, nullcontext
import hashlib
import copy
import decimal
import gc
import weakref
import random
from fractions import Fraction
import json
import re
import stat
from pathlib import Path
import sys
import tempfile
import struct
import subprocess
import unittest
from unittest.mock import MagicMock, patch
from decimal import Decimal
from types import SimpleNamespace
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benches import x10
from xbench_core import BenchContext, aggregate

MAIN_FRAME = SimpleNamespace(parent_frame=None, url="https://example.test/app")
PROJECT = "00000000-0000-4000-8000-000000000001"
VERSION = "00000000-0000-4000-8000-000000000002"
OTHER = "00000000-0000-4000-8000-000000000003"
DRAWING = "00000000-0000-4000-8000-000000000004"


def canonical_project():
    return dict({key: PROJECT for key in ("project_id", "route_project_id", "active_project_id", "project_binding")}, source="b" * 40, web_build="b" * 40,
                evidence_path="x10-evidence-" + "a" * 32 + "/x10-ui.json / x10-evidence-" + "a" * 32 + "/x10-ground.png",
                screenshot_bytes=70, screenshot_sha256="a" * 64)


def metadata_response(request, data, status=200, url=None):
    return SimpleNamespace(request=request, url=request.url if url is None else url, status=status,
                           body=lambda: json.dumps(data).encode("utf-8"))


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
    def test_finalized_outputs_are_isolated_from_nested_input_and_step_mutation(self):
        for valid in (True, False):
            with self.subTest(valid=valid):
                observation = self.observation(extra={"nested": [{"quantity": "7"}]})
                if not valid:
                    observation["source"] = ""
                expected = {"bom": {"quantities": [7]}, "rates": [{"unit_rate": "0.25"}]}
                original_observation = copy.deepcopy(observation)
                original_expected = copy.deepcopy(expected)
                first = x10.classify(observation, expected)
                second = x10.classify(observation, expected)
                verdicts = [row.verdict for row in first]
                evidence = [row.evidence for row in first]
                observation["extra"]["nested"][0]["quantity"] = "changed"
                observation["project_id"] = OTHER
                expected["bom"]["quantities"].append(999)
                expected["rates"][0]["unit_rate"] = "changed"
                self.assertEqual(first[0].observed, original_observation)
                self.assertEqual(first[0].expected, original_expected)
                self.assertEqual([row.verdict for row in first], verdicts)
                self.assertEqual([row.evidence for row in first], evidence)
                first[0].observed["extra"]["nested"][0]["quantity"] = "step mutation"
                first[0].expected["rates"][0]["unit_rate"] = "step mutation"
                self.assertEqual(second[0].observed, original_observation)
                self.assertEqual(second[0].expected, original_expected)
                self.assertEqual(observation["extra"]["nested"][0]["quantity"], "changed")
                self.assertEqual(expected["rates"][0]["unit_rate"], "changed")
                self.assertNotIn("PASS", verdicts)

    def test_pending_catalog_is_never_a_product_failure(self):
        observation = self.observation(ui_text="Checking Solar steps for the selected project drawing version.")
        self.assertEqual(aggregate(x10.classify(observation)), "UNREADABLE")

    def observation(self, **changes):
        value = dict(**canonical_project(), fresh_project=True, imported_version_selected=True, evidence_saved=True,
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
        return SimpleNamespace(frame=MAIN_FRAME, method=method, url=(origin or self.ctx.base) + "/api/capabilities?drawing_id="
                               + drawing + "&project_id=" + project + "&input_version_id=" + version)

    def complete_project(self, listener, project=PROJECT, sequence=1, versions=None):
        request = SimpleNamespace(frame=MAIN_FRAME, method="GET", url=self.ctx.base + "/api/projects/" + project)
        listener(request)
        response = metadata_response(request, {"project": {"id": project},
            "drawing_versions": versions if versions is not None else [
                {"version_id": VERSION, "drawing_id": DRAWING, "seq": sequence}]})
        listener.response(response)

    def emit_project(self, page, project=PROJECT):
        callbacks = dict(call.args for call in page.on.call_args_list)
        listener = lambda request: callbacks["request"](request)
        listener.response = callbacks["response"]
        self.complete_project(listener, project)

    def project_identity(self, sequence=1):
        observation = dict(fresh_project=True, **canonical_project())
        self.complete_project(x10._project_observer(self.ctx.base, observation), sequence=sequence)
        return observation

    def identity(self, page):
        observation = self.project_identity()
        observation.update(imported_version_id=VERSION, imported_drawing_id=DRAWING, selected_sequence=1)
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

        observed = dict(**canonical_project(), fresh_project=True, imported_version_selected=True, evidence_saved=True,
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
            observation.update(fresh_project=True, **canonical_project())

        def attach(page, ctx, config, bound, observation):
            events.append("select imported version")
            observation.update(imported_version_selected=True, imported_version_id=VERSION,
                               imported_drawing_id=DRAWING, selected_sequence=1)
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
        page.main_frame = MAIN_FRAME
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
        page.main_frame = MAIN_FRAME
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
                observation = self.project_identity()
                observation["evidence_saved"] = True
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
                    page.target.get_by_role.side_effect = lambda role, **kw: rail if role == "navigation" else picker

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
                    observation.update(fresh_project=True, **canonical_project())
                    self.emit_project(page)
                with patch.object(x10, "_prepare_workspace"), patch.object(x10, "_create_project", create), \
                        patch.object(x10, "_open_workflow", workflow):
                    observed = x10.observe(page, self.ctx, config, bound, self.session_info)
                self.assertEqual(callbacks, {})
                self.assertEqual(page.on.call_count, 6)
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
        listener(SimpleNamespace(frame=MAIN_FRAME, url=self.ctx.base + "/api/capabilities-extra?project_id=" + OTHER))
        self.assertEqual(observation["catalog_binding"]["project_id"], PROJECT)
        self.assertEqual(observation["catalog_epoch"], epoch)
        for query in ("&project_id=" + OTHER, "&input_version_id=invalid"):
            listener(SimpleNamespace(frame=MAIN_FRAME, method="GET", url=self.catalog_request().url + query))
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
        listener.response(SimpleNamespace(request=latest, url=latest.url, status=200))
        self.assertEqual(observation["catalog_binding"]["project_id"], PROJECT)
        epoch = observation["catalog_epoch"]
        listener.failed(first)
        listener.response(SimpleNamespace(request=first, url=first.url, status=503))
        for url in ("https://foreign.test/api/capabilities", self.ctx.base + "/api/capabilities-extra"):
            listener.response(SimpleNamespace(request=SimpleNamespace(url=url), url=url, status=500))
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
            observation = self.project_identity()
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
        with patch.object(x10.os, "open", return_value=123), patch.object(x10.os, "close"), \
                patch.object(x10.os, "fdopen", return_value=manager), \
                patch.object(x10.os, "fstat", return_value=SimpleNamespace(st_mode=stat.S_IFREG)):
            self.assert_binding_refusal()
        source.read.assert_called_once_with(declared + 1)
        source.read.reset_mock()
        with patch.object(x10.os, "open", return_value=123), patch.object(x10.os, "close"), \
                patch.object(x10.os, "fdopen", return_value=manager), \
                patch.object(x10.os, "fstat", return_value=SimpleNamespace(st_mode=stat.S_IFIFO)):
            self.assert_binding_refusal()
        source.read.assert_not_called()

    def test_growing_screenshot_read_is_explicitly_bounded_and_unscorable(self):
        page = MagicMock()
        page.main_frame = MAIN_FRAME
        info = {}
        def screenshot(**kwargs):
            path = Path(kwargs["path"])
            path.write_bytes(tiny_png())
            info["stat"] = path.lstat()
        page.get_by_role.return_value.screenshot.side_effect = screenshot
        source = MagicMock()
        source.read.return_value = b"x" * (x10.MAX_SCREENSHOT_BYTES + 1)
        manager = MagicMock()
        manager.__enter__.return_value = source
        source.fileno.return_value = 123
        real_fstat = x10.os.fstat
        observation = dict(**canonical_project(), fresh_project=True, imported_version_selected=True,
                           ground_option="Ground Physical unavailable")
        with patch.object(x10, "_binary_input", return_value=manager), \
                patch.object(x10.os, "fstat", side_effect=lambda fd: info["stat"] if fd == 123 else real_fstat(fd)), \
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
        observation = self.project_identity()
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
        page.main_frame = MAIN_FRAME
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
            observation.update(project_id=PROJECT, imported_version_id=VERSION,
                               imported_drawing_id=DRAWING, selected_sequence=1)
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
                    o.update(fresh_project=True, **canonical_project()), self.emit_project(p))), \
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
        page.main_frame = MAIN_FRAME
        surface = page.get_by_role.return_value
        for options in invalid:
            with self.subTest(options={key: value if not isinstance(value, bytes) else len(value)
                                      for key, value in options.items()}):
                data = tiny_png(**options)
                surface.screenshot.side_effect = lambda **kw: Path(kw["path"]).write_bytes(data)
                observation = dict(**canonical_project(), fresh_project=True, imported_version_selected=True,
                                   ground_option="Ground Physical unavailable")
                with self.assertRaises(ValueError):
                    x10._save(page, self.ctx, observation)
                self.assertFalse(observation["evidence_saved"])
                self.assertNotIn("evidence_path", observation)
                self.assertEqual([r.verdict for r in x10.classify(observation)], ["UNREADABLE"] * 6)
                self.assertEqual(list(self.ctx.out_dir.iterdir()), [])

    def test_png_decoded_limit_is_checked_before_decompression(self):
        page = MagicMock()
        page.main_frame = MAIN_FRAME
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
        page.main_frame = MAIN_FRAME
        for options in valid:
            with self.subTest(options=options):
                data = tiny_png(**options)
                page.get_by_role.return_value.screenshot.side_effect = lambda **kw: Path(kw["path"]).write_bytes(data)
                observation = dict(**canonical_project(), fresh_project=True, imported_version_selected=True,
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
                page.main_frame = MAIN_FRAME
                name = "Synthetic probe-" + "a" * 12
                creation = SimpleNamespace(status=201, request=SimpleNamespace(frame=MAIN_FRAME, method="POST"),
                    url=self.ctx.base + "/api/projects", json=lambda: {"project": {"id": identity, "name": name}})
                opened = SimpleNamespace(status=200, request=SimpleNamespace(frame=MAIN_FRAME, method="GET"),
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
        page.main_frame = MAIN_FRAME
        surface = page.get_by_role.return_value
        surface.screenshot.side_effect = lambda **kw: Path(kw["path"]).write_bytes(tiny_png())
        observation = dict(**canonical_project(), fresh_project=True, imported_version_selected=True,
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
                failure = patch.object(x10, "_write_evidence_json", side_effect=OSError("synthetic")) if mode == "json" else nullcontext()
                with failure:
                    with self.assertRaises((OSError, ValueError)):
                        x10._save(page, self.ctx, observation)
                self.assertFalse(observation["evidence_saved"])
                self.assertNotIn("evidence_path", observation)
                self.assertEqual(list(self.ctx.out_dir.iterdir()), generations)
                self.assertEqual(aggregate(x10.classify(observation)), "UNREADABLE")

    def test_screenshot_pin_rejects_replacement_before_and_after_rename(self):
        page = MagicMock()
        page.main_frame = MAIN_FRAME
        page.get_by_role.return_value.screenshot.side_effect = lambda **kw: Path(kw["path"]).write_bytes(tiny_png())
        observation = dict(**canonical_project(), fresh_project=True, imported_version_selected=True,
                           ground_option="Ground Physical unavailable")
        x10._save(page, self.ctx, observation)
        old = list(self.ctx.out_dir.iterdir())
        saved = json.loads((old[0] / "x10-ui.json").read_text())
        self.assertEqual(saved["screenshot_bytes"], len(tiny_png()))
        self.assertEqual(saved["screenshot_sha256"], hashlib.sha256(tiny_png()).hexdigest())
        write_json, rename = x10._write_evidence_json, x10._commit_generation
        for phase in ("json-write", "rename"):
            for data in (tiny_png(raw=b"\x00\xff\x00\x00"), b"tampered bytes"):
                with self.subTest(phase=phase, data=data):
                    def written(path, *args, **kwargs):
                        result = write_json(path, *args, **kwargs)
                        (path.parent / "x10-ground.png").write_bytes(data)
                        return result
                    def renamed(path, target):
                        result = rename(path, target)
                        (Path(target) / "x10-ground.png").write_bytes(data)
                        return result
                    failure = (patch.object(x10, "_write_evidence_json", written) if phase == "json-write"
                               else patch.object(x10, "_commit_generation", renamed))
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
            page.main_frame = MAIN_FRAME
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
        page.main_frame = MAIN_FRAME
        picker = page.get_by_role.return_value
        picker.input_value.return_value = "ground-physical"
        picker.locator.return_value.inner_text.return_value = unavailable
        picker.inner_text.return_value = "Checking Solar steps for the selected project drawing version."
        with patch.object(x10, "_save") as save, self.assertRaises(ValueError):
            x10._ground(page, self.ctx, {})
        save.assert_not_called()

    def test_capture_revalidates_ui_and_identity_and_rolls_back_only_new_generation(self):
        page = MagicMock()
        page.main_frame = MAIN_FRAME
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
        original_rename = x10._commit_generation
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
                    with (patch.object(x10, "_commit_generation", rename) if phase == "rename" else nullcontext()), \
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
                page.main_frame = MAIN_FRAME
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
        first = SimpleNamespace(frame=MAIN_FRAME, method="GET", url=self.ctx.base + "/api/projects/" + OTHER)
        latest = SimpleNamespace(frame=MAIN_FRAME, method="GET", url=self.ctx.base + "/api/projects/" + PROJECT)
        listener(first)
        listener(latest)
        def response(request, identity):
            return metadata_response(request, {"project": {"id": identity, "name": "private-name"},
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
            observation = dict(fresh_project=True, **canonical_project())
            self.complete_project(x10._project_observer(self.ctx.base, observation), versions=versions)
            with self.assertRaises(ValueError):
                x10._import_primary(page, self.ctx, config, bound, observation)
            preview.click.assert_not_called()
        page = MagicMock()
        page.main_frame = MAIN_FRAME
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
        created = SimpleNamespace(status=201, request=SimpleNamespace(frame=MAIN_FRAME, method="POST"),
            url=self.ctx.base + "/api/projects", json=lambda: {"project": {
                "id": PROJECT, "name": field.fill.call_args.args[0]}})
        opened = SimpleNamespace(status=200, request=SimpleNamespace(frame=MAIN_FRAME, method="GET"),
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
        original_rename = x10._commit_generation
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
                    with patch.object(x10, "_commit_generation", rename) if phase == "rename" else nullcontext():
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
            self.assertEqual(attempts, ["request", "response", "requestfailed", "framenavigated", "close", "crash"])
            self.assertEqual(rows[0].verdict, "UNREADABLE")
            self.assertEqual(list(self.ctx.out_dir.glob("x10-evidence-*")), old)
            self.assertTrue(json.loads((old[0] / "x10-ui.json").read_text())["evidence_saved"])
            self.assertEqual(json.loads((old[0] / "x10-ui.json").read_text())["evidence_path"], old_pointer)
            diagnostic = (self.ctx.out_dir / "x10-ui.json").read_text()
            self.assertNotIn("private cleanup diagnostic", diagnostic)
            self.assertIn("RuntimeError", diagnostic)

    def test_project_identity_aliases_must_agree(self):
        for target in ("project", "version"):
            for alias in (PROJECT if target == "project" else VERSION, OTHER, None, "invalid"):
                with self.subTest(target=target, alias=alias):
                    observation = {}
                    listener = x10._project_observer(self.ctx.base, observation)
                    request = SimpleNamespace(frame=MAIN_FRAME, method="GET", url=self.ctx.base + "/api/projects/" + PROJECT)
                    project = {"project_id": PROJECT}
                    version = {"version_id": VERSION, "drawing_id": DRAWING, "seq": 1}
                    (project if target == "project" else version)["id"] = alias
                    listener(request)
                    listener.response(metadata_response(request, {"project": project,
                                                                  "drawing_versions": [version]}))
                    valid = alias == (PROJECT if target == "project" else VERSION)
                    self.assertEqual(observation["project_binding"], PROJECT if valid else None)
                    self.assertEqual(len(observation["project_versions"]), 1 if valid else 0)

    def test_project_drawing_sequences_are_unambiguous(self):
        for drawing in (DRAWING, OTHER):
            observation = {}
            listener = x10._project_observer(self.ctx.base, observation)
            versions = [{"version_id": VERSION, "drawing_id": DRAWING, "seq": 1},
                        {"version_id": OTHER, "drawing_id": drawing, "seq": 1}]
            self.complete_project(listener, versions=versions)
            self.assertEqual(observation["project_binding"], None if drawing == DRAWING else PROJECT)
            self.assertEqual(len(observation["project_versions"]), 0 if drawing == DRAWING else 2)

    def test_rate_total_ignores_and_restores_ambient_decimal_context(self):
        rates = {"items": [{"quantity": "9" * 30 + "." + "9" * 30,
                            "unit_rate": "0." + "0" * 29 + "1"}] * 17}
        exact = sum((Fraction(row["quantity"]) * Fraction(row["unit_rate"])
                     for row in rates["items"]), Fraction())
        for rounding in (decimal.ROUND_DOWN, decimal.ROUND_UP):
            with decimal.localcontext() as caller:
                caller.prec, caller.Emin, caller.Emax = 2, -2, 2
                caller.rounding, caller.clamp = rounding, 1
                for signal in caller.traps:
                    caller.traps[signal] = True
                caller.flags[decimal.Inexact] = True
                before = repr(caller)
                self.assertEqual(Fraction(x10.rate_total(rates)), exact)
                self.assertEqual(repr(decimal.getcontext()), before)

    def test_classify_requires_consistent_canonical_project_fields(self):
        for rejection in (False, True):
            observation = ClassificationTests().observation()
            if rejection:
                observation.update(imported_version_selected=False, import_rejection=x10.IMPORT_REFUSALS[0])
            for field in canonical_project():
                for value in (None, "", "invalid", OTHER):
                    with self.subTest(rejection=rejection, field=field, value=value):
                        invalid = dict(observation, **{field: value})
                        self.assertEqual([r.verdict for r in x10.classify(invalid)], ["UNREADABLE"] * 6)
                missing = dict(observation)
                missing.pop(field)
                self.assertEqual([r.verdict for r in x10.classify(missing)], ["UNREADABLE"] * 6)
            self.assertEqual(x10.classify(observation)[0].verdict, "FAIL")

    def test_catalog_response_identity_must_match_current_request(self):
        valid = self.catalog_request().url
        changed = ["https://foreign.test" + valid[len(self.ctx.base):],
                   valid.replace("/api/capabilities?", "/api/capabilities-extra?"),
                   valid.split("&input_version_id=")[0],
                   valid.replace("drawing_id=" + DRAWING, "drawing_id=" + OTHER),
                   valid.replace("project_id=" + PROJECT, "project_id=" + OTHER),
                   valid.replace("input_version_id=" + VERSION, "input_version_id=" + OTHER),
                   valid + "&project_id=" + PROJECT]
        for url in [valid] + changed:
            with self.subTest(url=url):
                observation = {}
                listener = x10._catalog_observer(self.ctx.base, observation)
                request = self.catalog_request()
                self.complete_catalog(listener, request)
                listener.response(SimpleNamespace(request=request, url=url, status=200))
                self.assertEqual(observation["catalog_binding"] is not None, url == valid)
                if url != valid:
                    listener.response(SimpleNamespace(request=request, url=valid, status=200))
                    self.assertIsNone(observation["catalog_binding"])

    def test_rate_total_matches_fraction_for_bounds_and_equivalent_lines(self):
        maximum = "9" * 30 + "." + "9" * 30
        tiny = "0." + "0" * 29 + "1"
        cases = [
            [{"quantity": maximum, "unit_rate": maximum}, {"quantity": tiny, "unit_rate": tiny}],
            [{"quantity": maximum if i % 2 else tiny, "unit_rate": tiny if i % 2 else maximum}
             for i in range(10000)],
            [{"quantity": "2", "unit_rate": maximum}],
            [{"quantity": "1", "unit_rate": maximum}] * 2,
        ]
        totals = []
        for items in cases:
            exact = sum((Fraction(row["quantity"]) * Fraction(row["unit_rate"])
                         for row in items), Fraction())
            for ordered in (items, list(reversed(items)), items[1:] + items[:1]):
                actual = x10.rate_total({"items": ordered})
                self.assertEqual(Fraction(actual), exact)
            totals.append(exact)
        self.assertEqual(totals[-2], totals[-1])

    def test_project_latest_redirect_invalidates_existing_proof(self):
        for changed in (False, True):
            observation = self.project_identity()
            observation.update(imported_version_selected=False, evidence_saved=True,
                               import_rejection=x10.IMPORT_REFUSALS[0])
            listener = x10._project_observer(self.ctx.base, observation)
            request = SimpleNamespace(frame=MAIN_FRAME, method="GET", url=self.ctx.base + "/api/projects/" + PROJECT)
            listener(request)
            response = metadata_response(request, {"project": {"id": PROJECT}, "drawing_versions": []})
            listener.response(response)
            self.assertEqual(x10.classify(observation)[0].verdict, "FAIL")
            if changed:
                response.url = self.ctx.base + "/api/projects/" + OTHER
            listener.response(response)
            self.assertEqual(x10.classify(observation)[0].verdict, "UNREADABLE" if changed else "FAIL")
            if changed:
                response.url = request.url
                listener.response(response)
                self.assertIsNone(observation["project_binding"])

    def test_project_metadata_is_bounded_strict_json_before_decode(self):
        valid = json.dumps({"project": {"id": PROJECT}, "drawing_versions": []}).encode()
        cases = [(valid, True), (b" " * (x10.MAX_JSON_BYTES + 1), False),
                 (b"[" * (x10.MAX_JSON_DEPTH + 1) + b"0" + b"]" * (x10.MAX_JSON_DEPTH + 1), False),
                 (valid[:-1] + b',"project":{}}', False)]
        for data, accepted in cases:
            with self.subTest(bytes=len(data), accepted=accepted):
                observation = {}
                listener = x10._project_observer(self.ctx.base, observation)
                request = SimpleNamespace(frame=MAIN_FRAME, method="GET", url=self.ctx.base + "/api/projects/" + PROJECT)
                response = SimpleNamespace(request=request, url=request.url, status=200,
                                           body=lambda: data, json=MagicMock())
                listener(request)
                listener.response(response)
                response.json.assert_not_called()
                self.assertEqual(observation["project_binding"] == PROJECT, accepted)
        for data in (cases[1][0], cases[2][0]):
            listener(request)
            response.body = lambda: data
            with patch.object(x10.json, "loads") as decoder:
                listener.response(response)
            decoder.assert_not_called()

    def test_callback_exceptions_invalidate_and_sanitize_diagnostics(self):
        class RaisingRequest:
            url = "https://example.test/api/capabilities"
            @property
            def method(self):
                raise RuntimeError("private event diagnostic")
        class RaisingResponse:
            url = "https://example.test/api/capabilities"
            @property
            def request(self):
                raise RuntimeError("private event diagnostic")
        for mode in ("request-url", "request-method", "response-request", "response-body", "failed-url"):
            with self.subTest(mode=mode):
                page, surface, _ = self.integration_page(False)
                def capture(**kwargs):
                    Path(kwargs["path"]).write_bytes(tiny_png())
                    handlers = dict(call.args for call in page.on.call_args_list)
                    if mode == "request-url":
                        handlers["request"](SimpleNamespace(url="https://[", method="GET"))
                    elif mode == "request-method":
                        handlers["request"](RaisingRequest())
                    elif mode == "response-request":
                        handlers["response"](RaisingResponse())
                    elif mode == "failed-url":
                        handlers["requestfailed"](SimpleNamespace(url=None))
                    else:
                        request = SimpleNamespace(frame=MAIN_FRAME, method="GET", url=self.ctx.base + "/api/projects/" + PROJECT)
                        handlers["request"](request)
                        response = SimpleNamespace(request=request, url=request.url, status=200,
                            body=MagicMock(side_effect=RuntimeError("private event diagnostic")))
                        handlers["response"](response)
                surface.screenshot.side_effect = capture
                rows = self.run_page(page)
                self.assertEqual([row.verdict for row in rows], ["UNREADABLE"] * 6)
                self.assertTrue(rows[0].observed["event_callback_failed"])
                self.assertFalse(rows[0].observed["evidence_saved"])
                self.assertEqual(list(self.ctx.out_dir.glob("x10-evidence-*")), [])
                diagnostic = (self.ctx.out_dir / "x10-ui.json").read_text()
                self.assertIn("passive UI event", diagnostic)
                self.assertNotIn("private event diagnostic", diagnostic)

    def test_session_exit_failure_invalidates_only_current_generation(self):
        old_page, _, _ = self.integration_page(True)
        self.run_page(old_page)
        old = list(self.ctx.out_dir.glob("x10-evidence-*"))
        for refusal in (True, False):
            page, _, _ = self.integration_page(refusal)
            closed = []
            @contextmanager
            def session(*args, **kwargs):
                try:
                    yield page, self.session_info
                finally:
                    closed.append(True)
                    raise RuntimeError("private exit diagnostic")
            with patch.object(x10, "signed_in", session):
                rows = x10.run(self.ctx)
            self.assertEqual(closed, [True])
            self.assertEqual([row.n for row in rows], list(range(1, 7)))
            self.assertEqual([row.verdict for row in rows], ["UNREADABLE"] * 6)
            self.assertEqual(list(self.ctx.out_dir.glob("x10-evidence-*")), old)
            self.assertTrue(json.loads((old[0] / "x10-ui.json").read_text())["evidence_saved"])
            diagnostic = (self.ctx.out_dir / "x10-ui.json").read_text()
            self.assertIn("RuntimeError", diagnostic)
            self.assertNotIn("private exit diagnostic", diagnostic)

    def test_png_nonregular_and_replaced_descriptor_refuse_before_read(self):
        path = Path(self.temp.name) / "regular.png"
        path.write_bytes(tiny_png())
        initial = path.lstat()
        for mode in (stat.S_IFLNK, stat.S_IFDIR, stat.S_IFIFO):
            with patch.object(Path, "lstat", return_value=SimpleNamespace(st_mode=mode)), \
                    patch.object(x10.os, "open") as opened, self.assertRaises(ValueError):
                x10._valid_png(path)
            opened.assert_not_called()
        source, manager = MagicMock(), MagicMock()
        manager.__enter__.return_value = source
        for replaced in (SimpleNamespace(st_mode=stat.S_IFDIR),
                         SimpleNamespace(st_mode=initial.st_mode, st_dev=initial.st_dev,
                             st_ino=initial.st_ino + 1, st_size=initial.st_size, st_mtime_ns=initial.st_mtime_ns)):
            with patch.object(x10.os, "open", return_value=123), patch.object(x10.os, "close"), \
                    patch.object(x10.os, "fdopen", return_value=manager), \
                    patch.object(x10.os, "fstat", return_value=replaced), self.assertRaises(ValueError):
                x10._valid_png(path)
            source.read.assert_not_called()
        self.assertEqual(x10._valid_png(path), tiny_png())

    def test_primary_import_pins_project_scope_through_upload_and_preview(self):
        for phase in ("upload", "preview"):
            for mode in ("refresh", "switch", "switch-back"):
                with self.subTest(phase=phase, mode=mode):
                    page, surface, _ = self.integration_page(False)
                    upload = page.get_by_label.return_value
                    preview = page.get_by_role("button", name="Preview v1 in the drawing", exact=True)
                    original = upload.set_input_files.side_effect if phase == "upload" else preview.click.side_effect
                    def transition(*args, **kwargs):
                        original(*args, **kwargs)
                        if mode == "refresh":
                            self.emit_project(page)
                        else:
                            self.emit_project(page, OTHER)
                            if mode == "switch-back":
                                self.emit_project(page)
                    if phase == "upload":
                        upload.set_input_files.side_effect = transition
                    else:
                        preview.click.side_effect = transition
                    rows = self.run_page(page)
                    self.assertEqual(rows[0].verdict, "FAIL" if mode == "refresh" else "UNREADABLE")
                    if mode != "refresh":
                        self.assertFalse(rows[0].observed["imported_version_selected"])
                        surface.screenshot.assert_not_called()

    def test_current_identity_requires_latest_imported_tuple(self):
        page = MagicMock()
        page.main_frame = MAIN_FRAME
        observation, catalog = self.identity(page)
        for rows in ([], [dict(observation["project_versions"][0], drawing_id=OTHER)],
                     [dict(observation["project_versions"][0], seq=2)]):
            changed = dict(observation, project_versions=rows)
            with self.assertRaises(ValueError):
                x10._current_identity(page, changed)
        x10._current_identity(page, observation)

    def test_project_body_reentrancy_cannot_resurrect_superseded_metadata(self):
        for mode in ("pending", "success", "failed", "unchanged"):
            observation = dict(fresh_project=True, evidence_saved=True,
                               imported_version_selected=False, import_rejection=x10.IMPORT_REFUSALS[0],
                               **canonical_project())
            listener = x10._project_observer(self.ctx.base, observation)
            request = SimpleNamespace(frame=MAIN_FRAME, method="GET", url=self.ctx.base + "/api/projects/" + PROJECT)
            newer = SimpleNamespace(frame=MAIN_FRAME, method="GET", url=self.ctx.base + "/api/projects/" + OTHER)
            data = json.dumps({"project": {"id": PROJECT}, "drawing_versions": []}).encode()
            def body():
                if mode in ("pending", "success"):
                    listener(newer)
                    if mode == "success":
                        listener.response(metadata_response(newer, {"project": {"id": OTHER},
                                                                     "drawing_versions": []}))
                elif mode == "failed":
                    listener.failed(request)
                return data
            listener(request)
            listener.response(SimpleNamespace(request=request, url=request.url, status=200, body=body))
            expected = PROJECT if mode == "unchanged" else OTHER if mode == "success" else None
            self.assertEqual(observation["project_binding"], expected)
            self.assertEqual(aggregate(x10.classify(observation)), "FAIL" if mode == "unchanged" else "UNREADABLE")

    def test_rate_inputs_require_ascii_digits_before_browser(self):
        original = Path(next(row["path"] for row in self.ctx.manifest["fixtures"] if row["role"] == "rates")).read_bytes()
        for key in ("quantity", "unit_rate"):
            for value in ("ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡", "ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¯ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¼ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¹Ã…â€œÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¯ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¼ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢", "1ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢", "ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡.2", "1.ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¯ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¼ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢"):
                with self.subTest(key=key, value=value):
                    item = {"quantity": "1.25", "unit_rate": "2.00", key: value}
                    with self.assertRaises(ValueError):
                        x10.rate_total({"items": [item]})
                    self.replace_fixture("rates", json.dumps({"items": [item]}).encode())
                    self.assert_binding_refusal()
        self.replace_fixture("rates", original)
        self.assertEqual(x10.rate_total({"items": [{"quantity": "1.25", "unit_rate": "2.00"}]}), Decimal("2.50"))

    def test_indexed_png_filters_and_adam7_require_existing_palette_entries(self):
        palette = b"\x00\x00\x00\xff\xff\xff"
        cases = [
            ({"width": 2, "depth": 8, "raw": b"\x00\x00\x01"}, b"\x00\x00\x02"),
            ({"width": 2, "depth": 8, "raw": b"\x01\x01\x00"}, b"\x01\x01\x01"),
            ({"height": 2, "depth": 8, "raw": b"\x00\x01\x02\x00"}, b"\x00\x01\x02\x01"),
            ({"width": 2, "height": 2, "depth": 8, "raw": b"\x00\x01\x01\x03\x01\x00"},
             b"\x00\x01\x01\x03\x01\x01"),
            ({"width": 2, "height": 2, "depth": 8, "raw": b"\x00\x01\x01\x04\x00\x00"},
             b"\x00\x01\x01\x04\x01\x00"),
            ({"depth": 2, "raw": b"\x00\x40"}, b"\x00\x80"),
            ({"depth": 4, "raw": b"\x00\x10"}, b"\x00\x20"),
        ]
        page = MagicMock()
        page.main_frame = MAIN_FRAME
        surface = page.get_by_role.return_value
        for options, invalid in cases:
            for valid in (True, False):
                with self.subTest(options=options, valid=valid):
                    data = tiny_png(color=3, palette=palette, **dict(options, raw=options["raw"] if valid else invalid))
                    surface.screenshot.side_effect = lambda **kw: Path(kw["path"]).write_bytes(data)
                    observation = dict(**canonical_project(), fresh_project=True, imported_version_selected=True,
                                       ground_option="Ground Physical unavailable")
                    if valid:
                        x10._save(page, self.ctx, observation)
                    else:
                        with self.assertRaises(ValueError):
                            x10._save(page, self.ctx, observation)
                        self.assertFalse(observation["evidence_saved"])
                        self.assertEqual(aggregate(x10.classify(observation)), "UNREADABLE")
        path = Path(self.temp.name) / "indexed.png"
        # Only the first pixel counts; nonzero packed padding is legal.
        path.write_bytes(tiny_png(color=3, depth=1, palette=palette[:3], raw=b"\x00\x7f"))
        x10._valid_png(path)
        for raw in (b"\x00\x80", b"\x01\x00" * 14 + b"\x01\x80"):
            options = {} if len(raw) == 2 else {"width": 8, "height": 8, "interlace": 1}
            path.write_bytes(tiny_png(color=3, depth=1, palette=palette[:3], raw=raw, **options))
            with self.assertRaises(ValueError):
                x10._valid_png(path)
        path.write_bytes(tiny_png(color=3, depth=1, palette=palette[:3], width=8, height=8,
                                 interlace=1, raw=b"\x01\x00" * 15))
        x10._valid_png(path)

    def test_primary_receipt_is_rechecked_after_bind_before_upload(self):
        for mode in ("mutation", "replacement", "valid"):
            with self.subTest(mode=mode):
                page, surface, _ = self.integration_page(False)
                original_prepare = x10._prepare_workspace
                path = Path(next(row["path"] for row in self.ctx.manifest["fixtures"] if row["role"] == "drawing"))
                original = path.read_bytes()
                def prepare(page):
                    original_prepare(page)
                    if mode == "mutation":
                        path.write_bytes(original + b" ")
                    elif mode == "replacement":
                        replacement = path.with_suffix(".changed")
                        replacement.write_bytes(bytes([original[0] ^ 1]) + original[1:])
                        replacement.replace(path)
                with patch.object(x10, "_prepare_workspace", prepare):
                    rows = self.run_page(page)
                self.assertEqual(rows[0].verdict, "FAIL" if mode == "valid" else "UNREADABLE")
                if mode != "valid":
                    page.get_by_label.return_value.set_input_files.assert_not_called()
                    surface.screenshot.assert_not_called()
                path.write_bytes(original)

    def test_required_roles_are_unique_before_browser_entry(self):
        self.assertEqual(x10.bindings(self.ctx)[3], Decimal("0.07"))
        for role in x10.ROLES:
            row = next(row for row in self.ctx.manifest["fixtures"] if row["role"] == role)
            extra = dict(row, id="second-" + role)
            self.ctx.manifest["fixtures"].append(extra)
            self.ctx.fixtures.append(dict(extra, ok=True))
            with self.subTest(role=role):
                self.assert_binding_refusal()
            self.ctx.manifest["fixtures"].pop()
            self.ctx.fixtures.pop()
        self.assertEqual(x10.bindings(self.ctx)[3], Decimal("0.07"))

    def test_project_metadata_requires_get_and_wrong_methods_clear_proof(self):
        for method in ("GET", "POST", "HEAD"):
            observation = self.project_identity()
            listener = x10._project_observer(self.ctx.base, observation)
            self.complete_project(listener)
            request = SimpleNamespace(frame=MAIN_FRAME, method=method, url=self.ctx.base + "/api/projects/" + PROJECT)
            listener(request)
            self.assertIsNone(observation["project_binding"])
            response = metadata_response(request, {"project": {"id": PROJECT}, "drawing_versions": []})
            listener.response(response)
            self.assertEqual(observation["project_binding"], PROJECT if method == "GET" else None)

    def test_passive_identity_proof_cannot_come_from_cross_origin_urls(self):
        for factory, request in ((x10._catalog_observer, self.catalog_request()),
                                 (x10._project_observer, SimpleNamespace(frame=MAIN_FRAME, method="GET",
                                     url=self.ctx.base + "/api/projects/" + PROJECT))):
            for origin in ("https://foreign.test", "http://example.test", "https://example.test:8444"):
                with self.subTest(factory=factory.__name__, origin=origin):
                    observation = {}
                    listener = factory(self.ctx.base, observation)
                    foreign = SimpleNamespace(frame=MAIN_FRAME, method="GET", url=origin + request.url[len(self.ctx.base):])
                    listener(foreign)
                    listener.response(metadata_response(foreign, {"project": {"id": PROJECT}, "drawing_versions": []}))
                    key = "catalog_binding" if factory is x10._catalog_observer else "project_binding"
                    self.assertIsNone(observation[key])
                    listener(request)
                    listener.response(metadata_response(request, {"project": {"id": PROJECT}, "drawing_versions": []}))
                    self.assertIsNotNone(observation[key])
                    listener.response(metadata_response(request, {"project": {"id": PROJECT}}, url=foreign.url))
                    self.assertIsNone(observation[key])

    def test_evidence_destination_aliases_preserve_fixture_bytes(self):
        root = self.ctx.out_dir
        original_rows = [dict(row) for row in self.ctx.manifest["fixtures"]]
        page = MagicMock()
        page.main_frame = MAIN_FRAME
        fixture = Path(original_rows[0]["path"])
        original = fixture.read_bytes()
        self.ctx.out_dir = fixture
        observation = dict(**canonical_project(), fresh_project=True)
        with self.assertRaises(ValueError):
            x10._save(page, self.ctx, observation)
        self.assertEqual(fixture.read_bytes(), original)
        self.ctx.out_dir = root
        root.mkdir()
        for name in ("x10-ground.png", "x10-ui.json"):
            stage = root / (".x10-staging-alias-" + name)
            stage.mkdir()
            material = stage / name
            material.write_bytes(original)
            self.ctx.manifest["fixtures"][0]["path"] = str(material)
            with patch.object(x10.tempfile, "mkdtemp", return_value=str(stage)), self.assertRaises(ValueError):
                x10._save(page, self.ctx, observation)
            self.assertEqual(material.read_bytes(), original)
        self.ctx.manifest["fixtures"] = [dict(row) for row in original_rows]
        stage = root / ".x10-staging-hardlink"
        stage.mkdir()
        png, record = stage / "x10-ground.png", stage / "x10-ui.json"
        png.write_bytes(original)
        record.hardlink_to(png)
        with patch.object(x10.tempfile, "mkdtemp", return_value=str(stage)), self.assertRaises(ValueError):
            x10._save(page, self.ctx, observation)
        self.assertEqual(png.read_bytes(), original)
        self.assertEqual(record.read_bytes(), original)
        diagnostic = root / "x10-ui.json"
        diagnostic.write_bytes(original)
        self.ctx.manifest["fixtures"][0]["path"] = str(diagnostic)
        x10._diagnostic(self.ctx, observation, "synthetic", OSError())
        self.assertEqual(diagnostic.read_bytes(), original)
        page.get_by_role.assert_not_called()
        self.assertFalse(observation["evidence_saved"])

    def test_evidence_flush_close_and_atomic_rename_faults_preserve_old_generations(self):
        page = MagicMock()
        page.main_frame = MAIN_FRAME
        page.get_by_role.return_value.screenshot.side_effect = lambda **kw: Path(kw["path"]).write_bytes(tiny_png())
        observation = dict(**canonical_project(), fresh_project=True, imported_version_selected=True,
                           ground_option="Ground Physical unavailable")
        x10._save(page, self.ctx, observation)
        old = list(self.ctx.out_dir.iterdir())
        real_open, real_rename = Path.open, x10._commit_generation
        for phase in ("flush", "close", "rename", "rename-after"):
            class Writer:
                def __init__(self, stream):
                    self.stream = stream
                def __enter__(self):
                    return self
                def write(self, value):
                    return self.stream.write(value)
                def flush(self):
                    if phase == "flush":
                        raise OSError("synthetic flush")
                    self.stream.flush()
                def __exit__(self, *args):
                    self.stream.close()
                    if phase == "close":
                        raise OSError("synthetic close")
            def open_file(path, *args, **kwargs):
                stream = real_open(path, *args, **kwargs)
                return Writer(stream) if path.name == "x10-ui.json" and args and args[0] == "w" else stream
            def rename(path, target):
                if phase == "rename-after":
                    real_rename(path, target)
                raise OSError("synthetic replace")
            with self.subTest(phase=phase), patch.object(Path, "open", open_file), \
                    (patch.object(x10, "_commit_generation", rename) if phase.startswith("rename") else nullcontext()), \
                    self.assertRaises(OSError):
                x10._save(page, self.ctx, observation)
            self.assertEqual(list(self.ctx.out_dir.iterdir()), old)
            self.assertFalse(observation["evidence_saved"])
            self.assertNotIn("evidence_path", observation)
            self.assertEqual(aggregate(x10.classify(observation)), "UNREADABLE")

    def test_binary_descriptors_close_once_on_success_refusal_and_read_errors(self):
        real_open, real_fdopen, real_close = x10.os.open, x10.os.fdopen, x10.os.close
        original = Path(next(row["path"] for row in self.ctx.manifest["fixtures"] if row["role"] == "oracle")).read_bytes()
        for operation in ("fixtures", "png"):
            for fault in ("none", "parse", "read", "factory"):
                self.replace_fixture("oracle", b"invalid JSON" if operation == "fixtures" and fault == "parse" else original)
                events, streams = [], []
                def opened(*args, **kwargs):
                    fd = real_open(*args, **kwargs)
                    events.append(("open", fd))
                    return fd
                def closed(fd):
                    events.append(("close", fd))
                    real_close(fd)
                class Stream:
                    def __init__(self, stream):
                        self.stream, self.closes = stream, 0
                    def __enter__(self):
                        return self
                    def fileno(self):
                        return self.stream.fileno()
                    def read(self, count):
                        if fault == "read":
                            raise OSError("synthetic read")
                        return self.stream.read(count)
                    def __exit__(self, *args):
                        self.closes += 1
                        self.stream.close()
                def fdopened(*args, **kwargs):
                    self.assertIs(kwargs["closefd"], False)
                    if fault == "factory":
                        raise OSError("synthetic factory")
                    stream = Stream(real_fdopen(*args, **kwargs))
                    streams.append(stream)
                    return stream
                page = MagicMock()
                page.main_frame = MAIN_FRAME
                data = tiny_png(compressed=b"bad zlib") if fault == "parse" else tiny_png()
                page.get_by_role.return_value.screenshot.side_effect = lambda **kw: Path(kw["path"]).write_bytes(data)
                observation = dict(**canonical_project(), fresh_project=True)
                old = list(self.ctx.out_dir.glob("x10-evidence-*"))
                def execute():
                    return x10.bindings(self.ctx) if operation == "fixtures" else x10._save(page, self.ctx, observation)
                with self.subTest(operation=operation, fault=fault), patch.object(x10.os, "open", opened), \
                        patch.object(x10.os, "fdopen", fdopened), patch.object(x10.os, "close", closed):
                    if fault == "none":
                        execute()
                    else:
                        with self.assertRaises((OSError, ValueError)):
                            execute()
                self.assertTrue(events)
                self.assertEqual(len(events) % 2, 0)
                for index in range(0, len(events), 2):
                    self.assertEqual(events[index + 1], ("close", events[index][1]))
                    with self.assertRaises(OSError):
                        x10.os.fstat(events[index][1])
                self.assertTrue(all(stream.closes == 1 and stream.stream.closed for stream in streams))
                if operation == "png" and fault != "none":
                    self.assertFalse(observation["evidence_saved"])
                    self.assertEqual(list(self.ctx.out_dir.glob("x10-evidence-*")), old)

    def test_classification_requires_matching_valid_source_and_build(self):
        for refusal in (False, True):
            original = ClassificationTests().observation()
            if refusal:
                original.update(imported_version_selected=False, import_rejection=x10.IMPORT_REFUSALS[0])
            for key in ("source", "web_build"):
                for value in (None, "", "B" * 40, "b" * 39, "b" * 40 + "\n", "c" * 40, 42):
                    with self.subTest(refusal=refusal, key=key, value=value):
                        self.assertEqual([r.verdict for r in x10.classify(dict(original, **{key: value}))],
                                         ["UNREADABLE"] * 6)
                missing = dict(original)
                missing.pop(key)
                self.assertEqual([r.verdict for r in x10.classify(missing)], ["UNREADABLE"] * 6)
            self.assertEqual(x10.classify(original)[0].verdict, "FAIL")

    def test_partial_listener_registration_is_always_cleaned_up(self):
        config, bound, _, _ = x10.bindings(self.ctx)
        for fail_at in range(3):
            page = MagicMock()
            page.main_frame = MAIN_FRAME
            installed, removed = [], []
            def register(event, callback):
                installed.append((event, callback))
                if len(installed) - 1 == fail_at:
                    raise RuntimeError("private registration diagnostic")
            page.on.side_effect = register
            page.remove_listener.side_effect = lambda event, callback: removed.append((event, callback))
            with patch.object(x10, "_prepare_workspace") as prepare:
                observed = x10.observe(page, self.ctx, config, bound, self.session_info)
            prepare.assert_not_called()
            self.assertEqual(removed, installed)
            self.assertEqual([r.verdict for r in x10.classify(observed)], ["UNREADABLE"] * 6)
            self.assertNotIn("private registration diagnostic", (self.ctx.out_dir / "x10-ui.json").read_text())

    def test_overall_deadline_clamps_successive_ui_waits_with_virtual_clock(self):
        class Clock:
            now = 0.0
            def __call__(self):
                return self.now
        config, bound, _, _ = x10.bindings(self.ctx)
        for exhausted in (False, True):
            clock = Clock()
            page = MagicMock()
            page.main_frame = MAIN_FRAME
            waits = []
            def wait(*args, **kwargs):
                waits.append(kwargs["timeout"])
                clock.now += 0.6
            page.wait_for_function.side_effect = wait
            def prepare(page):
                page.wait_for_function("synthetic", timeout=30000)
                page.get_by_role("button", name="Synthetic").is_visible()
            def create(page, ctx, config, observation):
                page.wait_for_function("synthetic", timeout=30000)
                observation.update(fresh_project=True, **canonical_project())
            with patch.object(x10, "_prepare_workspace", prepare), patch.object(x10, "_create_project", create), \
                    patch.object(x10, "_import_primary", return_value=False) as attach:
                observed = x10.observe(page, self.ctx, config, bound, self.session_info,
                                       budget_seconds=1 if exhausted else 2, clock=clock)
            self.assertEqual(waits, [1000, 400] if exhausted else [2000, 1400])
            self.assertEqual([r.verdict for r in x10.classify(observed)], ["UNREADABLE"] * 6)
            if exhausted:
                attach.assert_not_called()
                self.assertEqual(observed["exception_type"], "TimeoutError")
            self.assertEqual(page.remove_listener.call_args_list, page.on.call_args_list)
        for seconds in (0, -1, float("inf"), float("nan"), True):
            observed = x10.observe(MagicMock(), self.ctx, config, bound, self.session_info,
                                   budget_seconds=seconds, clock=Clock())
            self.assertEqual(aggregate(x10.classify(observed)), "UNREADABLE")

    def test_strict_json_rejects_lone_surrogates_and_keeps_valid_unicode(self):
        for role in ("oracle", "rates"):
            path = Path(next(row["path"] for row in self.ctx.manifest["fixtures"] if row["role"] == role))
            original = path.read_bytes()
            prefix = original[:-1] + b',"extra":'
            for fragment in (b'"\\ud800"', b'"\\udfff"', b'{"\\ud800":1}',
                             b'[{"nested":"\\udc00"}]', b'"\\ud800x\\udc00"'):
                self.replace_fixture(role, prefix + fragment + b'}')
                self.assert_binding_refusal()
            for fragment in (b'"\\ud83d\\ude00"', '"cafÃƒÆ’Ã‚Â© ÃƒÂ§Ã¢â‚¬Â¢Ã…â€™"'.encode(), b'{"\\ud83d\\ude00":"ok"}'):
                self.replace_fixture(role, prefix + fragment + b'}')
                self.assertEqual(x10.bindings(self.ctx)[3], Decimal("0.07"))
            self.replace_fixture(role, original)

    def test_catalog_reentrant_metadata_access_never_commits_superseded_proof(self):
        for field in ("request", "url", "status"):
            for event in ("new-pending", "new-success", "failure"):
                with self.subTest(field=field, event=event):
                    observation = {}
                    listener = x10._catalog_observer(self.ctx.base, observation)
                    old, newer = self.catalog_request(), self.catalog_request(project=OTHER)
                    listener(old)
                    class Response:
                        triggered = False
                        def access(self, name, value):
                            if name == field and not self.triggered:
                                self.triggered = True
                                if event == "failure":
                                    listener.failed(old)
                                else:
                                    listener(newer)
                                    if event == "new-success":
                                        listener.response(SimpleNamespace(request=newer, url=newer.url, status=200))
                            return value
                        @property
                        def request(self):
                            return self.access("request", old)
                        @property
                        def url(self):
                            return self.access("url", old.url)
                        @property
                        def status(self):
                            return self.access("status", 200)
                    listener.response(Response())
                    binding = observation["catalog_binding"]
                    self.assertEqual(binding["project_id"] if binding else None,
                                     OTHER if event == "new-success" else None)

    def test_identity_observers_accept_only_main_frame_requests(self):
        child = SimpleNamespace(parent_frame=MAIN_FRAME)
        for factory in (x10._catalog_observer, x10._project_observer):
            observation = {}
            listener = factory(self.ctx.base, observation, MAIN_FRAME)
            for frame in (child, MAIN_FRAME):
                request = self.catalog_request() if factory is x10._catalog_observer else SimpleNamespace(
                    method="GET", url=self.ctx.base + "/api/projects/" + PROJECT)
                request.frame = frame
                listener(request)
                listener.response(metadata_response(request, {"project": {"id": PROJECT}, "drawing_versions": []}))
                key = "catalog_binding" if factory is x10._catalog_observer else "project_binding"
                self.assertEqual(observation[key] is not None, frame is MAIN_FRAME)
            epoch = observation["catalog_epoch" if factory is x10._catalog_observer else "project_epoch"]
            request.frame = child
            listener.failed(request)
            self.assertEqual(observation["catalog_epoch" if factory is x10._catalog_observer else "project_epoch"], epoch)

    def test_main_frame_navigation_and_switch_back_invalidate_evidence(self):
        for mode in ("child", "away", "switch-back"):
            page, surface, _ = self.integration_page(False)
            def capture(**kwargs):
                Path(kwargs["path"]).write_bytes(tiny_png())
                navigation = dict(call.args for call in page.on.call_args_list)["framenavigated"]
                if mode == "child":
                    navigation(SimpleNamespace(parent_frame=MAIN_FRAME, url="https://foreign.test"))
                else:
                    MAIN_FRAME.url = "https://foreign.test/away"
                    navigation(MAIN_FRAME)
                    if mode == "switch-back":
                        MAIN_FRAME.url = self.ctx.base + "/app"
                        navigation(MAIN_FRAME)
                        self.emit_project(page)
                        self.emit_catalog(page)
            surface.screenshot.side_effect = capture
            rows = self.run_page(page)
            self.assertEqual(rows[0].verdict, "FAIL" if mode == "child" else "UNREADABLE")
            self.assertEqual(page.remove_listener.call_args_list, page.on.call_args_list)
            if mode != "child":
                self.assertFalse(rows[0].observed["evidence_saved"])
            MAIN_FRAME.url = self.ctx.base + "/app"

    def test_refusal_early_return_respects_overall_deadline(self):
        config, bound, _, _ = x10.bindings(self.ctx)
        for elapsed in (0.999, 1.0, 1.001):
            now = [0.0]
            page = MagicMock()
            page.main_frame = MAIN_FRAME
            def refusal(page, ctx, config, bound, observation):
                observation.update(**canonical_project(), fresh_project=True, imported_version_selected=False,
                                   import_rejection=x10.IMPORT_REFUSALS[0])
                x10._save(page, ctx, observation)
                now[0] = elapsed
                return False
            page.get_by_role.return_value.screenshot.side_effect = lambda **kw: Path(kw["path"]).write_bytes(tiny_png())
            old = list(self.ctx.out_dir.glob("x10-evidence-*"))
            with patch.object(x10, "_prepare_workspace"), patch.object(x10, "_create_project"), \
                    patch.object(x10, "_import_primary", refusal):
                observed = x10.observe(page, self.ctx, config, bound, self.session_info,
                                       budget_seconds=1, clock=lambda: now[0])
            self.assertEqual(aggregate(x10.classify(observed)), "FAIL" if elapsed < 1 else "UNREADABLE")
            if elapsed >= 1:
                self.assertEqual(list(self.ctx.out_dir.glob("x10-evidence-*")), old)
                self.assertEqual(observed["exception_type"], "TimeoutError")

    def test_observer_duplicate_responses_cannot_revive_failed_lifecycle(self):
        for factory in (x10._catalog_observer, x10._project_observer):
            observation = {}
            listener = factory(self.ctx.base, observation)
            request = self.catalog_request() if factory is x10._catalog_observer else SimpleNamespace(
                frame=MAIN_FRAME, method="GET", url=self.ctx.base + "/api/projects/" + PROJECT)
            response = metadata_response(request, {"project": {"id": PROJECT}, "drawing_versions": []})
            listener(request)
            listener.response(response)
            listener.response(response)
            key = "catalog_binding" if factory is x10._catalog_observer else "project_binding"
            self.assertIsNotNone(observation[key])
            listener.failed(request)
            for _ in range(2):
                listener.response(response)
                self.assertIsNone(observation[key])

    def test_project_metadata_accessors_cannot_resurrect_superseded_proof(self):
        for field in ("request", "url", "status"):
            for action in ("new-pending", "new-success", "failure"):
                observation = {}
                listener = x10._project_observer(self.ctx.base, observation)
                old = SimpleNamespace(frame=MAIN_FRAME, method="GET", url=self.ctx.base + "/api/projects/" + PROJECT)
                newer = SimpleNamespace(frame=MAIN_FRAME, method="GET", url=self.ctx.base + "/api/projects/" + OTHER)
                listener(old)
                body = MagicMock(return_value=json.dumps({"project": {"id": PROJECT}, "drawing_versions": []}).encode())
                class Response:
                    triggered = False
                    def access(self, name, value):
                        if name == field and not self.triggered:
                            self.triggered = True
                            if action == "failure":
                                listener.failed(old)
                            else:
                                listener(newer)
                                if action == "new-success":
                                    listener.response(metadata_response(newer, {"project": {"id": OTHER},
                                                                                "drawing_versions": []}))
                        return value
                    @property
                    def request(self):
                        return self.access("request", old)
                    @property
                    def url(self):
                        return self.access("url", old.url)
                    @property
                    def status(self):
                        return self.access("status", 200)
                response = Response()
                response.body = body
                listener.response(response)
                body.assert_not_called()
                self.assertEqual(observation["project_binding"], OTHER if action == "new-success" else None)

    def test_observation_clock_refuses_nonfinite_and_backward_samples(self):
        config, bound, _, _ = x10.bindings(self.ctx)
        for samples in ([float("nan")], [float("inf")], [-float("inf")], [True],
                        [10, 9], [10, 10, float("nan")], [10, 11, 10.5]):
            values = iter(samples)
            last = [samples[-1]]
            def clock():
                last[0] = next(values, last[0])
                return last[0]
            observed = x10.observe(MagicMock(), self.ctx, config, bound, self.session_info, clock=clock)
            self.assertEqual([r.verdict for r in x10.classify(observed)], ["UNREADABLE"] * 6)
            self.assertEqual(observed["exception_type"], "ValueError")
        samples = iter([10, 10, 10.25, 10.5])
        budget = x10._ObservationBudget(1, lambda: next(samples))
        self.assertEqual([budget.milliseconds() for _ in range(3)], [1000, 750, 500])

    def test_diagnostics_atomically_preserve_previous_record_on_io_failure(self):
        self.ctx.out_dir.mkdir()
        target = self.ctx.out_dir / "x10-ui.json"
        old = b'{"previous":"diagnostic"}\n'
        real_open = Path.open
        for fault in ("write", "flush", "replace", "none"):
            target.write_bytes(old)
            class Writer:
                def __init__(self, stream):
                    self.stream = stream
                def __enter__(self):
                    return self
                def write(self, value):
                    if fault == "write":
                        self.stream.write(value[:3])
                        raise OSError("synthetic write")
                    self.stream.write(value)
                def flush(self):
                    if fault == "flush":
                        raise OSError("synthetic flush")
                    self.stream.flush()
                def __exit__(self, *args):
                    self.stream.close()
            def opened(path, *args, **kwargs):
                stream = real_open(path, *args, **kwargs)
                return Writer(stream) if args and args[0] == "w" else stream
            observation = {}
            with patch.object(Path, "open", opened), \
                    (patch.object(Path, "replace", side_effect=OSError("synthetic replace")) if fault == "replace" else nullcontext()):
                x10._diagnostic(self.ctx, observation, "synthetic", ValueError("private"))
            if fault == "none":
                self.assertEqual(json.loads(target.read_text())["exception_type"], "ValueError")
            else:
                self.assertEqual(target.read_bytes(), old)
                self.assertFalse(observation["diagnostic_saved"])
            self.assertEqual(list(self.ctx.out_dir.glob(".x10-diagnostic-*")), [])

    def test_cleanup_preserves_replacement_record_or_screenshot(self):
        page = MagicMock()
        page.get_by_role.return_value.screenshot.side_effect = lambda **kw: Path(kw["path"]).write_bytes(tiny_png())
        for replaced in ("record", "screenshot", "unchanged"):
            observation = dict(**canonical_project(), fresh_project=True)
            x10._save(page, self.ctx, observation)
            directory = self.ctx.out_dir / observation["evidence_path"].split("/")[0]
            if replaced == "record":
                record = directory / "x10-ui.json"
                value = json.loads(record.read_text())
                value["replacement"] = True
                record.write_text(json.dumps(value), encoding="utf-8")
            elif replaced == "screenshot":
                (directory / "x10-ground.png").write_bytes(tiny_png(raw=b"\x00\xff\x00\x00"))
            before = {path.name: path.read_bytes() for path in directory.iterdir()}
            x10._invalidate_evidence(self.ctx, observation)
            self.assertFalse(observation["evidence_saved"])
            if replaced == "unchanged":
                self.assertFalse(directory.exists())
            else:
                self.assertEqual({path.name: path.read_bytes() for path in directory.iterdir()}, before)

    def test_classification_requires_generation_pointer_and_screenshot_pin(self):
        original = ClassificationTests().observation()
        for refusal in (False, True):
            baseline = dict(original)
            if refusal:
                baseline.update(import_rejection=x10.IMPORT_REFUSALS[0], imported_version_selected=False)
            invalid = {"evidence_path": [None, "", "../escape", "x10-ui.json", original["evidence_path"] + "\n"],
                       "screenshot_bytes": [None, 0, -1, True, 1.5, x10.MAX_SCREENSHOT_BYTES + 1],
                       "screenshot_sha256": [None, "", "A" * 64, "a" * 63, "a" * 64 + "\n"]}
            for key, values in invalid.items():
                missing = dict(baseline)
                missing.pop(key)
                self.assertEqual([r.verdict for r in x10.classify(missing)], ["UNREADABLE"] * 6)
                for value in values:
                    self.assertEqual([r.verdict for r in x10.classify(dict(baseline, **{key: value}))], ["UNREADABLE"] * 6)
            for size in (1, x10.MAX_SCREENSHOT_BYTES):
                self.assertEqual(x10.classify(dict(baseline, screenshot_bytes=size))[0].verdict, "FAIL")

    def test_evidence_json_readback_is_bounded_regular_and_strict(self):
        path = Path(self.temp.name) / "record.json"
        path.write_bytes(b'{"valid":true}')
        self.assertEqual(x10._read_evidence_json(path)[0], {"valid": True})
        path.write_bytes(b'{"duplicate":1,"duplicate":2}')
        with self.assertRaises(ValueError):
            x10._read_evidence_json(path)
        for mode in (stat.S_IFLNK, stat.S_IFDIR):
            with patch.object(Path, "lstat", return_value=SimpleNamespace(st_mode=mode)), \
                    patch.object(x10.os, "open") as opened, self.assertRaises(ValueError):
                x10._read_evidence_json(path)
            opened.assert_not_called()
        info = path.lstat()
        stream, manager = MagicMock(), MagicMock()
        manager.__enter__.return_value = stream
        stream.read.return_value = b" " * (x10.MAX_JSON_BYTES + 1)
        with patch.object(x10.os, "open", return_value=123), patch.object(x10.os, "close") as closed, \
                patch.object(x10.os, "fdopen", return_value=manager), patch.object(x10.os, "fstat", return_value=info), \
                self.assertRaises(ValueError):
            x10._read_evidence_json(path)
        stream.read.assert_called_once_with(x10.MAX_JSON_BYTES + 1)
        closed.assert_called_once_with(123)
        page = MagicMock()
        page.get_by_role.return_value.screenshot.side_effect = lambda **kw: Path(kw["path"]).write_bytes(tiny_png())
        observation = dict(**canonical_project(), fresh_project=True)
        x10._save(page, self.ctx, observation)
        old = list(self.ctx.out_dir.iterdir())
        writer = x10._write_evidence_json
        def duplicate(path, value):
            writer(path, value)
            path.write_bytes(b'{"evidence_saved":true,"evidence_saved":true}')
        with patch.object(x10, "_write_evidence_json", duplicate), self.assertRaises(ValueError):
            x10._save(page, self.ctx, observation)
        self.assertEqual(list(self.ctx.out_dir.iterdir()), old)
        self.assertFalse(observation["evidence_saved"])

    def test_only_complete_200_catalog_responses_can_supply_proof(self):
        for status in (200, 204, 206):
            observation = self.project_identity()
            observation.update(imported_version_id=VERSION, imported_drawing_id=DRAWING,
                               selected_sequence=1, imported_version_selected=True, evidence_saved=True,
                               ground_option="Ground Physical unavailable")
            listener = x10._catalog_observer(self.ctx.base, observation)
            request = self.catalog_request()
            self.complete_catalog(listener, request, status=status)
            page = MagicMock()
            page.locator.return_value.is_visible.return_value = True
            page.locator.return_value.inner_text.return_value = VERSION
            if status == 200:
                x10._current_identity(page, observation)
            else:
                with self.assertRaises(ValueError):
                    x10._current_identity(page, observation)
                observation["evidence_saved"] = False
                self.assertEqual(aggregate(x10.classify(observation)), "UNREADABLE")
                listener.response(SimpleNamespace(request=request, url=request.url, status=200))
                self.assertIsNone(observation["catalog_binding"])

    def test_navigation_actions_share_deadline_without_real_waits(self):
        for method in ("goto", "reload", "go_back", "go_forward"):
            now = [0.0]
            page = MagicMock()
            observed_timeouts = []
            def action(*args, **kwargs):
                observed_timeouts.append(kwargs["timeout"])
                now[0] += 0.6
            getattr(page, method).side_effect = action
            proxy = x10._BudgetedUI(page, x10._ObservationBudget(1, lambda: now[0]))
            getattr(proxy, method)(timeout=30000)
            with self.assertRaises(TimeoutError):
                getattr(proxy, method)(timeout=30000)
            self.assertEqual(observed_timeouts, [1000, 400])

    def test_cancellation_cleans_observe_and_session_exit_before_propagating(self):
        old_page, _, _ = self.integration_page(True)
        self.run_page(old_page)
        old = list(self.ctx.out_dir.glob("x10-evidence-*"))
        for cancel in (KeyboardInterrupt, SystemExit):
            for phase in ("observe", "session-exit"):
                page, _, _ = self.integration_page(True)
                closed = []
                @contextmanager
                def session(*args, **kwargs):
                    try:
                        yield page, self.session_info
                    finally:
                        closed.append(True)
                        if phase == "session-exit":
                            raise cancel("synthetic cancel")
                if phase == "observe":
                    remove = page.remove_listener
                    original_remove = remove.side_effect
                    def removal(*args):
                        if original_remove:
                            original_remove(*args)
                        if args[0] == "request":
                            raise cancel("synthetic cancel")
                    # Raise after a successful observe save, during the return
                    # boundary, while requiring all removals to be attempted.
                    page.remove_listener.side_effect = removal
                with patch.object(x10, "signed_in", session), self.assertRaises(cancel):
                    x10.run(self.ctx)
                self.assertEqual(closed, [True])
                self.assertEqual(page.remove_listener.call_args_list, page.on.call_args_list)
                self.assertEqual(list(self.ctx.out_dir.glob("x10-evidence-*")), old)

    def test_failed_evidence_cleanup_retains_original_error_and_cannot_score(self):
        page = MagicMock()
        page.get_by_role.return_value.screenshot.side_effect = lambda **kw: Path(kw["path"]).write_bytes(tiny_png())
        baseline = dict(**canonical_project(), fresh_project=True)
        x10._save(page, self.ctx, baseline)
        old = self.ctx.out_dir / baseline["evidence_path"].split("/")[0]
        original_readback = x10._read_evidence_json
        for phase in ("staging", "committed"):
            observation = dict(**canonical_project(), fresh_project=True)
            def readback(path):
                if (phase == "staging" and path.parent.name.startswith(".x10-staging-")) or (
                        phase == "committed" and path.parent.name.startswith("x10-evidence-")):
                    raise ValueError("original synthetic readback failure")
                return original_readback(path)
            with patch.object(x10, "_read_evidence_json", readback), \
                    patch.object(x10.shutil, "rmtree", side_effect=OSError("secondary cleanup failure")), \
                    self.assertRaisesRegex(ValueError, "original synthetic readback failure"):
                x10._save(page, self.ctx, observation)
            self.assertFalse(observation["evidence_saved"])
            self.assertEqual(observation["evidence_cleanup_errors"], ["OSError"])
            self.assertEqual(aggregate(x10.classify(observation)), "UNREADABLE")
            self.assertTrue((old / "x10-ui.json").exists())
            for partial in self.ctx.out_dir.iterdir():
                if partial != old:
                    self.assertFalse((partial / "x10-ui.json").exists())

    def test_concurrent_destination_owner_survives_losing_attempt(self):
        page = MagicMock()
        page.get_by_role.return_value.screenshot.side_effect = lambda **kw: Path(kw["path"]).write_bytes(tiny_png())
        original_commit = x10._commit_generation
        peer = []
        def collision(staging, destination):
            destination.mkdir()
            (destination / "peer-material").write_bytes(b"peer-owned")
            peer.append(destination)
            return original_commit(staging, destination)
        observation = dict(**canonical_project(), fresh_project=True)
        with patch.object(x10, "_commit_generation", collision), self.assertRaises(OSError):
            x10._save(page, self.ctx, observation)
        self.assertEqual((peer[0] / "peer-material").read_bytes(), b"peer-owned")
        self.assertEqual(list(peer[0].iterdir()), [peer[0] / "peer-material"])
        self.assertEqual(list(self.ctx.out_dir.iterdir()), peer)
        self.assertFalse(observation["evidence_saved"])
        self.assertEqual(aggregate(x10.classify(observation)), "UNREADABLE")

    def test_diagnostic_exception_labels_are_never_persisted(self):
        drawing, project = "PRIVATE_CUSTOMER_DRAWING.dwg", "PRIVATE_CUSTOMER_PROJECT"
        for category in (ValueError, RuntimeError, OSError):
            observation = {}
            x10._diagnostic(self.ctx, observation, "primary drawing attachment", category(
                drawing + " in " + project + " https://private.invalid"))
            saved = (self.ctx.out_dir / "x10-ui.json").read_text()
            self.assertIn("primary drawing attachment", saved)
            self.assertIn(category.__name__, saved)
            for secret in (drawing, project, "private.invalid"):
                self.assertNotIn(secret, saved)

    def test_project_204_and_206_invalidate_previous_success_terminally(self):
        for status in (200, 204, 206):
            observation = self.project_identity()
            observation.update(imported_version_selected=False, evidence_saved=True,
                               import_rejection=x10.IMPORT_REFUSALS[0])
            listener = x10._project_observer(self.ctx.base, observation)
            request = SimpleNamespace(frame=MAIN_FRAME, method="GET", url=self.ctx.base + "/api/projects/" + PROJECT)
            listener(request)
            response = metadata_response(request, {"project": {"id": PROJECT}, "drawing_versions": []})
            listener.response(response)
            self.assertEqual(aggregate(x10.classify(observation)), "FAIL")
            response.status = status
            listener.response(response)
            self.assertEqual(aggregate(x10.classify(observation)), "FAIL" if status == 200 else "UNREADABLE")
            if status != 200:
                response.status = 200
                listener.response(response)
                self.assertIsNone(observation["project_binding"])

    def test_project_version_sequences_are_strict_positive_integers(self):
        for sequence in (True, False, 1.0, 1.5, "1", None, 0, -1, 1, 999999999):
            observation = {}
            listener = x10._project_observer(self.ctx.base, observation)
            self.complete_project(listener, versions=[{"version_id": VERSION, "drawing_id": DRAWING, "seq": sequence}])
            accepted = type(sequence) is int and 1 <= sequence <= 999999999
            self.assertEqual(observation["project_binding"] == PROJECT, accepted)

    def test_retained_callbacks_cannot_mutate_finalized_observation(self):
        for refusal in (True, False):
            page, _, _ = self.integration_page(refusal)
            rows = self.run_page(page)
            observed = rows[0].observed
            before = json.dumps(observed, sort_keys=True)
            files = {str(path): path.read_bytes() for path in self.ctx.out_dir.rglob("*") if path.is_file()}
            handlers = dict(call.args for call in page.on.call_args_list)
            project_request = SimpleNamespace(frame=MAIN_FRAME, method="GET", url=self.ctx.base + "/api/projects/" + OTHER)
            for request in (project_request, self.catalog_request(project=OTHER)):
                handlers["request"](request)
                handlers["response"](metadata_response(request, {"project": {"id": OTHER}, "drawing_versions": []}))
                handlers["requestfailed"](request)
            handlers["framenavigated"](MAIN_FRAME)
            self.assertEqual(json.dumps(observed, sort_keys=True), before)
            self.assertEqual({str(path): path.read_bytes() for path in self.ctx.out_dir.rglob("*") if path.is_file()}, files)

    def test_png_zlib_tail_refused_but_split_idat_is_valid(self):
        def chunk(kind, payload):
            return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)
        compressed = zlib.compress(b"\x00\x00\x00\x00")
        original = tiny_png()
        split = (original[:33] + chunk(b"IDAT", compressed[:3]) + chunk(b"IDAT", compressed[3:])
                 + chunk(b"IEND", b""))
        page = MagicMock()
        for data, valid in ((split, True), (tiny_png(compressed=compressed + b"tail"), False),
                            (tiny_png(compressed=compressed + zlib.compress(b"")), False)):
            page.get_by_role.return_value.screenshot.side_effect = lambda **kw: Path(kw["path"]).write_bytes(data)
            observation = dict(**canonical_project(), fresh_project=True)
            if valid:
                x10._save(page, self.ctx, observation)
            else:
                with self.assertRaises(ValueError):
                    x10._save(page, self.ctx, observation)
                self.assertFalse(observation["evidence_saved"])

    def test_json_inputs_and_project_metadata_require_strict_utf8(self):
        for role in ("oracle", "rates"):
            path = Path(next(row["path"] for row in self.ctx.manifest["fixtures"] if row["role"] == role))
            original = path.read_bytes()
            for encoding in ("utf-8-sig", "utf-16", "utf-32"):
                self.replace_fixture(role, original.decode().encode(encoding))
                self.assert_binding_refusal()
            self.replace_fixture(role, original[:-1] + ',"unicode":"café 界"}'.encode())
            self.assertEqual(x10.bindings(self.ctx)[3], Decimal("0.07"))
            self.replace_fixture(role, original)
        data = json.dumps({"project": {"id": PROJECT}, "drawing_versions": [], "unicode": "café 界"}, ensure_ascii=False)
        for encoding in ("utf-8", "utf-8-sig", "utf-16", "utf-32"):
            observation = {}
            listener = x10._project_observer(self.ctx.base, observation)
            request = SimpleNamespace(frame=MAIN_FRAME, method="GET", url=self.ctx.base + "/api/projects/" + PROJECT)
            listener(request)
            listener.response(SimpleNamespace(request=request, url=request.url, status=200,
                                               body=lambda: data.encode(encoding)))
            self.assertEqual(observation["project_binding"] == PROJECT, encoding == "utf-8")

    def test_passive_request_retention_and_budget_are_bounded(self):
        class Request:
            frame = MAIN_FRAME
            method = "GET"
            def __init__(self, url):
                self.url = url
        for factory in (x10._catalog_observer, x10._project_observer):
            observation = {}
            listener = factory(self.ctx.base, observation, max_requests=8)
            references = []
            url = self.catalog_request().url if factory is x10._catalog_observer else self.ctx.base + "/api/projects/" + PROJECT
            key = "catalog_binding" if factory is x10._catalog_observer else "project_binding"
            for index in range(1000):
                request = Request(url)
                references.append(weakref.ref(request))
                listener(request)
                response = metadata_response(request, {"project": {"id": PROJECT}, "drawing_versions": []})
                listener.response(response)
                if index < 8:
                    self.assertIsNotNone(observation[key])
                else:
                    self.assertIsNone(observation[key])
            del response, request
            gc.collect()
            self.assertEqual(sum(ref() is not None for ref in references), 0)
            self.assertTrue(observation["event_callback_failed"])
            for factory_budget in (0, -1, True):
                with self.assertRaises(ValueError):
                    factory(self.ctx.base, {}, max_requests=factory_budget)

    def test_png_chunk_order_rejects_ambiguous_critical_layouts(self):
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
        data = tiny_png()
        header, end = data[:33], chunk(b"IEND", b"")
        compressed = zlib.compress(b"\x00\x00\x00\x00")
        first, second = chunk(b"IDAT", compressed[:3]), chunk(b"IDAT", compressed[3:])
        ancillary = chunk(b"tEXt", b"synthetic\x00value")
        cases = [(header + first + second + end, True),
                 (header + ancillary + first + second + end, True),
                 (header + data[8:33] + first + second + end, False),
                 (header + first + ancillary + second + end, False),
                 (header + chunk(b"ABCD", b"") + first + second + end, False),
                 (header + first + second + chunk(b"PLTE", b"\x00\x00\x00") + end, False)]
        page = MagicMock()
        for image, accepted in cases:
            page.get_by_role.return_value.screenshot.side_effect = lambda **kw: Path(kw["path"]).write_bytes(image)
            observation = dict(**canonical_project(), fresh_project=True)
            if accepted:
                x10._save(page, self.ctx, observation)
            else:
                with self.assertRaises(ValueError):
                    x10._save(page, self.ctx, observation)
                self.assertFalse(observation["evidence_saved"])

    def test_identity_urls_reject_userinfo_fragment_encoding_and_duplicate_keys(self):
        for factory in (x10._catalog_observer, x10._project_observer):
            url = self.catalog_request().url if factory is x10._catalog_observer else self.ctx.base + "/api/projects/" + PROJECT
            invalid = [url.replace("https://", "https://user@"), url + "#fragment",
                       url.replace("/api/", "/api%2F")]
            if factory is x10._catalog_observer:
                invalid += [url + "&%70roject_id=" + PROJECT, url + "&input_version%5fid=" + VERSION]
            else:
                invalid += [url + "?project_id=" + PROJECT + "&%70roject_id=" + PROJECT]
            for bad in invalid:
                observation = {}
                listener = factory(self.ctx.base, observation)
                request = SimpleNamespace(frame=MAIN_FRAME, method="GET", url=bad)
                listener(request)
                listener.response(metadata_response(request, {"project": {"id": PROJECT}, "drawing_versions": []}))
                key = "catalog_binding" if factory is x10._catalog_observer else "project_binding"
                self.assertIsNone(observation[key])

    def test_seeded_event_differential_reference_for_identity_and_teardown(self):
        # Reference model uses integer request IDs and status rules, independent
        # of URL parsing, UUID extraction and observer closures.
        for seed in (7, 101, 20261115):
            for kind in ("project", "catalog"):
                rng = random.Random(seed)
                events = [("request", 0, 200)]
                count = 1
                for _ in range(80):
                    action = rng.choice(("request", "response", "failure", "navigation"))
                    if action == "request":
                        identity = count
                        count += 1
                    else:
                        identity = rng.randrange(count)
                    events.append((action, identity, rng.choice((200, 204, 206, 500))))
                events += [("teardown", 0, 200), ("request", count, 200), ("response", count, 200)]
                page = MagicMock()
                page.main_frame = MAIN_FRAME
                actual, handlers = {}, {}
                project = x10._project_observer(self.ctx.base, actual, MAIN_FRAME)
                catalog = x10._catalog_observer(self.ctx.base, actual, MAIN_FRAME)
                callbacks = {"request": lambda event: (project(event), catalog(event)),
                             "response": lambda event: (project.response(event), catalog.response(event)),
                             "failure": lambda event: (project.failed(event), catalog.failed(event))}
                reference = dict(active=True, latest=None, failed=False, bound=None, navigated=False)
                requests = {}
                active = True
                def dispatch(action, event):
                    nonlocal active
                    if not active:
                        return
                    if action == "teardown":
                        active = False
                    elif action == "navigation":
                        actual.update(event_callback_failed=True, project_binding=None, catalog_binding=None)
                    else:
                        callbacks[action](event)
                for position, (action, identity, status) in enumerate(events):
                    if action == "request":
                        url = (self.ctx.base + "/api/projects/" + (PROJECT if identity % 2 == 0 else OTHER)
                               if kind == "project" else self.catalog_request(project=PROJECT if identity % 2 == 0 else OTHER).url)
                        event = SimpleNamespace(frame=MAIN_FRAME, method="GET", url=url)
                        requests[identity] = event
                    elif action == "response":
                        request = requests[identity]
                        event = metadata_response(request, {"project": {"id": PROJECT if identity % 2 == 0 else OTHER},
                                                            "drawing_versions": []}, status=status)
                    else:
                        event = requests.get(identity)
                    if reference["active"]:
                        if action == "teardown":
                            reference["active"] = False
                        elif action == "navigation":
                            reference.update(bound=None, navigated=True)
                        elif action == "request":
                            reference.update(latest=identity, failed=False, bound=None)
                        elif identity == reference["latest"]:
                            if action == "failure" or status != 200:
                                reference.update(failed=True, bound=None)
                            elif not reference["failed"]:
                                reference["bound"] = PROJECT if identity % 2 == 0 else OTHER
                    dispatch(action, event)
                    binding = actual.get("project_binding" if kind == "project" else "catalog_binding")
                    value = binding.get("project_id") if isinstance(binding, dict) else binding
                    expected = reference["bound"] if not reference["navigated"] else None
                    current = value if not actual.get("event_callback_failed") else None
                    self.assertEqual(current, expected, msg=f"seed={seed} kind={kind} event={position} sequence={events[:position+1]}")

    def test_seeded_differential_drives_actual_observe_callbacks(self):
        # Exercise observe's registered wrappers, including navigation and its
        # post-teardown guard. The reference stores only integer lifecycle IDs.
        for seed in (7, 101, 20261115):
            for kind in ("project", "catalog"):
                with self.subTest(seed=seed, kind=kind):
                    page, surface, _ = self.integration_page(False)
                    captured = {"processed": 0, "mismatches": []}
                    original_save = x10._save
                    def save(page, ctx, observation, *args, **kwargs):
                        captured["observation"] = observation
                        return original_save(page, ctx, observation, *args, **kwargs)
                    rng = random.Random(seed)
                    events = [("request", 0, 200), ("response", 0, 200),
                              ("request", 1, 200), ("response", 0, 200),
                              ("failure", 1, 200), ("response", 1, 200)]
                    count = 2
                    for _ in range(40):
                        action = rng.choice(("request", "response", "failure"))
                        identity = count if action == "request" else rng.randrange(count)
                        if action == "request":
                            count += 1
                        events.append((action, identity, rng.choice((200, 204, 206, 500))))
                    events.insert(20, ("navigation", 0, 200))
                    requests = {}
                    def capture(**kwargs):
                        Path(kwargs["path"]).write_bytes(tiny_png())
                        handlers = dict(call.args for call in page.on.call_args_list)
                        actual = captured["observation"]
                        reference = dict(latest=None, failed=False, bound=None, navigated=False)
                        for position, (action, identity, status) in enumerate(events):
                            if action == "request":
                                project_id = PROJECT if identity % 2 == 0 else OTHER
                                url = (self.ctx.base + "/api/projects/" + project_id if kind == "project"
                                       else self.catalog_request(project=project_id).url)
                                requests[identity] = SimpleNamespace(frame=MAIN_FRAME, method="GET", url=url)
                                handlers["request"](requests[identity])
                                reference.update(latest=identity, failed=False, bound=None)
                            elif action == "navigation":
                                MAIN_FRAME.url = "https://foreign.test/away"
                                handlers["framenavigated"](MAIN_FRAME)
                                # A return to the original route cannot erase the event.
                                MAIN_FRAME.url = self.ctx.base + "/app"
                                handlers["framenavigated"](MAIN_FRAME)
                                reference.update(bound=None, navigated=True)
                            else:
                                request = requests[identity]
                                if action == "failure":
                                    handlers["requestfailed"](request)
                                else:
                                    handlers["response"](metadata_response(request, {
                                        "project": {"id": PROJECT if identity % 2 == 0 else OTHER},
                                        "drawing_versions": []}, status=status))
                                if identity == reference["latest"]:
                                    if action == "failure" or status != 200:
                                        reference.update(failed=True, bound=None)
                                    elif not reference["failed"]:
                                        reference["bound"] = PROJECT if identity % 2 == 0 else OTHER
                            binding = actual.get("project_binding" if kind == "project" else "catalog_binding")
                            value = binding.get("project_id") if isinstance(binding, dict) else binding
                            current = None if actual.get("event_callback_failed") else value
                            expected = None if reference["navigated"] else reference["bound"]
                            captured["processed"] += 1
                            if current != expected:
                                captured["mismatches"].append(
                                    f"seed={seed} kind={kind} event={position} current={current} expected={expected} sequence={events[:position+1]}")
                    surface.screenshot.side_effect = capture
                    try:
                        with patch.object(x10, "_save", save):
                            rows = self.run_page(page)
                        self.assertEqual(captured["processed"], len(events),
                                         msg=f"seed={seed} kind={kind} incomplete callback replay")
                        self.assertEqual(captured["mismatches"], [])
                        self.assertEqual(rows[0].verdict, "UNREADABLE")
                        actual = captured["observation"]
                        self.assertFalse(actual["evidence_saved"])
                        self.assertEqual(page.remove_listener.call_args_list, page.on.call_args_list)
                        before = json.dumps(actual, sort_keys=True)
                        handlers = dict(call.args for call in page.on.call_args_list)
                        # Actual retained callbacks are inert after observe's finally.
                        for request in requests.values():
                            handlers["request"](request)
                            handlers["response"](metadata_response(request, {
                                "project": {"id": PROJECT}, "drawing_versions": []}))
                            handlers["requestfailed"](request)
                        handlers["framenavigated"](MAIN_FRAME)
                        self.assertEqual(json.dumps(actual, sort_keys=True), before,
                                         msg=f"seed={seed} kind={kind} teardown")
                    finally:
                        MAIN_FRAME.url = self.ctx.base + "/app"

    def test_failed_then_successful_runs_isolate_projects_fixtures_and_listeners(self):
        first, surface, _ = self.integration_page(False)
        surface.screenshot.side_effect = OSError("synthetic capture failure")
        failed = self.run_page(first)[0].observed
        self.assertFalse(failed["evidence_saved"])
        self.assertNotIn("evidence_path", failed)
        self.assertEqual(first.remove_listener.call_args_list, first.on.call_args_list)
        before_failed = json.dumps(failed, sort_keys=True)
        old_handlers = dict(call.args for call in first.on.call_args_list)
        second_case = FixtureTests()
        second_case.setUp()
        try:
            self.assertNotEqual(self.ctx.out_dir, second_case.ctx.out_dir)
            self.assertTrue(set(row["path"] for row in self.ctx.fixtures).isdisjoint(
                row["path"] for row in second_case.ctx.fixtures))
            catalog_request = second_case.catalog_request
            second_case.catalog_request = lambda **kwargs: catalog_request(project=OTHER, **kwargs)
            second_case.emit_project = lambda page: FixtureTests.emit_project(second_case, page, OTHER)
            with patch(__name__ + ".PROJECT", OTHER):
                second, _, _ = second_case.integration_page(False)
                result = second_case.run_page(second)[0]
                self.assertEqual(result.verdict, "FAIL")  # rendered blocker, never bench PASS
                observed = result.observed
                self.assertEqual(observed["project_id"], OTHER)
                self.assertEqual(observed["project_binding"], OTHER)
                self.assertTrue(observed["evidence_saved"])
                self.assertTrue((second_case.ctx.out_dir / observed["evidence_path"].split(" / ")[0]).is_file())
                before_second = json.dumps(observed, sort_keys=True)
                for handler in old_handlers.values():
                    handler(SimpleNamespace(parent_frame=None, url="https://foreign.test/late"))
                self.assertEqual(json.dumps(observed, sort_keys=True), before_second)
                self.assertEqual(json.dumps(failed, sort_keys=True), before_failed)
                self.assertEqual(second.remove_listener.call_args_list, second.on.call_args_list)
                self.assertTrue(all(old is not new for old, new in zip(
                    old_handlers.values(), dict(call.args for call in second.on.call_args_list).values())))
        finally:
            second_case.doCleanups()

    def test_subprocess_hash_seed_determinism_for_verdicts_and_cents(self):
        script = r'''
import importlib.util, json, pathlib, socket, sys
path = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("x10_synthetic_tests", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
def forbidden(*args, **kwargs):
    raise AssertionError("network forbidden")
socket.socket = forbidden
case = module.ClassificationTests()
observations = [case.observation(), case.observation(source=""),
                case.observation(project_binding=module.OTHER),
                case.observation(screenshot_sha256="")]
rows = [[[row.n, row.verdict, row.evidence] for row in module.x10.classify(obs)] for obs in observations]
total = module.x10.rate_total({"items": [{"quantity":"0.1","unit_rate":"0.2"},
                                       {"quantity":"0.5","unit_rate":"0.1"}]})
print(json.dumps({"rows":rows,"cents":str(total)}, sort_keys=True))
'''
        results = []
        for seed in ("0", "1", "20261115"):
            completed = subprocess.run([sys.executable, "-I", "-c", script, str(Path(__file__).resolve())],
                env={"PYTHONHASHSEED": seed}, capture_output=True, text=True, timeout=30,
                cwd=Path(__file__).resolve().parents[3])
            self.assertEqual(completed.returncode, 0, msg=f"seed={seed} {completed.stderr}")
            value = json.loads(completed.stdout)
            self.assertEqual(value["cents"], "0.07")
            self.assertNotIn("PASS", [row[1] for rows in value["rows"] for row in rows])
            results.append(value)
        self.assertTrue(all(value == results[0] for value in results[1:]))

    def test_png_transparency_chunks_require_legal_unique_bounded_values(self):
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
        cases = []
        for color, raw, transparency in ((0, b"\0\0", b"\0\0"),
                (2, b"\0\0\0\0", b"\0" * 6), (3, b"\0\0", b"\xff"),
                (4, b"\0\0\0", b"\0\0"), (6, b"\0\0\0\0\0", b"\0" * 6)):
            png = tiny_png(color=color, raw=raw, palette=b"\0\0\0" if color == 3 else None)
            position = png.index(b"IDAT") - 4
            trns = chunk(b"tRNS", transparency)
            cases.append((png[:position] + trns + png[position:], color in (0, 2, 3)))
            cases.append((png[:position] + trns + trns + png[position:], False))
            if color == 3:
                cases.append((png[:position] + chunk(b"tRNS", b"\xff\xff") + png[position:], False))
                cases.append((png[:33] + trns + png[33:], False))
            if color == 0:
                cases.append((png[:position] + chunk(b"tRNS", b"\1\0") + png[position:], False))
        path = Path(self.temp.name) / "transparency.png"
        for data, accepted in cases:
            path.write_bytes(data)
            if accepted:
                self.assertEqual(x10._valid_png(path), data)
            else:
                with self.assertRaises(ValueError):
                    x10._valid_png(path)

    def test_spaces_and_unicode_paths_preserve_binding_publication_and_cleanup(self):
        folder = Path(self.temp.name) / "synthetic fixtures \u00e9 \u592a\u967d"
        folder.mkdir()
        for row in self.ctx.manifest["fixtures"]:
            old = Path(row["path"])
            new = folder / old.name
            old.rename(new)
            row["path"] = str(new)
            proof = next(proof for proof in self.ctx.fixtures if proof["id"] == row["id"])
            proof["path"] = str(new)
        self.ctx.out_dir = folder / "evidence \u00e9 \u592a\u967d"
        self.assertEqual(x10.bindings(self.ctx)[3], Decimal("0.07"))
        page = MagicMock()
        page.get_by_role.return_value.screenshot.side_effect = lambda **kw: Path(kw["path"]).write_bytes(tiny_png())
        observation = dict(**canonical_project(), fresh_project=True)
        x10._save(page, self.ctx, observation)
        record = self.ctx.out_dir / observation["evidence_path"].split(" / ")[0]
        self.assertTrue(record.is_file())
        self.assertEqual(hashlib.sha256(record.read_bytes()).hexdigest(), observation["evidence_record_sha256"])
        x10._diagnostic(self.ctx, dict(observation), "synthetic", OSError("synthetic"))
        self.assertTrue((self.ctx.out_dir / "x10-ui.json").is_file())
        x10._invalidate_evidence(self.ctx, observation)
        self.assertFalse(record.exists())
        self.assertFalse(observation["evidence_saved"])
        self.assertTrue(all(Path(row["path"]).is_file() for row in self.ctx.fixtures))

    def test_page_close_and_crash_invalidate_only_current_evidence(self):
        for event in ("close", "crash"):
            for phase in ("capture", "committed"):
                with self.subTest(event=event, phase=phase):
                    previous = dict(**canonical_project(), fresh_project=True)
                    old_page = MagicMock()
                    old_page.get_by_role.return_value.screenshot.side_effect = lambda **kw: Path(kw["path"]).write_bytes(tiny_png())
                    x10._save(old_page, self.ctx, previous)
                    old_record = self.ctx.out_dir / previous["evidence_path"].split(" / ")[0]
                    old_bytes = old_record.read_bytes()
                    page, surface, _ = self.integration_page(False)
                    original_save = x10._save
                    def terminate():
                        dict(call.args for call in page.on.call_args_list)[event]()
                    def capture(**kwargs):
                        Path(kwargs["path"]).write_bytes(tiny_png())
                        if phase == "capture":
                            terminate()
                    def save(*args, **kwargs):
                        original_save(*args, **kwargs)
                        if phase == "committed":
                            terminate()
                    surface.screenshot.side_effect = capture
                    with patch.object(x10, "_save", save):
                        rows = self.run_page(page)
                    self.assertEqual(rows[0].verdict, "UNREADABLE")
                    self.assertFalse(rows[0].observed["evidence_saved"])
                    self.assertNotIn("evidence_path", rows[0].observed)
                    self.assertEqual(page.remove_listener.call_args_list, page.on.call_args_list)
                    self.assertEqual(old_record.read_bytes(), old_bytes)

    def test_invalid_budget_configuration_refuses_before_browser_entry(self):
        invalid = (True, False, 0, -1, float("nan"), float("inf"), -float("inf"), "600", None)
        for value in invalid:
            with self.subTest(value=value):
                self.ctx.manifest["x10"]["observation_budget_seconds"] = value
                clock = MagicMock(return_value=10.0)
                with patch.object(x10.time, "monotonic", clock), patch.object(x10, "signed_in") as session:
                    rows = x10.run(self.ctx)
                session.assert_not_called()
                self.assertEqual([row.verdict for row in rows], ["UNREADABLE"] * 6)
                clock.assert_not_called()
        self.ctx.manifest["x10"].pop("observation_budget_seconds")
        for seconds in (1, 0.25):
            budget = x10._ObservationBudget(seconds, lambda: 10.0)
            self.assertEqual(budget.milliseconds(), int(seconds * 1000))

    def test_malformed_evidence_records_preserve_unrelated_material(self):
        cases = (b"[" * 65 + b"0" + b"]" * 65, b'{"invalid":"\xff"}', b" " * (x10.MAX_JSON_BYTES + 1))
        for malformed in cases:
            observation = dict(**canonical_project(), fresh_project=True)
            page = MagicMock()
            page.get_by_role.return_value.screenshot.side_effect = lambda **kw: Path(kw["path"]).write_bytes(tiny_png())
            x10._save(page, self.ctx, observation)
            record = self.ctx.out_dir / observation["evidence_path"].split(" / ")[0]
            peer = record.parent / "unrelated.bin"
            peer.write_bytes(b"synthetic peer material")
            record.write_bytes(malformed)
            with self.assertRaises(ValueError):
                x10._read_evidence_json(record)
            x10._invalidate_evidence(self.ctx, observation)
            self.assertEqual(record.read_bytes(), malformed)
            self.assertEqual(peer.read_bytes(), b"synthetic peer material")
            self.assertFalse(observation["evidence_saved"])
            self.assertEqual([row.verdict for row in x10.classify(observation)], ["UNREADABLE"] * 6)

    def test_png_chunk_count_budget_bounds_empty_chunk_work(self):
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
        png = tiny_png()
        position = png.index(b"IDAT") - 4
        path = Path(self.temp.name) / "chunk-count.png"
        for kind in (b"IDAT", b"tEXt"):
            for count, accepted in ((2, True), (x10.MAX_PNG_CHUNKS, False)):
                image = png[:position] + chunk(kind, b"") * count + png[position:]
                path.write_bytes(image)
                if accepted:
                    self.assertEqual(x10._valid_png(path), image)
                else:
                    with self.assertRaisesRegex(ValueError, "chunk budget"):
                        x10._valid_png(path)

    def test_classify_and_rate_total_leave_nested_inputs_unchanged(self):
        classifier = ClassificationTests()
        for observation in (classifier.observation(extra={"nested": [1, {"value": "synthetic"}]}),
                            classifier.observation(source="", extra={"nested": [1, {"value": "synthetic"}]})):
            before = copy.deepcopy(observation)
            rows = x10.classify(observation, expected={"nested": ["synthetic"]})
            self.assertNotIn("PASS", [row.verdict for row in rows])
            self.assertEqual(observation, before)
        for rates, valid in (({"items": [{"quantity": "2", "unit_rate": "0.25", "extra": {"v": [1, 2]}}]}, True),
                             ({"items": [{"quantity": "2", "unit_rate": "bad", "extra": {"v": [1, 2]}}]}, False)):
            before = copy.deepcopy(rates)
            if valid:
                self.assertEqual(x10.rate_total(rates), Decimal("0.50"))
            else:
                with self.assertRaises(ValueError):
                    x10.rate_total(rates)
            self.assertEqual(rates, before)

    def test_request_metadata_reentrancy_preserves_newer_proof(self):
        for kind, factory in (("project", x10._project_observer), ("catalog", x10._catalog_observer)):
            for attribute in ("url", "method", "frame"):
                for action in ("newer", "failure", "control"):
                    with self.subTest(kind=kind, attribute=attribute, action=action):
                        observation = {}
                        listener = factory(self.ctx.base, observation, MAIN_FRAME)
                        def request(project):
                            url = (self.ctx.base + "/api/projects/" + project if kind == "project"
                                   else self.catalog_request(project=project).url)
                            return SimpleNamespace(frame=MAIN_FRAME, method="GET", url=url)
                        def complete(value, project):
                            listener(value)
                            listener.response(metadata_response(value, {"project": {"id": project},
                                                                       "drawing_versions": []}))
                        prior = request(PROJECT)
                        complete(prior, PROJECT)
                        fired = [False]
                        def dispatch():
                            if fired[0]:
                                return
                            fired[0] = True
                            if action == "newer":
                                complete(request(OTHER), OTHER)
                            elif action == "failure":
                                listener.failed(prior)
                        class Reentrant:
                            def __getattribute__(self, name):
                                if name in ("url", "method", "frame"):
                                    if name == attribute:
                                        dispatch()
                                    return getattr(older, name)
                                return object.__getattribute__(self, name)
                        older = request(PROJECT)
                        event = Reentrant()
                        listener(event)
                        key = "project_binding" if kind == "project" else "catalog_binding"
                        binding = observation[key]
                        identity = binding.get("project_id") if isinstance(binding, dict) else binding
                        self.assertEqual(identity, OTHER if action == "newer" else None)
                        if action != "control":
                            listener.response(metadata_response(event, {"project": {"id": PROJECT},
                                                                        "drawing_versions": []}))
                            binding = observation[key]
                            identity = binding.get("project_id") if isinstance(binding, dict) else binding
                            self.assertEqual(identity, OTHER if action == "newer" else None)

    def test_observe_request_preflight_reentrancy_cannot_restore_old_proof(self):
        for attribute in ("url", "method", "frame"):
            page, surface, _ = self.integration_page(False)
            captured, completed = {}, []
            original_save = x10._save
            def save(page, ctx, observation, *args, **kwargs):
                captured["observation"] = observation
                return original_save(page, ctx, observation, *args, **kwargs)
            def capture(**kwargs):
                Path(kwargs["path"]).write_bytes(tiny_png())
                handlers = dict(call.args for call in page.on.call_args_list)
                older = self.catalog_request()
                fired = [False]
                def dispatch():
                    if not fired[0]:
                        fired[0] = True
                        self.emit_project(page, OTHER)
                        request = self.catalog_request(project=OTHER)
                        handlers["request"](request)
                        handlers["response"](metadata_response(request, {}))
                class Reentrant:
                    def __getattribute__(self, name):
                        if name in ("url", "method", "frame"):
                            if name == attribute:
                                dispatch()
                            return getattr(older, name)
                        return object.__getattribute__(self, name)
                handlers["request"](Reentrant())
                completed.append(captured["observation"].get("catalog_binding"))
            surface.screenshot.side_effect = capture
            with patch.object(x10, "_save", save):
                rows = self.run_page(page)
            self.assertEqual(len(completed), 1)
            self.assertEqual(completed[0]["project_id"], OTHER)
            self.assertEqual(rows[0].verdict, "UNREADABLE")

    def test_json_numeric_token_budgets_before_browser_and_metadata_proof(self):
        invalid = (b"1" * 129, b"1e1001", b"1e-1001", b"1e9999999999999999", b"-0", b"-0.0", b"-0e2")
        valid = (b"0", b"1" * 128, b"1e1000", b"1e-1000")
        for role in ("oracle", "rates"):
            path = Path(next(row["path"] for row in self.ctx.manifest["fixtures"] if row["role"] == role))
            original = path.read_bytes()
            for number in invalid + valid:
                self.replace_fixture(role, original[:-1] + b',"numeric":' + number + b'}')
                if number in invalid:
                    self.assert_binding_refusal()
                else:
                    self.assertEqual(x10.bindings(self.ctx)[3], Decimal("0.07"))
            self.replace_fixture(role, original)
        for number in invalid + valid:
            observation = {}
            listener = x10._project_observer(self.ctx.base, observation)
            request = SimpleNamespace(frame=MAIN_FRAME, method="GET", url=self.ctx.base + "/api/projects/" + PROJECT)
            data = b'{"project":{"id":"' + PROJECT.encode() + b'"},"drawing_versions":[],"numeric":' + number + b'}'
            listener(request)
            listener.response(SimpleNamespace(request=request, url=request.url, status=200, body=lambda: data))
            self.assertEqual(observation["project_binding"] == PROJECT, number in valid)

    def test_directory_link_junction_and_ancestor_substitution_refuse_mutation(self):
        page = MagicMock()
        page.get_by_role.return_value.screenshot.side_effect = lambda **kw: Path(kw["path"]).write_bytes(tiny_png())
        root = self.ctx.out_dir
        root.mkdir()
        real_lstat = Path.lstat
        for kind in ("symlink", "junction"):
            def lstat(path, *args, **kwargs):
                info = real_lstat(path, *args, **kwargs)
                if path == root:
                    return SimpleNamespace(st_mode=stat.S_IFLNK if kind == "symlink" else info.st_mode,
                        st_dev=info.st_dev, st_ino=info.st_ino, st_file_attributes=0x400 if kind == "junction" else 0)
                return info
            with patch.object(Path, "lstat", lstat), self.assertRaises(ValueError):
                x10._save(page, self.ctx, dict(**canonical_project(), fresh_project=True))
        page.get_by_role.assert_not_called()
        observation = dict(**canonical_project(), fresh_project=True)
        x10._save(page, self.ctx, observation)
        owned = root / observation["evidence_path"].split("/")[0]
        before = {path.name: path.read_bytes() for path in owned.iterdir()}
        def replacement(path, *args, **kwargs):
            info = real_lstat(path, *args, **kwargs)
            if path == root:
                return SimpleNamespace(st_mode=info.st_mode, st_dev=info.st_dev, st_ino=info.st_ino + 1,
                                       st_file_attributes=0)
            return info
        with patch.object(Path, "lstat", replacement), patch.object(x10.shutil, "rmtree") as removed:
            x10._invalidate_evidence(self.ctx, observation)
        removed.assert_not_called()
        self.assertEqual({path.name: path.read_bytes() for path in owned.iterdir()}, before)
        changed = [False]
        def race(path, *args, **kwargs):
            return replacement(path, *args, **kwargs) if changed[0] else real_lstat(path, *args, **kwargs)
        def capture(**kwargs):
            Path(kwargs["path"]).write_bytes(tiny_png())
            changed[0] = True
        page.get_by_role.return_value.screenshot.side_effect = capture
        with patch.object(Path, "lstat", race), patch.object(x10, "_write_evidence_json") as writer, \
                patch.object(x10, "_commit_generation") as publisher, self.assertRaises(ValueError):
            x10._save(page, self.ctx, dict(**canonical_project(), fresh_project=True))
        writer.assert_not_called()
        publisher.assert_not_called()
        self.assertEqual({path.name: path.read_bytes() for path in owned.iterdir()}, before)


if __name__ == "__main__":
    unittest.main()
