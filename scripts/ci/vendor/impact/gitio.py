"""Bounded, shell-free git reads for the change-impact package."""

from pathlib import Path
import re
import subprocess

MAX_PATHS = 200_000


class GitError(RuntimeError):
    """A git failure, retaining argv and at most 300 stderr characters."""

    def __init__(self, argv, stderr):
        self.argv = argv
        self.stderr = str(stderr)[:300]
        super().__init__(f"git {argv[3:]}: {self.stderr}")


def _run(repo, *args):
    # All subprocesses use list argv and a 60-second timeout, never a shell.
    argv = ["git", "-C", str(repo), *args]
    try:
        result = subprocess.run(argv, timeout=60, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(argv, str(exc)) from exc
    if result.returncode:
        raise GitError(argv, result.stderr)
    return result.stdout


def rev_parse(repo, ref):
    value = _run(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").strip()
    if re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise GitError(["git", "-C", str(repo), "rev-parse", str(ref)], "expected 40 lowercase hex revision")
    return value


def ls_tree(repo, rev):
    # Tree listings are bounded to 200k paths and NUL-delimited for literal names.
    sha = rev_parse(repo, rev)
    output = _run(repo, "ls-tree", "-r", "--name-only", "-z", sha)
    if output.count("\0") > MAX_PATHS:
        raise GitError(["git", "-C", str(repo), "ls-tree", sha], "tree listing exceeds 200,000 paths limit")
    return sorted(output.rstrip("\0").split("\0")) if output else []


def toplevel(path):
    try:
        return str(Path(_run(path, "rev-parse", "--show-toplevel").strip()).resolve())
    except (GitError, OSError):
        return None


def default_head(repo):
    for ref in ("origin/HEAD", "origin/main", "origin/master", "HEAD"):
        try:
            return rev_parse(repo, ref)
        except GitError:
            pass
    return None


def remote_url(repo):
    try:
        return _run(repo, "remote", "get-url", "origin").strip() or None
    except GitError:
        return None
