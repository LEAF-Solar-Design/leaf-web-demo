"""Offline historical change-impact cases built in disposable Git repositories."""

from contextlib import contextmanager, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import yaml

try:
    from . import check, gitio, index, manifest, plan, record, rules
    from .extractors import git_text
except ImportError:
    import check
    import gitio
    import index
    import manifest
    import plan
    import record
    import rules
    from extractors import git_text

FIXTURES = Path(__file__).parent / "fixtures"
GROUPS = (("omission", "fixtures", 6), ("repaired", "repaired", 3), ("negative", "negative", 2))


def _inside(root, name):
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or path == root.resolve() or ".git" in Path(name).parts:
        raise ValueError(f"fixture path escapes its tree: {name}")
    return path


@contextmanager
def _isolated(root):
    values = {
        "IMPACT_HOME": str(root / "home"),
        "CHANGE_IMPACT_GATE_FILE": str(root / "unused-gate"),
        "CHANGE_IMPACT_DISABLE": None,
        "CLAUDE_CODE_SESSION_ID": None,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    # Inherited Git overrides must never redirect fixture writes to a real checkout.
    values.update({key: None for key in os.environ if key.startswith("GIT_") and key not in values})
    previous = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _content(value, inputs, inventory):
    if isinstance(value, str):
        return value
    if not isinstance(value, dict) or set(value) != {"from_input"}:
        raise ValueError("file must be inline text or a from_input reference")
    name = value["from_input"]
    raw = _inside(inputs, name).read_bytes()
    if name not in inventory or hashlib.sha256(raw).hexdigest() != inventory[name]["sha256"]:
        raise ValueError(f"vendored input digest mismatch: {name}")
    return raw.decode("utf-8")


def _write_tree(repo, files, inputs, inventory):
    for name, value in sorted(files.items()):
        path = _inside(repo, name)
        if value is None:
            if path.exists():
                path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_content(value, inputs, inventory), encoding="utf-8", newline="\n")


def _build_repo(root, name, spec, inputs, inventory, aspects=None):
    repo = root / name
    repo.mkdir()
    git_text(repo, "init", "-q", "-b", "main")
    for key, value in (("user.name", "Impact Fixture"), ("user.email", "impact@example.invalid"),
                       ("commit.gpgsign", "false"), ("core.autocrlf", "false"),
                       ("core.hooksPath", str(root / "no-hooks"))):
        git_text(repo, "config", key, value)
    files = dict(spec["files"])
    if aspects is not None:
        files["ASPECTS.yaml"] = yaml.safe_dump(aspects, sort_keys=False)
    base = None
    if "base_files" in spec:
        base_files = {**files, **spec["base_files"]}
        _write_tree(repo, base_files, inputs, inventory)
        git_text(repo, "add", ".")
        git_text(repo, "commit", "-q", "--allow-empty", "-m", "base")
        base = gitio.rev_parse(repo, "HEAD")
        # Drop any base-only file before materializing the head tree.
        for path in set(base_files) - set(files):
            _inside(repo, path).unlink(missing_ok=True)
    _write_tree(repo, files, inputs, inventory)
    git_text(repo, "add", ".")
    git_text(repo, "commit", "-q", "--allow-empty", "-m", "head")
    head = gitio.rev_parse(repo, "HEAD")
    return repo, {"base": base or head, "head": head}


def _replace(value, replacements):
    if isinstance(value, dict):
        return {key: _replace(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace(item, replacements) for item in value]
    if isinstance(value, str):
        for token, replacement in replacements.items():
            value = value.replace(token, replacement)
    return value


def _assess(expect, rows, rendered, verdict):
    required = [row for row in rows if row["required"]]
    if "verdict" in expect and verdict != expect["verdict"]:
        raise AssertionError(f"verdict: expected {expect['verdict']}, got {verdict}")
    if len(required) < expect.get("required_min", 0):
        raise AssertionError(f"required rows: expected at least {expect['required_min']}, got {len(required)}")
    if "required" in expect and len(required) != expect["required"]:
        raise AssertionError(f"required rows: expected {expect['required']}, got {len(required)}")
    for wanted in expect.get("rows", []):
        found = next((row for row in rows if row["concern"] == wanted["concern"]
                      and row["subject"] == wanted["subject"]), None)
        if found is None:
            raise AssertionError(f"missing row {wanted['concern']}/{wanted['subject']}")
        for key in ("outcome", "required"):
            expected = wanted.get(key, True if key == "required" else None)
            if expected is not None and found[key] != expected:
                raise AssertionError(f"{wanted['subject']} {key}: expected {expected}, got {found[key]}")
        for key in ("evidence", "reason"):
            snippets = wanted.get(key + "_contains", [])
            for snippet in [snippets] if isinstance(snippets, str) else snippets:
                if snippet not in (found.get(key) or ""):
                    raise AssertionError(f"{wanted['subject']} {key} lacks {snippet!r}")
    for absent in expect.get("absent_rows", []):
        for row in required:
            if "concern" in absent and row["concern"] != absent["concern"]:
                continue
            if "subject" in absent and row["subject"] != absent["subject"]:
                continue
            if "subject_prefix" in absent and not row["subject"].startswith(absent["subject_prefix"]):
                continue
            if "subject_not_in" in absent and row["subject"] in absent["subject_not_in"]:
                continue
            raise AssertionError(f"unexpected required row {row['concern']}/{row['subject']}")
    for snippet in expect.get("render_contains", []):
        if snippet not in rendered:
            raise AssertionError(f"render lacks {snippet!r}")


def run_case(path, families_path=None, version="0.1.0"):
    """Return one case result; a custom rules file supports the trigger mutation test."""
    path = Path(path)
    result = {"id": path.stem, "group": "unknown", "ok": False, "detail": ""}
    try:
        case = yaml.safe_load(manifest.read_text(path))
        result.update(id=case["id"], group=case["group"])
        if re.fullmatch(r"[a-z0-9-]{1,64}", case["id"]) is None or case["id"] != path.stem:
            raise ValueError("case id must match its filename")
        if case["group"] not in {group for group, _, _ in GROUPS}:
            raise ValueError("unknown fixture group")
        if case["kind"] not in ("plan", "check", "transaction"):
            raise ValueError("unknown fixture kind")
        inputs = path.parent / "_inputs"
        inventory = {entry["path"]: entry for entry in json.loads((inputs / "MANIFEST.json").read_text(encoding="utf-8"))["entries"]}
        families = rules.load_families(families_path)
        with tempfile.TemporaryDirectory(prefix="impact-fixture-") as directory:
            root = Path(directory)
            with _isolated(root), patch.object(plan, "load_families", return_value=families), patch.object(check, "load_families", return_value=families):
                repos, replacements = {}, {}
                for number, (name, spec) in enumerate(case["repos"].items()):
                    if name == "self":
                        continue
                    external = manifest.validate_manifest({
                        "schema_version": 1, "project_id": f"repo-{number}", "repository": name,
                        "authored_at_revision": "0" * 40,
                        "components": [{"id": "source", "paths": ["**"], "facets": []}],
                    })
                    repo, commits = _build_repo(root, f"repo-{number}", spec, inputs, inventory, external)
                    repos[name] = {"path": str(repo), "manifest": str(repo / "ASPECTS.yaml"),
                                   "digest": manifest.manifest_digest(external), "project_id": external["project_id"]}
                    replacements["FIXTURE:" + name] = commits["head"]
                aspects = manifest.validate_manifest(_replace(case["manifest"], replacements))
                repo, commits = _build_repo(root, "self", case["repos"]["self"], inputs, inventory, aspects)
                repos[aspects["repository"]] = {
                    "path": str(repo), "manifest": str(repo / "ASPECTS.yaml"),
                    "digest": manifest.manifest_digest(aspects), "project_id": aspects["project_id"],
                }
                index_file = index.index_path()
                index_file.parent.mkdir(parents=True, exist_ok=True)
                index_file.write_text(json.dumps({"schema_version": 1, "repos": repos}), encoding="utf-8")
                destination, receipt = root / "record.yaml", root / "receipt.json"
                run = case["run"]
                if run.get("record"):
                    template = yaml.safe_load(_inside(repo, run["record"]).read_text(encoding="utf-8"))
                    digest = manifest.manifest_digest(aspects)
                    template = _replace(template, {"FIXTURE:manifest-digest": digest, "FIXTURE:manifest-prefix": digest[:12]})
                    record.save_record(destination, template)
                args = SimpleNamespace(workdir=str(repo), change_id=case["id"], record=str(destination),
                                       receipt=str(receipt), json=False, strict=False)
                output = io.StringIO()
                with redirect_stdout(output):
                    if case["kind"] == "plan":
                        args.owned, args.target = run["owned"], []
                        code = plan.run(args, version)
                    else:
                        args.base = commits[run.get("base", "base")]
                        args.head = "worktree" if run.get("head") == "worktree" else commits[run.get("head", "head")]
                        transaction = run.get("transaction", {})
                        args.transaction, args.target = transaction.get("kind"), transaction.get("target")
                        code = check.run(args, version)
                if code != 0 or not destination.exists():
                    raise AssertionError("assessment did not produce a record: " + output.getvalue().strip())
                rows = record.load_record(destination)["rows"]
                verdict = ("INCOMPLETE" if any(row["required"] and row["outcome"] == "unresolved" for row in rows)
                           else "COMPLETE") if case["kind"] == "plan" else json.loads(receipt.read_text())["verdict"]
                _assess(case["expect"], rows, output.getvalue(), verdict)
                if "check" in run:
                    extra = run["check"]
                    args.record = str(root / "checked-record.yaml")
                    args.base, args.head = commits[extra["base"]], commits[extra["head"]]
                    args.transaction = args.target = None
                    with redirect_stdout(io.StringIO()):
                        code = check.run(args, version)
                    if code or json.loads(receipt.read_text())["verdict"] != case["expect"]["check_verdict"]:
                        raise AssertionError("follow-up check verdict differs")
        result.update(ok=True, detail="expectations satisfied")
    except Exception as exc:
        result["detail"] = " ".join(str(exc).splitlines()) or type(exc).__name__
    return result


def run(args, version):
    paths = sorted(FIXTURES.glob("*.yaml"))
    if args.only:
        paths = [path for path in paths if path.stem == args.only]
        if not paths:
            if args.json:
                print("[]")
            else:
                print(f"selftest: unknown fixture {args.only}")
            return 2
    results = [run_case(path, version=version) for path in paths]
    ok = bool(results) and all(item["ok"] for item in results)
    if not args.only:
        ok = ok and all(sum(item["group"] == group for item in results) == total for group, _, total in GROUPS)
    if args.json:
        print(json.dumps(results, sort_keys=True))
    else:
        for item in results:
            print(f"fixture {item['id']}: PASS" if item["ok"] else f"fixture {item['id']}: FAIL {item['detail']}")
        for group, label, total in GROUPS:
            selected = [item for item in results if item["group"] == group]
            print(f"{label} {sum(item['ok'] for item in selected)}/{len(selected) if args.only else total}")
        print("selftest: OK" if ok else "selftest: FAIL")
    return 0 if ok else 1
