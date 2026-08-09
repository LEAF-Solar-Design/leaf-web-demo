"""Regression tests for the count-by-layer AutoLISP JSON serializer.

The source checks always run.  When AutoCAD Core Console is installed, the
runtime test also extracts the real serializer definitions and evaluates them
in AutoLISP.  This keeps the portable CI gate useful without pretending that a
Python reimplementation executes the shipped LISP.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
LSP_PATH = REPO_ROOT / "engine" / "tools" / "count_by_layer.lsp"

CASES = [
    "plain layer",
    'quote \" layer',
    r"back\slash",
    "tab\tlayer",
    "line\nfeed",
    "carriage\rreturn",
    "form\ffeed",
    "back\bspace",
    "control-\x01-layer",
]


def _source() -> str:
    return LSP_PATH.read_text(encoding="utf-8")


def _reference_json_string(value: str) -> str:
    """Mirror the serializer's deliberately small ASCII escape table."""
    escaped: list[str] = []
    short = {
        '"': r'\"',
        "\\": r"\\",
        "\b": r"\b",
        "\t": r"\t",
        "\n": r"\n",
        "\f": r"\f",
        "\r": r"\r",
    }
    for char in value:
        if char in short:
            escaped.append(short[char])
        elif ord(char) < 0x20:
            escaped.append(f"\\u00{ord(char):02X}")
        else:
            escaped.append(char)
    return '"' + "".join(escaped) + '"'


def test_serializer_source_covers_json_string_escapes_and_round_trips_names():
    source = _source()

    required_branches = {
        '((= code 34) (setq out (strcat out "\\\\\\\"")))',
        '((= code 92) (setq out (strcat out "\\\\\\\\")))',
        '((= code 8)  (setq out (strcat out "\\\\b")))',
        '((= code 9)  (setq out (strcat out "\\\\t")))',
        '((= code 10) (setq out (strcat out "\\\\n")))',
        '((= code 12) (setq out (strcat out "\\\\f")))',
        '((= code 13) (setq out (strcat out "\\\\r")))',
        '(strcat out "\\\\u00"',
    }
    assert required_branches <= {line.strip() for line in source.splitlines()}
    assert '(leaf-json-escape s)' in source

    encoded = "{" + ",".join(
        f"{_reference_json_string(name)}:1" for name in CASES
    ) + "}"
    assert json.loads(encoded) == {name: 1 for name in CASES}


def test_count_command_remains_read_only_and_keeps_result_schema():
    source = _source().lower()

    mutators = (
        "(entmake",
        "(entmakex",
        "(entmod",
        "(entdel",
        "(command",
        "(setvar",
        "(vla-",
    )
    for mutator in mutators:
        assert mutator not in source

    for schema_fragment in (
        r'{\"ok\":true,\"tool\":\"count-by-layer\",\"version\":\"1.0.0\",',
        r'\"result\":{\"counts\":{',
        r'\"overlay\":null,',
        r'\"cost\":null,\"error\":null}',
    ):
        assert schema_fragment in _source()


def _autocad_runtime() -> tuple[Path, Path] | None:
    executable = shutil.which("accoreconsole.exe")
    if executable:
        executable_path = Path(executable)
        templates = list(executable_path.parent.glob("Template/acad.dwt"))
        if templates:
            return executable_path, templates[0]

    autodesk = Path(r"C:\Program Files\Autodesk")
    if not autodesk.is_dir():
        return None
    candidates = sorted(autodesk.glob("AutoCAD */accoreconsole.exe"), reverse=True)
    for executable_path in candidates:
        year = executable_path.parent.name.rsplit(" ", 1)[-1]
        templates = [
            executable_path.parent / "Template" / "acad.dwt",
            Path(r"C:\ProgramData\Autodesk")
            / f"MEP {year}"
            / "enu"
            / "Template"
            / "AutoCAD Templates"
            / "acad.dwt",
        ]
        for template in templates:
            if template.is_file():
                return executable_path, template
    return None


def _lisp_literal(value: str) -> str:
    pieces = []
    for char in value:
        code = ord(char)
        if char == '"':
            pieces.append(r'\"')
        elif char == "\\":
            pieces.append(r"\\")
        elif code < 0x20:
            pieces.append(f'" (chr {code}) "')
        else:
            pieces.append(char)
    return '"' + "".join(pieces) + '"'


@pytest.mark.skipif(
    _autocad_runtime() is None,
    reason="AutoCAD Core Console is not installed",
)
def test_real_autolisp_serializer_round_trips_representative_names(tmp_path):
    runtime = _autocad_runtime()
    assert runtime is not None
    executable, template = runtime
    serializer_source = _source().split("(defun c:LEAFTOOL", 1)[0]
    output = tmp_path / "serializer-result.json"
    encoded_cases = " ".join(
        f"(leaf-json-str (strcat {_lisp_literal(value)}))" for value in CASES
    )
    harness = serializer_source + f"""
(setq leaf-test-values (list {encoded_cases}))
(setq leaf-test-file (open \"{output.as_posix()}\" \"w\"))
(write-line (strcat \"[\" (car leaf-test-values)
  (apply 'strcat
    (mapcar '(lambda (value) (strcat \",\" value)) (cdr leaf-test-values)))
  \"]\") leaf-test-file)
(close leaf-test-file)
"""
    harness_path = tmp_path / "serializer-harness.lsp"
    harness_path.write_text(harness, encoding="ascii")
    script_path = tmp_path / "serializer.scr"
    script_path.write_text(
        f'(load "{harness_path.as_posix()}")\n_.QUIT\n_N\n', encoding="ascii"
    )

    completed = subprocess.run(
        [str(executable), "/i", str(template), "/s", str(script_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert output.is_file(), completed.stdout + completed.stderr
    assert json.loads(output.read_text(encoding="utf-8")) == CASES
