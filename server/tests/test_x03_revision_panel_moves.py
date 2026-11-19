"""Compare two receipt-linked disposable DXFs through the ordinary author and sandbox."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cad_versions
import dxf_intake
import tool_loader
import tools_fallback


ASKS = [
    "Count installed panels from two linked drawing revisions and show which placements moved between floors.",
    "Compare panel counts across drawing versions and list the solar modules that changed levels.",
    "Between the previous and current revisions, how many PV modules are there and which moved to another storey?",
]
FLOORS = [{"name": f"F{i}", "elevation": i * 10} for i in range(4)]
SCOPE = ("test-tenant", "test-project", "drawing-A")


def dxf(z, rows, columns, loose_handle, child_handle="A2"):
    def insert(handle, name, pairs):
        return [(0, "INSERT"), (5, handle), (2, name), (8, "0"), *pairs]

    def block(name, base_y, children):
        return [(0, "BLOCK"), (2, name), (10, 0), (20, base_y), (30, 0), *children, (0, "ENDBLK")]

    blocks = block("PV Module", 0, [(0, "POINT"), (10, 0), (20, 0)]) + block("Rack", 2,
        insert(child_handle, "PV Module", [(10, 1), (20, 2), (30, 0), (70, columns), (71, rows),
                                           (44, 4), (45, 3)]))
    # Parent local Y is world +Z, scaled by 2; rack base y=2 cancels child y=2.
    refs = insert("B1", "Rack", [(10, 0), (20, z), (30, 0), (42, 2),
                                 (210, 1), (220, 0), (230, 0)])
    refs += insert(loose_handle, "PV Module", [(10, 7), (20, 0), (30, 0)])
    refs += insert("FE", "PV Module", [(10, 2), (20, 1), (30, 5), (67, 1)])
    records = [(0, "SECTION"), (2, "BLOCKS"), *blocks, (0, "ENDSEC"),
               (0, "SECTION"), (2, "ENTITIES"), *refs, (0, "ENDSEC"), (0, "EOF")]
    return "".join(f"{code}\n{value}\n" for code, value in records).encode()


def persisted_pair(tmp_path, before, after):
    store = cad_versions.CadVersionStore()
    parent = store.record_upload(*SCOPE, before)
    child = store.record_derive(*SCOPE, parent.version, after)
    paths = [tmp_path / f"v{v}.dxf" for v in (parent.version, child.version)]
    for path, data in zip(paths, (before, after)):
        path.write_bytes(data)
    return store, paths


def invoke(ask, intake, params):
    _, source, _ = tools_fallback.author_tool(ask)
    return tool_loader._run_source_in_sandbox(source, "compare_panels.py", intake, params)


@pytest.mark.parametrize("ask,prior_z,current_z,shape1,shape2", [
    (ASKS[0], 2, 12, (2, 2), (2, 2)),
    (ASKS[1], 7, 10, (3, 1), (2, 2)),
    (ASKS[2], 14, 3, (2, 3), (3, 1)),
])
def test_linked_revision_panel_moves(tmp_path, ask, prior_z, current_z, shape1, shape2):
    from authored_revision_panel_moves import bind_linked_dxfs

    before = dxf(prior_z, *shape1, "C1")
    after = dxf(current_z, *shape2, "D2")
    store, paths = persisted_pair(tmp_path, before, after)
    bound = bind_linked_dxfs(store, *SCOPE, 1, 2, *(p.read_bytes() for p in paths))
    status, payload = invoke(ask, bound, {"floors": FLOORS})
    assert status == "ok", payload
    result, _ = payload

    def placements(z, shape, loose):
        entries = {f"B1[0,0]/A2[{r},{c}]": (f"F{(z + 6*r)//10}", [0, 1 + 4*c, z + 6*r])
                   for r in range(shape[0]) for c in range(shape[1])}
        entries[f"{loose}[0,0]"] = ("F0", [7, 0, 0])
        return entries

    old, new = placements(prior_z, shape1, "C1"), placements(current_z, shape2, "D2")
    assert result["counts"] == {"prior": len(old), "current": len(new)}
    assert {item["path"] for item in result["added"]} == new.keys() - old.keys()
    assert {item["path"] for item in result["removed"]} == old.keys() - new.keys()
    moved = {p for p in old.keys() & new.keys() if old[p][0] != new[p][0]}
    assert {item["prior"]["path"] for item in result["moved"]} == moved
    for item in result["moved"]:
        path = item["prior"]["path"]
        for tag, version, expected in (("prior", 1, old), ("current", 2, new)):
            assert item[tag] == {"drawing_id": SCOPE[-1], "version": version, "path": path,
                                 "floor": expected[path][0], "position": expected[path][1]}


def test_editable_ids_do_not_establish_linkage():
    intake = dxf_intake.parse_dxf_bytes(dxf(2, 2, 1, "C1"))
    status, payload = invoke(ASKS[0], intake, {"floors": FLOORS, "drawing_id": SCOPE[-1],
                                               "prior_version": 1, "current_version": 2,
                                               "prior_intake": intake})
    assert status == "tool_error" and "link" in str(payload).lower(), payload


def test_refuse_unbound_bytes_and_missing_identity(tmp_path):
    from authored_revision_panel_moves import bind_linked_dxfs

    before, after = dxf(2, 1, 1, "C1"), dxf(12, 1, 1, "D2", "")
    store, files = persisted_pair(tmp_path, before, after)
    with pytest.raises(cad_versions.DigestMismatch):
        bind_linked_dxfs(store, *SCOPE, 1, 2, before, before)
    with pytest.raises(ValueError, match="parent"):
        bind_linked_dxfs(store, *SCOPE, 2, 1, after, before)
    bound = bind_linked_dxfs(store, *SCOPE, 1, 2, *(p.read_bytes() for p in files))
    status, payload = invoke(ASKS[1], bound, {"floors": FLOORS})
    assert status == "tool_error" and "handle" in str(payload).lower(), payload
