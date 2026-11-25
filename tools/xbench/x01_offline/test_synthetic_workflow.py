"""Joined, hand-authored synthetic X01 example; never a product or live grader.

Run directly with stdlib unittest. All mutations are in memory; checker arithmetic
is reused, and neither the shared grader nor committed vectors are consulted.
Coordinates are inches, with a 1/25.4 sheet ratio (one inch -> one paper mm).
The source bytes are a custody specimen, independent of the export under test.
"""

import copy
import hashlib
import json
import unittest

try:
    from . import check_cases as cases
    from . import check_plot_layers as layers
    from . import check_unit_equivalence as units
except ImportError:  # Direct script invocation from the repository root.
    import check_cases as cases
    import check_plot_layers as layers
    import check_unit_equivalence as units


SCHEMA = "leaf.xbench.x01.synthetic-workflow.v1"
OBJECT_IDS = ["M1", "T1", "T2", "F1"]


def scenario():
    """One scene: a 10x10 module, two separate tags and a 4% fill strip."""
    scene = dict(
        geometry=dict(original=[dict(id="M1", handle="M1", point=[10, 10])],
                      export=[dict(id="M1/export", handle="M1", point=[10, 10])]),
        boxes=[dict(id="M1", kind="module", center=[15, 15], width=10,
                    height=10, rotation_deg=0, line_count=0),
               dict(id="T1", kind="tag", center=[25, 12], width=2,
                    height=2, rotation_deg=0, line_count=1, paper_text_height_mm=2),
               dict(id="T2", kind="tag", center=[30, 12], width=2,
                    height=2, rotation_deg=0, line_count=1, paper_text_height_mm=2)],
        fill=dict(module=dict(id="M1", rect=[10, 10, 20, 20]),
                  fills=[dict(id="F1", outer=[10, 10, 10.4, 20], holes=[])]),
        # Hand-authored physical equivalents, in OBJECT_IDS order.
        unit_points=[(1, None, [[10, 10], [25, 12], [30, 12], [10, 10]]),
                     (6, None, [[.254, .254], [.635, .3048], [.762, .3048], [.254, .254]]),
                     (0, .0127, [[20, 20], [50, 24], [60, 24], [20, 20]])],
        export=layers.fixture(layers=[("FILLS", ())], entities=[
            layers.entity("M1", "MODULES", "LWPOLYLINE",
                          [(90, 4), (70, 1), (10, 10), (20, 10),
                           (10, 20), (20, 10), (10, 20), (20, 20), (10, 10), (20, 20)]),
            layers.entity("T1", "TAGS", "TEXT", [(10, 25), (20, 12), (40, 2), (1, "T1")]),
            layers.entity("T2", "TAGS", "MTEXT", [(10, 30), (20, 12), (40, 2), (1, "T2")]),
            layers.entity("F1", "FILLS", "SOLID",
                          [(10, 10), (20, 10), (11, 10.4), (21, 10),
                           (12, 10), (22, 20), (13, 10.4), (23, 20)])]),
        policy=dict(request_live_grade=False, numeric_verdict="PASS", diagnostics=[]))
    scene["source_before"] = scene["export"]
    scene["source_after"] = scene["export"]
    return scene


def issue(name, *ids):
    return dict(name=name, object_ids=list(ids))


def evaluate(scene):
    """Local step equivalents only, in a deliberately separate synthetic schema."""
    rows = []

    def row(step, verdict, diagnostics):
        rows.append(dict(step=step, verdict=verdict, diagnostics=diagnostics))

    before, after = scene["source_before"], scene["source_after"]
    if before is None or after is None:
        row(1, "UNREADABLE", [issue("SOURCE_UNREADABLE", *OBJECT_IDS)])
    elif hashlib.sha256(before).digest() != hashlib.sha256(after).digest():
        row(1, "FAIL", [issue("INPUT_CHANGED", *OBJECT_IDS)])
    else:
        row(1, "PASS", [])

    if scene["export"] is None:
        row(2, "UNREADABLE", [issue("EXPORT_ABSENT", "M1")])
    else:
        result = cases.geometry(scene["geometry"])
        row(2, result["verdict"], result["diagnostics"])

    present = {obj["id"] for obj in scene["boxes"] if obj["kind"] == "tag"}
    missing = [tag for tag in ("T1", "T2") if tag not in present]
    if missing:
        row(3, "UNREADABLE", [issue("TAGS_ABSENT", *missing)])
    else:
        result = cases.tag_overlaps(dict(objects=scene["boxes"]))
        row(3, result["verdict"], result["diagnostics"])

    if scene["export"] is None:
        row(4, "UNREADABLE", [issue("EXPORT_ABSENT", *OBJECT_IDS)])
    else:
        result = layers.check_bytes(scene["export"], required_roles=("module", "tag"))
        diagnostics = []
        for item in result["diagnostics"]:
            # Preserve occurrence context and never fabricate an absent handle.
            ids = ([item["handle"]] if item["handle"] is not None else
                   ["T1", "T2"] if item["role"] == "tag" else ["M1"])
            diagnostics.append(dict(item, object_ids=ids))
        row(4, result["verdict"], diagnostics)

    result = cases.fill_coverage(scene["fill"])
    row(5, result["verdict"], result["diagnostics"])

    try:
        reference = None
        for code, scale, points in scene["unit_points"]:
            physical = units.points_metres(points, code, scale)
            if reference is None:
                reference = physical
            else:
                units.require_equivalent(reference, physical)
    except units.UnitError as error:
        row(6, "UNREADABLE" if error.name == "MISSING_UNIT_SCALE" else "FAIL",
            [issue(error.name, *OBJECT_IDS)])
    else:
        row(6, "PASS", [])

    policy = scene["policy"]
    diagnostics = []
    if policy["request_live_grade"] is not False:
        diagnostics.append(issue("LIVE_GRADE_REFUSED", *OBJECT_IDS))
    if policy["numeric_verdict"] == "PASS" and policy["diagnostics"]:
        diagnostics.append(issue("NAMED_FAILURE_CONCEALED", *[
            oid for item in policy["diagnostics"] for oid in item["object_ids"]]))
        diagnostics.extend(issue(item["name"], *item["object_ids"])
                           for item in policy["diagnostics"])
    row(7, "FAIL" if diagnostics else "PASS", diagnostics)
    verdict = ("FAIL" if any(r["verdict"] == "FAIL" for r in rows) else
               "UNREADABLE" if any(r["verdict"] == "UNREADABLE" for r in rows) else "PASS")
    return dict(schema=SCHEMA, evidence_kind="synthetic_offline", live_grade=False,
                verdict=verdict, steps=rows)


class SyntheticWorkflowTests(unittest.TestCase):
    def assert_report(self, report, expected):
        self.assertEqual(report["schema"], SCHEMA)
        self.assertEqual(report["evidence_kind"], "synthetic_offline")
        self.assertIs(report["live_grade"], False)
        self.assertEqual([r["step"] for r in report["steps"]], list(range(1, 8)))
        self.assertEqual({r["step"]: r["verdict"] for r in report["steps"]
                          if r["verdict"] != "PASS"}, expected)
        encoded = json.dumps(report, allow_nan=False)
        self.assertNotIn("source_sha", encoded)
        self.assertNotIn("readiness", encoded)
        self.assertNotIn('"live_grade": true', encoded)
        for r in report["steps"]:
            for diagnostic in r["diagnostics"]:
                self.assertTrue(diagnostic["object_ids"])
        self.assertEqual(report["verdict"], "FAIL" if "FAIL" in expected.values()
                         else "UNREADABLE" if expected else "PASS")

    def assert_defect(self, scene, step, name, ids, verdict="FAIL"):
        report = evaluate(scene)
        self.assert_report(report, {step: verdict})
        diagnostics = report["steps"][step - 1]["diagnostics"]
        self.assertTrue(any(d.get("name", d.get("reason")) == name and
                            d["object_ids"] == ids for d in diagnostics), diagnostics)
        return report

    def test_joined_scene_passes(self):
        scene = scenario()
        self.assert_report(evaluate(scene), {})
        self.assertAlmostEqual(cases.fill_coverage(scene["fill"])["fraction"], .04)
        # The analytic box and fill module describe the same M1 rectangle.
        self.assertEqual(cases.corners(scene["boxes"][0]),
                         [(10, 10), (20, 10), (20, 20), (10, 20)])
        self.assertEqual(scene["geometry"]["original"][0]["point"],
                         scene["fill"]["module"]["rect"][:2])
        self.assertEqual(layers.check_bytes(scene["export"])["inspected_occurrences"], 3)
        for tag in scene["boxes"][1:]:
            self.assertAlmostEqual(units.paper_height_mm(2, 1, 1 / 25.4),
                                   tag["paper_text_height_mm"])

    def test_step1_source_byte_mutation(self):
        scene = scenario()
        scene["source_after"] += b"changed"
        self.assert_defect(scene, 1, "INPUT_CHANGED", OBJECT_IDS)

    def test_step2_moved_and_missing_module(self):
        for missing in (False, True):
            scene = scenario()
            if missing:
                scene["geometry"]["export"] = []
            else:
                scene["geometry"]["export"][0]["point"] = [11, 10]
            self.assert_defect(scene, 2, "MISSING" if missing else "MOVED",
                               ["M1"] if missing else ["M1", "M1/export"])

    def test_step3_small_and_overlapping_tag(self):
        scene = scenario()
        scene["boxes"][1]["paper_text_height_mm"] = 1.7
        self.assert_defect(scene, 3, "TEXT_TOO_SMALL", ["T1"])
        scene = scenario()
        scene["boxes"][1]["center"] = [30, 12]
        self.assert_defect(scene, 3, "TAG_OVERLAP", ["T1", "T2"])

    def test_step4_nonplot_module_and_tag(self):
        for layer, ids in (("MODULES", ["M1"]), ("TAGS", ["T1"])):
            scene = scenario()
            marker = f"0\nLAYER\n2\n{layer}\n".encode("ascii")
            scene["export"] = scene["export"].replace(marker, marker + b"290\n0\n", 1)
            self.assert_defect(scene, 4, "layer_nonplot", ids)

    def test_step5_above_five_percent_fill(self):
        scene = scenario()
        scene["fill"]["fills"][0]["outer"] = [10, 10, 10.6, 20]
        self.assert_defect(scene, 5, "FILL_OVERLAP", ["M1", "F1"])

    def test_step6_unresolved_scale_and_wrong_physical_coordinate(self):
        scene = scenario()
        scene["unit_points"][2] = (0, None, scene["unit_points"][2][2])
        self.assert_defect(scene, 6, "MISSING_UNIT_SCALE", OBJECT_IDS, "UNREADABLE")
        scene = scenario()
        scene["unit_points"][1][2][0][0] = .3
        self.assert_defect(scene, 6, "UNIT_EQUIVALENCE_MISMATCH", OBJECT_IDS)

    def test_step7_refuses_live_claim_and_concealed_failure(self):
        scene = scenario()
        scene["policy"]["request_live_grade"] = True
        self.assert_defect(scene, 7, "LIVE_GRADE_REFUSED", OBJECT_IDS)
        scene = scenario()
        scene["policy"]["diagnostics"] = [issue("TEXT_TOO_SMALL", "T1")]
        report = self.assert_defect(scene, 7, "NAMED_FAILURE_CONCEALED", ["T1"])
        self.assertIn(issue("TEXT_TOO_SMALL", "T1"), report["steps"][6]["diagnostics"])

    def test_absent_export_is_unreadable(self):
        scene = scenario()
        scene["export"] = None
        report = evaluate(scene)
        self.assert_report(report, {2: "UNREADABLE", 4: "UNREADABLE"})
        self.assertEqual(report["steps"][1]["diagnostics"], [issue("EXPORT_ABSENT", "M1")])

    def test_absent_tags_are_unreadable(self):
        scene = scenario()
        scene["boxes"] = scene["boxes"][:1]
        scene["export"] = layers.fixture(entities=[layers.entity("M1")])
        report = evaluate(scene)
        self.assert_report(report, {3: "UNREADABLE", 4: "UNREADABLE"})
        self.assertEqual(report["steps"][2]["diagnostics"], [issue("TAGS_ABSENT", "T1", "T2")])
        self.assertEqual(report["steps"][3]["diagnostics"], [dict(
            layers.diagnostic("required_role_absent", role="tag"), object_ids=["T1", "T2"])])

    def test_rows_remain_independent_without_hiding_failure(self):
        scene = scenario()
        pristine = copy.deepcopy(scene)
        scene["boxes"][1]["paper_text_height_mm"] = 1.7
        self.assert_defect(scene, 3, "TEXT_TOO_SMALL", ["T1"])
        self.assert_report(evaluate(pristine), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
