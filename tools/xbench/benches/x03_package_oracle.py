"""Validate a recorded tool manifest against ZIP metadata without extraction."""
import hashlib
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


def package_quality(path, manifest, *, max_member_bytes=8*1024*1024, max_total_bytes=32*1024*1024):
    if not isinstance(manifest, dict) or not isinstance(manifest.get("members"), list):
        return "INCOMPLETE"
    expected = [member_key(name) for name in manifest["members"]]
    if None in expected or len(set(expected)) != len(expected):
        return "FAIL"
    hashes = manifest.get("sha256")
    if not isinstance(hashes, dict) or set(hashes) != set(manifest["members"]):
        return "INCOMPLETE"
    digests = {member_key(name): digest for name, digest in hashes.items()}
    total = 0
    seen, files = set(), set()
    try:
        with ZipFile(path) as archive:
            for entry in archive.infolist():
                if entry.flag_bits & 1:
                    return "INCOMPLETE"
                key = member_key(entry.filename)
                if key is None or key in seen or stat.S_ISLNK(entry.external_attr >> 16):
                    return "FAIL"
                seen.add(key)
                if not entry.is_dir():
                    total += entry.file_size
                    if key not in digests or entry.file_size > max_member_bytes or total > max_total_bytes:
                        return "FAIL"
                    with archive.open(entry) as stream:
                        digest = hashlib.sha256()
                        remaining = entry.file_size
                        while remaining:
                            chunk = stream.read(min(65536, remaining))
                            if not chunk:
                                return "FAIL"
                            digest.update(chunk)
                            remaining -= len(chunk)
                        if stream.read(1) or digest.hexdigest() != digests[key]:
                            return "FAIL"
                    files.add(key)
    except BadZipFile:
        return "FAIL"
    except (OSError, NotImplementedError, RuntimeError):
        return "INCOMPLETE"
    return "PASS" if set(expected) == files else "FAIL"
