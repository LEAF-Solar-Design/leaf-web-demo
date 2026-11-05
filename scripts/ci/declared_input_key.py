"""Dormant declared-input fingerprints; this module makes no runner decisions.

The manifest is {"schema": SCHEMA, "suites": {suite_id: entry}}. compute_key
also accepts the suites mapping or one entry. Recipe is a recipe digest;
toolchain is a JSON identity. Other digests are explicit, nonempty strings.
Trees must remain quiescent while being fingerprinted.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import stat
import sys


SCHEMA = "leaf.ci.declared-inputs.v1"


class DeclaredInputError(ValueError):
    """A named failure, never a successful empty fingerprint or skip."""

    def __init__(self, code, path=None):
        self.code = code
        self.path = None if path is None else str(path)
        super().__init__(code if path is None else f"{code}: {path}")


def _canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DeclaredInputError("invalid_identity") from exc


def _root_name(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise DeclaredInputError("invalid_root", value)
    windows = PureWindowsPath(value)
    if (Path(value).is_absolute() or windows.drive or windows.root or
            "\\" in value or ".." in Path(value).parts):
        raise DeclaredInputError("invalid_root", value)
    return Path(value).as_posix()


def _validate_entry(entry):
    if (not isinstance(entry, dict) or
            not isinstance(entry.get("roots"), list) or not entry["roots"] or
            entry.get("status") not in ("unverified", "verified") or
            not isinstance(entry.get("evidence"), str) or not entry["evidence"].strip() or
            not isinstance(entry.get("notes"), str)):
        raise DeclaredInputError("invalid_declaration")
    return sorted({_root_name(root) for root in entry["roots"]})


def validate_declared(declared):
    """Validate the versioned manifest without reading or admitting any suite."""
    if (not isinstance(declared, dict) or declared.get("schema") != SCHEMA or
            not isinstance(declared.get("suites"), dict) or not declared["suites"]):
        raise DeclaredInputError("invalid_declaration")
    for suite_id, entry in declared["suites"].items():
        if not isinstance(suite_id, str) or not suite_id:
            raise DeclaredInputError("invalid_declaration")
        _validate_entry(entry)
    return declared


def _suite_entry(declared, suite_id):
    if not isinstance(suite_id, str) or not suite_id or not isinstance(declared, dict):
        raise DeclaredInputError("invalid_declaration")
    if "schema" in declared:
        declared = validate_declared(declared)["suites"]
    if "roots" in declared:
        entry = declared
    else:
        if suite_id not in declared:
            raise DeclaredInputError("unknown_suite", suite_id)
        entry = declared[suite_id]
    _validate_entry(entry)
    return entry


def _within(path, root):
    return path == root or root in path.parents


def _resolve_input(path, repo, roots, symlinks=None):
    """Resolve each link individually, rejecting even an outside intermediate hop."""
    pending = list(path.relative_to(repo).parts)
    current = repo
    links = 0
    while pending:
        part = pending.pop(0)
        if part == "..":
            current = current.parent
            if not _within(current, repo):
                raise DeclaredInputError("outside_symlink", path)
            continue
        current = current / part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            links += 1
            if links > 40:
                raise DeclaredInputError("symlink_cycle", path)
            text = os.readlink(current)
            raw_target = current.parent / text
            target = Path(os.path.abspath(raw_target))
            if not _within(target, repo) or not any(_within(target, root) for root in roots):
                raise DeclaredInputError("outside_symlink", current)
            if symlinks is not None:
                symlinks[current.relative_to(repo).as_posix()] = [stat.S_IMODE(info.st_mode), text]
            # Preserve '..' until after preceding links are resolved. Lexical
            # normalization alone can hide an outside intermediate hop.
            if not _within(raw_target, repo):
                raise DeclaredInputError("outside_symlink", current)
            pending = list(raw_target.relative_to(repo).parts) + pending
            current = repo
    if not any(_within(current, root) for root in roots):
        raise DeclaredInputError("outside_symlink", path)
    return current


def _snapshot_root(repo, name, roots):
    files, symlinks = [], {}
    root = repo / name

    def visit(path, is_root=False):
        try:
            _resolve_input(path, repo, roots, symlinks)
            info = path.lstat()
        except FileNotFoundError as exc:
            code = "missing_root" if is_root else "missing_input"
            raise DeclaredInputError(code, path) from exc
        except OSError as exc:
            raise DeclaredInputError("unreadable_input", path) from exc
        relative = path.relative_to(repo).as_posix()
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            # The resolver recorded the link, including links in root ancestors.
            return
        elif stat.S_ISREG(info.st_mode):
            if not mode & 0o444:
                raise DeclaredInputError("unreadable_file", path)
            try:
                flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0) |
                         getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
                with os.fdopen(os.open(path, flags), "rb") as stream:
                    opened = os.fstat(stream.fileno())
                    if (not stat.S_ISREG(opened.st_mode) or
                            (opened.st_dev, opened.st_ino, opened.st_mode) !=
                            (info.st_dev, info.st_ino, info.st_mode)):
                        raise DeclaredInputError("changed_input", path)
                    digest = hashlib.sha256()
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                files.append([relative, mode, digest.hexdigest()])
            except OSError as exc:
                raise DeclaredInputError("unreadable_file", path) from exc
        elif stat.S_ISDIR(info.st_mode):
            if not mode & 0o444 or not mode & 0o111:
                raise DeclaredInputError("unreadable_directory", path)
            try:
                children = sorted(path.iterdir(), key=lambda child: child.name)
            except OSError as exc:
                raise DeclaredInputError("unreadable_directory", path) from exc
            for child in children:
                visit(child)
        else:
            raise DeclaredInputError("unsupported_input", path)

    visit(root, is_root=True)
    return {"root": name, "files": sorted(files),
            "symlinks": [[path, *symlinks[path]] for path in sorted(symlinks)]}


def compute_key(repo, suite_id, recipe, toolchain, image_digest, runner_digest,
                declared, require_verified=False):
    """Hash explicit identities and all declared files/modes/link targets.

    Symlink targets must exist inside the declared root union. Links are bound
    by their literal targets; target bytes are inventoried at their own declared
    paths. No ignored files, generated-file exemptions, or missing-root skips.
    """
    entry = _suite_entry(declared, suite_id)
    if require_verified and entry["status"] != "verified":
        raise DeclaredInputError("unverified_suite", suite_id)
    for value in (recipe, image_digest, runner_digest):
        if not isinstance(value, str) or not value.strip():
            raise DeclaredInputError("invalid_identity")
    if toolchain is None or toolchain == "" or toolchain == {} or toolchain == []:
        raise DeclaredInputError("invalid_identity")
    try:
        repo = Path(repo).resolve(strict=True)
        if not repo.is_dir():
            raise DeclaredInputError("invalid_repo", repo)
    except (OSError, RuntimeError) as exc:
        raise DeclaredInputError("invalid_repo", repo) from exc
    names = _validate_entry(entry)
    roots = [repo / name for name in names]
    payload = {"schema": "leaf.ci.declared-input-key.v1", "suite": suite_id,
               "recipe": recipe, "toolchain": toolchain, "image": image_digest,
               "runner": runner_digest,
               "roots": [_snapshot_root(repo, name, roots) for name in names]}
    return hashlib.sha256(_canonical(payload)).hexdigest()


def check_reads(declared_roots, observed_paths):
    """Return {status, outside}; paths share a base (cwd for relative paths).

    Check both lexical and resolved membership in the declared union, so sibling
    prefixes, traversal and symlink escapes cannot turn an outside read complete.
    Missing roots/observations fail incomplete even with no outside reads.
    """
    roots, invalid_roots = [], []
    for raw in declared_roots:
        try:
            if ".." in Path(raw).parts:
                raise ValueError("parent traversal")
            lexical = Path(os.path.abspath(raw))
            resolved = lexical.resolve(strict=True)
            roots.append((lexical, resolved.is_dir()))
        except (OSError, RuntimeError, ValueError, TypeError):
            invalid_roots.append(str(raw))

    def covered(path):
        return any(path == root or is_dir and _within(path, root)
                   for root, is_dir in roots)

    for root, _ in roots:
        try:
            resolved = _resolve_input(root, Path(root.anchor), [path for path, _ in roots])
            if not covered(resolved):
                invalid_roots.append(str(root))
        except (DeclaredInputError, OSError, RuntimeError):
            invalid_roots.append(str(root))
    outside = []
    for raw in observed_paths:
        try:
            if ".." in Path(raw).parts:
                raise ValueError("parent traversal")
            lexical = Path(os.path.abspath(raw))
            resolved = _resolve_input(lexical, Path(lexical.anchor), [path for path, _ in roots])
            if covered(lexical) and covered(resolved):
                continue
        except (DeclaredInputError, OSError, RuntimeError, ValueError, TypeError):
            pass
        outside.append(str(raw))
    result = {"status": "incomplete" if outside or invalid_roots or not roots else "complete",
              "outside": sorted(set(outside))}
    if invalid_roots:
        result["invalid_roots"] = sorted(set(invalid_roots))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    key = commands.add_parser("key")
    key.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    key.add_argument("--declared", type=Path, default=Path(__file__).with_name("declared_inputs.json"))
    key.add_argument("--suite", required=True)
    key.add_argument("--recipe", required=True, help="suite recipe digest")
    key.add_argument("--toolchain", required=True, help="toolchain identity string")
    key.add_argument("--image-digest", required=True)
    key.add_argument("--runner-digest", required=True)
    key.add_argument("--require-verified", action="store_true")
    args = parser.parse_args(argv)
    try:
        declared = validate_declared(json.loads(args.declared.read_text(encoding="utf-8")))
        digest = compute_key(args.repo, args.suite, args.recipe, args.toolchain,
                             args.image_digest, args.runner_digest, declared,
                             require_verified=args.require_verified)
        roots = _validate_entry(_suite_entry(declared, args.suite))
    except (DeclaredInputError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps({"suite": args.suite, "key": digest, "roots": roots}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
