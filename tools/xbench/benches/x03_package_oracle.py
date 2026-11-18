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


def compression_ratio_quality(path, manifest, max_ratio=None):
    """Apply the declared per-member expansion ceiling before reading any payload."""
    from fractions import Fraction
    from benches.x03_quality import finite_measure
    if not finite_measure(max_ratio) or max_ratio <= 0:
        return "INCOMPLETE"
    return package_quality(path, manifest, _ratio_limit=Fraction(str(max_ratio)))


def package_quality(path, manifest, *, _ratio_limit=None, max_member_bytes=8*1024*1024, max_total_bytes=32*1024*1024, max_members=1024):
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
            if _ratio_limit is not None and any(e.file_size > _ratio_limit * e.compress_size for e in entries):
                return "FAIL"
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


def reproducible_zip_quality(first, second, ignore):
    """Compare SHA-256 of normalized ZIPs. Only order and DOS timestamps may be normalized."""
    from copy import copy
    from io import BytesIO
    from pathlib import Path
    from zipfile import ZIP_DEFLATED, ZIP_STORED
    if not isinstance(ignore, list) or not all(k in ("member-order", "timestamps") for k in ignore):
        return "INCOMPLETE"

    def digest(path):
        # Restrict normalization to lossless round-trips so undeclared metadata cannot disappear.
        if Path(path).stat().st_size > 32 * 1024 * 1024:
            return None
        raw = Path(path).read_bytes()
        with ZipFile(BytesIO(raw)) as archive:
            entries = archive.infolist()
            keys = [member_key(entry.orig_filename) for entry in entries]
            if not entries or len(entries) > 1024 or None in keys or len(set(keys)) != len(keys):
                return None
            if any(e.compress_type not in (ZIP_STORED, ZIP_DEFLATED) or e.flag_bits & 1 for e in entries):
                return None
            if sum(e.file_size for e in entries) > 32 * 1024 * 1024:
                return None

            def repack(normalize):
                buffer = BytesIO()
                with ZipFile(buffer, "w") as output:
                    output.comment = archive.comment
                    ordered = sorted(entries, key=lambda e: e.filename) if normalize and "member-order" in ignore else entries
                    for original in ordered:
                        info = copy(original)
                        if normalize and "timestamps" in ignore:
                            info.date_time = (1980, 1, 1, 0, 0, 0)
                        output.writestr(info, archive.read(original))
                return buffer.getvalue()

            if repack(False) != raw:
                return None
            return hashlib.sha256(repack(True)).digest()

    try:
        left, right = digest(first), digest(second)
    except (OSError, BadZipFile, RuntimeError, ValueError):
        return "INCOMPLETE"
    if left is None or right is None:
        return "INCOMPLETE"
    return "PASS" if left == right else "FAIL"


def target_dependency_quality(markers, target_facts, shipped):
    """Evaluate recorded fact-equality markers without consulting the host or loading packages."""
    if not isinstance(markers, dict) or not isinstance(target_facts, dict) or not isinstance(shipped, set):
        return "INCOMPLETE"
    unknown = False
    for dependency, requirements in markers.items():
        if not isinstance(requirements, dict):
            return "INCOMPLETE"
        missing = False
        inactive = False
        for fact, expected in requirements.items():
            observed = target_facts.get(fact)
            if not isinstance(expected, str) or not expected:
                return "INCOMPLETE"
            if not isinstance(observed, str) or not observed:
                missing = True
            elif observed != expected:
                inactive = True
        if inactive:
            continue
        if missing:
            unknown = True
        elif dependency not in shipped:
            return "FAIL"
    return "INCOMPLETE" if unknown else "PASS"
