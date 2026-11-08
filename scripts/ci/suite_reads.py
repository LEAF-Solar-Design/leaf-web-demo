"""Declared suite read sets: check a run against them, select suites for a diff, record new reads."""

import argparse
import fnmatch
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


DECLARED = ".codebuild/suite-reads.json"
_SUITE_NAME = re.compile(r"[a-z][a-z0-9-]*\Z")


class Unusable(Exception):
    """Input cannot establish a complete, trustworthy read set."""


def _path(value):
    return (isinstance(value, str) and bool(value) and not value.startswith("/")
            and "\\" not in value and "\0" not in value and ".." not in value.split("/"))


def _strings(values):
    return (isinstance(values, list) and all(_path(value) for value in values)
            and values == sorted(set(values)))


def _backslash_parent(value):
    if (isinstance(value, str) and "\\" in value and not value.startswith("/")
            and "\0" not in value and ".." not in value.split("/")):
        parent = value.rsplit("/", 1)[0] if "/" in value else "."
        if _path(parent):
            return parent
    return None


def _keys(value, keys):
    return isinstance(value, dict) and set(value) == set(keys)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _load(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs)
        valid = (_keys(data, ("version", "run_all", "ignore", "suites"))
                 and type(data["version"]) is int and data["version"] == 1
                 and _strings(data["run_all"]) and _strings(data["ignore"])
                 and isinstance(data["suites"], dict))
        if not valid:
            raise ValueError("top-level schema")
        for name, suite in data["suites"].items():
            if not _SUITE_NAME.fullmatch(name) or not _keys(suite, ("globs", "files", "dirs", "exists", "children")):
                raise ValueError("suite schema")
            if not all(_strings(suite[key]) for key in ("globs", "files", "dirs", "exists")):
                raise ValueError("suite paths")
            if not isinstance(suite["children"], list):
                raise ValueError("children schema")
            for child in suite["children"]:
                if (not isinstance(child, dict)
                        or not _keys({key: value for key, value in child.items()
                                      if key not in {"sub", "opts", "argv"}}, ("exe", "cwd", "reads"))):
                    raise ValueError("child schema")
                if not isinstance(child["exe"], str) or not child["exe"] or "/" in child["exe"]:
                    raise ValueError("child executable")
                if child["cwd"] is not None and not _path(child["cwd"]):
                    raise ValueError("child cwd")
                if child.get("sub") is not None and not isinstance(child["sub"], str):
                    raise ValueError("child subcommand")
                if not _strings(child["reads"]):
                    raise ValueError("child reads")
                if "argv" in child and not (isinstance(child["argv"], list)
                                            and all(isinstance(arg, str) for arg in child["argv"])):
                    raise ValueError("child argv")
                if "opts" in child and not (
                        isinstance(child["opts"], list)
                        and all(isinstance(option, str) and option.startswith("-")
                                and "\0" not in option for option in child["opts"])
                        and child["opts"] == sorted(set(child["opts"]))):
                    raise ValueError("child options")
        return data
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise Unusable("invalid declared file") from exc


def _matches(path, glob):
    if glob == "**":
        return True
    if glob.endswith("/**"):
        directory = glob[:-3]
        return path == directory or path.startswith(directory + "/")
    return fnmatch.fnmatchcase(path, glob)


def _any(path, globs):
    return any(_matches(path, glob) for glob in globs)


def _covered(path, kind, suite):
    return (path in suite[kind] or _any(path, suite["globs"])
            or any(_any(path, child["reads"]) for child in suite["children"]))


def _child_covered(child, suite):
    paths = child["paths"]
    for declared in suite["children"]:
        if child["exe"] != declared["exe"] or child["cwd"] != declared["cwd"]:
            continue
        if child["exe"] == "git":
            covered = "**" in declared["reads"]
        elif paths:
            covered = all(_any(path, declared["reads"]) for path in paths)
        else:
            covered = ("argv" in child and "argv" in declared
                       and child["argv"] == declared["argv"])
        if covered:
            return True
    return False


def _git(root, *args):
    try:
        result = subprocess.run(["git", "-C", str(root), *args],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        return result.stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise Unusable("git command failed") from exc


def _ancestors(path):
    parts = path.split("/")
    return [".", *("/".join(parts[:index]) for index in range(1, len(parts)))]


def _logs(directory):
    try:
        errors = sorted(directory.glob("*.error"))
        if errors:
            with errors[0].open(encoding="utf-8", errors="replace") as stream:
                first = stream.readline(301).splitlines()
            detail = first[0][:300] if first else ""
            raise Unusable(f"audit error file ({detail})")
        logs = sorted(directory.glob("*.jsonl"))
        if not logs:
            raise Unusable("no audit logs")
        entries = []
        for log in logs:
            lines = log.read_text(encoding="utf-8").splitlines()
            if not lines:
                raise Unusable("empty audit log")
            for line in lines:
                entry = json.loads(line, object_pairs_hook=_pairs)
                if not isinstance(entry, dict):
                    raise Unusable(f"invalid audit entry ({repr(entry)[:200]})")
                kind = entry.get("k")
                if kind == "o":
                    raise Unusable("audit overflow")
                if kind in ("f", "d", "e"):
                    if not _keys(entry, ("k", "p")):
                        raise Unusable(f"invalid audit path ({repr(entry)[:200]})")
                    if not _path(entry["p"]) and not (
                            kind == "e" and _backslash_parent(entry["p"]) is not None):
                        raise Unusable(f"invalid audit path ({repr(entry['p'])[:200]})")
                elif kind == "x":
                    if (not _keys({key: value for key, value in entry.items()
                                   if key not in {"sub", "opts", "argv"}}, ("k", "exe", "cwd", "paths"))
                            or not isinstance(entry["exe"], str) or not entry["exe"]
                            or "/" in entry["exe"]
                            or (entry["cwd"] is not None and not _path(entry["cwd"]))
                            or (entry.get("sub") is not None and not isinstance(entry["sub"], str))
                            or ("argv" in entry and not (
                                isinstance(entry["argv"], list)
                                and all(isinstance(arg, str) for arg in entry["argv"])))
                            or ("opts" in entry and not (
                                isinstance(entry["opts"], list)
                                and all(isinstance(option, str) and option.startswith("-")
                                        and "\0" not in option for option in entry["opts"])
                                and entry["opts"] == sorted(set(entry["opts"]))))
                            or not _strings(entry["paths"])):
                        raise Unusable(f"invalid audit child ({repr(entry)[:200]})")
                elif (_keys(entry, ("v", "pid", "argv")) and type(entry["v"]) is int
                      and entry["v"] == 1 and type(entry["pid"]) is int
                      and isinstance(entry["argv"], list)
                      and all(isinstance(arg, str) for arg in entry["argv"])):
                    continue
                else:
                    raise Unusable(f"invalid audit entry ({repr(entry)[:200]})")
                entries.append(entry)
        return entries
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise Unusable("invalid audit log") from exc


def _tracked(root):
    tracked = {os.fsdecode(path) for path in _git(root, "ls-files", "-z").split(b"\0") if path}
    tracked_dirs = {"."}
    for path in tracked:
        tracked_dirs.update(_ancestors(path))
    return tracked, tracked_dirs


def _observed(root, directory, ignore, tracked_sets=None):
    entries = _logs(directory)
    tracked, tracked_dirs = _tracked(root) if tracked_sets is None else tracked_sets
    files, dirs, exists, children = set(), set(), set(), {}
    for entry in entries:
        kind = entry["k"]
        if kind == "x":
            paths = entry["paths"]
            entry = {**entry, "paths": [path for path in paths if not _any(path, ignore)]}
            key = {key: value for key, value in entry.items()
                   if key != "argv" or (entry["exe"] != "git" and not entry["paths"])}
            children.setdefault(json.dumps(key, sort_keys=True), entry)
            continue
        path = entry["p"]
        if kind == "e" and not _path(path):
            path, kind = _backslash_parent(path), "d"
        if _any(path, ignore):
            continue
        if kind == "f" and (path in tracked or path in tracked_dirs or not os.path.exists(root / path)):
            files.add(path)
        elif kind == "d" and (path in tracked or path in tracked_dirs or not os.path.exists(root / path)):
            dirs.add(path)
        elif kind == "e" and (path in tracked or path in tracked_dirs
                              or not os.path.exists(root / path)):
            exists.add(path)
    return sorted(files), sorted(dirs), sorted(exists), [children[key] for key in sorted(children)]


def _undeclared(observed, suite):
    files, dirs, exists, children = observed
    # e carries existence/type only: M cannot change it; A/D/T under a listed dir select.
    return ([path for path in files if not _covered(path, "files", suite)],
            [path for path in dirs if path not in suite["files"] and not _covered(path, "dirs", suite)],
            [path for path in exists if path not in suite["files"]
             and (path.rsplit("/", 1)[0] if "/" in path else ".") not in suite["dirs"]
             and not _covered(path, "exists", suite)],
            [child for child in children if not _child_covered(child, suite)])


def _details(missing):
    files, dirs, exists, children = missing
    return ([f"  undeclared file: {path}" for path in files]
            + [f"  undeclared dir: {path}" for path in dirs]
            + [f"  undeclared exists: {path}" for path in exists]
            + [f"  undeclared child: {child['exe']} cwd={child['cwd']} paths={','.join(child['paths'])}"
               + (f" sub={child['sub']}" if "sub" in child else "")
               + (f" opts={child['opts']}" if "opts" in child else "")
               + (f" argv={' '.join(child['argv'])}" if "argv" in child else "")
               for child in children])


def _check(args, data, root, tracked_sets=None, emit=print):
    if args.suite not in data["suites"]:
        raise Unusable("unknown suite")
    observed = _observed(root, Path(args.audit_dir), data["ignore"], tracked_sets)
    missing = _undeclared(observed, data["suites"][args.suite])
    count = sum(map(len, missing))
    if not count:
        emit(f"suite read check: ok suite={args.suite} files={len(observed[0])} "
              f"dirs={len(observed[1])} children={len(observed[3])}")
        return 0
    emit(f"suite read check: FAIL suite={args.suite} undeclared={count}")
    for line in _details(missing)[:200]:
        emit(line)
    emit(f"  fix: python3 scripts/ci/suite_reads.py record --suite {args.suite} "
          f"--audit-dir {args.audit_dir}, then declare any child by hand")
    return 1


def _check_all(args, data, root):
    # One process for many suites: ops-dashboard runs 1,903 per-file suites, and a process per suite costs about 95 s of start-up.
    try:
        directories = sorted((path for path in Path(args.audit_root).iterdir() if path.is_dir()),
                             key=lambda path: path.name)
    except OSError as exc:
        raise Unusable("invalid audit root") from exc
    if not directories:
        raise Unusable("no suites")
    tracked_sets = _tracked(root)
    lines = 0

    def emit(line):
        nonlocal lines
        if lines < 2000:
            print(line)
        elif lines == 2000:
            print("  ... output truncated")
        lines += 1

    result = 0
    for directory in directories:
        suite_args = argparse.Namespace(suite=directory.name, audit_dir=str(directory))
        try:
            code = _check(suite_args, data, root, tracked_sets, emit)
        except Unusable as exc:
            emit(f"suite read check: UNUSABLE suite={directory.name} reason={exc}")
            code = 2
        result = max(result, code)
    return result


def _record(args, data, root, declared):
    if not _SUITE_NAME.fullmatch(args.suite):
        raise Unusable("invalid suite name")
    observed = _observed(root, Path(args.audit_dir), data["ignore"])
    suite = data["suites"].setdefault(args.suite, {"globs": [], "files": [], "dirs": [], "exists": [], "children": []})
    files, dirs, _, children = _undeclared(observed, suite)
    suite["dirs"] = sorted(set(suite["dirs"]) | set(dirs))
    _, _, exists, _ = _undeclared(observed, suite)
    suite["files"] = sorted(set(suite["files"]) | set(files))
    suite["exists"] = sorted(set(suite["exists"]) | set(exists))
    changed = []
    notices = []
    if args.children:
        # --children writes only what check would accept, never "**" for git: a git child is a carve-out decision for a person.
        for child in children:
            detail = _details(([], [], [], [child]))[0]
            if child["exe"] == "git":
                notices.append('  git child needs "**" in reads or an always-run carve-out: ' + detail)
                continue
            if child["paths"]:
                declaration = next((entry for entry in suite["children"]
                                    if entry["exe"] == child["exe"] and entry["cwd"] == child["cwd"]
                                    and "argv" not in entry), None)
                if declaration is None:
                    declaration = {"exe": child["exe"], "cwd": child["cwd"], "reads": []}
                    suite["children"].append(declaration)
                declaration["reads"] = sorted(set(declaration["reads"]) | set(child["paths"]))
            elif "argv" in child:
                declaration = {"exe": child["exe"], "cwd": child["cwd"], "reads": [], "argv": child["argv"]}
                if declaration in suite["children"]:
                    continue
                suite["children"].append(declaration)
            else:
                notices.append(detail + " (no argv; declare by hand)")
                continue
            if not any(entry is declaration for entry in changed):
                changed.append(declaration)
        suite["children"].sort(key=lambda child: (child["exe"], child["cwd"] or "",
                                                 json.dumps(child.get("argv") or [])))
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=declared.parent,
                                         prefix=declared.name + ".", delete=False) as stream:
            temporary = stream.name
            json.dump(data, stream, indent=1)
            stream.write("\n")
        os.replace(temporary, declared)
        temporary = None
    except OSError as exc:
        raise Unusable("cannot write declared file") from exc
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    if args.children:
        for child in suite["children"]:
            if any(entry is child for entry in changed):
                print(f"  declared child: exe={child['exe']} cwd={child['cwd']} "
                      f"reads={child['reads']} argv={child.get('argv', [])}")
        print("  review: an opaque child must read nothing beyond its argv, cwd and env hints")
        for line in notices:
            print(line)
        return int(any(not _child_covered(child, suite) for child in children))
    for line in _details(([], [], [], children))[:200]:
        print(line)
    return 1 if children else 0


def _diff(args, root):
    options = ["--cached"] if args.cached else [args.base, args.head]
    raw = _git(root, "diff", "-z", "--raw", "--find-renames", *options)
    fields = raw.split(b"\0")
    if fields[-1] != b"":
        raise Unusable("invalid git diff")
    fields.pop()
    changes = []
    index = 0
    while index < len(fields):
        header = os.fsdecode(fields[index]).split()
        index += 1
        if (len(header) != 5 or not re.fullmatch(r":[0-7]{6}", header[0])
                or not re.fullmatch(r"[0-7]{6}", header[1])
                or not all(re.fullmatch(r"[0-9a-f]+", value) for value in header[2:4])):
            raise Unusable("invalid git diff")
        old_mode, new_mode = header[0][1:], header[1]
        status = header[4]
        if not re.fullmatch(r"[ACDMRT][0-9]*", status):
            raise Unusable("unsupported git status " + status)
        kind = status[0]
        if kind == "M" and "120000" in (old_mode, new_mode):
            kind = "T"
        count = 2 if kind in {"R", "C"} else 1
        if index + count > len(fields):
            raise Unusable("invalid git diff")
        paths = [os.fsdecode(path) for path in fields[index:index + count]]
        index += count
        if not all(_path(path) for path in paths):
            raise Unusable("invalid git path")
        if count == 2:
            changes.extend((("D", paths[0]), ("A", paths[1])))
        else:
            changes.append((kind, paths[0]))
    return changes


def _select(args, data, root):
    try:
        changes = _diff(args, root)
        for _, path in changes:
            if _any(path, data["run_all"]):
                return {name: 1 for name in data["suites"]}, "run_all " + path
        selected = {}
        for name, suite in data["suites"].items():
            ancestors = {parent for key in ("files", "dirs", "exists")
                         for path in suite[key] for parent in _ancestors(path)}
            selected[name] = int(any(
                _covered(path, "files", suite)
                or any(_any(candidate, child["reads"])
                       for child in suite["children"] for candidate in [path, *_ancestors(path)])
                or (kind in {"A", "D", "T"} and (
                    path in suite["exists"] or path in suite["dirs"] or path in ancestors
                    or any(_any(candidate, suite["globs"])
                           for candidate in [path, *_ancestors(path)])
                    or any(parent in suite["dirs"] or parent in suite["files"]
                           or parent in suite["exists"] for parent in _ancestors(path))))
                for kind, path in changes))
        return selected, "selected" if changes else "no changes"
    except Unusable as exc:
        return {name: 1 for name in data["suites"]}, str(exc)


def _suite_names(path):
    """Recover output names even when another part of the manifest is invalid."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        suites = data.get("suites", {})
        if isinstance(suites, dict):
            return list(suites)
    except (OSError, UnicodeError, ValueError, AttributeError):
        pass
    return []


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "select", "record"):
        command = commands.add_parser(name)
        command.add_argument("--root")
        command.add_argument("--declared", default=DECLARED)
        if name == "select":
            group = command.add_mutually_exclusive_group(required=True)
            group.add_argument("--cached", action="store_true")
            group.add_argument("--base")
            command.add_argument("--head")
        elif name == "check":
            group = command.add_mutually_exclusive_group(required=True)
            group.add_argument("--suite")
            group.add_argument("--all", action="store_true")
            command.add_argument("--audit-dir")
            command.add_argument("--audit-root")
        else:
            command.add_argument("--suite", required=True)
            command.add_argument("--audit-dir", required=True)
            command.add_argument("--children", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "check":
        if args.all:
            if args.audit_dir is not None or args.audit_root is None:
                parser.error("--all requires --audit-root and excludes --audit-dir")
        elif args.audit_root is not None or args.audit_dir is None:
            parser.error("--suite requires --audit-dir and excludes --audit-root")
    if args.command == "select" and (bool(args.base) != bool(args.head)):
        parser.error("--base and --head must be provided together")
    root = Path(args.root).absolute() if args.root else None
    declared = None
    try:
        if root is None:
            root = Path(os.fsdecode(_git(Path.cwd(), "rev-parse", "--show-toplevel")).rstrip("\r\n"))
        declared = root / args.declared
        data = _load(declared)
        if args.command == "select":
            selected, reason = _select(args, data, root)
        elif args.command == "check":
            if args.all:
                return _check_all(args, data, root)
            return _check(args, data, root)
        else:
            return _record(args, data, root, declared)
    except Unusable as exc:
        if args.command != "select":
            name = "all" if args.command == "check" and args.all else args.suite
            print(f"suite read check: UNUSABLE suite={name} reason={exc}")
            return 2
        selected = {name: 1 for name in _suite_names(declared or Path(args.declared))}
        reason = str(exc)
    for name, value in selected.items():
        print(f"{name}={value}")
    print(f"scope reason: {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
