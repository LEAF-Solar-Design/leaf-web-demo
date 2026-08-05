#!/usr/bin/env python3
"""Decide whether a main push touched only documentation.

Reads a newline-delimited changed-file list on stdin and prints exactly one
word: "skip" (every path is documentation; the build train may no-op) or
"build". The caller must treat ANY other outcome — nonzero exit, empty
output, garbage output — as "build". This program never decides to skip on
uncertainty: an empty or unreadable file list means the diff could not be
trusted, so it builds.

The ignore set is deliberately narrow and content-audited against the
release Dockerfiles: only docs/** and ROOT-level *.md are outside every
image build context (deploy/Dockerfile.* COPY server/, da/, engine/,
platform/, contract/, data/, harness/, web/ and named files — none copy
docs/ or root markdown, and .dockerignore does not exclude markdown).
Nested markdown IS an image input: server/README.md lands in the app and
broker images byte-for-byte, so it builds. Never grow this set into a
per-service dependency closure; the contract is deploy-nothing-or-
everything.

The caller must compute the file list with rename detection DISABLED
(git diff --no-renames): a detected rename reports only its destination,
so a file moved out of a build-input tree (server/app.py -> docs/app.py)
would classify here as docs-only. With --no-renames both sides arrive as
delete+add, and either non-docs side builds.
"""

import sys


def is_ignored(path: str) -> bool:
    if path.startswith("docs/"):
        return True
    if "/" in path or "\\" in path:
        return False
    return path.endswith(".md")


def decide(paths) -> str:
    cleaned = [p.strip() for p in paths if p and p.strip()]
    if not cleaned:
        return "build"
    # git quotes paths with unusual bytes ("docs/\303\251.md"); a quoted
    # path starts with '"', never matches docs/ or *.md, and so builds.
    return "skip" if all(is_ignored(p) for p in cleaned) else "build"


def main() -> int:
    print(decide(sys.stdin.read().splitlines()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
