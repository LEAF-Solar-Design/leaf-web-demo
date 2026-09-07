"""Offline tests for LeafApplyMutations script and protected provisioning."""
from __future__ import annotations

import hashlib
import json

import pytest

import apply_lisp
import mutation_apply as subject


class Response:
    def __init__(self, status: int, body=None, text: str = ""):
        self.status_code = status
        self._body = body or {}
        self.text = text

    def json(self):
        return self._body


class Http:
    def __init__(self, post=(), patch=(), get=(), delete=()):
        self.responses = {
            "post": list(post), "patch": list(patch), "get": list(get),
            "delete": list(delete),
        }
        self.calls = []

    def _call(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses[method].pop(0)

    def post(self, url, **kwargs):
        return self._call("post", url, **kwargs)

    def patch(self, url, **kwargs):
        return self._call("patch", url, **kwargs)

    def get(self, url, **kwargs):
        return self._call("get", url, **kwargs)

    def delete(self, url, **kwargs):
        return self._call("delete", url, **kwargs)


@pytest.fixture(autouse=True)
def auth(monkeypatch):
    monkeypatch.setattr(
        subject.client, "_auth_headers",
        lambda: {"Authorization": "Bearer secret"},
    )


def test_fixed_script_is_crlf_closed_format_and_never_evaluates_plan():
    script = apply_lisp.build_apply_scr()
    assert script.endswith("\r\n")
    assert "REMOVE" in script and "TRANSFORM" in script and "ADD" in script
    assert "LEAF_MUTATION_PLAN|1" in script
    assert "BASE_SHA256" in script
    assert "AcDbPolyline" in script and "(cons 210 normal)" in script
    assert "(entmod out)" in script and "(entupd e)" in script
    assert '"mutation-plan.txt"' in script
    assert '"output.dwg"' in script
    lowered = script.lower()
    assert "(eval " not in lowered
    assert "(read " not in lowered
    assert "(load " not in lowered
    assert "vl-load-com" not in lowered


def test_fixed_script_pins_add_layer_and_remove_constraints():
    script = apply_lisp.build_apply_scr()
    assert 'ok (>= (length raw) 3)' in script
    assert '0123456789_.-$ ")))' in script
    # W4g-3 (contract v2): REMOVE, RELAYER and the SET* ops name the four
    # kinds the browser engine writes, as one closed list, and nothing else.
    assert script.count('(member kind (list "LWPOLYLINE" "LINE" "CIRCLE" "ARC"))') == 2
    assert '(member kind (list "LWPOLYLINE" "LINE"))' in script
    assert '"POLYLINE"' not in script
    assert '"INSERT"' not in script


def test_v2_script_accepts_both_plan_headers_and_carries_every_v2_line():
    script = apply_lisp.build_apply_scr()
    assert '(member line (list "LEAF_MUTATION_PLAN|1" "LEAF_MUTATION_PLAN|2"))' in script
    for tag in ("ADDOPEN", "ADDLINE", "ADDCIRCLE", "ADDARC", "RELAYER",
                "SETPOINTS", "SETCIRCLE", "SETARC"):
        assert f'((= (car v) "{tag}")' in script, tag
        assert f'((= (car op) "{tag}")' in script, tag
    # Angles on the plan are degrees; entmake takes radians.
    assert "(defun leaf-deg2rad (d) (* pi (/ d 180.0)))" in script
    assert "(cons 50 (leaf-deg2rad" in script and "(cons 51 (leaf-deg2rad" in script
    # A LINE's endpoints come back to world coordinates through AutoCAD's
    # own trans from the plan's normal; nothing is evaluated.
    assert "(trans (list (car (car pts)) (cadr (car pts)) elev) normal 0)" in script
    lowered = script.lower()
    assert "(eval " not in lowered and "(read " not in lowered and "(load " not in lowered
    assert "vl-load-com" not in lowered
    for line in script.split("\r\n"):
        assert line.count("(") == line.count(")"), line[:80]


def test_mutation_inspect_script_adds_geometry_and_the_bounded_catalogue():
    from lisp import MUTATION_INSPECT_BLOCKS, build_scr
    plain = build_scr("output-intake.txt")
    extended = build_scr("output-intake.txt", extra_blocks=MUTATION_INSPECT_BLOCKS)
    assert build_scr() == build_scr(extra_blocks=())  # LeafExtract: byte-identical
    assert extended != plain
    head, tail = extended.split('(command "_.QUIT" "_Y")')
    legacy_head = plain.split('(command "_.QUIT" "_Y")')[0]
    # The IN record keeps its legacy unit (radians, unconverted) in BOTH
    # scripts: degrees are confined to the DM record, never INSERT rotation.
    assert head.startswith(legacy_head)
    assert '(rtos (* 180.0 (/ (cond (rot rot)(T 0.0)) pi)) 2 6)' not in extended
    for tag in ("LN|", "CI|", "AR|", "BK|", "BKE|", "BKCAP|"):
        assert tag in extended and tag not in plain
    assert extended.index('"BK|"') > extended.index('"AR|"')
    assert '(<= total 200)' in extended and '(> total 200)' in extended
    assert '(<= cnt 60)' in extended and '(> cnt 60)' in extended
    assert '(/= (substr name 1 1) "*")' in extended
    assert '(/= (cdr (assoc 0 (entget be))) "ENDBLK")' in extended
    assert 'body kind kind "OTHER" layer ""' in extended
    for line in extended.splitlines():
        assert line.count("(") == line.count(")"), line[:80]
    assert '"output-intake.txt"' in extended and "{OUT}" not in extended
    spec = subject.activity_spec()
    assert spec["settings"]["inspectScript"]["value"] == extended


@pytest.mark.parametrize("contract", [2, 3])
@pytest.mark.parametrize("setting", ["inspectScript", "script"])
def test_activity_script_lines_stay_within_console_reader_cap(contract, setting):
    from lisp import MAX_SCRIPT_LINE_CHARS

    assert MAX_SCRIPT_LINE_CHARS == 1800
    script = subject.activity_spec(contract)["settings"][setting]["value"]
    for line in script.splitlines():
        assert len(line) <= MAX_SCRIPT_LINE_CHARS, line[:40]


def test_build_scr_rejects_an_overlong_finished_line():
    from lisp import build_scr

    block = '(princ "' + "x" * 1990 + '")'
    assert len(block) == 2000
    with pytest.raises(ValueError) as error:
        build_scr(extra_blocks=(block,))
    assert block[:40] in str(error.value)
    assert "1800" in str(error.value)


def test_build_scr_checks_the_line_after_output_substitution():
    from lisp import build_scr

    with pytest.raises(ValueError):
        build_scr("x" * 1800)


def test_activity_spec_pins_pure_script_contract():
    spec = subject.activity_spec()
    assert spec["id"] == "LeafApplyMutations"
    assert spec["engine"] == "Autodesk.AutoCAD+26_0"
    assert "appbundles" not in spec
    assert spec["commandLine"] == [
        subject.COMMAND_LINE, subject.INSPECTION_COMMAND_LINE,
    ]
    assert "$(args[Result].path)" in subject.INSPECTION_COMMAND_LINE
    assert "$(args[HostDwg].path)" not in subject.INSPECTION_COMMAND_LINE
    assert spec["parameters"] == {
        "HostDwg": {"verb": "get", "required": True, "localName": "host.dwg"},
        "Plan": {"verb": "get", "required": True, "localName": "mutation-plan.txt"},
        "Result": {"verb": "put", "required": True, "localName": "output.dwg"},
        "Intake": {
            "verb": "put", "required": False,
            "localName": "output-intake.txt",
        },
    }
    assert spec["settings"]["script"]["value"] == apply_lisp.build_apply_scr()
    assert '"output-intake.txt"' in spec["settings"]["inspectScript"]["value"]


def test_409_advances_version_and_patches_alias(monkeypatch):
    http = Http(
        post=[Response(409), Response(201, {"version": 4}), Response(409)],
        patch=[Response(200)],
    )
    monkeypatch.setattr(subject, "requests", http)
    result = subject.provision_activity()
    assert result == {
        "id": "LeafApplyMutations", "alias": "prod", "version": 4,
        "advanced": True,
    }
    body = json.loads(http.calls[1][2]["data"])
    assert "id" not in body
    assert http.calls[1][1].endswith("/activities/LeafApplyMutations/versions")
    assert http.calls[-1][1].endswith("/activities/LeafApplyMutations/aliases/prod")
    assert json.loads(http.calls[-1][2]["data"]) == {"version": 4}


def test_create_points_new_alias_without_patch(monkeypatch):
    http = Http(post=[Response(201, {"version": 1}), Response(201)])
    monkeypatch.setattr(subject, "requests", http)
    result = subject.provision_activity()
    assert result["version"] == 1 and result["advanced"] is False
    assert not http.responses["patch"]


def test_readiness_resolves_alias_version_and_matches(monkeypatch):
    http = Http(get=[
        Response(200, {"id": "prod", "version": 7}),
        Response(200, subject.activity_spec()),
    ])
    monkeypatch.setattr(subject, "requests", http)
    result = subject.readiness()
    assert result == {
        "ready": True, "mismatches": [],
        "activity": {"alias": "prod", "version": 7},
        "contract": 2,
    }
    assert http.calls[1][1].endswith("/activities/LeafApplyMutations/versions/7")


def test_alias_state_captures_version_or_absence(monkeypatch):
    http = Http(get=[Response(200, {"version": 7}), Response(404)])
    monkeypatch.setattr(subject, "requests", http)
    assert subject.alias_state() == {
        "id": "LeafApplyMutations", "alias": "prod", "exists": True,
        "version": 7,
    }
    assert subject.alias_state() == {
        "id": "LeafApplyMutations", "alias": "prod", "exists": False,
        "version": None,
    }


def test_readiness_accepts_aps_omission_of_optional_required_false(monkeypatch):
    spec = subject.activity_spec()
    spec["parameters"]["Intake"].pop("required")
    http = Http(get=[Response(200, {"version": 7}), Response(200, spec)])
    monkeypatch.setattr(subject, "requests", http)

    assert subject.readiness() == {
        "ready": True, "mismatches": [],
        "activity": {"alias": "prod", "version": 7},
        "contract": 2,
    }


def test_readiness_rejects_aps_omission_of_required_true(monkeypatch):
    spec = subject.activity_spec()
    spec["parameters"]["Result"].pop("required")
    http = Http(get=[Response(200, {"version": 7}), Response(200, spec)])
    monkeypatch.setattr(subject, "requests", http)

    result = subject.readiness()
    assert result["ready"] is False
    assert "activity parameters mismatch" in result["mismatches"]


def test_restore_alias_repoints_or_removes_exact_alias(monkeypatch):
    http = Http(
        patch=[Response(200)], delete=[Response(204)],
        get=[Response(200, {"version": 3}), Response(404)],
    )
    monkeypatch.setattr(subject, "requests", http)
    assert subject.restore_alias(3)["version"] == 3
    assert json.loads(http.calls[0][2]["data"]) == {"version": 3}
    assert subject.restore_alias(None)["exists"] is False
    assert [call[0] for call in http.calls] == ["patch", "get", "delete", "get"]


def test_restore_alias_recreates_missing_alias_and_reads_it_back(monkeypatch):
    http = Http(
        patch=[Response(404)], post=[Response(201)],
        get=[Response(200, {"version": 5})],
    )
    monkeypatch.setattr(subject, "requests", http)
    assert subject.restore_alias(5)["version"] == 5
    assert [call[0] for call in http.calls] == ["patch", "post", "get"]


def test_restore_alias_fails_on_wrong_readback(monkeypatch):
    http = Http(patch=[Response(200)], get=[Response(200, {"version": 6})])
    monkeypatch.setattr(subject, "requests", http)
    with pytest.raises(RuntimeError, match="readback mismatch"):
        subject.restore_alias(5)


@pytest.mark.parametrize("mutation,expected", [
    (lambda spec: spec.update({"engine": "wrong"}), "activity engine mismatch"),
    (lambda spec: spec.update({"commandLine": ["unsafe"]}), "activity commandLine mismatch"),
    (lambda spec: spec["parameters"].pop("Plan"), "activity parameters mismatch"),
    (lambda spec: spec["settings"]["script"].update({"value": "unsafe"}), "activity settings mismatch"),
    (lambda spec: spec.update({"appbundles": ["unexpected"]}), "activity appbundles mismatch"),
])
def test_readiness_mismatch_fails_closed(monkeypatch, mutation, expected):
    spec = subject.activity_spec()
    mutation(spec)
    http = Http(get=[Response(200, {"version": 2}), Response(200, spec)])
    monkeypatch.setattr(subject, "requests", http)
    result = subject.readiness()
    assert result["ready"] is False
    assert expected in result["mismatches"]


def test_readiness_http_failure_fails_closed_without_secret(monkeypatch, capsys):
    secret = "SUPER-SECRET-TOKEN"
    http = Http(get=[Response(500, text=secret)])
    monkeypatch.setattr(subject, "requests", http)
    result = subject.readiness()
    combined = json.dumps(result) + capsys.readouterr().out + capsys.readouterr().err
    assert result["ready"] is False
    assert secret not in combined
    assert "Bearer secret" not in combined
    assert "HTTP 500" in result["mismatches"][0]


def test_module_has_no_paid_workitem_or_import_time_provisioning_surface():
    assert not hasattr(subject, "submit_workitem")
    assert not hasattr(subject, "provision_all")


def test_cli_provision_emits_stable_nonsecret_json(monkeypatch, capsys):
    monkeypatch.setattr(subject, "provision_activity", lambda contract=2: {
        "id": "LeafApplyMutations", "alias": "prod", "version": 8,
        "advanced": True,
    })
    assert subject.main(["provision", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "ok": True, "operation": "provision", "id": "LeafApplyMutations",
        "alias": "prod", "version": 8, "advanced": True,
        "contract": 2,
    }


def test_cli_readiness_mismatch_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(subject, "readiness", lambda contract=2: {
        "ready": False, "mismatches": ["activity engine mismatch"],
    })
    assert subject.main(["readiness", "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "operation": "readiness", "ready": False,
        "mismatches": ["activity engine mismatch"],
        "contract": 2,
    }


@pytest.mark.parametrize("raw,expected", [("9", 9), ("absent", None)])
def test_cli_restore_alias_parses_and_receipts(monkeypatch, capsys, raw, expected):
    observed = []

    def restore(version, contract=2):
        observed.append(version)
        return {
            "id": "LeafApplyMutations", "alias": "prod",
            "exists": version is not None, "version": version,
        }

    monkeypatch.setattr(subject, "restore_alias", restore)
    assert subject.main(["restore-alias", "--version", raw, "--json"]) == 0
    assert observed == [expected]
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["ok"] is True and receipt["version"] == expected


def test_cli_failure_redacts_exception(monkeypatch, capsys):
    monkeypatch.setattr(
        subject, "provision_activity",
        lambda contract=2: (_ for _ in ()).throw(RuntimeError("secret signed form")),
    )
    assert subject.main(["provision", "--json"]) == 1
    output = capsys.readouterr().out
    assert "secret" not in output
    assert json.loads(output)["error"] == "provision failed"


def test_v3_activity_adds_insert_and_preserves_v2_apply_script():
    from lisp import MUTATION_INSPECT_BLOCKS, build_scr

    v2_settings = {
        "script": {"value": apply_lisp.build_apply_scr()},
        "inspectScript": {"value": build_scr(
            "output-intake.txt", extra_blocks=MUTATION_INSPECT_BLOCKS)},
    }
    v2 = subject.activity_spec()
    v3 = subject.activity_spec(3)
    assert v2["settings"] == v2_settings
    assert subject.activity_spec(2) == v2
    assert subject.ACTIVITY_ID == "LeafApplyMutations" and subject.ALIAS == "prod"
    assert v3["id"] == "LeafApplyMutationsV3"
    headers_v2 = '(list "LEAF_MUTATION_PLAN|1" "LEAF_MUTATION_PLAN|2")'
    headers_v3 = '(list "LEAF_MUTATION_PLAN|1" "LEAF_MUTATION_PLAN|2" "LEAF_MUTATION_PLAN|3")'
    script_v2 = v2_settings["script"]["value"]
    script_v3 = v3["settings"]["script"]["value"]
    # The v2 APPLY script is byte-identical to 81e5d234. The shared inspect
    # script changed and must: the old one hangs the console.
    assert hashlib.sha256(script_v2.encode("utf-8")).hexdigest() == (
        "a7ed0bb7dbd8266404574b523550d8103318981c47a927a6ad4c9daab07f6c35")
    assert hashlib.sha256(v2_settings["inspectScript"]["value"].encode("utf-8")).hexdigest() == (
        "56d279f80d5e2898e6b588e6e36e16750a1f53edf5546b8351b9bdb59d3c6588")
    assert script_v2.count(headers_v2) == 1
    assert "LEAF_MUTATION_PLAN|3" not in script_v2
    assert script_v3.count(headers_v3) == 1
    assert "leaf-apply-addinsert" in script_v3
    assert "ADDINSERT" not in script_v2
    for version in (1, 2, 3):
        assert script_v3.count(f"LEAF_MUTATION_PLAN|{version}") == 1
    parser_v3 = next(line for line in script_v3.splitlines() if line.startswith("(defun leaf-parse-line"))
    assert '((= (car v) "ADDINSERT") (leaf-addinsert-op v))' in parser_v3
    assert v3["settings"]["inspectScript"] == v2_settings["inspectScript"]
    assert '(rtos (cond (rot rot)(T 0.0)) 2 5)' in v3["settings"]["inspectScript"]["value"]
    assert '(rtos (cond (rot rot)(T 0.0)) 2 6)' not in v3["settings"]["inspectScript"]["value"]
    v3["id"] = v2["id"]
    v3["settings"]["script"]["value"] = script_v2
    v3["settings"]["inspectScript"] = v2_settings["inspectScript"]
    assert v3 == v2
    assert subject.MUTATION_INSPECT_BLOCKS_V3 == MUTATION_INSPECT_BLOCKS


def test_v3_readiness_absent_alias_is_a_refusal(monkeypatch):
    http = Http(get=[Response(404)])
    monkeypatch.setattr(subject, "requests", http)
    result = subject.readiness(3)
    assert result == {
        "ready": False, "contract": 3,
        "mismatches": ["activity alias read failed with HTTP 404"],
    }
    assert [call[1] for call in http.calls] == [
        f"{subject.client.DA}/activities/LeafApplyMutationsV3/aliases/prod",
    ]


def test_v3_readiness_compares_its_own_version(monkeypatch):
    http = Http(get=[Response(200, {"version": 9}), Response(200, subject.activity_spec(3))])
    monkeypatch.setattr(subject, "requests", http)
    assert subject.readiness(3) == {
        "ready": True, "contract": 3, "mismatches": [],
        "activity": {"alias": "prod", "version": 9},
    }
    assert http.calls[0][1].endswith("/activities/LeafApplyMutationsV3/aliases/prod")
    assert http.calls[1][1].endswith("/activities/LeafApplyMutationsV3/versions/9")


def test_readiness_and_submission_share_the_configurable_alias(monkeypatch):
    monkeypatch.setattr(subject.client, "ALIAS", "canary")
    http = Http(get=[Response(200, {"version": 3}), Response(200, subject.activity_spec(2))])
    monkeypatch.setattr(subject, "requests", http)
    result = subject.readiness()
    assert result == {
        "ready": True, "mismatches": [],
        "activity": {"alias": "canary", "version": 3},
        "contract": 2,
    }
    assert http.calls[0][1].endswith("/activities/LeafApplyMutations/aliases/canary")
    assert http.calls[1][1].endswith("/activities/LeafApplyMutations/versions/3")
    assert subject.CONTRACTS[2].alias == "canary" and subject.CONTRACTS[3].alias == "canary"


def test_v3_provision_advances_only_the_separate_activity(monkeypatch):
    http = Http(
        post=[Response(409), Response(201, {"version": 2}), Response(409)],
        patch=[Response(200)],
    )
    monkeypatch.setattr(subject, "requests", http)
    assert subject.provision_activity(3) == {
        "id": "LeafApplyMutationsV3", "alias": "prod", "version": 2, "advanced": True,
    }
    assert json.loads(http.calls[0][2]["data"]) == subject.activity_spec(3)
    assert http.calls[1][1].endswith("/activities/LeafApplyMutationsV3/versions")
    assert http.calls[2][1].endswith("/activities/LeafApplyMutationsV3/aliases")
    assert http.calls[3][1].endswith("/activities/LeafApplyMutationsV3/aliases/prod")


def test_v3_alias_snapshot_and_restore_stay_on_the_separate_activity(monkeypatch):
    http = Http(
        get=[Response(200, {"version": 4}), Response(200, {"version": 3}), Response(404)],
        patch=[Response(404)], post=[Response(201)], delete=[Response(204)],
    )
    monkeypatch.setattr(subject, "requests", http)
    assert subject.alias_state(3)["version"] == 4
    assert subject.restore_alias(3, contract=3)["version"] == 3
    assert subject.restore_alias(None, contract=3)["exists"] is False
    assert all("/activities/LeafApplyMutationsV3/" in call[1] for call in http.calls)
    assert [call[0] for call in http.calls] == ["get", "patch", "post", "get", "delete", "get"]


def test_cli_v3_readiness_echoes_contract_when_alias_is_absent(monkeypatch, capsys):
    http = Http(get=[Response(404)])
    monkeypatch.setattr(subject, "requests", http)
    assert subject.main(["readiness", "--contract", "3", "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "operation": "readiness", "contract": 3, "ready": False,
        "mismatches": ["activity alias read failed with HTTP 404"],
    }


@pytest.mark.parametrize("command,function,extra", [
    ("provision", "provision_activity", []),
    ("readiness", "readiness", []),
    ("alias-state", "alias_state", []),
    ("restore-alias", "restore_alias", ["--version", "absent"]),
])
def test_every_cli_command_routes_the_contract(monkeypatch, capsys, command, function, extra):
    calls = []

    def run(*args, contract=2):
        calls.append((args, contract))
        return {"ready": True}

    monkeypatch.setattr(subject, function, run)
    assert subject.main([command, *extra, "--contract", "3", "--json"]) == 0
    assert calls == [((None,) if command == "restore-alias" else (), 3)]
    assert json.loads(capsys.readouterr().out)["contract"] == 3


@pytest.mark.parametrize("rgb,expected", [("12,34,56", [12, 34, 56]), ("~", None)])
def test_entity_properties_are_additive_after_a_line(rgb, expected):
    import intake_parse

    line = "LN|0|1.000,2.000,0.000|3.000,4.000,0.000|1A\n"
    legacy = intake_parse.parse_text(line, "test.dwg")
    parsed = intake_parse.parse_text(line + f"EP|1A|256|{rgb}|ByLayer|-1", "test.dwg")
    assert parsed.pop("properties") == {
        "1A": {"aci": 256, "rgb": expected, "linetype": "ByLayer", "lineweight": -1},
    }
    assert parsed == legacy
    assert len(parsed["polylines"]) == 1 and not parsed.get("parseErrors")


def test_dimension_record_reads_coordinate_and_angular_precisions():
    import intake_parse

    parsed = intake_parse.parse_text(
        "DM|linear|1.23456,2.34567,3.45678|4.56789,5.67891,6.78912|"
        "7.89123,8.91234,9.12345|30.12345678|Standard|0.0000004,0.0000006,1|12.34567|2A",
        "test.dwg",
    )
    assert parsed["dimensions"] == [{
        "type": "linear", "p1": [1.235, 2.346, 3.457], "p2": [4.568, 5.679, 6.789],
        "dimline": [7.891, 8.912, 9.123], "rotation_deg": 30.123457, "style": "Standard",
        "nrm": [0.0, 0.000001, 1.0], "measurement": 12.346, "handle": "2A",
    }]
    assert not parsed["polylines"] and not parsed.get("parseErrors")


@pytest.mark.parametrize("normal,handle", [
    ("0,0,0", "2A"),
    ("0,0,1", "not-a-handle"),
    ("0,0,0.0000004", "2A"),  # rounds to the zero vector at the 6-decimal reading
])
def test_malformed_dimension_normal_or_handle_is_a_parse_error(normal, handle):
    import intake_parse

    parsed = intake_parse.parse_text(
        f"DM|linear|0,0,0|3,4,0|0,5,0|0|Standard|{normal}|5|{handle}",
        "test.dwg",
    )
    assert len(parsed["parseErrors"]) == 1 and parsed["parseErrors"][0].startswith("DM:")
    assert "dimensions" not in parsed


@pytest.mark.parametrize("record,field", [
    ("EP|1A|7|~|Continuous|25", "properties"),
    ("DM|aligned|0,0,0|3,4,0|0,5,0|0|Standard|0,0,1|5|2A", "dimensions"),
])
def test_sidecar_records_close_a_complete_polyline_first(record, field):
    import intake_parse

    legacy = "PL|Panels|1|2|0,0,1|1A\nPV|0,0\nPV|2,0\nPV|2,2\nPX|Leaf\nPXS|kept\n"
    expected = intake_parse.parse_text(legacy, "test.dwg")
    parsed = intake_parse.parse_text(legacy + record + "\nPV|99,99\nPXS|ignored", "test.dwg")
    assert parsed.pop(field)
    assert parsed == expected
    assert len(parsed["polylines"]) == 1
    assert parsed["polylines"][0]["pts"] == [[0.0, 0.0, 2.0], [2.0, 0.0, 2.0], [2.0, 2.0, 2.0]]


@pytest.mark.parametrize("record", [
    "EP|1A|7|~|Continuous",
    "EP|1A|bad|~|Continuous|25",
    "EP|1A|7|1,2|Continuous|25",
    "EP|1A|7|1,2,256|Continuous|25",
    "EP|1A|7|~|Continuous|bad",
])
def test_malformed_properties_are_parse_errors(record):
    import intake_parse

    parsed = intake_parse.parse_text(record, "test.dwg")
    assert len(parsed["parseErrors"]) == 1 and parsed["parseErrors"][0].startswith("EP:")
    assert "properties" not in parsed


def test_block_catalogue_is_additive_to_legacy_blockdefs_and_polylines():
    import intake_parse

    legacy = (
        "PL|0|0|0|0,0,1|A1\nPV|0,0\nPV|1,1\n"
        "BD|PVBlock\nBDE|LINE|0,0;1,1;\n"
    )
    expected = intake_parse.parse_text(legacy, "test.dwg")
    parsed = intake_parse.parse_text(
        legacy + "BK|Fixture|1.12345,2,0|2|1\n"
        "BKE|Fixture|LINE|1,2,0|4,2,0|0\n"
        "BKE|Fixture|CIRCLE|3,4,0|2.12345|0,0,1|0\nBKCAP|203", "test.dwg")
    assert parsed.pop("blocks") == {
        "Fixture": {"base": [1.123, 2.0, 0.0], "count": 2, "complete": True,
                    "children": [
                        {"kind": "LINE", "layer": "0", "pts": [[1.0, 2.0, 0.0], [4.0, 2.0, 0.0]]},
                        {"kind": "CIRCLE", "layer": "0", "c": [3.0, 4.0, 0.0], "r": 2.123,
                         "nrm": [0.0, 0.0, 1.0]},
                    ]},
    }
    assert parsed.pop("blocksCapped") == 203
    assert parsed == expected


def test_block_child_record_closes_the_polyline_before_its_own_geometry():
    import intake_parse

    parsed = intake_parse.parse_text(
        "BK|Fixture|0,0,0|1|1\nPL|0|0|0|0,0,1|A1\nPV|0,0\nPV|1,1\n"
        "BKE|Fixture|OTHER|INSERT|\nPV|99,99", "test.dwg")
    assert parsed["polylines"][0]["pts"] == [[0.0, 0.0, 0.0], [1.0, 1.0, 0.0]]
    assert parsed["blocks"]["Fixture"]["complete"] is False
    assert parsed["blocks"]["Fixture"]["children"] == [
        {"kind": "OTHER", "layer": "", "type": "INSERT"}]


def test_bk_with_the_wrong_field_count_is_a_parse_error():
    import intake_parse

    parsed = intake_parse.parse_text("BK|SITE|Door|0,0,0|1|1", "test.dwg")
    assert len(parsed["parseErrors"]) == 1 and parsed["parseErrors"][0].startswith("BK:")
    assert parsed.get("blocks", {}) == {}


def test_bke_circle_with_the_wrong_body_arity_is_a_parse_error():
    import intake_parse

    parsed = intake_parse.parse_text(
        "BK|B|0,0,0|1|1\nBKE|B|CIRCLE|0,0,0|2|SITE|walls", "test.dwg")
    assert len(parsed["parseErrors"]) == 1 and parsed["parseErrors"][0].startswith("BKE:")
    assert parsed["blocks"]["B"]["complete"] is False
    assert parsed["blocks"]["B"]["children"] == []


def test_bke_circle_with_an_extra_field_is_a_parse_error_not_silently_dropped():
    import intake_parse

    # Legacy (pre-normal) shape: c, r with an extra trailing field before the
    # layer. The old handler read only body[0]/body[1] and silently ignored
    # anything past that; arity is now checked, so this refuses instead.
    parsed = intake_parse.parse_text(
        "BK|B|0,0,0|1|1\nBKE|B|CIRCLE|0,0,0|2|0,0,1|extra|walls", "test.dwg")
    assert len(parsed["parseErrors"]) == 1 and parsed["parseErrors"][0].startswith("BKE:")
    assert parsed["blocks"]["B"]["complete"] is False
    assert parsed["blocks"]["B"]["children"] == []


@pytest.mark.parametrize("record,reason", [
    ("BKE|B|LINE|bad|1,0,0|0", "bad LINE origin"),
    ("BKE|B|LWPOLYLINE|0|0,0,1|0||0", "no points"),
    ("BKE|B|CIRCLE|0,0,0|0|0,0,1|0", "zero radius"),
    ("BKE|B|ARC|0,0,0|-1|0|90|0,0,1|0", "negative radius"),
    ("BKE|B|CIRCLE|0,0,0|2|0,bad,1|0", "unreadable normal"),
])
def test_bke_child_parse_failure_marks_the_block_incomplete(record, reason):
    import intake_parse

    parsed = intake_parse.parse_text(f"BK|B|0,0,0|1|1\n{record}", "test.dwg")
    assert len(parsed["parseErrors"]) == 1 and parsed["parseErrors"][0].startswith("BKE:"), reason
    assert parsed["blocks"]["B"]["complete"] is False
    assert parsed["blocks"]["B"]["children"] == []


@pytest.mark.parametrize("header", ["BK|B|0,0,0|1|1\n", ""])
def test_truncated_bke_marks_its_named_block_incomplete(header):
    import intake_parse

    parsed = intake_parse.parse_text(header + "BKE|B|LINE", "test.dwg")
    assert len(parsed["parseErrors"]) == 1 and parsed["parseErrors"][0].startswith("BKE:")
    assert parsed["blocks"]["B"]["complete"] is False
    assert parsed["blocks"]["B"]["children"] == []


def test_catalogue_decodes_names_and_layers_without_collisions_or_double_decoding():
    import intake_parse

    parsed = intake_parse.parse_text(
        "BK|A%7CB|1,2,0|1|1\n"
        "BKE|A%7CB|LINE|0,0,0|1,0,0|SITE%7Cwalls%0D%0A%257C\n"
        "BK|A B|7,8,0|0|1\n"
        "BK|A%257CB|9,10,0|0|1\n"
        "BK|B%0DC%0AD%25|0,0,0|0|1", "test.dwg")
    assert not parsed.get("parseErrors")
    blocks = parsed["blocks"]
    assert set(blocks) == {"A|B", "A B", "A%7CB", "B\rC\nD%"}
    assert blocks["A|B"]["base"] == [1.0, 2.0, 0.0]
    assert blocks["A B"]["base"] == [7.0, 8.0, 0.0]
    assert blocks["A%7CB"]["base"] == [9.0, 10.0, 0.0]
    assert blocks["A|B"]["children"][0]["layer"] == "SITE|walls\r\n%7C"


def test_catalogue_lisp_looks_up_the_raw_name_and_encodes_only_record_fields():
    from lisp import MUTATION_INSPECT_BLOCKS

    # Helpers now occupy separate lines before the catalogue emission progn.
    helper = "\n".join(MUTATION_INSPECT_BLOCKS[3:-1])
    catalogue = MUTATION_INSPECT_BLOCKS[-1]
    assert '(setq name (cdr (assoc 2 bk)))' in catalogue
    assert '(entnext (tblobjname "BLOCK" name))' in catalogue
    assert '(leaf-bk-child name bed)' in catalogue
    assert '(strcat "BK|" (leaf-bk-encode name)' in catalogue
    assert '(setq name (leaf-bk-encode' not in catalogue
    assert 'vl-string-translate' not in catalogue
    assert '(strcat "BKE|" (leaf-bk-encode name)' in helper
    assert '(setq layer (leaf-bk-encode layer))' in helper
    for code, escaped in [(37, "%25"), (124, "%7C"), (13, "%0D"), (10, "%0A")]:
        assert f'((= ch {code}) "{escaped}")' in helper
    assert '(foreach ch (vl-string->list value)' in helper
    assert '(vl-string-translate "|\\r\\n" "   " value)' in helper


def test_leafextract_script_matches_the_pre_catalogue_pinned_text():
    from lisp import build_scr

    expected = (
        "(setvar \"CMDECHO\" 0)\r\n"
        "(progn (setq f (open \"result.txt\" \"w\")) (setq lay (tblnext \"LAYER\" T)) (while lay (write-line (strcat \"LAYER|\" (cdr (assoc 2 lay))) f) (setq lay (tblnext \"LAYER\"))) (princ \"LAYERS-DONE\") (close f))\r\n"
        "(progn (setq f (open \"result.txt\" \"a\")) (setq ss (ssget \"_X\" (list (cons 0 \"LWPOLYLINE\") (cons 410 \"Model\")))) (if ss (progn (setq nn (sslength ss) i 0) (while (< i nn) (setq ed (entget (ssname ss i) (list \"*\")) layn (cdr (assoc 8 ed)) cl (cdr (assoc 70 ed)) el (cdr (assoc 38 ed)) nrm (cdr (assoc 210 ed)) hnd (cdr (assoc 5 ed))) (if (null el) (setq el 0.0)) (if (null nrm) (setq nrm (list 0.0 0.0 1.0))) (write-line (strcat \"PL|\" layn \"|\" (itoa (cond (cl cl)(T 0))) \"|\" (rtos el 2 3) \"|\" (rtos (car nrm) 2 6) \",\" (rtos (cadr nrm) 2 6) \",\" (rtos (caddr nrm) 2 6) \"|\" hnd) f) (foreach g ed (if (= 10 (car g)) (write-line (strcat \"PV|\" (rtos (cadr g) 2 3) \",\" (rtos (caddr g) 2 3)) f))) (setq xd (assoc -3 ed)) (if xd (foreach app (cdr xd) (progn (write-line (strcat \"PX|\" (car app)) f) (foreach pr (cdr app) (if (= 1000 (car pr)) (write-line (strcat \"PXS|\" (cdr pr)) f)))))) (setq i (1+ i))))) (princ \"PL-DONE\") (close f))\r\n"
        "(progn (setq f (open \"result.txt\" \"a\")) (setq ss (ssget \"_X\" (list (cons 0 \"INSERT\") (cons 410 \"Model\")))) (if ss (progn (setq nn (sslength ss) i 0) (while (< i nn) (setq ed (entget (ssname ss i)) nm (cdr (assoc 2 ed)) layn (cdr (assoc 8 ed)) ip (cdr (assoc 10 ed)) rot (cdr (assoc 50 ed)) nrm (cdr (assoc 210 ed)) sx (cdr (assoc 41 ed)) sy (cdr (assoc 42 ed)) sz (cdr (assoc 43 ed)) hnd (cdr (assoc 5 ed))) (if (null nrm) (setq nrm (list 0.0 0.0 1.0))) (write-line (strcat \"IN|\" nm \"|\" layn \"|\" (rtos (car ip) 2 3) \",\" (rtos (cadr ip) 2 3) \",\" (rtos (caddr ip) 2 3) \"|\" (rtos (cond (rot rot)(T 0.0)) 2 5) \"|\" (rtos (car nrm) 2 6) \",\" (rtos (cadr nrm) 2 6) \",\" (rtos (caddr nrm) 2 6) \"|\" (rtos (cond (sx sx)(T 1.0)) 2 4) \",\" (rtos (cond (sy sy)(T 1.0)) 2 4) \",\" (rtos (cond (sz sz)(T 1.0)) 2 4) \"|\" hnd) f) (setq i (1+ i))))) (princ \"IN-DONE\") (close f))\r\n"
        "(progn (setq f (open \"result.txt\" \"a\")) (setq ss (ssget \"_X\" (list (cons 0 \"3DFACE\") (cons 410 \"Model\")))) (if ss (progn (setq nn (sslength ss) i 0) (while (< i nn) (setq ed (entget (ssname ss i)) layn (cdr (assoc 8 ed))) (setq p1 (cdr (assoc 10 ed)) p2 (cdr (assoc 11 ed)) p3 (cdr (assoc 12 ed)) p4 (cdr (assoc 13 ed))) (write-line (strcat \"F3|\" layn \"|\" (rtos (car p1) 2 3) \",\" (rtos (cadr p1) 2 3) \",\" (rtos (caddr p1) 2 3) \"|\" (rtos (car p2) 2 3) \",\" (rtos (cadr p2) 2 3) \",\" (rtos (caddr p2) 2 3) \"|\" (rtos (car p3) 2 3) \",\" (rtos (cadr p3) 2 3) \",\" (rtos (caddr p3) 2 3) \"|\" (rtos (car p4) 2 3) \",\" (rtos (cadr p4) 2 3) \",\" (rtos (caddr p4) 2 3)) f) (setq i (1+ i))))) (princ \"F3-DONE\") (close f))\r\n"
        "(progn (setq f (open \"result.txt\" \"a\")) (setq ss (ssget \"_X\" (list (cons 0 \"INSERT\") (cons 2 \"*PVBlock*\") (cons 410 \"Model\")))) (if ss (progn (setq seen nil i 0 nn (sslength ss)) (while (and (< i nn) (< (length seen) 12)) (setq nm (cdr (assoc 2 (entget (ssname ss i))))) (if (not (member nm seen)) (progn (setq seen (cons nm seen)) (setq bdef (tblobjname \"BLOCK\" nm)) (if bdef (progn (write-line (strcat \"BD|\" nm) f) (setq be (entnext bdef) cnt 0) (while (and be (< cnt 60)) (setq bed (entget be) bt (cdr (assoc 0 bed)) pts \"\") (foreach gg bed (if (= 10 (car gg)) (setq pts (strcat pts (rtos (cadr gg) 2 3) \",\" (rtos (caddr gg) 2 3) \";\")))) (if (/= pts \"\") (write-line (strcat \"BDE|\" bt \"|\" pts) f)) (setq be (entnext be) cnt (1+ cnt))))))) (setq i (1+ i))))) (princ \"BD-DONE\") (close f))\r\n"
        "(progn (setq f (open \"result.txt\" \"a\")) (setq gd (dictsearch (namedobjdict) \"ACAD_GEOGRAPHICDATA\")) (if (null gd) (write-line \"GEO|none\" f) (foreach pr gd (write-line (strcat \"GEO|\" (itoa (car pr)) \"|\" (cond ((= (type (cdr pr)) 'STR) (cdr pr)) ((= (type (cdr pr)) 'REAL) (rtos (cdr pr) 2 8)) ((= (type (cdr pr)) 'INT) (itoa (cdr pr))) ((= (type (cdr pr)) 'LIST) (strcat (rtos (car (cdr pr)) 2 6) \",\" (rtos (cadr (cdr pr)) 2 6) (if (caddr (cdr pr)) (strcat \",\" (rtos (caddr (cdr pr)) 2 6)) \"\"))) (T \"?\")) ) f))) (princ \"GEO-DONE\") (close f))\r\n"
        "(progn (setq f (open \"result.txt\" \"a\")) (setq idict (dictsearch (namedobjdict) \"ACAD_IMAGE_DICT\")) (if idict (progn (foreach pr idict (if (= 3 (car pr)) (write-line (strcat \"IMGNAME|\" (cdr pr)) f))) (foreach pr idict (if (= 350 (car pr)) (progn (setq ie (entget (cdr pr))) (if ie (write-line (strcat \"IMG|\" (cond ((cdr (assoc 1 ie)) (cdr (assoc 1 ie))) (T \"?\"))) f))))))) (princ \"IMG-DONE\") (close f))\r\n"
        "(command \"_.QUIT\" \"_Y\")\r\n"
    )
    assert build_scr().encode("utf-8") == expected.encode("utf-8")
