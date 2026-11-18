"""Validate a recorded tool manifest against ZIP metadata without extraction."""
import ntpath
import posixpath
import stat
import unicodedata
from zipfile import BadZipFile, ZipFile


def member_key(name):
    if not isinstance(name, str):
        return None
    path = unicodedata.normalize("NFC", name.replace("\\", "/"))
    if path.startswith("/") or ntpath.splitdrive(path)[0] or ".." in path.split("/"):
        return None
    key = posixpath.normpath(path).casefold()
    return key if key not in {"", "."} and ":" not in key else None


def package_quality(path, manifest):
    if not isinstance(manifest, dict) or not isinstance(manifest.get("members"), list):
        return "INCOMPLETE"
    expected = [member_key(name) for name in manifest["members"]]
    if None in expected or len(set(expected)) != len(expected):
        return "FAIL"
    seen, files = set(), set()
    try:
        with ZipFile(path) as archive:
            for entry in archive.infolist():
                key = member_key(entry.filename)
                if key is None or key in seen or stat.S_ISLNK(entry.external_attr >> 16):
                    return "FAIL"
                seen.add(key)
                if not entry.is_dir():
                    files.add(key)
    except (OSError, BadZipFile):
        return "INCOMPLETE"
    return "PASS" if set(expected) == files else "FAIL"
