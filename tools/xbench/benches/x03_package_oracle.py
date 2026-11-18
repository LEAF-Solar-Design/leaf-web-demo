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
    # Canonical caseless matching decomposes before folding combining marks.
    decomposed = unicodedata.normalize("NFD", posixpath.normpath(path))
    key = unicodedata.normalize("NFC", decomposed.casefold())
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
            keys = [member_key(entry.orig_filename) for entry in entries]
            if None in keys or len(set(keys)) != len(keys):
                return "FAIL"
            inventory = {key for entry, key in zip(entries, keys) if not entry.is_dir()}
            if inventory != set(expected):
                return "FAIL"
            if not inventory:
                return "INCOMPLETE"
            for entry, key in zip(entries, keys):
                if entry.flag_bits & 1:
                    return "INCOMPLETE"
                if stat.S_ISLNK(entry.external_attr >> 16):
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


def runtime_abi_quality(requirements, target_profiles):
    """A single recorded target profile must support the whole pinned runtime tuple."""
    fields = ("runtime", "version", "architecture", "abi")
    if not isinstance(requirements, dict) or not isinstance(target_profiles, list) or not target_profiles:
        return "INCOMPLETE"
    if not all(isinstance(requirements.get(key), str) and requirements[key].strip() for key in fields):
        return "INCOMPLETE"
    incomplete = False
    for profile in target_profiles:
        if not isinstance(profile, dict) or not all(isinstance(profile.get(k), str) and profile[k].strip() for k in fields):
            incomplete = True
            continue
        if all(profile[key] == requirements[key] for key in fields):
            return "PASS"
    return "INCOMPLETE" if incomplete else "FAIL"


def dependency_closure_quality(required, lock):
    """Resolve exact version pins. This package contract requires an acyclic dependency graph."""
    from graphlib import CycleError, TopologicalSorter
    if not isinstance(required, dict) or not isinstance(lock, dict):
        return "INCOMPLETE"
    pending = list(required.items())
    graph = {}
    while pending:
        name, version = pending.pop()
        if not all(isinstance(value, str) and value.strip() for value in (name, version)):
            return "INCOMPLETE"
        record = lock.get(name)
        if not isinstance(record, dict) or not record.get("version") or not isinstance(record.get("requires"), dict):
            return "INCOMPLETE"
        if record["version"] != version:
            return "FAIL"
        if name not in graph:
            graph[name] = tuple(record["requires"])
            pending.extend(record["requires"].items())
    try:
        TopologicalSorter(graph).prepare()
    except CycleError:
        return "FAIL"
    return "PASS"


def entrypoint_quality(declared, modules):
    """Resolve module:symbol against a complete static module and export inventory."""
    if not isinstance(declared, str) or not declared or not isinstance(modules, list):
        return "INCOMPLETE"
    module, separator, symbol = declared.partition(":")
    if not separator or not all(p.isidentifier() for p in module.split(".")) or not symbol.isidentifier():
        return "FAIL"
    matches = []
    for row in modules:
        if not isinstance(row, dict) or not isinstance(row.get("module"), str):
            return "INCOMPLETE"
        if row["module"] == module:
            matches.append(row)
    if len(matches) != 1:
        return "FAIL"
    exports = matches[0].get("callable_exports")
    if not isinstance(exports, list) or not all(isinstance(name, str) for name in exports):
        return "INCOMPLETE"
    return "PASS" if exports.count(symbol) == 1 else "FAIL"
