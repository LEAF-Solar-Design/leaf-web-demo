#!/usr/bin/env python3
"""Run the workflow gate against an LF export of this worktree.

This Windows checkout stores workflows CRLF while the suite asserts no CR
byte, and the worktree's line endings have been observed flipping back under
git. So never test the worktree directly: export HEAD with autocrlf off,
overlay the working-tree edits normalised to LF, and run there. What the
suite sees is then exactly what git will store.

Scratch helper, not part of the change: delete before committing.
"""
import pathlib
import shutil
import subprocess
import sys
import tarfile
import tempfile

SRC = pathlib.Path(__file__).resolve().parent


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="lf-export-") as tmp:
        export = pathlib.Path(tmp)
        tar_path = export / "head.tar"
        with tar_path.open("wb") as fh:
            subprocess.run(
                ["git", "-c", "core.autocrlf=false", "archive", "HEAD"],
                cwd=SRC, stdout=fh, check=True)
        with tarfile.open(tar_path) as tf:
            tf.extractall(export)
        tar_path.unlink()

        changed = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=SRC, capture_output=True, text=True, check=True).stdout.split()
        for rel in changed:
            src_file = SRC / rel
            if not src_file.exists():
                continue
            dst = export / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src_file.read_bytes().replace(b"\r\n", b"\n"))
            print(f"overlaid {rel}")

        stray = [
            str(p.relative_to(export))
            for p in export.rglob("*")
            if p.is_file() and p.suffix in {".yml", ".py"} and b"\r" in p.read_bytes()
        ]
        if stray:
            print(f"LF export still has CR bytes in: {stray}", file=sys.stderr)
            return 2

        return subprocess.run(
            [sys.executable, "-m", "pytest", *sys.argv[1:]],
            cwd=export / "scripts").returncode


if __name__ == "__main__":
    raise SystemExit(main())
