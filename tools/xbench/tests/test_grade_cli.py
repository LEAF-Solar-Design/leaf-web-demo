"""Exercise CLI flow using only synthetic files and fake health responses."""

import hashlib
import json

import pytest

import grade
import xbench_core as core


SHA = "c" * 40
PASS_MODULE = """from xbench_core import Step
LIMITS = 'synthetic only'
def run(ctx):
    assert ctx.bench == 'X01'
    assert ctx.source_sha == 'c' * 40
    assert ctx.base == 'http://localhost:8000'
    assert ctx.manifest['bench'] == ctx.bench
    assert ctx.fixtures[0]['ok']
    assert ctx.out_dir.name == 'out'
    return [Step(1, 'PASS', 'synthetic pass')]
"""


@pytest.fixture
def cli(tmp_path):
    data = b"synthetic fixture"
    fixture = tmp_path / "sample.bin"
    fixture.write_bytes(data)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "schema": core.MANIFEST_SCHEMA, "bench": "X01", "fixtures": [{
            "id": "sample", "path": str(fixture), "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }],
    }), encoding="utf-8")
    benches = tmp_path / "benches"
    benches.mkdir()
    module = benches / "x01.py"
    module.write_text(PASS_MODULE, encoding="utf-8")
    return dict(args=["X01", "--manifest", str(manifest), "--base", "http://localhost:8000/",
                      "--out", str(tmp_path / "out"), "--readiness-dir", str(tmp_path / "ready"),
                      "--benches-dir", str(benches)],
                manifest=manifest, module=module, fixture=fixture,
                receipt=tmp_path / "out" / "grade.json", ready=tmp_path / "ready" / "X01.json")


def healthy(url, timeout):
    return json.dumps({"source_sha": SHA}).encode()


def assert_no_receipts(cli):
    assert not cli["receipt"].exists()
    assert not cli["ready"].exists()
    assert not cli["receipt"].parent.exists()


@pytest.mark.parametrize("options", [
    {},
    {"mode": "gate", "candidate": SHA, "empty": "", "equals": "a=b"},
    {"a" + "0_" * 15 + "z": "é" * 256},
    {"key" + str(n): str(n) for n in range(16)},
])
def test_options_reach_bench_and_receipt(cli, options):
    cli["module"].write_text(
        "import json\nfrom xbench_core import Step\n"
        "def run(ctx):\n"
        "    assert type(ctx.options) is dict\n"
        "    return [Step(1, 'PASS', json.dumps(ctx.options, ensure_ascii=False))]\n",
        encoding="utf-8",
    )
    args = cli["args"] + [arg for key, value in options.items()
                          for arg in ("--opt", key + "=" + value)]
    assert grade.main(args, opener=healthy) == 0
    value = json.loads(cli["receipt"].read_text(encoding="utf-8"))
    assert json.loads(value["steps"][0]["evidence"]) == options
    assert value["options"] == options
    assert list(value)[-2:] == ["limits", "options"]


def test_context_options_default_is_independent(cli):
    args = ("X01", {}, "http://localhost:8000", cli["receipt"].parent, SHA, [])
    first = core.BenchContext(*args)
    second = core.BenchContext(*args)
    assert first.options == second.options == {}
    first.options["mode"] = "gate"
    assert second.options == {}


@pytest.mark.parametrize("options", [
    ["missing_equals"],
    ["=value"],
    ["Mode=gate"],
    ["1mode=gate"],
    ["bad-key=value"],
    ["møde=value"],
    ["mode\n=value"],
    ["a" * 33 + "=value"],
    ["mode=" + "x" * 257],
    ["mode=gate", "mode=other"],
    ["key" + str(n) + "=value" for n in range(17)],
] + [["mode=" + char] for char in ("\x00", "\t", "\n", "\r", "\x1f", "\x7f", "\x85", "\x9f")])
def test_invalid_options(cli, capsys, options):
    def forbidden(url, timeout):
        pytest.fail("health called for invalid options")

    args = cli["args"] + [arg for option in options for arg in ("--opt", option)]
    assert grade.main(args, opener=forbidden) == 5
    captured = capsys.readouterr()
    assert len(captured.err.splitlines()) == 1
    assert captured.out == ""
    assert_no_receipts(cli)


@pytest.mark.parametrize("bad", ["X11", "x01", "unknown"])
def test_invalid_bench(cli, capsys, bad):
    cli["args"][0] = bad
    assert grade.main(cli["args"], opener=healthy) == 5
    assert len(capsys.readouterr().err.splitlines()) == 1
    assert_no_receipts(cli)


def test_invalid_base(cli, capsys):
    cli["args"][cli["args"].index("--base") + 1] = "http://example.test"
    assert grade.main(cli["args"], opener=healthy) == 5
    assert capsys.readouterr().err
    assert_no_receipts(cli)


def test_missing_required_argument(tmp_path, capsys):
    assert grade.main(["X01"], opener=healthy) == 5
    assert len(capsys.readouterr().err.splitlines()) == 1
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("mode", ["missing", "invalid", "mismatch"])
def test_bad_manifest(cli, capsys, mode):
    if mode == "missing":
        cli["manifest"].unlink()
    elif mode == "invalid":
        cli["manifest"].write_text("not JSON")
    else:
        obj = json.loads(cli["manifest"].read_text())
        obj["bench"] = "X02"
        cli["manifest"].write_text(json.dumps(obj))
    assert grade.main(cli["args"], opener=healthy) == 5
    assert capsys.readouterr().err
    assert_no_receipts(cli)


def test_missing_module(cli, capsys):
    cli["module"].unlink()
    assert grade.main(cli["args"], opener=healthy) == 5
    assert capsys.readouterr().err == "bench X01 grader not built\n"
    assert_no_receipts(cli)


def test_fixture_failure_skips_health(cli):
    cli["fixture"].unlink()

    def forbidden(url, timeout):
        pytest.fail("health called before fixtures passed")

    assert grade.main(cli["args"], opener=forbidden) == 4
    value = json.loads(cli["receipt"].read_text())
    assert value["verdict"] == "UNREADABLE" and value["source_sha"] is None
    assert value["steps"] == []
    assert value["fixtures"][0]["reason"] == "missing"
    assert value["limits"] == "fixture verification failed: sample"
    assert not cli["ready"].exists()


@pytest.mark.parametrize("mode", ["transport", "json"])
def test_health_failure(cli, mode):
    def broken(url, timeout):
        if mode == "transport":
            raise OSError("offline")
        return b"not JSON"

    assert grade.main(cli["args"], opener=broken) == 4
    value = json.loads(cli["receipt"].read_text())
    assert value["verdict"] == "UNREADABLE" and value["source_sha"] is None
    assert value["steps"] == []
    assert value["limits"].startswith("staging health unreadable: ")
    assert not cli["ready"].exists()


@pytest.mark.parametrize("body,exception", [
    ("raise RuntimeError('synthetic crash')", "RuntimeError: synthetic crash"),
    ("return 'not a list'", "TypeError:"),
    ("return [object()]", "TypeError:"),
    ("return (Step(1, 'PASS', 'tuple'),)", "TypeError:"),
    ("return [Step(1, 'PASS', 'a'), Step(1, 'FAIL', 'b')]", "ValueError:"),
])
def test_module_failure(cli, body, exception):
    cli["module"].write_text("from xbench_core import Step\ndef run(ctx):\n    " + body + "\n")
    assert grade.main(cli["args"], opener=healthy) == 4
    value = json.loads(cli["receipt"].read_text())
    assert value["verdict"] == "UNREADABLE" and value["source_sha"] == SHA
    assert value["steps"][0]["n"] == 1
    assert value["steps"][0]["verdict"] == "UNREADABLE"
    assert value["steps"][0]["evidence"].startswith(exception)
    assert not cli["ready"].exists()


def test_module_import_failure(cli):
    cli["module"].write_text("raise RuntimeError('import failed')\n")
    assert grade.main(cli["args"], opener=healthy) == 4
    value = json.loads(cli["receipt"].read_text())
    assert value["steps"][0]["evidence"] == "RuntimeError: import failed"
    assert not cli["ready"].exists()


def test_module_exception_evidence_truncation(cli):
    cli["module"].write_text("def run(ctx):\n    raise ValueError('x' * 3000)\n")
    assert grade.main(cli["args"], opener=healthy) == 4
    assert len(json.loads(cli["receipt"].read_text())["steps"][0]["evidence"]) == 2000


@pytest.mark.parametrize("verdict,code", [("PASS", 0), ("FAIL", 1), ("UNREADABLE", 4)])
def test_module_verdicts(cli, capsys, verdict, code):
    cli["module"].write_text(PASS_MODULE.replace("Step(1, 'PASS'", "Step(1, '" + verdict + "'"))
    assert grade.main(cli["args"], opener=healthy) == code
    value = json.loads(cli["receipt"].read_text())
    assert value["verdict"] == verdict and value["source_sha"] == SHA
    assert value["limits"] == "synthetic only"
    assert value["manifest_sha256"] == hashlib.sha256(cli["manifest"].read_bytes()).hexdigest()
    assert value["started_at"].endswith("Z") and value["finished_at"].endswith("Z")
    assert capsys.readouterr().out == f"X01 {verdict} {SHA} {cli['receipt']}\n"
    assert cli["ready"].exists() == (verdict == "PASS")
    if verdict == "PASS":
        ready = json.loads(cli["ready"].read_text())
        assert ready["receipt"] == str(cli["receipt"])
        assert ready["sha"] == SHA and ready["verdict"] == "READINESS_PASS"


def test_no_readiness(cli):
    assert grade.main(cli["args"] + ["--no-readiness"], opener=healthy) == 0
    assert json.loads(cli["receipt"].read_text())["verdict"] == "PASS"
    assert not cli["ready"].exists()


def test_empty_steps(cli):
    cli["module"].write_text("def run(ctx):\n    return []\n")
    assert grade.main(cli["args"], opener=healthy) == 4
    value = json.loads(cli["receipt"].read_text())
    assert value["verdict"] == "UNREADABLE" and value["steps"] == []
    assert value["limits"] == ""
    assert not cli["ready"].exists()
