"""W4g-7b-06i: the fixed-engine canary over the whole enabled v3 case set."""
from __future__ import annotations

import copy
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "da"))

import dxf_intake
import intake_dxf
import intake_parse
import write_loop
import mutation_apply
from mutation_plan import emit_plan, uses_v3, validate_mutations


BASE_SHA = "1" * 64
ACCORECONSOLE = Path(r"C:\Program Files\Autodesk\AutoCAD 2026\accoreconsole.exe")
_CANARY_SKIP_REASON = f"local AutoCAD 2026 console is required ({ACCORECONSOLE})"


def _fixture_block():
    return {
        "base": [0.0, 0.0, 0.0], "count": 1, "complete": True,
        "children": [{"kind": "LINE", "layer": "0",
                      "pts": [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]]}],
    }


def _full_v3_added():
    """The whole enabled v3 add case set in one plan: an INSERT (with its
    property setter riding the created entity's own A: ordinal), a styled
    LINE, and a LINEAR plus an ALIGNED dimension on the same definition
    points. `_full_v3_mutations` adds the existing-handle setters, including
    set_color, that round out the whole enabled v3 case set."""
    return [
        {"handle": "insert-1", "kind": "INSERT", "name": "Fixture", "layer": "0",
         "pt": [10, 20, 0], "rot": 90, "scale": [2, 3, 1], "color": 5},
        {"handle": "added-line", "kind": "LINE", "layer": "0",
         "pts": [[0, 0], [5, 5]], "color": 1},
        {"handle": "lin-dim", "kind": "DIMENSION", "dimtype": "LINEAR", "layer": "0",
         "def1": [0, 0, 0], "def2": [3, 4, 0], "dimline": [1.5, 6, 0],
         "rotation": 0, "style": "Standard"},
        {"handle": "align-dim", "kind": "DIMENSION", "dimtype": "ALIGNED", "layer": "0",
         "def1": [0, 0, 0], "def2": [3, 4, 0], "dimline": [1.5, 6, 0],
         "style": "Standard"},
    ]


def _full_v3_mutations(target_handle):
    """Removals / adds / properties, in that canonical order: an existing
    LWPOLYLINE's color, lineweight, and linetype alongside the adds above."""
    return {
        "added": _full_v3_added(),
        "set_color": [{"handle": target_handle, "aci": 4}],
        "set_lineweight": [{"handle": target_handle, "weight": 25}],
        "set_linetype": [{"handle": target_handle, "name": "Continuous"}],
    }


def _base():
    return {
        "dwg": "upload.dxf", "layers": ["0"],
        "polylines": [
            {"layer": "0", "closed": True,
             "pts": [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0]],
             "xdata": None, "handle": "3B"},
        ],
        "circles": [], "arcs": [], "inserts": [],
        "blocks": {"Fixture": _fixture_block()},
        "properties": {
            "3B": {"aci": 256, "rgb": None, "linetype": "ByLayer", "lineweight": -1},
        },
    }


# --- (2) pure-python mock round trip: runs on any host ----------------------

def test_mock_full_v3_case_set_round_trips_through_dxf():
    base = _base()
    before = copy.deepcopy(base)
    canonical = validate_mutations(base, _full_v3_mutations("3B"))
    assert uses_v3(canonical)

    insert_ordinal = next(i for i, e in enumerate(canonical["added"]) if e["kind"] == "INSERT")
    line_ordinal = next(i for i, e in enumerate(canonical["added"]) if e["kind"] == "LINE")
    plan = emit_plan(canonical, base_sha256=BASE_SHA)
    assert f"SETCOLOR|A:{insert_ordinal}|5\n".encode() in plan
    assert f"SETCOLOR|A:{line_ordinal}|1\n".encode() in plan
    assert b"SETCOLOR|H:3B|4\n" in plan
    assert b"SETLINEWEIGHT|H:3B|25\n" in plan
    assert b"SETLINETYPE|H:3B|Continuous\n" in plan

    result = write_loop.apply_mutations(base, canonical)
    assert base == before  # apply_mutations never mutates its input

    assert len(result["inserts"]) == 1
    assert result["inserts"][0]["name"] == "Fixture"
    assert result["properties"]["insert-1"] == {"aci": 5, "rgb": None}
    added_line = next(p for p in result["polylines"] if p["handle"] == "added-line")
    assert added_line["closed"] is False
    assert result["properties"]["added-line"] == {"aci": 1, "rgb": None}
    assert result["properties"]["3B"] == {
        "aci": 4, "rgb": None, "linetype": "Continuous", "lineweight": 25}
    assert len(result["dimensions"]) == 2
    result_dims = {d["type"]: d for d in result["dimensions"]}
    assert result_dims["LINEAR"]["measurement"] == 3.0
    assert result_dims["ALIGNED"]["measurement"] == 5.0

    # The full v3 round trip: mock writer -> DXF synthesis -> DXF parse,
    # then the same live-shaped verifier proves every effect (no UNVERIFIED
    # note: the EP record is present).
    data = intake_dxf.intake_to_dxf(result)
    parsed = dxf_intake.parse_dxf_bytes(data)
    note = write_loop.verify_live_mutation_effects(base, parsed, canonical)
    assert note is None
    parsed_dims = {d["type"]: d for d in parsed["dimensions"]}
    assert f'{parsed_dims["LINEAR"]["measurement"]:.3f}' == "3.000"
    assert f'{parsed_dims["ALIGNED"]["measurement"]:.3f}' == "5.000"
    assert parsed["properties"]["3B"]["aci"] == 4
    assert parsed["properties"]["3B"]["lineweight"] == 25
    assert parsed["properties"]["3B"]["linetype"] == "Continuous"


# --- (1) bounded accoreconsole canary ----------------------------------------

def _console(work, source, script_name, script, *, apply_failed=False):
    path = work / script_name
    path.write_text(script, encoding="utf-8", newline="")
    result = subprocess.run(
        [str(ACCORECONSOLE), "/i", str(source), "/s", str(path)],
        cwd=work, capture_output=True, text=True, timeout=120, check=False,
    )
    # Redirected accoreconsole output is UTF-16LE read through text mode.
    text = result.stdout.replace("\x00", "")
    stderr = result.stderr.replace("\x00", "")
    assert result.returncode == 0, text + stderr
    # Keep the QUIT boundary before removing echoes; only printed markers count.
    before_quit = text.split('(command "_.QUIT"', 1)[0]
    text = "\n".join(line for line in text.splitlines()
                     if not line.strip().startswith("Command:"))
    before_quit = "\n".join(line for line in before_quit.splitlines()
                            if not line.strip().startswith("Command:"))
    assert "LEAF-MUTATION-PLAN-INVALID" not in text, text
    assert ("LEAF-MUTATION-APPLY-FAILED" in text) == apply_failed, text
    # QUIT can emit a harmless cancellation after SAVEAS; UNDO must not.
    assert "; error: Function cancelled" not in before_quit + stderr, text + stderr
    return text


@pytest.mark.skipif(not ACCORECONSOLE.exists(), reason=_CANARY_SKIP_REASON)
def test_accoreconsole_full_v3_case_set_canary(tmp_path):
    # Same local binary and tracked seed as da/test_mutation_apply_accoreconsole.py.
    # "9462" (data/rooftop_demo.intake.json polylines[0], layer "Panels") stands
    # in for "an existing LWPOLYLINE": no entmake seeding needed for it.
    target_handle = "9462"
    host = tmp_path / "host.dwg"
    shutil.copyfile(ROOT / "data" / "rooftop_demo.dwg", host)
    # Seed the Fixture block definition before the plan is read, in the same
    # console session (the 02s recipe). Let the production apply script
    # provide the final SAVEAS and QUIT lines.
    setup = "\r\n".join([
        '(setvar "CMDECHO" 0)',
        '(setvar "FILEDIA" 0)',
        '(entmake (list (cons 0 "BLOCK") (cons 2 "Fixture") (cons 70 0) (cons 10 (list 0.0 0.0 0.0))))',
        '(entmake (list (cons 0 "LINE") (cons 8 "0") (cons 10 (list 0.0 0.0 0.0)) (cons 11 (list 1.0 0.0 0.0))))',
        '(entmake (list (cons 0 "ENDBLK")))',
        '(entmake (list (cons 0 "LWPOLYLINE") (cons 100 "AcDbEntity") (cons 8 "0") (cons 100 "AcDbPolyline") (cons 90 2) (cons 70 0) (cons 10 (list 12.0 23.0)) (cons 10 (list 17.0 23.0))))',
        "",
    ])
    base_stub = {
        "polylines": [{
            "handle": target_handle, "layer": "Panels", "closed": True,
            "pts": [[14323.816, 2836.126, -25.296], [14400.816, 2836.126, -25.296],
                    [14400.816, 2874.595, -25.296], [14323.816, 2874.595, -25.296]],
            "xdata": None,
        }],
        "inserts": [], "blocks": {"Fixture": _fixture_block()},
    }
    canonical = validate_mutations(base_stub, _full_v3_mutations(target_handle))
    insert_ordinal = next(i for i, e in enumerate(canonical["added"]) if e["kind"] == "INSERT")
    line_ordinal = next(i for i, e in enumerate(canonical["added"]) if e["kind"] == "LINE")
    plan = emit_plan(
        canonical, base_sha256=hashlib.sha256(host.read_bytes()).hexdigest())
    assert f"SETCOLOR|A:{insert_ordinal}|5\n".encode() in plan
    assert f"SETCOLOR|A:{line_ordinal}|1\n".encode() in plan
    assert f"SETCOLOR|H:{target_handle}|4\n".encode() in plan
    assert f"SETLINEWEIGHT|H:{target_handle}|25\n".encode() in plan
    assert f"SETLINETYPE|H:{target_handle}|Continuous\n".encode() in plan
    (tmp_path / "mutation-plan.txt").write_bytes(plan.replace(b"\n", b"\r\n"))

    settings = mutation_apply.activity_spec(3)["settings"]
    inspect = settings["inspectScript"]["value"]
    quit_line = '(command "_.QUIT" "_Y")\r\n'
    assert inspect.endswith(quit_line)
    # Extract the unmodified drawing first (renamed output), then continue
    # the SAME session straight into the frozen apply script.
    before = inspect[:-len(quit_line)].replace("output-intake.txt", "base-intake.txt")
    _console(tmp_path, host, "apply.scr", setup + before + settings["script"]["value"])

    base = intake_parse.parse(tmp_path / "base-intake.txt", "canary")
    assert not base.get("parseErrors"), base.get("parseErrors")
    assert base["blocks"]["Fixture"]["complete"] is True
    # Rows for the planner's own doubt (v63): the dimstyle catalogue on a
    # real DWG head is readable through the inspect variant before the
    # DIMENSION add (the DS block precedes the plan).
    assert "Standard" in base.get("dimstyles", [])
    assert target_handle in base["properties"]
    # The stub used to compute `canonical` above matches what the real head
    # actually reports, so the plan we already sent is the one this base
    # would itself produce.
    assert validate_mutations(base, _full_v3_mutations(target_handle)) == canonical

    output = tmp_path / "output.dwg"
    assert output.exists() and output.stat().st_size > 0
    _console(tmp_path, output, "after.scr", inspect)
    families = tmp_path / "output-intake.txt"
    actual = intake_parse.parse(families, "canary")
    assert not actual.get("parseErrors"), actual.get("parseErrors")
    assert "Standard" in actual.get("dimstyles", [])

    added_insert, = [e for e in actual["inserts"] if e["name"] == "Fixture"]
    # The IN record at the legacy radians (rtos precision 5), like 02s.
    assert added_insert["rot"] == 1.5708
    assert added_insert["handle"] not in {e["handle"] for e in base.get("inserts", [])}
    assert actual["properties"][added_insert["handle"]]["aci"] == 5

    new_lines = [p for p in actual["polylines"]
                 if p["handle"] not in {e["handle"] for e in base.get("polylines", [])}]
    assert len(new_lines) == 1
    assert actual["properties"][new_lines[0]["handle"]]["aci"] == 1

    assert actual["properties"][target_handle]["aci"] == 4
    assert actual["properties"][target_handle]["lineweight"] == 25
    assert actual["properties"][target_handle]["linetype"] == "Continuous"

    added_dims = {d["type"]: d for d in actual["dimensions"]
                  if d["handle"] not in {e["handle"] for e in base.get("dimensions", [])}}
    assert set(added_dims) == {"LINEAR", "ALIGNED"}
    assert f'{added_dims["LINEAR"]["measurement"]:.3f}' == "3.000"
    assert f'{added_dims["ALIGNED"]["measurement"]:.3f}' == "5.000"
    assert added_dims["LINEAR"]["layer"] == "0"
    assert added_dims["ALIGNED"]["layer"] == "0"

    # No UNVERIFIED note allowed: the EP record is present, every effect
    # (INSERT, LINE, the LWPOLYLINE property setters, both dimensions) verifies.
    note = write_loop.verify_live_mutation_effects(base, actual, canonical)
    assert note is None

    # GROUP uses an existing LINE and ordinal zero of this new plan.
    group_plan = validate_mutations(actual, {
        "added": [{"handle": "group-circle", "kind": "CIRCLE", "layer": "0",
                   "c": [4, 2, 0], "r": 1}],
        "added_groups": [{"name": "CanaryRack", "members": [new_lines[0]["handle"], {"add": 0}]}],
    })
    group_host = tmp_path / "group-host.dwg"
    shutil.copyfile(output, group_host)
    output.unlink()
    (tmp_path / "mutation-plan.txt").write_bytes(emit_plan(
        group_plan, base_sha256=hashlib.sha256(group_host.read_bytes()).hexdigest()))
    _console(tmp_path, group_host, "group.scr", settings["script"]["value"])
    _console(tmp_path, output, "group-inspect.scr", inspect)
    grouped = intake_parse.parse(families, "canary")
    assert not grouped.get("parseErrors"), grouped.get("parseErrors")
    rack, = [g for g in grouped["groups"] if g["name"] == "CANARYRACK"]
    circle_handle = next(r["handle"] for r in grouped["created"] if r["ordinal"] == 0)
    assert set(rack["members"]) == {new_lines[0]["handle"], circle_handle}
    write_loop.verify_live_mutation_effects(actual, grouped, group_plan)

    ungroup_plan = validate_mutations(grouped, {"removed_groups": ["CanaryRack"]})
    shutil.copyfile(output, group_host)
    output.unlink()
    (tmp_path / "mutation-plan.txt").write_bytes(emit_plan(
        ungroup_plan, base_sha256=hashlib.sha256(group_host.read_bytes()).hexdigest()))
    _console(tmp_path, group_host, "ungroup.scr", settings["script"]["value"])
    _console(tmp_path, output, "ungroup-inspect.scr", inspect)
    ungrouped = intake_parse.parse(families, "canary")
    assert not ungrouped.get("parseErrors"), ungrouped.get("parseErrors")
    assert not any(g["name"] == "CANARYRACK" for g in ungrouped.get("groups", []))
    write_loop.verify_live_mutation_effects(grouped, ungrouped, ungroup_plan)

    # Atomic REPLACE copies a LINE, CIRCLE and open two-vertex LWPOLYLINE.
    polyline, = [p for p in ungrouped["polylines"]
                 if p["pts"] == [[12.0, 23.0, 0.0], [17.0, 23.0, 0.0]]]
    members = [new_lines[0]["handle"], circle_handle, polyline["handle"]]
    block_plan = validate_mutations(ungrouped, {
        "block_defs": [{"name": "B", "base": [10, 20, 0], "members": members, "insert": 0}],
        "removed": members,
        "added": [{"handle": "block-insert", "kind": "INSERT", "name": "B", "layer": "0",
                   "pt": [10, 20, 0], "rot": 0, "scale": [1, 1, 1]}],
    })
    shutil.copyfile(output, group_host)
    output.unlink()
    (tmp_path / "mutation-plan.txt").write_bytes(emit_plan(
        block_plan, base_sha256=hashlib.sha256(group_host.read_bytes()).hexdigest()))
    # Refuse the second child only after BLOCK and the first child exist.
    # Exercise the production nil handler and its UNDO mark, then save the
    # rolled-back state so the next console process can inspect the proof.
    failure_script = settings["script"]["value"].replace(
        '(setq leaf-ops (leaf-read-plan "mutation-plan.txt"))',
        '(setq leaf-canary-children 0)\r\n'
        '(defun leaf-bd-create-child (ed) (setq leaf-canary-children (1+ leaf-canary-children)) '
        '(if (= leaf-canary-children 2) (progn '
        '(if begun (progn (setq leaf-proof (open "partial-block.txt" "w")) '
        '(write-line "second child refused after BLOCK began" leaf-proof) (close leaf-proof))) nil) (entmake ed)))\r\n'
        '(setq leaf-ops (leaf-read-plan "mutation-plan.txt"))',
    ).replace(
        '(if leaf-apply-ok (command "_.SAVEAS" "" "output.dwg"))',
        '(if leaf-apply-ok (command "_.SAVEAS" "" "output.dwg") '
        '(command "_.SAVEAS" "" "rolled-back.dwg"))',
    )
    _console(tmp_path, group_host, "block-failure.scr", failure_script, apply_failed=True)
    assert (tmp_path / "partial-block.txt").read_text().strip() == "second child refused after BLOCK began"
    rolled_back = tmp_path / "rolled-back.dwg"
    assert rolled_back.exists() and rolled_back.stat().st_size > 0
    rollback_inspect = inspect.replace(
        quit_line, '(if (not (tblsearch "BLOCK" "B")) (progn '
        '(setq leaf-proof (open "rollback-table.txt" "w")) '
        '(write-line "no B table entry" leaf-proof) (close leaf-proof)))\r\n' + quit_line)
    _console(tmp_path, rolled_back, "rollback-inspect.scr", rollback_inspect)
    assert (tmp_path / "rollback-table.txt").read_text().strip() == "no B table entry"
    rollback = intake_parse.parse(families, "canary")
    assert not rollback.get("parseErrors"), rollback.get("parseErrors")
    assert "B" not in rollback.get("blocks", {})
    assert set(members) <= {
        e["handle"] for field in ("polylines", "circles") for e in rollback.get(field, [])
    }
    for field in ("polylines", "circles", "arcs", "inserts", "dimensions"):
        assert rollback.get(field, []) == ungrouped.get(field, [])
    assert rollback.get("properties") == ungrouped.get("properties")
    _console(tmp_path, group_host, "block.scr", settings["script"]["value"])
    _console(tmp_path, output, "block-inspect.scr", inspect)
    blocked = intake_parse.parse(families, "canary")
    assert not blocked.get("parseErrors"), blocked.get("parseErrors")
    assert [c["kind"] for c in blocked["blocks"]["B"]["children"]] == ["LINE", "CIRCLE", "LWPOLYLINE"]
    assert len([e for e in blocked["inserts"] if e["name"] == "B"]) == 1
    assert not set(members) & {e["handle"] for field in ("polylines", "circles") for e in blocked.get(field, [])}
    assert all("properties" in c for c in blocked["blocks"]["B"]["children"])
    write_loop.verify_live_mutation_effects(ungrouped, blocked, block_plan)

    # W4g-7c-3s-1: the command creates a real MULTILEADER. Derived values
    # come from inspection, without depending on the later mock/DXF slices.
    leader_host = tmp_path / "leader-host.dwg"
    shutil.copyfile(output, leader_host)
    output.unlink()
    leader_plan = validate_mutations(blocked, {"added": [{
        "handle": "leader", "kind": "MLEADER", "layer": "0", "style": "Standard",
        "pts": [[0, 0, 0], [5, 4, 0]], "text": "Valve",
    }]})
    leader_bytes = emit_plan(
        leader_plan, base_sha256=hashlib.sha256(leader_host.read_bytes()).hexdigest())
    assert leader_bytes.splitlines()[-1] == (
        b"ADDMLEADER|0|Standard|0.000,0.000,0.000|5.000,4.000,0.000|Valve")
    (tmp_path / "mutation-plan.txt").write_bytes(leader_bytes)
    leader_script = settings["script"]["value"].replace(
        '(setq leaf-ops (leaf-read-plan "mutation-plan.txt"))',
        '(command "_.-LAYER" "_N" "LEAF-ML-OVERRIDE" "")\r\n'
        '(setvar "MLEADERLAYER" "LEAF-ML-OVERRIDE")\r\n'
        '(setq leaf-ops (leaf-read-plan "mutation-plan.txt"))',
    ).replace(
        '(if leaf-apply-ok (command "_.SAVEAS" "" "output.dwg"))',
        '(setq leaf-proof (open "mleaderlayer-after.txt" "w"))\r\n'
        '(write-line (getvar "MLEADERLAYER") leaf-proof)\r\n'
        '(close leaf-proof)\r\n'
        '(if leaf-apply-ok (command "_.SAVEAS" "" "output.dwg"))',
    )
    _console(tmp_path, leader_host, "leader.scr", leader_script)
    assert (tmp_path / "mleaderlayer-after.txt").read_text().strip() == "LEAF-ML-OVERRIDE"
    _console(tmp_path, output, "leader-inspect.scr", inspect)
    leaders = intake_parse.parse(families, "canary")
    assert not leaders.get("parseErrors"), leaders.get("parseErrors")
    leader, = leaders["mleaders"]
    assert leader["pts"] == [[0, 0, 0], [5, 4, 0]]
    assert leader["landing"] == [5, 4, 0]
    assert leader["dogleg_dir"] == [1, 0, 0]
    assert leader["text"] == "Valve"
    assert leader["style"] == "Standard"
    assert leader["layer"] == "0"
    assert leader["height"] == 0.18
    assert leader["arrow"] == 0.18
    assert leader["dogleg"] == 0.36
    standard, = [s for s in leaders["mlstyles"] if s["name"] == "Standard"]
    assert standard["segments"] == 1

    # Unknown styles are rejected at parse time. Change the second parsed
    # op to an absent style to reach the production apply refusal after the
    # first MLEADER exists, then inspect the saved UNDO-Back result.
    (tmp_path / "mutation-plan.txt").write_bytes(
        leader_bytes + leader_bytes.splitlines(keepends=True)[-1])
    failed_leader_script = settings["script"]["value"].replace(
        '(setq leaf-ops (leaf-read-plan "mutation-plan.txt"))',
        '(setq leaf-ops (leaf-read-plan "mutation-plan.txt"))\r\n'
        '(setq leaf-ops (list (car leaf-ops) '
        '(subst "NoSuchStyle" "Standard" (cadr leaf-ops))))',
    ).replace(
        '(if leaf-apply-ok (command "_.SAVEAS" "" "output.dwg"))',
        '(if leaf-apply-ok (command "_.SAVEAS" "" "output.dwg") '
        '(command "_.SAVEAS" "" "leader-rolled-back.dwg"))',
    )
    _console(tmp_path, leader_host, "leader-failure.scr",
             failed_leader_script, apply_failed=True)
    leader_rollback = tmp_path / "leader-rolled-back.dwg"
    assert leader_rollback.exists() and leader_rollback.stat().st_size > 0
    _console(tmp_path, leader_rollback, "leader-rollback-inspect.scr", inspect)
    restored = intake_parse.parse(families, "canary")
    assert not restored.get("parseErrors"), restored.get("parseErrors")
    assert not restored.get("mleaders")
    assert not restored.get("mleaders_unsupported")
    for field in ("polylines", "circles", "arcs", "inserts", "dimensions"):
        assert restored.get(field, []) == blocked.get(field, [])


    # 3s-4: refuse a frozen target after a successful add, then inspect the
    # UNDO result and the restored sysvars in a fresh console process.
    frozen_line = leader_bytes.splitlines(keepends=True)[-1].replace(
        b"ADDMLEADER|0|", b"ADDMLEADER|LEAF-FROZEN|")
    (tmp_path / "mutation-plan.txt").write_bytes(leader_bytes + frozen_line)
    frozen_script = settings["script"]["value"].replace(
        '(setq leaf-ops (leaf-read-plan "mutation-plan.txt"))',
        '(command "_.-LAYER" "_N" "LEAF-FROZEN" "")\r\n'
        '(command "_.-LAYER" "_F" "LEAF-FROZEN" "")\r\n'
        '(setq leaf-proof (open "frozen-before.txt" "w"))\r\n'
        '(write-line (getvar "CLAYER") leaf-proof)\r\n'
        '(write-line (getvar "CMLEADERSTYLE") leaf-proof)\r\n'
        '(write-line (getvar "MLEADERLAYER") leaf-proof)\r\n'
        '(close leaf-proof)\r\n'
        '(setq leaf-ops (leaf-read-plan "mutation-plan.txt"))',
    ).replace(
        '(if leaf-apply-ok (command "_.SAVEAS" "" "output.dwg"))',
        '(if leaf-apply-ok (command "_.SAVEAS" "" "output.dwg") '
        '(command "_.SAVEAS" "" "frozen-rolled-back.dwg"))',
    )
    _console(tmp_path, leader_host, "frozen-leader.scr", frozen_script, apply_failed=True)
    frozen_inspect = inspect.replace(
        quit_line,
        '(setq leaf-proof (open "frozen-after.txt" "w"))\r\n'
        '(write-line (getvar "CLAYER") leaf-proof)\r\n'
        '(write-line (getvar "CMLEADERSTYLE") leaf-proof)\r\n'
        '(write-line (getvar "MLEADERLAYER") leaf-proof)\r\n'
        '(close leaf-proof)\r\n' + quit_line,
    )
    _console(tmp_path, tmp_path / "frozen-rolled-back.dwg", "frozen-inspect.scr", frozen_inspect)
    assert (tmp_path / "frozen-after.txt").read_text() == (tmp_path / "frozen-before.txt").read_text()
    frozen = intake_parse.parse(families, "canary")
    assert not frozen.get("parseErrors"), frozen.get("parseErrors")
    assert not frozen.get("mleaders")
    assert not frozen.get("mleaders_unsupported")
    for field in ("polylines", "circles", "arcs", "inserts", "dimensions",
                  "blocks", "groups", "properties", "mlstyles"):
        assert frozen.get(field) == blocked.get(field)


# --- (3) skip-visibility row: a "skipped local engine suite is no proof" guard

def test_accoreconsole_canary_skip_reason_names_the_binary_path():
    """A runner without accoreconsole SKIPS visibly, naming the exact binary
    path it looked for, so `scripts/run-all-gates.py` can allowlist that
    exact reason as the only sanctioned skip for this suite (never a bare,
    unexplained skip that could hide a broken canary)."""
    marker = next(
        m for m in test_accoreconsole_full_v3_case_set_canary.pytestmark
        if m.name == "skipif")
    assert marker.kwargs["reason"] == _CANARY_SKIP_REASON
    assert str(ACCORECONSOLE) in marker.kwargs["reason"]
