"""Validate and stage a local package for the internal dev catalog."""

import argparse
import importlib.util
from pathlib import Path
import shutil
import sys
import zipfile


def _checker():
    path = Path(__file__).resolve().parents[2] / "server/branch_tools/package_check.py"
    spec = importlib.util.spec_from_file_location("_dev_package_check", path)
    module = importlib.util.module_from_spec(spec)
    # Source execution avoids cache writes and never adds the repository to sys.path.
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    return module.check_branch_package


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    destination = None
    created = False
    try:
        root = args.package
        if root.is_symlink() or not root.is_dir():
            raise ValueError("package must be a directory, not a symlink")
        files = {}
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ValueError("package must not contain symlinks")
            if path.is_file():
                files[path.relative_to(root).as_posix()] = path.read_bytes()
        result = _checker()(files, root.name)
        if not result.ok:
            for failure in result.failures:
                print(f"{failure['rule']}: {failure['path']}: {failure['message']}", file=sys.stderr)
            return 1
        name = result.manifest["name"]
        version = result.manifest["version"]
        destination = args.out / name / version
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.mkdir()  # Atomic refusal of existing versions, including empty dirs.
        created = True
        with zipfile.ZipFile(destination / "package.zip", "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for path, data in sorted(files.items()):
                info = zipfile.ZipInfo(f"{name}/{path}", date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, data)
        (destination / "digest.txt").write_text(result.package_digest + "\n", encoding="ascii")
        return 0
    except (OSError, ValueError) as exc:
        if created:
            shutil.rmtree(destination)
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
