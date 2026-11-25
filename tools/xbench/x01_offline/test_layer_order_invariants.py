"""Capped order invariants over scratch ASCII DXFs, without child processes."""

import copy
import itertools
import json
from pathlib import Path
import tempfile
import unittest

try:
    from . import check_plot_layers as layers
except ImportError:
    import check_plot_layers as layers


def entity(handle, layer, kind="LINE", extra=()):
    return [(0, kind), (5, handle), (8, layer)] + list(extra)


def insert(handle, layer, block, extra=()):
    return entity(handle, layer, "INSERT", [(2, block)] + list(extra))


def scratch_dxf(layer_order, block_order, model_order, nonplot=False):
    """Construct all sections ourselves so every layer row can move."""
    definitions = {
        "INNER": [entity("MODULE_LEAF", "0"),
                  entity("EXPLICIT_MODULE", "MODULES"),
                  entity("EXPLICIT_TAG", "TAGS", "MTEXT")],
        "OUTER": [insert("NEST", "0", "INNER")],
        "LABELS": [entity("INHERITED_TAG", "0", "TEXT")],
    }
    model = {
        "ROOT": insert("ROOT", "CARRIER", "OUTER", [(70, 2), (71, 2)]),
        "TAG_ROOT": insert("TAG_ROOT", "TAGS", "LABELS"),
        "DIRECT_MODULE": entity("DIRECT_MODULE", "MODULES"),
    }
    pairs = [(0, "SECTION"), (2, "TABLES"), (0, "TABLE"), (2, "LAYER")]
    for name in layer_order:
        pairs.extend([(0, "LAYER"), (2, name),
                      (290, 0 if nonplot and name == "CARRIER" else 1)])
    pairs.extend([(0, "ENDTAB"), (0, "ENDSEC"), (0, "SECTION"), (2, "BLOCKS")])
    for name in block_order:
        pairs.extend([(0, "BLOCK"), (2, name)])
        for child in definitions[name]:
            pairs.extend(child)
        pairs.append((0, "ENDBLK"))
    pairs.extend([(0, "ENDSEC"), (0, "SECTION"), (2, "ENTITIES")])
    for handle in model_order:
        pairs.extend(model[handle])
    pairs.extend([(0, "ENDSEC"), (0, "EOF")])
    return "".join(f"{code}\n{value}\n" for code, value in pairs).encode("ascii")


def normalized(result):
    """Only diagnostics are an unordered collection; ancestry stays ordered.

    Serialize whole diagnostic records to retain every field, list order,
    occurrence token and original object ID (the DXF handle).
    """
    result = copy.deepcopy(result)
    result["diagnostics"] = sorted(result["diagnostics"],
                                   key=lambda item: json.dumps(item, sort_keys=True))
    return result


LAYER_NAMES = ("0", "MODULES", "TAGS", "CARRIER")
BLOCK_NAMES = ("INNER", "OUTER", "LABELS")
MODEL_NAMES = ("ROOT", "TAG_ROOT", "DIRECT_MODULE")
# Four distinct layer permutations, independently crossed with definition and
# model order reversals: sixteen unique permutations, all tiny and deterministic.
ORDERS = tuple(itertools.product(
    tuple(itertools.islice(itertools.permutations(LAYER_NAMES), 4)),
    (BLOCK_NAMES, BLOCK_NAMES[::-1]), (MODEL_NAMES, MODEL_NAMES[::-1])))


class LayerOrderInvariants(unittest.TestCase):
    def check_scratch(self, data, required_roles=("module", "tag")):
        with tempfile.TemporaryDirectory(prefix="x01 layer order ") as directory:
            path = Path(directory) / "synthetic layers.dxf"
            path.write_bytes(data)
            result = layers.check_file(path, module_layers=["MODULES", "CARRIER"],
                                       tag_layers=["TAGS"], required_roles=required_roles)
            self.assertEqual(result, layers.check_bytes(
                data, module_layers=["MODULES", "CARRIER"], tag_layers=["TAGS"],
                required_roles=required_roles))
            self.assertEqual(path.read_bytes(), data)
        return result

    def test_independent_record_orders(self):
        self.assertEqual(len(ORDERS), 16)
        self.assertLessEqual(len(ORDERS), 32)
        # PASS and concrete FAIL cases use the same capped set of permutations.
        for nonplot in (False, True):
            baseline = self.check_scratch(scratch_dxf(
                LAYER_NAMES, BLOCK_NAMES, MODEL_NAMES, nonplot))
            self.assertEqual(baseline["verdict"], "FAIL" if nonplot else "PASS")
            self.assertEqual(baseline["inspected_occurrences"], 14)
            if nonplot:
                self.assertEqual(len(baseline["diagnostics"]), 12)
            else:
                self.assertEqual(baseline["diagnostics"], [])
            for layer_order, block_order, model_order in ORDERS:
                with self.subTest(nonplot=nonplot, layers=layer_order,
                                  blocks=block_order, model=model_order):
                    actual = self.check_scratch(scratch_dxf(
                        layer_order, block_order, model_order, nonplot))
                    self.assertEqual(normalized(actual), normalized(baseline))

    def test_plot_flag_changes_named_occurrences_and_original_ids(self):
        before_data = scratch_dxf(LAYER_NAMES, BLOCK_NAMES, MODEL_NAMES)
        after_data = scratch_dxf(LAYER_NAMES, BLOCK_NAMES, MODEL_NAMES, True)
        # Exactly one concrete table flag changes, not any entity or handle.
        self.assertEqual(before_data.replace(b"2\nCARRIER\n290\n1\n",
                                             b"2\nCARRIER\n290\n0\n", 1), after_data)
        before, after = self.check_scratch(before_data), self.check_scratch(after_data)
        self.assertNotEqual(normalized(before), normalized(after))
        self.assertEqual(before["verdict"], "PASS")
        self.assertEqual(after["verdict"], "FAIL")
        self.assertEqual(before["inspected_occurrences"], after["inspected_occurrences"])
        expected = []
        for row in range(2):
            for column in range(2):
                for handle, layer, role in (("MODULE_LEAF", "CARRIER", "module"),
                                            ("EXPLICIT_MODULE", "MODULES", "module"),
                                            ("EXPLICIT_TAG", "TAGS", "tag")):
                    expected.append(dict(handle=handle,
                        occurrence_path=[f"ROOT[row={row},col={column}]", "NEST", handle],
                        effective_layer=layer, role=role, reason="layer_nonplot"))
        self.assertEqual(after["diagnostics"], expected)
        # An ancestor's reason survives explicit child layers and roles. Only
        # descendants change; independent roots retain their original evidence.
        self.assertEqual({d["handle"] for d in after["diagnostics"]},
                         {"MODULE_LEAF", "EXPLICIT_MODULE", "EXPLICIT_TAG"})
        self.assertNotIn("DIRECT_MODULE", {d["handle"] for d in after["diagnostics"]})
        self.assertNotIn("INHERITED_TAG", {d["handle"] for d in after["diagnostics"]})

    def test_ancestry_reversal_is_unequal(self):
        original = self.check_scratch(scratch_dxf(
            LAYER_NAMES, BLOCK_NAMES, MODEL_NAMES, True))
        reversed_path = copy.deepcopy(original)
        reversed_path["diagnostics"][0]["occurrence_path"].reverse()
        self.assertNotEqual(normalized(original), normalized(reversed_path))
        # Swapping just the INSERT ancestors is also semantically different,
        # even with the same final leaf handle and the same set of tokens.
        swapped = copy.deepcopy(original)
        path = swapped["diagnostics"][0]["occurrence_path"]
        path[0], path[1] = path[1], path[0]
        self.assertNotEqual(normalized(original), normalized(swapped))

    def test_independently_named_required_roles(self):
        data = scratch_dxf(LAYER_NAMES, BLOCK_NAMES, MODEL_NAMES)
        for role in ("module", "tag"):
            self.assertEqual(self.check_scratch(data, (role,))["verdict"], "PASS")
        # All leaves of the opposite role are deliberately unclassified. This
        # tests role names independently rather than relying on default policy.
        for required, module_names, tag_names in (
                ("module", ["ABSENT_MODULE"], ["TAGS"]),
                ("tag", ["MODULES", "CARRIER"], ["ABSENT_TAG"])):
            result = layers.check_bytes(data, module_layers=module_names,
                                        tag_layers=tag_names, required_roles=[required])
            self.assertEqual(result["verdict"], "UNREADABLE")
            self.assertEqual(result["diagnostics"], [dict(handle=None, occurrence_path=[],
                effective_layer=None, role=required, reason="required_role_absent")])


if __name__ == "__main__":
    unittest.main()
