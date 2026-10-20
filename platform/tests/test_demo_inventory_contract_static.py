"""Run the demo inventory and migration assertions without database imports."""
import ast
import json
import re
from pathlib import Path
from types import SimpleNamespace


def test_demo_inventory_and_migration_contract():
    platform_dir = Path(__file__).resolve().parents[1]
    source_path = platform_dir / "tests/test_db_readiness_static.py"
    source = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    function_name = "test_demo_migration_and_inventory_register_both_tables"
    function = next((node for node in source.body
                     if isinstance(node, ast.FunctionDef) and node.name == function_name), None)
    assert function is not None, f"{function_name} not found in {source_path}"
    columns_index = next((index for index, statement in enumerate(function.body)
                          if isinstance(statement, ast.Assign) and any(
                              isinstance(target, ast.Name) and target.id == "columns"
                              for target in statement.targets)), None)
    assert columns_index is not None, f"columns boundary not found in {function_name}"
    function.body = function.body[:columns_index]
    module = ast.Module(body=[function], type_ignores=[])
    namespace = {
        "json": json,
        "re": re,
        "_AUTHORITY_INVENTORY_PATH": platform_dir / "authority-inventory.json",
        "db": SimpleNamespace(_PKG_DIR=platform_dir),
    }
    exec(compile(module, str(source_path), "exec"), namespace)
    namespace[function_name]()
