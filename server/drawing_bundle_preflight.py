"""Resolve local drawing dependencies against a bounded ZIP inventory."""
from __future__ import annotations

import io
import stat
import zlib
from pathlib import PurePosixPath
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

from guest_uploads import validate_upload
from dxf_intake import DxfParseError, parse_dxf_bytes

MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
_MAX_EXPANDED_BYTES = 128 * 1024 * 1024
_MAX_DRAWING_BYTES = 4 * 1024 * 1024
_MAX_TOTAL_DRAWING_BYTES = 12 * 1024 * 1024
_MAX_MEMBERS = 1024
_MAX_DRAWINGS = 64
_MAX_DEPENDENCIES = 10000
_FILE_KINDS = {"xref", "image", "underlay", "font", "shape_font"}
_FONT_EXTENSIONS = {".shx", ".ttf", ".otf", ".ttc"}


class BundlePreflightError(ValueError):
    """The request cannot produce a complete archive report."""


def _archive_path(name: str, *, parent: tuple[str, ...] = ()) -> str | None:
    """Resolve CAD path separators without consulting the host filesystem."""
    name = name.replace("\\", "/")
    if not name or name.startswith("/") or ":" in name or any(ord(char) < 32 for char in name):
        return None
    parts = list(parent)
    for part in name.split("/"):
        if part == "..":
            if not parts:
                return None
            parts.pop()
        elif part not in ("", "."):
            parts.append(part)
    return "/".join(parts) or None


def _inventory(archive: ZipFile) -> dict[str, tuple[str, ZipInfo]]:
    entries = archive.infolist()
    if len(entries) > _MAX_MEMBERS or sum(item.file_size for item in entries) > _MAX_EXPANDED_BYTES:
        raise BundlePreflightError("ZIP exceeds the preflight inventory limits")
    files = {}
    seen = set()
    for item in entries:
        name = item.orig_filename.replace("\\", "/")
        path = _archive_path(name)
        if path is None or ".." in name.split("/") or len(path) > 512:
            raise BundlePreflightError("ZIP contains an unsafe member name")
        key = path.casefold()
        if key in seen:
            raise BundlePreflightError("ZIP contains ambiguous member names")
        seen.add(key)
        mode = stat.S_IFMT(item.external_attr >> 16)
        if mode not in (0, stat.S_IFREG, stat.S_IFDIR) or item.flag_bits & 1:
            raise BundlePreflightError("ZIP contains a linked, special or encrypted member")
        if not name.endswith("/") and mode != stat.S_IFDIR and not item.external_attr & 0x10:
            files[key] = path, item
    return files


def _has_complete_sections(raw: bytes) -> bool:
    section_open = False
    expect_name = False
    saw_section = False
    with io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8-sig", errors="replace") as lines:
        for code_line in lines:
            value_line = next(lines, None)
            if value_line is None:
                return False
            try:
                code = int(code_line)
            except ValueError:
                return False
            value = value_line.strip()
            if code == 999:
                continue
            if expect_name:
                if code != 2 or not value:
                    return False
                expect_name = False
                section_open = saw_section = True
            elif (code, value) == (0, "EOF"):
                return saw_section and not section_open and all(not line.strip() for line in lines)
            elif (code, value) == (0, "SECTION") and not section_open:
                expect_name = True
            elif (code, value) == (0, "ENDSEC") and section_open:
                section_open = False
            elif not section_open or (code == 0 and value == "SECTION"):
                return False
    return False


def _read_dependencies(archive: ZipFile, path: str, item: ZipInfo) -> list[dict]:
    if item.compress_type not in (ZIP_STORED, ZIP_DEFLATED):
        raise BundlePreflightError(f"unsupported compression for drawing {path}")
    try:
        raw = archive.read(item)
        _, reason = validate_upload(path, raw)
        if reason is not None:
            raise BundlePreflightError(f"invalid drawing {path}: {reason}")
        if not _has_complete_sections(raw):
            raise BundlePreflightError(f"invalid ASCII DXF framing for drawing {path}")
        intake = parse_dxf_bytes(raw, source_name=path, include_measurement_evidence=False)
    except (BadZipFile, DxfParseError, OSError, EOFError, zlib.error) as exc:
        raise BundlePreflightError(f"cannot read drawing {path}") from exc
    return intake["dependency_report"]["dependencies"]


def _resolve(archive: ZipFile, primary_member: str) -> dict:
    files = _inventory(archive)
    primary = _archive_path(primary_member)
    if primary is None or PurePosixPath(primary).suffix.lower() != ".dxf" or primary.casefold() not in files:
        raise BundlePreflightError("select a primary ASCII DXF member in the ZIP")

    root_key = primary.casefold()
    queued = {root_key}
    pending = [root_key]
    drawings = []
    dependencies = []
    drawing_bytes = 0
    for key in pending:
        path, item = files[key]
        drawing_bytes += item.file_size
        if item.file_size > _MAX_DRAWING_BYTES or drawing_bytes > _MAX_TOTAL_DRAWING_BYTES:
            raise BundlePreflightError("ZIP exceeds the preflight drawing size limits")
        drawings.append(path)
        parent = tuple(path.split("/")[:-1])
        for record in _read_dependencies(archive, path, item):
            kind, declared = record["kind"], record["name"]
            if kind not in _FILE_KINDS:
                continue
            declared_suffix = PurePosixPath(declared.replace("\\", "/")).suffix.lower()
            target = _archive_path(declared, parent=parent)
            target_key = target.casefold() if target else None
            member = files.get(target_key)
            if kind in {"font", "shape_font"} and declared_suffix not in _FONT_EXTENSIONS:
                resolution, member = "system_font", None
            elif target_key is None:
                resolution = "outside_bundle"
            elif member is None:
                resolution = "missing"
            elif kind == "xref" and declared_suffix != ".dxf":
                resolution = "present_uninspected"
            else:
                resolution = "present"
                if kind == "xref" and target_key not in queued:
                    if len(queued) >= _MAX_DRAWINGS:
                        raise BundlePreflightError("ZIP exceeds the preflight drawing count limit")
                    queued.add(target_key)
                    pending.append(target_key)
            if len(dependencies) >= _MAX_DEPENDENCIES:
                raise BundlePreflightError("ZIP exceeds the preflight dependency count limit")
            dependencies.append({
                "drawing": path,
                "kind": kind,
                "declared_path": declared,
                "resolution": resolution,
                "member": member[0] if member else None,
            })
    return {
        "schema": "leaf.drawing-bundle-preflight.v1",
        "primary_member": files[root_key][0],
        "drawings_inspected": drawings,
        "dependencies": dependencies,
    }


def inspect_drawing_bundle(raw: bytes, *, primary_member: str) -> dict:
    """Report bundle presence only. No extraction, upload or conversion."""
    if len(raw) > MAX_ARCHIVE_BYTES:
        raise BundlePreflightError("ZIP exceeds the preflight archive size limit")
    try:
        with ZipFile(io.BytesIO(raw)) as archive:
            return _resolve(archive, primary_member)
    except (BadZipFile, UnicodeError, OSError, EOFError) as exc:
        raise BundlePreflightError("expected a readable ZIP archive") from exc
