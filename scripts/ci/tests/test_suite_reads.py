"""Hermetic repositories exercise read-set checking, selection, and recording."""

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


TOOL = Path(__file__).resolve().parents[1] / "suite_reads.py"
SPEC = importlib.util.spec_from_file_location("test_leaf_suite_reads", TOOL)
suite_reads = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(suite_reads)


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=Read Set Test",
         "-c", "user.email=read-set@example.invalid", "-c", "core.autocrlf=false",
         "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, check=True, timeout=15,
    ).stdout.strip()


def write(root, path, text="payload\n"):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def commit(root):
    git(root, "add", "--all")
    git(root, "commit", "--quiet", "-m", "fixture")
    return git(root, "rev-parse", "HEAD")


def suite(*, files=(), dirs=(), exists=(), globs=(), children=()):
    return {"globs": sorted(globs), "files": sorted(files), "dirs": sorted(dirs),
            "exists": sorted(exists), "children": list(children)}


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "--quiet")
    for path in ("alpha/input", "alpha/other", "beta/input", "assets/icon", "unread"):
        write(root, path)
    base = commit(root)
    declared = root / ".codebuild" / "suite-reads.json"
    declared.parent.mkdir()
    logs = tmp_path / "audit"
    logs.mkdir()
    data = {
        "version": 1, "run_all": [".codebuild/**", "scripts/ci/**"], "ignore": [],
        "suites": {"alpha": suite(files=["alpha/input"], dirs=["alpha"], globs=["assets/**"]),
                   "beta": suite(files=["beta/input"])},
    }
    declared.write_text(json.dumps(data), encoding="utf-8")
    return root, declared, logs, data, base


def manifest(repository, data=None):
    root, declared, logs, original, base = repository
    declared.write_text(json.dumps(original if data is None else data), encoding="utf-8")


def log(repository, *entries):
    directory = repository[2]
    (directory / "1-test.jsonl").write_text(
        json.dumps({"v": 1, "pid": 1, "argv": ["pytest"]}) + "\n"
        + "".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")


def run(repository, command, *extra, default_root=True, cwd=None):
    root, declared, logs, data, base = repository
    arguments = [sys.executable, str(TOOL), command]
    if default_root:
        arguments += ["--root", str(root)]
    if command in {"check", "record"}:
        arguments += ["--suite", "alpha", "--audit-dir", str(logs)]
    result = subprocess.run(arguments + list(extra), cwd=cwd or root,
                            capture_output=True, text=True, timeout=20)
    assert not result.stderr, result.stderr
    return result.returncode, result.stdout


def selected(repository, *args):
    code, output = run(repository, "select", *args)
    assert code == 0
    lines = output.splitlines()
    selections = dict(line.split("=", 1) for line in lines[:-1])
    assert lines[-1].startswith("scope reason: ")
    return selections, lines[-1][len("scope reason: "):]


def test_check_covered_and_counts(repository):
    root, declared, logs, data, base = repository
    data["suites"]["alpha"]["children"] = [{"exe": "git", "cwd": ".", "reads": ["**"]}]
    manifest(repository)
    log(repository, {"k": "f", "p": "alpha/input"}, {"k": "f", "p": "assets/icon"},
        {"k": "f", "p": "beta/input"}, {"k": "d", "p": "alpha"},
        {"k": "x", "exe": "git", "cwd": ".", "paths": ["beta/input"]})
    assert run(repository, "check") == (0, "suite read check: ok suite=alpha files=3 dirs=1 children=1\n")


def test_check_reports_all_undeclared_kinds(repository):
    log(repository, {"k": "f", "p": "unread"}, {"k": "f", "p": "missing/probe"},
        {"k": "d", "p": "beta"}, {"k": "x", "exe": "git", "cwd": ".", "paths": ["unread"]})
    code, output = run(repository, "check")
    assert code == 1
    assert "FAIL suite=alpha undeclared=4" in output
    for line in ("  undeclared file: unread", "  undeclared file: missing/probe",
                 "  undeclared dir: beta", "  undeclared child: git cwd=. paths=unread"):
        assert line in output
    assert f"--audit-dir {repository[2]}, then declare any child by hand" in output


@pytest.mark.parametrize("paths,expected", [
    (["scripts/a.py"], 1), (["scripts/a.py", "scripts/nested/b.py"], 1),
    (["scripts"], 1), (["hooks/a.py", "scripts/a.py"], 1), (["hooks/a.py"], 1),
])
def test_check_child_existence_globs(repository, paths, expected):
    repository[3]["suites"]["alpha"] = suite(children=[
        {"exe": "git", "cwd": ".", "sub": "ls-files", "reads": ["scripts/**"]},
    ])
    manifest(repository)
    log(repository, {"k": "x", "exe": "git", "cwd": ".", "sub": "ls-files", "paths": paths})
    code, output = run(repository, "check")
    assert code == expected
    assert ("undeclared child:" in output) == bool(expected)


@pytest.mark.parametrize("observed_sub,declared_sub,reads,expected", [
    ("rev-parse", "ls-files", ["scripts/**"], 1),
    ("rev-parse", "rev-parse", [], 1),
    ("rev-parse", "rev-parse", ["**"], 0),
    ("rev-parse", "rev-parse", [], 1),
    ("missing", "missing", ["**"], 0),
    ("missing", None, ["**"], 0),
    (None, "missing", ["**"], 0),
    (None, None, ["**"], 0),
    ("rev-parse", "missing", ["**"], 0),
    ("missing", "rev-parse", ["**"], 0),
])
def test_check_child_subcommand_and_nonvacuous_cwd(repository, observed_sub, declared_sub,
                                                 reads, expected):
    child = {"exe": "git", "cwd": ".", "reads": reads}
    entry = {"k": "x", "exe": "git", "cwd": ".", "paths": []}
    if declared_sub != "missing":
        child["sub"] = declared_sub
    if observed_sub != "missing":
        entry["sub"] = observed_sub
    repository[3]["suites"]["alpha"] = suite(children=[child])
    manifest(repository)
    log(repository, entry)
    assert run(repository, "check")[0] == expected


@pytest.mark.parametrize("mutation,expected", [
    ({}, 0), ({"sub": "log"}, 0), ({"exe": "other"}, 1), ({"cwd": "scripts"}, 1),
])
def test_check_child_nonempty_paths_still_require_identity(repository, mutation, expected):
    repository[3]["suites"]["alpha"] = suite(children=[
        {"exe": "git", "cwd": ".", "sub": "ls-files", "reads": ["**"]},
    ])
    manifest(repository)
    log(repository, {"k": "x", "exe": "git", "cwd": ".", "sub": "ls-files",
                     "paths": ["scripts/a.py"], **mutation})
    assert run(repository, "check")[0] == expected


def test_check_child_empty_paths_match_its_cwd_glob(repository):
    repository[3]["suites"]["alpha"] = suite(children=[
        {"exe": "git", "cwd": "scripts", "reads": ["scripts/**"]},
    ])
    manifest(repository)
    log(repository, {"k": "x", "exe": "git", "cwd": "scripts", "paths": []})
    assert run(repository, "check")[0] == 1


def test_file_probe_of_tracked_directory_requires_declaration(repository):
    root, declared, logs, data, base = repository
    log(repository, {"k": "f", "p": "beta"})
    code, output = run(repository, "check")
    assert code == 1
    assert "undeclared file: beta" in output
    assert "undeclared dir:" not in output
    data["suites"]["alpha"]["dirs"] = ["alpha", "beta"]
    manifest(repository)
    assert run(repository, "check")[0] == 1
    data["suites"]["alpha"]["files"].append("beta")
    manifest(repository)
    assert run(repository, "check") == (0, "suite read check: ok suite=alpha files=1 dirs=0 children=0\n")


def test_check_ignores_existing_untracked_and_ignored_observations(repository):
    root, declared, logs, data, base = repository
    write(root, "generated")
    data["ignore"] = ["beta/**", "ignored/**"]
    manifest(repository)
    log(repository, {"k": "f", "p": "generated"}, {"k": "f", "p": "beta/input"},
        {"k": "f", "p": "ignored/missing"}, {"k": "d", "p": "beta"},
        {"k": "d", "p": "ignored/absent"})
    assert run(repository, "check") == (0, "suite read check: ok suite=alpha files=0 dirs=0 children=0\n")


@pytest.mark.parametrize("problem", ["no-logs", "overflow", "error", "bad-json", "empty-log",
                                          "bad-entry", "unsorted", "parent", "unknown-key", "unknown-suite"])
def test_unusable_inputs(repository, problem):
    root, declared, logs, data, base = repository
    log(repository)
    extra = []
    if problem == "no-logs":
        (logs / "1-test.jsonl").unlink()
    elif problem == "overflow":
        log(repository, {"k": "o"})
    elif problem == "error":
        (logs / "123.error").write_text("RuntimeError", encoding="ascii")
    elif problem == "bad-json":
        (logs / "1-test.jsonl").write_text("{\n", encoding="utf-8")
    elif problem == "empty-log":
        (logs / "1-test.jsonl").write_text("", encoding="utf-8")
    elif problem == "bad-entry":
        log(repository, {"k": "unknown"})
    elif problem == "unsorted":
        data["suites"]["alpha"]["files"] = ["z", "a"]
        manifest(repository)
    elif problem == "parent":
        data["suites"]["alpha"]["files"] = ["../outside"]
        manifest(repository)
    elif problem == "unknown-key":
        data["unknown"] = []
        manifest(repository)
    elif problem == "unknown-suite":
        extra = ["--suite", "unknown"]
    code, output = run(repository, "check", *extra)
    assert code == 2
    assert "suite read check: UNUSABLE suite=" in output
    assert "reason=" in output
    if problem != "unknown-suite":
        assert run(repository, "record")[0] == 2


@pytest.mark.parametrize("change,expected,reason", [
    ("modify-declared", {"alpha": "1", "beta": "0"}, "selected"),
    ("modify-unread", {"alpha": "0", "beta": "0"}, "selected"),
    ("modify-listed-dir-file", {"alpha": "0", "beta": "0"}, "selected"),
    ("add-listed", {"alpha": "1", "beta": "0"}, "selected"),
    ("add-under-root", {"alpha": "0", "beta": "1"}, "selected"),
    ("delete-declared", {"alpha": "1", "beta": "0"}, "selected"),
    ("rename", {"alpha": "1", "beta": "1"}, "selected"),
    ("run-all", {"alpha": "1", "beta": "1"}, "run_all scripts/ci/new.py"),
    ("child-wildcard", {"alpha": "0", "beta": "1"}, "selected"),
    ("no-changes", {"alpha": "0", "beta": "0"}, "no changes"),
])
def test_select_git_changes(repository, change, expected, reason):
    root, declared, logs, data, base = repository
    # Keep the declaration untracked so its creation does not invoke run_all in these diffs.
    git(root, "config", "core.excludesFile", str(root.parent / "excludes"))
    (root.parent / "excludes").write_text(".codebuild/\n", encoding="utf-8")
    if change == "modify-declared":
        write(root, "alpha/input", "changed\n")
    elif change == "modify-unread":
        write(root, "unread", "changed\n")
    elif change == "modify-listed-dir-file":
        write(root, "alpha/other", "changed\n")
    elif change == "add-listed":
        write(root, "alpha/nested/new")
    elif change == "add-under-root":
        data["suites"]["beta"]["dirs"] = ["."]
        manifest(repository)
        write(root, "new/nested/input")
    elif change == "delete-declared":
        (root / "alpha/input").unlink()
    elif change == "rename":
        data["suites"]["beta"]["files"] = ["beta/input", "beta/renamed"]
        manifest(repository)
        git(root, "mv", "alpha/input", "beta/renamed")
    elif change == "run-all":
        write(root, "scripts/ci/new.py")
    elif change == "child-wildcard":
        data["suites"]["beta"]["children"] = [{"exe": "git", "cwd": ".", "reads": ["**"]}]
        manifest(repository)
        write(root, "unread", "changed\n")
    head = base if change == "no-changes" else commit(root)
    assert selected(repository, "--base", base, "--head", head) == (expected, reason)


def test_select_cached_and_default_git_root(repository):
    root, declared, logs, data, base = repository
    write(root, "alpha/input", "modified\n")
    git(root, "add", "alpha/input")
    assert selected(repository, "--cached") == ({"alpha": "1", "beta": "0"}, "selected")
    code, output = run(repository, "select", "--cached", default_root=False, cwd=root / "alpha")
    assert code == 0
    assert output == "alpha=1\nbeta=0\nscope reason: selected\n"


@pytest.mark.parametrize("field", ["reads", "exists"])
@pytest.mark.parametrize("kind,path", [
    ("M", "scripts/a.py"), ("A", "scripts/new.py"), ("D", "scripts/a.py"),
    ("T", "scripts/a.py"), ("A", "scripts"), ("D", "scripts"),
    ("T", "scripts"), ("A", "scripts-other/a.py"), ("M", "hooks/a.py"),
])
def test_select_child_reads_and_existence(repository, monkeypatch, field, kind, path):
    child = {"exe": "git", "cwd": ".", "reads": []}
    data = repository[3]
    data["suites"]["alpha"] = suite(children=[child])
    if field == "reads":
        child["reads"] = ["scripts/**"]
    else:
        data["suites"]["alpha"]["exists"] = ["scripts"]
    manifest(repository)
    monkeypatch.setattr(suite_reads, "_diff", lambda *args: [(kind, path)])
    selections, reason = suite_reads._select(None, suite_reads._load(repository[1]), repository[0])
    expected = int((path == "scripts" or path.startswith("scripts/"))
                   and (kind != "M" or field == "reads"))
    assert selections == {"alpha": expected, "beta": 0}
    assert reason == "selected"


def test_select_child_existence_symlink_modification(repository, monkeypatch):
    repository[3]["suites"]["alpha"] = suite(children=[
        {"exe": "git", "cwd": ".", "reads": ["scripts/**"]},
    ])
    monkeypatch.setattr(suite_reads, "_git", lambda *args:
                        b":120000 120000 1234567 7654321 M\0scripts/link\0")
    args = type("Arguments", (), {"cached": True})()
    assert suite_reads._select(args, repository[3], repository[0]) == (
        {"alpha": 1, "beta": 0}, "selected")


@pytest.mark.parametrize("status", ["A", "D"])
@pytest.mark.parametrize("probe", ["x", "."])
def test_select_descendant_of_file_probe(repository, status, probe):
    root, declared, logs, data, base = repository
    data["suites"]["alpha"] = suite(files=[probe])
    manifest(repository)
    if status == "D":
        write(root, "x/y")
        git(root, "add", "x/y")
        git(root, "commit", "--quiet", "-m", "add descendant")
        base = git(root, "rev-parse", "HEAD")
        (root / "x/y").unlink()
        git(root, "add", "x/y")
    else:
        write(root, "x/y")
        git(root, "add", "x/y")
    git(root, "commit", "--quiet", "-m", "change descendant")
    head = git(root, "rev-parse", "HEAD")
    assert selected(repository, "--base", base, "--head", head) == (
        {"alpha": "1", "beta": "0"}, "selected")


def test_select_invalid_manifest_and_failed_git_are_full(repository):
    root, declared, logs, data, base = repository
    data["suites"]["alpha"]["files"] = ["z", "a"]
    manifest(repository)
    selections, reason = selected(repository, "--base", base, "--head", base)
    assert selections == {"alpha": "1", "beta": "1"}
    assert "invalid declared" in reason
    data["suites"]["alpha"]["files"] = ["alpha/input"]
    manifest(repository)
    selections, reason = selected(repository, "--base", "not-a-revision", "--head", base)
    assert selections == {"alpha": "1", "beta": "1"}
    assert "git" in reason


def test_select_unsupported_status_fails_closed(repository, monkeypatch, capsys):
    monkeypatch.setattr(suite_reads, "_git", lambda *args: b":100644 100644 0000000 0000000 U\0alpha/input\0")
    assert suite_reads.main(["select", "--cached", "--root", str(repository[0])]) == 0
    assert capsys.readouterr().out == "alpha=1\nbeta=1\nscope reason: unsupported git status U\n"


def test_rename_and_copy_parse_both_sides(repository, monkeypatch):
    monkeypatch.setattr(suite_reads, "_git", lambda *args:
                        b":100644 100644 1234567 1234567 R100\0old\0new\0"
                        b":100644 100644 1234567 1234567 C100\0source\0copy\0")
    arguments = type("Arguments", (), {"cached": True})()
    assert suite_reads._diff(arguments, repository[0]) == [
        ("D", "old"), ("A", "new"), ("D", "source"), ("A", "copy"),
    ]


def test_record_sorted_files_dirs_and_manual_children(repository):
    root, declared, logs, data, base = repository
    log(repository, {"k": "f", "p": "unread"}, {"k": "f", "p": "a-missing"},
        {"k": "f", "p": "beta/input"}, {"k": "d", "p": "beta"},
        {"k": "d", "p": "a-missing-dir"},
        {"k": "x", "exe": "git", "cwd": ".", "paths": ["unread"]})
    code, output = run(repository, "record")
    assert code == 1
    assert "undeclared child: git cwd=. paths=unread" in output
    text = declared.read_text(encoding="utf-8")
    recorded = json.loads(text)
    assert recorded["suites"]["alpha"]["files"] == ["a-missing", "alpha/input", "beta/input", "unread"]
    assert recorded["suites"]["alpha"]["dirs"] == ["a-missing-dir", "alpha", "beta"]
    assert recorded["suites"]["alpha"]["children"] == []
    assert text == json.dumps(recorded, indent=1) + "\n"
    assert not list(declared.parent.glob("suite-reads.json.*"))
    log(repository, {"k": "f", "p": "unread"}, {"k": "f", "p": "a-missing"},
        {"k": "d", "p": "beta"}, {"k": "d", "p": "a-missing-dir"})
    assert run(repository, "check")[0] == 0
    assert run(repository, "record")[0] == 0


@pytest.mark.parametrize("existing", [False, True])
def test_i3_record_children_merges_paths_preserves_exact_argv_and_is_byte_stable(repository, existing):
    root, declared, logs, data, base = repository
    if existing:
        data["suites"]["alpha"]["children"] = [{"exe": "bash", "cwd": ".", "reads": ["alpha/input"]}]
        manifest(repository)
    python_argv = ["python", "-c", "import sys\nfor value in sys.argv:\n    print(value)\n"]
    log(repository,
        {"k": "x", "exe": "uname", "cwd": None, "paths": [], "argv": ["uname", "-p"]},
        {"k": "x", "exe": "uname", "cwd": None, "paths": [], "argv": ["uname", "-m"]},
        {"k": "x", "exe": "bash", "cwd": ".", "paths": ["alpha/other"],
         "argv": ["bash", "root:/alpha/other"], "sub": "ignored", "opts": ["-n"]},
        {"k": "x", "exe": "bash", "cwd": ".", "paths": ["beta/input"]},
        {"k": "x", "exe": "python", "cwd": ".", "paths": [], "argv": python_argv})
    code, output = run(repository, "record", "--children")
    assert code == 0
    expected = [
        {"exe": "bash", "cwd": ".", "reads": (["alpha/input"] if existing else []) + ["alpha/other", "beta/input"]},
        {"exe": "python", "cwd": ".", "reads": [], "argv": python_argv},
        {"exe": "uname", "cwd": None, "reads": [], "argv": ["uname", "-m"]},
        {"exe": "uname", "cwd": None, "reads": [], "argv": ["uname", "-p"]},
    ]
    assert json.loads(declared.read_text(encoding="utf-8"))["suites"]["alpha"]["children"] == expected
    assert output.splitlines() == [
        f"  declared child: exe={child['exe']} cwd={child['cwd']} reads={child['reads']} argv={child.get('argv', [])}"
        for child in expected
    ] + ["  review: an opaque child must read nothing beyond its argv, cwd and env hints"]
    before = declared.read_bytes()
    assert run(repository, "record", "--children")[0] == 0
    assert declared.read_bytes() == before
    assert run(repository, "check")[0] == 0


@pytest.mark.parametrize("children_flag", [False, True])
@pytest.mark.parametrize("entry,notice", [
    ({"exe": "git", "cwd": ".", "paths": ["unread"], "argv": ["git", "show", "HEAD:unread"]},
     '  git child needs "**" in reads or an always-run carve-out: '),
    ({"exe": "opaque", "cwd": None, "paths": []}, " (no argv; declare by hand)"),
    ({"exe": "uname", "cwd": ".", "paths": [], "argv": ["uname", "-p"]}, None),
    ({"exe": "bash", "cwd": ".", "paths": ["unread"]}, None),
])
def test_i4_record_children_is_opt_in_and_leaves_git_and_unknown_identity_manual(
        repository, children_flag, entry, notice):
    log(repository, {"k": "x", **entry})
    code, output = run(repository, "record", *(["--children"] if children_flag else []))
    recorded = json.loads(repository[1].read_text(encoding="utf-8"))["suites"]["alpha"]["children"]
    if children_flag and notice is None:
        assert code == 0
        declaration = {"exe": entry["exe"], "cwd": entry["cwd"], "reads": entry["paths"]}
        if not entry["paths"]:
            declaration["argv"] = entry["argv"]
        assert recorded == [declaration]
        assert run(repository, "check")[0] == 0
    else:
        assert code == 1
        assert recorded == []
        detail, = suite_reads._details(([], [], [], [entry]))
        expected = (notice + detail if entry["exe"] == "git" else detail + notice) if children_flag else detail
        assert expected in output.splitlines()


def test_record_creates_unknown_suite(repository):
    log(repository, {"k": "f", "p": "unread"})
    assert run(repository, "record", "--suite", "new-suite")[0] == 0
    recorded = json.loads(repository[1].read_text(encoding="utf-8"))
    assert recorded["suites"]["new-suite"] == suite(files=["unread"])
    assert run(repository, "check", "--suite", "new-suite")[0] == 0


@pytest.mark.parametrize("path,pattern,expected", [
    ("anything/nested", "**", True), ("assets", "assets/**", True),
    ("assets/nested/input", "assets/**", True), ("assets-other", "assets/**", False),
    ("a/b.py", "*.py", True), ("a/b.txt", "*.py", False),
])
def test_glob_semantics(path, pattern, expected):
    assert suite_reads._matches(path, pattern) is expected


@pytest.mark.parametrize("mutation", [
    lambda d: d.update(version=True),
    lambda d: d.update(ignore=["same", "same"]),
    lambda d: d.update(run_all=["/absolute"]),
    lambda d: d.update(run_all=["windows\\path"]),
    lambda d: d["suites"].update({"Bad_Name": suite()}),
    lambda d: d["suites"]["alpha"].update(extra=[]),
    lambda d: d["suites"]["alpha"].update(children=[{"exe": "git", "cwd": "."}]),
    lambda d: d["suites"]["alpha"].update(children=[{"exe": "/git", "cwd": ".", "reads": [], "exists": []}]),
    lambda d: d["suites"]["alpha"].update(children=[{"exe": "git", "cwd": "..", "reads": [], "exists": []}]),
])
def test_strict_declared_schema(repository, mutation):
    data = copy.deepcopy(repository[3])
    mutation(data)
    manifest(repository, data)
    log(repository)
    assert run(repository, "check")[0] == 2


@pytest.mark.parametrize("value", [None, ["z", "a"], ["same", "same"], ["../outside"],
                                  ["/absolute"], ["windows\\path"], [42]])
def test_child_exists_schema_validates_like_reads(repository, value):
    repository[3]["suites"]["alpha"] = suite(children=[
        {"exe": "git", "cwd": ".", "reads": [], "exists": value},
    ])
    manifest(repository)
    log(repository)
    assert run(repository, "check")[0] == 2


@pytest.mark.parametrize("mutation", ["missing-reads", "extra-key", "bad-sub", "exists"])
def test_child_declared_schema_is_strict(repository, mutation):
    child = {"exe": "git", "cwd": ".", "reads": []}
    if mutation == "missing-reads":
        del child["reads"]
    elif mutation == "extra-key":
        child["extra"] = None
    elif mutation == "bad-sub":
        child["sub"] = 42
    else:
        child["exists"] = []
    repository[3]["suites"]["alpha"] = suite(children=[child])
    manifest(repository)
    log(repository)
    assert run(repository, "check")[0] == 2


@pytest.mark.parametrize("extra", [{"sub": 42}, {"sub": []}, {"other": None},
                                   {"sub": "log", "other": None}])
def test_child_log_schema_is_strict(repository, extra):
    log(repository, {"k": "x", "exe": "git", "cwd": ".", "paths": [], **extra})
    code, output = run(repository, "check")
    assert code == 2
    assert "invalid audit child" in output


@pytest.mark.parametrize("kind,expected", [("e", 1), ("f", 1)])
def test_child_exists_never_covers_content_reads(repository, kind, expected):
    repository[3]["suites"]["alpha"] = suite(children=[
        {"exe": "git", "cwd": ".", "reads": []},
    ])
    manifest(repository)
    log(repository, {"k": kind, "p": "unread"})
    assert run(repository, "check")[0] == expected


@pytest.mark.parametrize("message", ["RuntimeError: failed to resolve input", "x" * 500, ""])
def test_unusable_error_explains_first_failure(repository, message):
    logs = repository[2]
    log(repository)
    (logs / "1.error").write_text(message + "\ntraceback detail\n", encoding="utf-8")
    (logs / "2.error").write_text("later failure\n", encoding="utf-8")
    code, output = run(repository, "check")
    assert code == 2
    assert f"reason=audit error file ({message[:300]})\n" in output
    assert "traceback detail" not in output
    assert "later failure" not in output


@pytest.mark.parametrize("value", ["/absolute", "../parent", "bad\\path", "bad\0path", None,
                                  42, "/" + "x" * 400])
def test_unusable_audit_path_includes_bounded_repr(repository, value):
    log(repository, {"k": "f", "p": value})
    code, output = run(repository, "check")
    assert code == 2
    assert f"reason=invalid audit path ({repr(value)[:200]})\n" in output


@pytest.mark.parametrize("entry", [None, ["invalid"], {"k": "unknown"}, {"k": []}, {"k": "f"},
                                  {"k": "x", "exe": "/git", "cwd": ".", "paths": []}])
def test_unusable_audit_entry_includes_repr(repository, entry):
    log(repository, entry)
    code, output = run(repository, "check")
    assert code == 2
    assert "UNUSABLE" in output
    assert f"({repr(entry)[:200]})\n" in output


def test_trace_sidecars_are_never_parsed_as_logs(repository):
    log(repository, {"k": "f", "p": "alpha/input"})
    (repository[2] / "1-test.trace").write_text("invalid JSON\n", encoding="utf-8")
    assert run(repository, "check") == (0, "suite read check: ok suite=alpha files=1 dirs=0 children=0\n")


@pytest.mark.parametrize("path", ["unread", "beta", "missing/probe"])
@pytest.mark.parametrize("coverage", ["exists", "files", "dirs", "globs", "child", None])
def test_check_existence_reads(repository, path, coverage):
    data = repository[3]
    if coverage == "child":
        data["suites"]["alpha"]["children"] = [{"exe": "git", "cwd": ".", "reads": [path]}]
    elif coverage is not None:
        data["suites"]["alpha"][coverage] = [path]
    manifest(repository)
    log(repository, {"k": "e", "p": path})
    code, output = run(repository, "check")
    if coverage is None or coverage == "dirs":
        assert code == 1
        assert f"  undeclared exists: {path}\n" in output
    else:
        assert code == 0


@pytest.mark.parametrize("path", ["unread", "beta", "missing/probe"])
def test_exists_declaration_never_covers_content_reads(repository, path):
    repository[3]["suites"]["alpha"] = suite(exists=[path])
    manifest(repository)
    log(repository, {"k": "f", "p": path})
    code, output = run(repository, "check")
    assert code == 1
    assert f"  undeclared file: {path}\n" in output


def test_check_ignores_untracked_and_ignored_existence_reads(repository):
    root, declared, logs, data, base = repository
    write(root, "generated")
    data["ignore"] = ["beta/**", "ignored/**"]
    manifest(repository)
    log(repository, {"k": "e", "p": "generated"}, {"k": "e", "p": "beta/input"},
        {"k": "e", "p": "beta"}, {"k": "e", "p": "ignored/missing"})
    assert run(repository, "check")[0] == 0


@pytest.mark.parametrize("change,expected", [
    ("modify", "0"), ("delete", "1"), ("add-descendant", "1"),
    ("replace-with-symlink", "1"), ("retarget-symlink", "1"),
    ("modify-file-read", "1"), ("modify-descendant", "0"),
    ("rename", "1"),
])
@pytest.mark.parametrize("cached", [False, True])
def test_select_existence_changes(repository, change, expected, cached):
    root, declared, logs, data, base = repository
    git(root, "config", "core.excludesFile", str(root.parent / "excludes"))
    (root.parent / "excludes").write_text(".codebuild/\n", encoding="utf-8")
    data["suites"]["alpha"] = suite(exists=["a.txt", "dir"])
    if change == "modify-file-read":
        data["suites"]["alpha"] = suite(files=["a.txt"])
    manifest(repository)
    write(root, "dir/keep.txt")
    if change == "retarget-symlink":
        (root / "a.txt").symlink_to("unread")
    else:
        write(root, "a.txt")
    base = commit(root)
    if change in {"modify", "modify-file-read"}:
        write(root, "a.txt", "changed\n")
    elif change == "delete":
        (root / "a.txt").unlink()
    elif change == "add-descendant":
        write(root, "dir/new.txt")
    elif change in {"replace-with-symlink", "retarget-symlink"}:
        (root / "a.txt").unlink()
        (root / "a.txt").symlink_to("beta/input")
    elif change == "modify-descendant":
        write(root, "dir/keep.txt", "changed\n")
    elif change == "rename":
        git(root, "mv", "a.txt", "moved.txt")
    git(root, "add", "--all")
    if cached:
        args = ["--cached"]
    else:
        git(root, "commit", "--quiet", "-m", "change")
        args = ["--base", base, "--head", git(root, "rev-parse", "HEAD")]
    assert selected(repository, *args) == ({"alpha": expected, "beta": "0"}, "selected")


@pytest.mark.parametrize("old_mode,new_mode,status,kind", [
    ("100644", "100755", "M", "M"),
    ("100644", "120000", "T", "T"),
    ("100644", "120000", "M", "T"),
    ("120000", "100644", "M", "T"),
    ("120000", "120000", "M", "T"),
])
def test_raw_modes_classify_changes(repository, monkeypatch, old_mode, new_mode, status, kind):
    calls = []

    def raw_diff(root, *args):
        calls.append(args)
        return f":{old_mode} {new_mode} 1234567 7654321 {status}\0a.txt\0".encode()

    monkeypatch.setattr(suite_reads, "_git", raw_diff)
    args = type("Arguments", (), {"cached": True})()
    assert suite_reads._diff(args, repository[0]) == [(kind, "a.txt")]
    assert calls == [("diff", "-z", "--raw", "--find-renames", "--cached")]
    repository[3]["suites"]["alpha"] = suite(exists=["a.txt"])
    selections, reason = suite_reads._select(args, repository[3], repository[0])
    assert selections == {"alpha": int(kind == "T"), "beta": 0}
    assert reason == "selected"


@pytest.mark.parametrize("probe", [".", "dir"])
@pytest.mark.parametrize("kind", ["A", "D", "T"])
def test_existence_ancestors_select_structural_changes(repository, monkeypatch, probe, kind):
    data = repository[3]
    data["suites"]["alpha"] = suite(exists=[probe])
    monkeypatch.setattr(suite_reads, "_diff", lambda *args: [(kind, "dir/nested/input")])
    selections, reason = suite_reads._select(None, data, repository[0])
    assert selections == {"alpha": 1, "beta": 0}
    assert reason == "selected"


def test_record_sorted_existence_reads(repository):
    root, declared, logs, data, base = repository
    data["suites"]["alpha"]["exists"] = ["z-existing-declaration"]
    manifest(repository)
    log(repository, {"k": "e", "p": "unread"}, {"k": "e", "p": "a-missing"},
        {"k": "e", "p": "beta"}, {"k": "e", "p": "unread"},
        {"k": "e", "p": "alpha/input"})
    assert run(repository, "record")[0] == 0
    recorded = json.loads(declared.read_text(encoding="utf-8"))
    assert recorded["suites"]["alpha"]["exists"] == [
        "a-missing", "beta", "unread", "z-existing-declaration",
    ]
    assert recorded["suites"]["alpha"]["files"] == ["alpha/input"]
    assert recorded["suites"]["alpha"]["dirs"] == ["alpha"]
    assert run(repository, "check")[0] == 0


@pytest.mark.parametrize("value", [None, ["z", "a"], ["same", "same"], ["../outside"], [42]])
def test_exists_schema_validates_like_files(repository, value):
    repository[3]["suites"]["alpha"]["exists"] = value
    manifest(repository)
    log(repository)
    assert run(repository, "check")[0] == 2


def test_exists_schema_is_required(repository):
    del repository[3]["suites"]["alpha"]["exists"]
    manifest(repository)
    log(repository)
    assert run(repository, "check")[0] == 2


def batch_log(repository, name, *entries):
    directory = repository[2] / name
    directory.mkdir()
    log((*repository[:2], directory, *repository[3:]), *entries)
    return directory


def batch_arguments(repository):
    return ["check", "--all", "--audit-root", str(repository[2]),
            "--root", str(repository[0])]


def test_check_all_mixed_results_sorted_and_shared_work(repository, monkeypatch, capsys):
    root, declared, logs, data, base = repository
    data["suites"]["gamma"] = suite()
    manifest(repository)
    (logs / "gamma").mkdir()
    beta = batch_log(repository, "beta", {"k": "f", "p": "unread"})
    batch_log(repository, "alpha", {"k": "f", "p": "alpha/input"})
    (logs / "not-a-directory").write_text("ignored", encoding="utf-8")
    git_calls, load_calls = [], []
    original_git, original_load = suite_reads._git, suite_reads._load

    def counted_git(root, *args):
        git_calls.append(args)
        return original_git(root, *args)

    def counted_load(path):
        load_calls.append(path)
        return original_load(path)

    monkeypatch.setattr(suite_reads, "_git", counted_git)
    monkeypatch.setattr(suite_reads, "_load", counted_load)
    assert suite_reads.main(batch_arguments(repository)) == 2
    assert capsys.readouterr().out == (
        "suite read check: ok suite=alpha files=1 dirs=0 children=0\n"
        "suite read check: FAIL suite=beta undeclared=1\n"
        "  undeclared file: unread\n"
        "  fix: python3 scripts/ci/suite_reads.py record --suite beta "
        f"--audit-dir {beta}, then declare any child by hand\n"
        "suite read check: UNUSABLE suite=gamma reason=no audit logs\n"
    )
    assert git_calls == [("ls-files", "-z")]
    assert load_calls == [declared]


@pytest.mark.parametrize("undeclared", [False, True])
def test_check_all_usable_exit_codes(repository, capsys, undeclared):
    batch_log(repository, "beta", {"k": "f", "p": "unread" if undeclared else "beta/input"})
    batch_log(repository, "alpha", {"k": "f", "p": "alpha/input"})
    assert suite_reads.main(batch_arguments(repository)) == int(undeclared)
    results = [line for line in capsys.readouterr().out.splitlines()
               if line.startswith("suite read check:")]
    assert results == [
        "suite read check: ok suite=alpha files=1 dirs=0 children=0",
        ("suite read check: FAIL suite=beta undeclared=1" if undeclared else
         "suite read check: ok suite=beta files=1 dirs=0 children=0"),
    ]


@pytest.mark.parametrize("unknown", [False, True])
def test_check_all_unknown_or_empty(repository, capsys, unknown):
    if unknown:
        (repository[2] / "unknown").mkdir()
    assert suite_reads.main(batch_arguments(repository)) == 2
    assert capsys.readouterr().out == (
        "suite read check: UNUSABLE suite=unknown reason=unknown suite\n" if unknown else
        "suite read check: UNUSABLE suite=all reason=no suites\n"
    )


@pytest.mark.parametrize("arguments", [
    ["--all", "--suite", "alpha", "--audit-root", "audit"],
    ["--all", "--audit-dir", "audit", "--audit-root", "audit"],
    ["--suite", "alpha", "--audit-dir", "audit", "--audit-root", "audit"],
    ["--all"], ["--suite", "alpha"], ["--audit-root", "audit"],
])
def test_check_batch_argument_pairs(arguments, capsys):
    with pytest.raises(SystemExit) as exc:
        suite_reads.main(["check", *arguments])
    assert exc.value.code == 2
    assert "error:" in capsys.readouterr().err


def test_check_all_output_caps_and_counts_after_truncation(repository, capsys):
    data = repository[3]
    entries = [{"k": "f", "p": f"missing-{index:03d}"} for index in range(205)]
    for index in range(11):
        name = f"suite-{index:02d}"
        data["suites"][name] = suite()
        batch_log(repository, name, *entries)
    manifest(repository)
    assert suite_reads.main(batch_arguments(repository)) == 1
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2001
    assert lines[-1] == "  ... output truncated"
    assert lines.count("  ... output truncated") == 1
    assert lines[0] == "suite read check: FAIL suite=suite-00 undeclared=205"
    assert lines[200] == "  undeclared file: missing-199"
    assert lines[201].startswith("  fix:")
    assert "  undeclared file: missing-200" not in lines
    (repository[2] / "zzz-unknown").mkdir()
    assert suite_reads.main(batch_arguments(repository)) == 2
    assert len(capsys.readouterr().out.splitlines()) == 2001


@pytest.mark.parametrize("directory", ["alpha", "."])
@pytest.mark.parametrize("coverage", ["globs", "reads", "exists"])
@pytest.mark.parametrize("kind", ["M", "A", "D", "T"])
def test_r10_exact_directory_coverage_agrees_with_selection(repository, monkeypatch,
                                                          directory, coverage, kind):
    child = {"exe": "opaque", "cwd": ".", "reads": []}
    declared = suite()
    if coverage == "globs":
        declared["globs"] = [directory]
        assert suite_reads._undeclared(([], [directory], [], []), declared) == ([], [], [], [])
    elif coverage == "exists":
        declared["exists"] = [directory]
        assert suite_reads._undeclared(([], [], [directory], []), declared) == ([], [], [], [])
    else:
        child[coverage] = [directory]
        declared["children"] = [child]
        entry = {"k": "x", "exe": "opaque", "cwd": ".",
                 "paths": [directory]}
        assert suite_reads._child_covered(entry, declared)
    repository[3]["suites"]["alpha"] = declared
    monkeypatch.setattr(suite_reads, "_diff", lambda *args: [(kind, "alpha/nested/input")])
    selections, _ = suite_reads._select(None, repository[3], repository[0])
    assert selections["alpha"] == int(kind != "M" or coverage == "reads")


@pytest.mark.parametrize("patterns,expected", [([], False), (["scripts/**"], False),
                                               (["."], False), (["**"], True)])
def test_r11_empty_child_without_cwd_needs_universal_glob(patterns, expected):
    declared = {"exe": "git", "cwd": None, "reads": patterns}
    entry = {"k": "x", "exe": "git", "cwd": None, "paths": []}
    assert suite_reads._child_covered(entry, suite(children=[declared])) is expected


@pytest.mark.parametrize("sub,opts,expected", [
    ("ls-files", None, False), ("ls-files", [], False),
    ("ls-files", ["-z"], False), ("ls-files", ["--cached", "--exclude=", "-z"], False),
    ("ls-files", ["-s"], False), ("ls-files", ["--stage"], False),
    ("ls-files", ["--eol"], False), ("ls-files", ["--debug"], False),
    ("ls-files", ["--format="], False), ("show", [], False), (None, [], False),
])
def test_r12_only_membership_git_options_allow_exists(repository, sub, opts, expected):
    declared = {"exe": "git", "cwd": ".", "sub": sub, "reads": ["scripts/**"]}
    entry = {"k": "x", "exe": "git", "cwd": ".", "sub": sub, "paths": ["scripts"]}
    if opts is not None:
        entry["opts"] = opts
    repository[3]["suites"]["alpha"] = suite(children=[declared])
    manifest(repository)
    log(repository, entry)
    assert run(repository, "check")[0] == int(not expected)
    declared["reads"] = ["**"]
    manifest(repository)
    assert run(repository, "check")[0] == 0


@pytest.mark.parametrize("opts", [None, "-s", ["-z", "-s"], ["-s", "-s"], [42], ["stage"]])
def test_r12_option_log_schema_is_strict(repository, opts):
    log(repository, {"k": "x", "exe": "git", "cwd": ".", "paths": [], "opts": opts})
    code, output = run(repository, "check")
    assert code == 2
    assert "invalid audit child" in output


@pytest.mark.parametrize("extra", [{}, {"sub": None}, {"sub": "ls-files", "opts": ["-s"]}])
def test_r13_undeclared_child_includes_subcommand_and_options(repository, extra):
    log(repository, {"k": "x", "exe": "git", "cwd": ".", "paths": ["unread"], **extra})
    code, output = run(repository, "check")
    assert code == 1
    line, = [line for line in output.splitlines() if "undeclared child:" in line]
    assert (" sub=" in line) == ("sub" in extra)
    assert (" opts=" in line) == ("opts" in extra)
    if "sub" in extra:
        assert f"sub={extra['sub']}" in line
    if "opts" in extra:
        assert f"opts={extra['opts']}" in line


@pytest.mark.parametrize("cwd", [".", None])
def test_r14_ignored_child_paths_do_not_create_cwd_observations(repository, cwd):
    data = repository[3]
    data["ignore"] = ["ignored/**"]
    data["suites"]["alpha"] = suite(children=[
        {"exe": "git", "cwd": cwd, "reads": []},
    ])
    manifest(repository)
    log(repository, {"k": "x", "exe": "git", "cwd": cwd, "paths": ["ignored/input"]})
    assert run(repository, "check")[0] == 1
    log(repository, {"k": "x", "exe": "git", "cwd": cwd, "paths": []})
    assert run(repository, "check")[0] == 1


@pytest.mark.parametrize("exe,sub,cwd,paths,reads,expected", [
    ("uname", None, None, [], [], 1),
    ("sh", None, ".", ["unread"], [], 1),
    ("sh", None, ".", ["unread"], ["unread"], 0),
    ("git", "show", ".", [], [], 1),
    ("git", "show", ".", [], ["."], 1),
    ("git", "ls-files", ".", [], [], 1),
    ("git", "ls-files", None, [], [], 1),
    ("git", "ls-files", None, [], ["**"], 0),
    ("git", "ls-files", ".", ["unread"], ["unread"], 1),
])
def test_c11_child_requirements_need_content_reads_except_git_membership(
        repository, exe, sub, cwd, paths, reads, expected):
    repository[3]["suites"]["alpha"] = suite(children=[
        {"exe": exe, "cwd": cwd, "sub": sub, "reads": reads},
    ])
    manifest(repository)
    argv = ["sh", "unread"] if exe == "sh" else ["git", "show", "HEAD:x"]
    log(repository, {"k": "x", "exe": exe, "cwd": cwd, "sub": sub, "paths": paths, "argv": argv})
    assert run(repository, "check")[0] == expected


def test_c12_exclude_standard_is_not_membership_only(repository):
    declared = {"exe": "git", "cwd": ".", "sub": "ls-files", "reads": []}
    repository[3]["suites"]["alpha"] = suite(children=[declared])
    manifest(repository)
    log(repository, {"k": "x", "exe": "git", "cwd": ".", "sub": "ls-files", "paths": ["unread"],
                     "opts": ["--exclude-standard", "-o"]})
    assert run(repository, "check")[0] == 1
    declared["reads"] = ["**"]
    manifest(repository)
    assert run(repository, "check")[0] == 0


def test_c13_tracked_symlink_directory_listing_requires_coverage_and_selects_type_change(repository, monkeypatch):
    root, declared, logs, data, base = repository
    (root / "listed-link").symlink_to("alpha", target_is_directory=True)
    commit(root)
    log(repository, {"k": "d", "p": "listed-link"})
    code, output = run(repository, "check")
    assert code == 1
    assert "undeclared dir: listed-link" in output
    data["suites"]["alpha"] = suite(dirs=["listed-link"])
    manifest(repository)
    assert run(repository, "check")[0] == 0
    monkeypatch.setattr(suite_reads, "_diff", lambda *args: [("T", "listed-link")])
    selections, reason = suite_reads._select(None, data, root)
    assert selections == {"alpha": 1, "beta": 0}
    assert reason == "selected"


@pytest.mark.parametrize("argv,valid", [([], True), (["sh", "root:unread"], True),
                                        (None, False), ("git status", False),
                                        ([42], False), (["git", None], False)])
def test_c14_child_argv_is_validated_and_printed_but_never_covers_paths(repository, argv, valid):
    entry = {"k": "x", "exe": "opaque", "cwd": ".", "paths": ["unread"], "argv": argv}
    log(repository, entry)
    code, output = run(repository, "check")
    assert code == (1 if valid else 2)
    if valid:
        assert "argv=" + " ".join(argv) in output
        repository[3]["suites"]["alpha"] = suite(children=[
            {"exe": "opaque", "cwd": ".", "reads": ["unread"]},
        ])
        manifest(repository)
        assert run(repository, "check")[0] == 0
    else:
        assert "invalid audit child" in output


@pytest.mark.parametrize("cwd", [".", "scripts", None])
def test_d5_git_needs_universal_reads_and_empty_python_needs_exact_argv(repository, cwd):
    data = repository[3]
    child = {"exe": "git", "cwd": cwd, "sub": "ls-files", "opts": ["-z"],
             "reads": ["scripts/**"]}
    data["suites"]["alpha"] = suite(children=[child])
    manifest(repository)
    log(repository, {"k": "x", "exe": "git", "cwd": cwd, "sub": "show", "opts": [],
                     "paths": ["scripts/a.py"], "argv": ["git", "show", "HEAD:scripts/a.py"]})
    assert run(repository, "check")[0] == 1
    child["reads"] = ["**"]
    manifest(repository)
    assert run(repository, "check")[0] == 0

    argv = ["python", "-I", "-c", "pass"]
    child = {"exe": "python", "cwd": cwd, "reads": ["**"]}
    data["suites"]["alpha"] = suite(children=[child])
    entry = {"k": "x", "exe": "python", "cwd": cwd, "paths": [], "argv": argv}
    manifest(repository)
    log(repository, entry)
    assert run(repository, "check")[0] == 1
    child["reads"], child["argv"] = [], argv
    manifest(repository)
    assert run(repository, "check")[0] == 0
    log(repository, entry, {**entry, "argv": ["python", "-I", "-c", "print(1)"]})
    code, output = run(repository, "check")
    assert code == 1
    assert "argv=python -I -c print(1)" in output
    log(repository, {key: value for key, value in entry.items() if key != "argv"})
    assert run(repository, "check")[0] == 1


@pytest.mark.parametrize("exe,paths,count", [("git", [], 1), ("git", ["unread"], 1),
                                           ("opaque", ["unread"], 1), ("opaque", [], 2)])
def test_d6_observed_children_dedupe_argv_only_for_pathless_non_git(repository, exe, paths, count):
    entry = {"k": "x", "exe": exe, "cwd": ".", "paths": paths}
    log(repository, {**entry, "argv": [exe, "first"]},
        {**entry, "argv": [exe, "second"]}, {**entry, "argv": [exe, "first"]})
    observed = suite_reads._observed(repository[0], repository[2], [])
    assert len(observed[3]) == count
    assert observed[3][0]["argv"] == [exe, "first"]


@pytest.mark.parametrize("kind", ["f", "e"])
@pytest.mark.parametrize("path", ["unread", "alpha", "missing/probe"])
def test_d7_directory_declarations_never_cover_file_or_existence_entries(repository, kind, path):
    data = repository[3]
    data["suites"]["alpha"] = suite(dirs=[path])
    manifest(repository)
    log(repository, {"k": kind, "p": path})
    code, output = run(repository, "check")
    assert code == 1
    assert f"undeclared {'file' if kind == 'f' else 'exists'}: {path}" in output
    data["suites"]["alpha"]["files" if kind == "f" else "exists"] = [path]
    manifest(repository)
    assert run(repository, "check")[0] == 0


@pytest.mark.parametrize("field", ["files", "dirs", "exists"])
@pytest.mark.parametrize("kind", ["A", "D", "T", "M"])
def test_d8_ancestors_of_declared_entries_select_structural_changes(repository, monkeypatch, field, kind):
    data = repository[3]
    data["suites"]["alpha"] = suite(**{field: ["link/deep/input"]})
    monkeypatch.setattr(suite_reads, "_diff", lambda *args: [(kind, "link")])
    selections, _ = suite_reads._select(None, data, repository[0])
    assert selections == {"alpha": int(kind != "M"), "beta": 0}


@pytest.mark.parametrize("cwd", ["ignored", None])
def test_d9_ignore_keeps_empty_path_children_even_with_ignored_cwd(repository, cwd):
    data = repository[3]
    data["ignore"] = ["ignored/**"]
    data["suites"]["alpha"] = suite()
    manifest(repository)
    entry = {"k": "x", "exe": "git", "cwd": cwd, "paths": [], "argv": ["git", "status"]}
    log(repository, entry)
    code, output = run(repository, "check")
    assert code == 1
    assert "undeclared child: git" in output
    log(repository, {**entry, "cwd": ".", "paths": ["ignored/input"]})
    assert run(repository, "check")[0] == 1


@pytest.mark.parametrize("exe,paths,argv", [("git", ["unread"], ["git", "show", "HEAD:x"]),
                                         ("python", [], ["python", "-I", "-c", "pass"]),
                                         ("opaque", ["unread"], [])])
def test_d10_every_child_failure_prints_supplied_argv(repository, exe, paths, argv):
    log(repository, {"k": "x", "exe": exe, "cwd": ".", "paths": paths, "argv": argv})
    code, output = run(repository, "check")
    assert code == 1
    line, = [line for line in output.splitlines() if "undeclared child:" in line]
    assert "argv=" + " ".join(argv) in line


@pytest.mark.parametrize("exe", ["git", "opaque"])
def test_e8_ignore_keeps_children_and_requires_exact_argv_after_filtering(repository, exe):
    data = repository[3]
    data["ignore"] = ["ignored/**"]
    entry = {"k": "x", "exe": exe, "cwd": ".", "paths": ["ignored/input"],
             "argv": [exe, "first"]}
    data["suites"]["alpha"] = suite(children=[{"exe": exe, "cwd": ".", "reads": []}])
    manifest(repository)
    log(repository, entry, {**entry, "argv": [exe, "second"]})
    observed = suite_reads._observed(repository[0], repository[2], data["ignore"])[3]
    assert len(observed) == (1 if exe == "git" else 2)
    assert all(child["paths"] == [] for child in observed)
    code, output = run(repository, "check")
    assert code == 1 and "undeclared child: " + exe in output
    child = data["suites"]["alpha"]["children"][0]
    if exe == "git":
        child["reads"] = ["**"]
    else:
        child["argv"] = entry["argv"]
        manifest(repository)
        assert run(repository, "check")[0] == 1
        data["suites"]["alpha"]["children"].append({**child, "argv": [exe, "second"]})
    manifest(repository)
    assert run(repository, "check")[0] == 0


@pytest.mark.parametrize("path,dirs,expected", [
    ("alpha/other", ["alpha"], 0),
    ("alpha/nested/missing", ["alpha"], 1),
    ("alpha/nested/missing", ["alpha/nested"], 0),
    ("unread", ["."], 0),
    ("unread", ["alpha"], 1),
    ("alpha/other", ["."], 1),
])
def test_exists_is_covered_only_by_direct_parent_listing(repository, path, dirs, expected):
    repository[3]["suites"]["alpha"] = suite(dirs=dirs)
    manifest(repository)
    log(repository, {"k": "e", "p": path})
    code, output = run(repository, "check")
    assert code == expected
    if expected:
        assert f"undeclared exists: {path}" in output


@pytest.mark.parametrize("existing", [[], ["D/x"]])
def test_record_uses_observed_listing_without_pruning_existing_exists(repository, existing):
    root, declared, logs, data, base = repository
    write(root, "D/x")
    commit(root)
    data["suites"]["alpha"] = suite(exists=existing)
    manifest(repository)
    log(repository, {"k": "d", "p": "D"}, {"k": "e", "p": "D/x"},
        {"k": "e", "p": "D/y/z"})
    assert run(repository, "record")[0] == 0
    before = declared.read_bytes()
    recorded = json.loads(before)["suites"]["alpha"]
    assert recorded["dirs"] == ["D"]
    assert recorded["exists"] == sorted([*existing, "D/y/z"])
    assert run(repository, "check")[0] == 0
    assert run(repository, "record")[0] == 0
    assert declared.read_bytes() == before


@pytest.mark.parametrize("kind", ["A", "D", "T", "M"])
def test_parent_listing_selects_existence_changes_but_not_content(repository, monkeypatch, kind):
    root, declared, logs, data, base = repository
    write(root, "D/x")
    commit(root)
    data["suites"]["alpha"] = suite(dirs=["D"])
    monkeypatch.setattr(suite_reads, "_diff", lambda *args: [(kind, "D/x")])
    selections, reason = suite_reads._select(None, data, root)
    assert selections == {"alpha": int(kind != "M"), "beta": 0}
    assert reason == "selected"


@pytest.mark.parametrize("extra", ["size", "mtime"])
def test_existence_log_cannot_carry_content_metadata(repository, extra):
    log(repository, {"k": "e", "p": "alpha/other", extra: 123})
    code, output = run(repository, "check")
    assert code == 2
    assert "invalid audit path" in output


@pytest.mark.parametrize("parent,path", [
    ("da", "da/C:\\Program Files\\Autodesk"),
    (".", "C:\\Program Files\\Autodesk"),
])
def test_backslash_existence_depends_on_parent_listing(repository, parent, path):
    root, declared, logs, data, base = repository
    write(root, "da/tracked")
    commit(root)
    data["suites"]["alpha"] = suite()
    manifest(repository)
    log(repository, {"k": "e", "p": path})
    code, output = run(repository, "check")
    assert code == 1
    assert f"undeclared dir: {parent}" in output
    assert run(repository, "record")[0] == 0
    recorded = json.loads(declared.read_bytes())["suites"]["alpha"]
    assert recorded == suite(dirs=[parent])
    assert run(repository, "check")[0] == 0


@pytest.mark.parametrize("entry", [
    {"k": "f", "p": "da/C:\\x"},
    {"k": "d", "p": "da/C:\\x"},
    {"k": "x", "exe": "opaque", "cwd": ".", "paths": ["da/C:\\x"]},
    {"k": "e", "p": "/da/C:\\x"},
    {"k": "e", "p": "da/../C:\\x"},
    {"k": "e", "p": "da/C:\\x\0"},
    {"k": "e", "p": "da\\bad/C:\\x"},
])
def test_backslash_exception_rejects_other_kinds_and_invalid_parents(repository, entry):
    log(repository, entry)
    code, output = run(repository, "check")
    assert code == 2
    assert "invalid audit" in output
