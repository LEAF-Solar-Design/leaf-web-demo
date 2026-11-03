"""Build a reproducible release archive and checksum-bound POSIX installer."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
from pathlib import Path
import re
import tarfile


def build(source: Path, output: Path) -> str:
    source, output = Path(source), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    paths = sorted([source / "requirements.lock", *(
        path for path in (source / "leaf_agent").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}
    )], key=lambda path: path.relative_to(source).as_posix())
    raw = io.BytesIO()
    with gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0, compresslevel=9) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for path in paths:
                if path.is_symlink():
                    raise ValueError("Release source must not contain symlinks")
                body = path.read_bytes()
                entry = tarfile.TarInfo(path.relative_to(source).as_posix())
                entry.size = len(body)
                entry.mode = 0o644
                entry.mtime = entry.uid = entry.gid = 0
                entry.uname = entry.gname = ""
                archive.addfile(entry, io.BytesIO(body))
    data = raw.getvalue()
    digest = hashlib.sha256(data).hexdigest()
    (output / "package.tar.gz").write_bytes(data)
    version = re.search(r'^__version__ = "([^"]+)"', (source / "leaf_agent/__init__.py").read_text(encoding="utf-8"), re.M).group(1)
    template = (source / "install.sh.tmpl").read_text(encoding="utf-8")
    rendered = template.replace("@SHA256@", digest).replace("@VERSION@", version)
    (output / "install.sh").write_bytes(rendered.encode("utf-8"))
    return digest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(build(Path(__file__).resolve().parent, args.output))


if __name__ == "__main__":
    main()
