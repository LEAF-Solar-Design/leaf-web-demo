"""Validate a recorded tool manifest against ZIP metadata without extraction."""
import hashlib
import re
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
    for component in path.split("/"):
        if component in {"", "."}:
            continue
        device = component.split(".", 1)[0].rstrip(" ")
        if (component.endswith((" ", ".")) or any(ord(c) < 32 or ord(c) == 127 for c in component)
                or re.fullmatch(r"CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[1-9???]", device, re.I)):
            return None
    key = posixpath.normpath(path).casefold()
    return key if key not in {"", "."} and ":" not in key else None


def package_quality(path, manifest, *, max_member_bytes=8*1024*1024, max_total_bytes=32*1024*1024, max_members=1024):
    if not isinstance(manifest, dict) or not isinstance(manifest.get("members"), list):
        return "INCOMPLETE"
    if type(max_members) is not int or max_members < 0 or len(manifest["members"]) > max_members:
        return "FAIL"
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
            entries = archive.infolist()
            if len(entries) > max_members:
                return "FAIL"
            if sum(entry.file_size for entry in entries) > max_total_bytes:
                return "FAIL"
            for entry in entries:
                if entry.flag_bits & 1:
                    return "INCOMPLETE"
                key = member_key(entry.orig_filename)
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
    for key in seen:
        parts = key.split("/")
        if any("/".join(parts[:end]) in files for end in range(1, len(parts))):
            return "FAIL"
    return "PASS" if set(expected) == files else "FAIL"
