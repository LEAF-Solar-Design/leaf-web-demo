#!/usr/bin/env python3
"""Hosted Linux runner leg (v0) for the tool-package contract v1.

Contract obligations implemented (contract/CONTRACT.md):
  - section 4: validation (JSON Schema + procedural rules V1-V17)
  - section 5.1: canonical package digest
  - section 5.3: canonical introspection surface (byte-comparable with the C# host)
  - section 5.4: byte-identical re-emit

Explicitly NOT implemented: CAD execution. On the hosted lane, CAD execution runs on
Autodesk-cloud Windows engines (APS, proven separately); faking it here would be
dishonest. The report states cad_execution: not-attempted.

Usage: runner.py <package.zip> <out_dir>
"""

import argparse
import hashlib
import io
import json
import re
import struct
import sys
import zipfile
import zlib
from pathlib import Path

import jsonschema

def resolve_schema_path(module_dir: Path) -> Path | None:
    for candidate in (module_dir / "tool.schema.json",
                      module_dir.parent / "contract" / "tool.schema.json"):
        if candidate.is_file():
            return candidate
    return None


SCHEMA_PATH = resolve_schema_path(Path(__file__).parent)

NAME_RX = re.compile(r"[a-z][a-z0-9]*(-[a-z0-9]+)*")
CSX_NAME_RX = re.compile(r"[a-z0-9][a-z0-9-]*\.csx")
LOAD_RX = re.compile(r'[ \t]*#load[ \t]+"(lib/[a-z0-9][a-z0-9-]*\.csx)"[ \t]*')
DIRECTIVE_RX = re.compile(r"^[ \t\f\v]*#[ \t]*(r|load)(?![A-Za-z0-9_])")
BIDI_RX = re.compile(r"[\u202a-\u202e\u2066-\u2069]")
CONTROL_RX = re.compile(r"[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069\ufeff]")
FIELD_RULES = {
    "name": "V3", "version": "V4", "description": "V5", "entry": "V6",
    "params": "V7", "returns": "V7", "capabilities": "V8", "family": "V9",
    "hosts": "V10", "timeout_ms": "V11", "idempotent": "V12",
    "provenance": "V13", "review": "V14",
}

REQUIRED_KEYS = [
    "name", "version", "description", "entry", "params", "returns",
    "capabilities", "hosts", "timeout_ms", "idempotent", "provenance", "review",
]


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def package_digest(files: dict) -> str:
    lines = "".join(
        f"{path}\t{len(data)}\t{hashlib.sha256(data).hexdigest()}\n"
        for path, data in sorted(files.items())
    )
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def check_integers_only(node, path, failures):
    if isinstance(node, float):
        failures.append(f"V15: non-integer number at {path}")
    elif isinstance(node, dict):
        for k, v in node.items():
            check_integers_only(v, f"{path}.{k}", failures)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            check_integers_only(v, f"{path}[{i}]", failures)


def read_archive(zip_bytes: bytes) -> tuple[str, dict]:
    if len(zip_bytes) > 8 * 1024 * 1024:
        raise ValueError("V16: archive exceeds 8 MiB")
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            entries = zf.infolist()
            file_entries = [info for info in entries if not info.is_dir()]
            if len(file_entries) > 64 or len(entries) > 70:
                raise ValueError("V16: more than 64 files")
            if sum(info.file_size for info in file_entries) > 4 * 1024 * 1024:
                raise ValueError("V16: package exceeds 4 MiB")
            names = set()
            for info in entries:
                name = info.orig_filename
                parts = name[:-1].split("/") if name.endswith("/") else name.split("/")
                if ("\\" in name or ":" in name
                        or any(ord(c) < 32 or ord(c) == 127 for c in name)
                        or any(p in ("", "..") for p in parts)):
                    raise ValueError("V16: disallowed entry name")
                if info.flag_bits & 1:
                    raise ValueError("V16: encrypted entry")
                if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise ValueError("V16: unsupported compression in entry")
                if info.compress_size > 4 * 1024 * 1024 + 64 * 1024:
                    raise ValueError("V16: package exceeds 4 MiB")
                if info.compress_type == zipfile.ZIP_STORED and info.compress_size != info.file_size:
                    raise ValueError("V16: entry larger than declared")
                if name in names:
                    raise ValueError(f"V16: duplicate entry '{name}'")
                names.add(name)
            roots = {info.orig_filename.split("/", 1)[0] for info in entries}
            if len(roots) != 1 or any("/" not in info.orig_filename for info in file_entries):
                raise ValueError("V3: transport archive must contain exactly one top-level directory")
            root = roots.pop()
            for info in entries:
                if info.is_dir() and info.orig_filename not in (f"{root}/", f"{root}/lib/"):
                    raise ValueError(f"V16: disallowed directory '{info.orig_filename}'")
            if f"{root}/lib/" in names and not any(
                    info.orig_filename.startswith(f"{root}/lib/") for info in file_entries):
                raise ValueError("V16: empty lib/")
            files = {}
            for info in file_entries:
                if info.compress_type == zipfile.ZIP_STORED:
                    with zf.open(info) as entry:
                        data = entry.read(info.file_size + 1)
                else:
                    # ZipExtFile trusts the declared size and may discard excess
                    # output. Bound inflation ourselves, using only public ZIP IO.
                    zf.fp.seek(info.header_offset)
                    header = zf.fp.read(30)
                    if len(header) != 30:
                        raise ValueError("V16: not a zip archive")
                    fields = struct.unpack("<4s5H3I2H", header)
                    if fields[0] != b"PK\x03\x04" or fields[3] != info.compress_type:
                        raise ValueError("V16: not a zip archive")
                    if fields[2] & 1:
                        raise ValueError("V16: encrypted entry")
                    local_name = zf.fp.read(fields[9])
                    encoding = "utf-8" if fields[2] & 0x800 else "cp437"
                    if local_name.decode(encoding) != info.orig_filename:
                        raise ValueError("V16: disallowed entry name")
                    data_offset = info.header_offset + 30 + fields[9] + fields[10]
                    if data_offset + info.compress_size > len(zip_bytes):
                        raise ValueError("V16: not a zip archive")
                    compressed = memoryview(zip_bytes)[data_offset:data_offset + info.compress_size]
                    inflater = zlib.decompressobj(-15)
                    data = inflater.decompress(compressed, info.file_size + 1)
                    if (len(data) > info.file_size or inflater.unconsumed_tail
                            or not inflater.eof):
                        raise ValueError("V16: entry larger than declared")
                    if zlib.crc32(data) != info.CRC:
                        raise ValueError("V16: entry checksum mismatch")
                    del compressed, inflater
                if len(data) > info.file_size:
                    raise ValueError("V16: entry larger than declared")
                files[info.orig_filename.split("/", 1)[1]] = data
            return root, files
    except (zipfile.BadZipFile, zlib.error, UnicodeDecodeError) as ex:
        raise ValueError("V16: not a zip archive") from ex


def check_scripts(files, failures):
    for path, data in files.items():
        if not path.endswith(".csx"):
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            failures.append(f"V17: '{path}' is not valid UTF-8")
            continue
        if "\ufeff" in text:
            failures.append(f"V17: byte order mark in '{path}'")
        if BIDI_RX.search(text):
            failures.append(f"V17: bidirectional control character in '{path}'")
        # Fail closed even for directive-looking lines inside strings or comments.
        for line in re.split(r"\r\n|[\r\n\u0085\u2028\u2029]", text):
            directive = DIRECTIVE_RX.match(line)
            if not directive:
                continue
            if directive.group(1) == "r":
                failures.append(f"V17: #r directive in '{path}'")
                continue
            load = LOAD_RX.fullmatch(line)
            if load:
                if load.group(1) not in files:
                    failures.append(f"V17: #load target '{load.group(1)}' missing")
            else:
                quoted = re.search(r'"([^"\r\n]*)"', line)
                if quoted:
                    failures.append(f"V17: #load target '{quoted.group(1)}' not lib/<file>.csx in '{path}'")
                else:
                    failures.append(f"V17: malformed #load directive in '{path}'")


def check_controls(node, field, failures):
    if isinstance(node, str):
        if CONTROL_RX.search(node):
            failures.append(f"{FIELD_RULES.get(field, 'V2')}: control character in {field}")
    elif isinstance(node, dict):
        for key, value in node.items():
            check_controls(key, field, failures)
            check_controls(value, field, failures)
    elif isinstance(node, list):
        for value in node:
            check_controls(value, field, failures)


def strict_pattern(validator, pattern, instance, schema):
    if isinstance(instance, str) and re.fullmatch(pattern, instance) is None:
        yield jsonschema.ValidationError(f"{instance!r} does not match {pattern!r}")


def validate(files: dict, pkg_dir_name: str, failures: list):
    # V16: layout
    if len(files) > 64:
        failures.append("V16: more than 64 files")
    if sum(len(d) for d in files.values()) > 4 * 1024 * 1024:
        failures.append("V16: package exceeds 4 MiB")
    for path in files:
        ok = path in ("tool.json", "main.csx") or (
            path.startswith("lib/")
            and path.count("/") == 1
            and CSX_NAME_RX.fullmatch(path[4:])
        )
        if not ok:
            failures.append(f"V16: disallowed file '{path}'")
    check_scripts(files, failures)

    # V1: manifest present, UTF-8, no BOM, single JSON object
    if "tool.json" not in files:
        failures.append("V1: tool.json missing")
        return None
    raw = files["tool.json"]
    if len(raw) > 256 * 1024:
        failures.append("V1: tool.json exceeds 256 KiB")
        return None
    if raw[:3] == b"\xef\xbb\xbf":
        failures.append("V1: tool.json has a BOM")
        return None
    try:
        # parse_float trap: any float literal anywhere violates V15 (reported with
        # the same rule id the C# leg uses; sentinel keeps json parsing intact).
        float_seen = []

        def _float(s):
            float_seen.append(s)
            return 0

        def _object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"V1: duplicate key '{key}' in tool.json")
                result[key] = value
            return result

        def _integer(s):
            if len(s) > 19:
                raise ValueError("V1: tool.json parse error: integer out of range")
            return int(s)

        manifest = json.loads(raw.decode("utf-8"), parse_float=_float,
                              parse_int=_integer, object_pairs_hook=_object)
    except RecursionError:
        failures.append("V1: tool.json nested too deeply")
        return None
    except (UnicodeDecodeError, json.JSONDecodeError) as ex:
        failures.append(f"V1: tool.json parse error: {ex}")
        return None
    except ValueError as ex:
        message = str(ex)
        failures.append(message if message.startswith("V1:")
                        else f"V1: tool.json parse error: {message}")
        return None
    manifest_failure_start = len(failures)
    try:
        # Bound nesting before any recursive walker, independently of Python's
        # recursion limit. The root value has depth zero.
        stack = [(manifest, 0)]
        while stack:
            node, depth = stack.pop()
            if depth > 64:
                failures.append("V1: tool.json nested too deeply")
                return None
            if isinstance(node, dict):
                stack.extend((value, depth + 1) for value in node.values())
            elif isinstance(node, list):
                stack.extend((value, depth + 1) for value in node)

        if not isinstance(manifest, dict):
            failures.append("V1: tool.json is not a JSON object")
            return None

        # V2: closed key set (schema enforces too; explicit rule ids for the report)
        for key in manifest:
            if key not in REQUIRED_KEYS and key != "family":
                failures.append(f"V2: unknown key '{key}'")
        for key in REQUIRED_KEYS:
            if key not in manifest:
                failures.append(f"V2: missing required key '{key}'")
        # Full JSON Schema validation (covers V3-V14 field shapes)
        if SCHEMA_PATH is None or not SCHEMA_PATH.is_file():
            failures.append("exception: tool.schema.json not found")
        else:
            schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
            validator_type = jsonschema.validators.extend(
                jsonschema.Draft202012Validator, {"pattern": strict_pattern})
            validator = validator_type(schema)
            for err in sorted(validator.iter_errors(manifest), key=lambda e: str(list(e.absolute_path))):
                path = list(err.absolute_path)
                rule = FIELD_RULES.get(path[0], "V2") if path else "V2"
                failures.append(f"{rule}: schema: {'/'.join(str(p) for p in path) or '$'}: {err.message}")
        for field, value in manifest.items():
            check_controls(field, field, failures)
            check_controls(value, field, failures)

        # Procedural rules the schema cannot see
        if manifest.get("name") != pkg_dir_name:
            failures.append(f"V3: package dir '{pkg_dir_name}' != name '{manifest.get('name')}'")
        if "main.csx" not in files:
            failures.append("V6: main.csx missing")
        if float_seen:
            failures.append("V15: non-integer number in tool.json")
        check_integers_only(manifest, "$", failures)

        # params/returns must themselves be valid JSON Schema 2020-12
        for field in ("params", "returns"):
            try:
                jsonschema.Draft202012Validator.check_schema(manifest.get(field, {}))
            except jsonschema.SchemaError as ex:
                failures.append(f"V7: {field} is not a valid JSON Schema: {ex.message}")

        return manifest
    except RecursionError:
        # Preserve file-level V16/V17 findings while discarding partial
        # manifest diagnostics from a walk that could not finish.
        del failures[manifest_failure_start:]
        failures.append("V1: tool.json nested too deeply")
        return None


def build_introspection(manifest: dict, files: dict, digest: str) -> dict:
    intro = {
        "contract_version": 1,
        "name": manifest["name"],
        "version": manifest["version"],
        "description": manifest["description"],
        "entry": manifest["entry"],
        "params": manifest["params"],
        "returns": manifest["returns"],
        "capabilities": manifest["capabilities"],
        "hosts": manifest["hosts"],
        "timeout_ms": manifest["timeout_ms"],
        "idempotent": manifest["idempotent"],
        "provenance": manifest["provenance"],
        "review": manifest["review"],
        "files": [
            {"path": p, "sha256": hashlib.sha256(d).hexdigest(), "size": len(d)}
            for p, d in sorted(files.items())
        ],
        "package_digest": digest,
    }
    if "family" in manifest:
        intro["family"] = manifest["family"]
    return intro


def main() -> int:
    parser = argparse.ArgumentParser(usage="%(prog)s <package.zip> <out_dir>")
    parser.add_argument("package_zip", type=Path)
    parser.add_argument("out_dir", type=Path)
    args = parser.parse_args()
    zip_path, out_dir = args.package_zip, args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {"leg": "runner-linux", "contract_version": 1, "cad_execution": "not-attempted"}
    failures = []
    ok = False
    try:
        if zip_path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("V16: archive exceeds 8 MiB")
        pkg_dir_name, files = read_archive(zip_path.read_bytes())

        manifest = validate(files, pkg_dir_name, failures)
        digest = package_digest(files)
        report["package_digest"] = digest
        if not failures and manifest is not None:
            intro = build_introspection(manifest, files, digest)
            (out_dir / "introspection.json").write_bytes(
                canonical_json(intro).encode("utf-8")
            )
            reemit_root = out_dir / "reemit" / pkg_dir_name
            for path, data in files.items():
                dest = reemit_root / path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
            report["reemit_files"] = len(files)
            ok = True
    except Exception as ex:  # noqa: BLE001 - report and fail closed
        if isinstance(ex, ValueError) and str(ex).startswith(("V16:", "V3:")):
            failures.append(str(ex))
        else:
            failures.append(f"exception: {ex}")
    report["validation"] = {"passed": not failures, "failures": failures}
    report["ok"] = ok and not failures
    (out_dir / "report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    for failure in failures:
        print(failure, file=sys.stderr)
    print(f"GATE-RUNNER-RESULT: {'OK' if report['ok'] else 'FAIL'}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
