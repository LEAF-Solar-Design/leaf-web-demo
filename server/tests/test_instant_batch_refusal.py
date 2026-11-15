"""X09 batch refusal without importing credential-bearing broker modules.

Compile the real entry function only. Resolution is a tripwire so tests prove
the refusal occurs before any file, sandbox, APS or platform state is touched.
"""

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

import pytest


SERVER = Path(__file__).resolve().parents[1]


class ReachedLegacyResolution(Exception):
    pass


def load_run(validation_errors=()):
    tree = ast.parse((SERVER / "tool_loader.py").read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run_tool_dynamic")
    namespace = {"Any": Any, "Dict": Dict, "Optional": Optional,
                 "time": SimpleNamespace(perf_counter=lambda: 1.0),
                 "ErrorCode": SimpleNamespace(BAD_PARAMS="BAD_PARAMS"),
                 "validate_params": lambda *_: list(validation_errors),
                 "err_envelope": lambda code, message, **kw: {"error": code, "message": message, **kw}}

    def tripwire(*_args, **_kwargs):
        raise ReachedLegacyResolution

    namespace["resolve_local_file"] = tripwire
    namespace["_test_source_filename"] = tripwire
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(SERVER / "tool_loader.py"), "exec"), namespace)
    return namespace["run_tool_dynamic"]


def catalog_tool():
    catalog = json.loads((SERVER / "catalog_tools.json").read_text(encoding="utf-8"))
    rows = catalog["tools"] if isinstance(catalog, dict) else catalog
    return next(dict(tool) for tool in rows if tool["name"] == "instant-list-layers")


@pytest.mark.parametrize("aps_live", [False, True])
def test_catalog_instant_without_batch_fallback_never_resolves_another_body(aps_live):
    tool = catalog_tool()
    assert tool["execution_class"] == "instant" and tool["batch_fallback"] is False
    result = load_run()(tool, {}, {}, aps_live=aps_live, da=object(), tenant_id="synthetic-tenant")
    assert result["error"] == "BAD_PARAMS"
    assert result["retryable"] is False
    assert result["tool"] == tool["name"] and result["version"] == tool["version"]
    assert "instant executor" in result["message"]
    assert "batch_fallback=false" in result["message"]


@pytest.mark.parametrize("changes", [{"execution_class": "batch"},
                                     {"execution_class": None},
                                     {"batch_fallback": True},
                                     {"batch_fallback": None},
                                     {"batch_fallback": 0}])
def test_guard_preserves_existing_dispatch_for_other_metadata(changes):
    tool = catalog_tool()
    tool.update(changes)
    with pytest.raises(ReachedLegacyResolution):
        load_run()(tool, {}, {}, aps_live=False)


def test_invalid_parameters_still_stop_before_dispatch():
    result = load_run(["prefix must be text"])(catalog_tool(), {}, {"prefix": 1}, aps_live=False)
    assert result["error"] == "BAD_PARAMS"
    assert result["message"] == "params schema: prefix must be text"


def test_staged_source_cannot_force_a_nonfallback_instant_into_batch():
    result = load_run()(catalog_tool(), {}, {}, aps_live=False, test_source="synthetic unused source")
    assert result["error"] == "BAD_PARAMS"
    assert "instant executor" in result["message"]
