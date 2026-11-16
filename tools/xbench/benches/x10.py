"""X10 first reachable blocker, using only normal project UI controls.

Manifest ``x10`` config: project_name, fixtures (role -> manifest ID), and
optional import_controls/workflow_controls (ordered accessible button/tab names).
Each bound fixture declares its matching role. Required roles are ROLES below.
The oracle JSON has a nonempty ``expected`` object; rates JSON has ``items``
with decimal-string quantity/unit_rate fields. Neither is product evidence.
Customer manifests and fixture contents must remain outside git.
"""

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
import re
import shutil
import struct
import tempfile
from uuid import UUID, uuid4
from urllib.parse import parse_qs, urlsplit
import zlib

from xbench_core import Step
from xbench_session import redact, signed_in


LIMITS = ("First-blocker grading slice only. Import/terrain acceptance is incomplete; "
          "downstream checks 2..6 are unimplemented. No PASS is possible yet.")
ROLES = ("drawing", "comparison_drawing", "bom", "terrain", "rates", "oracle")
MAX_JSON_BYTES = 1048576
MAX_JSON_DEPTH = 64
MAX_RATE_ITEMS = 10000
MAX_SCREENSHOT_BYTES = 16 * 1024 * 1024
IMPORT_REFUSALS = ("Choose the drawing again to retry the upload.",
                   "Retry to attach this drawing to the project.")
DOWNSTREAM = (
    "cut/fill quantities against terrain and oracle",
    "BOM reconciliation plus comparison drawing changes",
    "UI rate edit with exact cents against oracle",
    "missing quantities/rates remain unpriced",
    "save/reopen/export hashes",
)


def safe_text(value):
    """Persist bounded plain UI sentences, never transport diagnostics or markup."""
    text = re.sub(r"<[^>]*>", "[markup]", str(value))
    text = re.sub(r"https?://\S+|\b\S+@\S+\b", "[redacted]", text)
    text = re.sub(r"(?i)\b(authorization|bearer|token|secret|password)\b[^\n]*",
                  "[redacted]", text)
    return redact("".join(c for c in text if c in "\n\t" or ord(c) >= 32))[:4000]


def _strict_json(path):
    # Bound the read even if the file grows after stat; never read-all first.
    with path.open("rb") as source:
        if path.stat().st_size > MAX_JSON_BYTES:
            raise ValueError("JSON byte budget exceeded")
        data = source.read(MAX_JSON_BYTES + 1)
    if len(data) > MAX_JSON_BYTES:
        raise ValueError("JSON byte budget exceeded")
    return _strict_json_bytes(data)


def _strict_json_bytes(data):
    if len(data) > MAX_JSON_BYTES:
        raise ValueError("JSON byte budget exceeded")
    text = data.decode("utf-8")
    depth, quoted, escaped = 0, False, False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise ValueError("JSON depth budget exceeded")
        elif char in "]}":
            depth -= 1

    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("nonfinite JSON constant")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant, parse_float=Decimal)


def rate_total(rates):
    """Exact fixture arithmetic; this does not assert a product result."""
    if not isinstance(rates, dict) or not isinstance(rates.get("items"), list) or not rates["items"]:
        raise ValueError("rates.items must be a nonempty list")
    if len(rates["items"]) > MAX_RATE_ITEMS:
        raise ValueError("rates item budget exceeded")
    values = []
    for item in rates["items"]:
        if not isinstance(item, dict):
            raise ValueError("invalid rate item")
        pair = []
        for key in ("quantity", "unit_rate"):
            value = item.get(key)
            if not isinstance(value, str) or not re.fullmatch(r"\d{1,30}(?:\.\d{1,30})?", value):
                raise ValueError("rates require nonnegative decimal strings")
            pair.append(Decimal(value))
        values.append(pair)
    with localcontext() as context:
        context.prec = 128 + len(str(len(values)))
        return sum((quantity * rate for quantity, rate in values), Decimal(0))


def bindings(ctx):
    if not isinstance(ctx.manifest, dict):
        raise ValueError("invalid manifest")
    config = ctx.manifest.get("x10")
    if not isinstance(config, dict):
        raise ValueError("missing x10 config")
    name = config.get("project_name")
    if not isinstance(name, str) or not name.strip() or len(name) > 100 or "\n" in name:
        raise ValueError("missing or invalid x10.project_name")
    mapping = config.get("fixtures")
    if not isinstance(mapping, dict) or set(mapping) != set(ROLES):
        raise ValueError("x10.fixtures must bind drawing, comparison_drawing, bom, terrain, rates, oracle")
    if (not all(isinstance(value, str) and value.strip() and len(value) <= 200
                for value in mapping.values()) or len(set(mapping.values())) != len(ROLES)):
        raise ValueError("fixture bindings must have distinct manifest IDs")
    def unique_rows(rows, manifest=False):
        if not isinstance(rows, list):
            raise ValueError("fixture rows must be a list")
        result = {}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("fixture row must be an object")
            identity = row.get("id")
            if (not isinstance(identity, str) or not identity.strip() or len(identity) > 200
                    or not isinstance(row.get("path"), str) or not row["path"]
                    or type(row.get("bytes")) is not int or row["bytes"] < 0
                    or not isinstance(row.get("sha256"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) is None
                    or (manifest and not isinstance(row.get("role"), str))):
                raise ValueError("invalid fixture row fields")
            if identity in result:
                raise ValueError("duplicate fixture ID")
            result[identity] = row
        return result

    fixtures = unique_rows(ctx.manifest.get("fixtures"), manifest=True)
    verified = unique_rows(ctx.fixtures)
    bound, json_bytes, fingerprints = {}, {}, {}
    for role, identity in mapping.items():
        row = fixtures.get(identity)
        proof = verified.get(identity)
        if (row is None or row.get("role") != role or proof is None or proof.get("ok") is not True
                or any(proof.get(key) != row.get(key) for key in ("path", "bytes", "sha256"))):
            raise ValueError("missing, wrong-role or unverified fixture binding: " + role)
        bound[role] = Path(row["path"])
        if role in ("oracle", "rates") and row["bytes"] > MAX_JSON_BYTES:
            raise ValueError("JSON byte budget exceeded")
        digest = hashlib.sha256()
        size = 0
        data = bytearray() if role in ("oracle", "rates") else None
        if not stat.S_ISREG(bound[role].lstat().st_mode):
            raise ValueError("fixture is not a regular file: " + role)
        descriptor = os.open(bound[role], os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(descriptor, "rb") as source:
            opened = os.fstat(source.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise ValueError("opened fixture is not a regular file: " + role)
            while True:
                block = source.read(min(1024 * 1024, row["bytes"] - size + 1))
                if not block:
                    break
                size += len(block)
                if size > row["bytes"]:
                    raise ValueError("fixture grew beyond declared size: " + role)
                if data is not None:
                    if size > MAX_JSON_BYTES:
                        raise ValueError("JSON byte budget exceeded")
                    data.extend(block)
                digest.update(block)
            fingerprints[role] = _file_fingerprint(os.fstat(source.fileno()))
            if fingerprints[role] != _file_fingerprint(opened):
                raise ValueError("fixture changed during verified read: " + role)
        if size != row.get("bytes") or digest.hexdigest() != row.get("sha256"):
            raise ValueError("fixture content changed: " + role)
        if data is not None:
            json_bytes[role] = bytes(data)
    for key in ("import_controls", "workflow_controls"):
        controls = config.get(key, [])
        if (not isinstance(controls, list) or len(controls) > 8
                or any(not isinstance(label, str) or not label.strip() or len(label) > 100 for label in controls)):
            raise ValueError("invalid x10 UI control path")
    oracle = _strict_json_bytes(json_bytes["oracle"])
    if not isinstance(oracle, dict) or not isinstance(oracle.get("expected"), dict) or not oracle["expected"]:
        raise ValueError("oracle.expected must be a nonempty object")
    total = rate_total(_strict_json_bytes(json_bytes["rates"]))
    # Verified parsed bytes, plus an observable replacement guard; not an atomic
    # filesystem snapshot. Never reopen a path to obtain parsing input.
    for role, path in bound.items():
        if _file_fingerprint(path.lstat()) != fingerprints[role]:
            raise ValueError("fixture identity changed: " + role)
    return config, bound, oracle["expected"], total


def _file_fingerprint(info):
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("fixture is not a regular file")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def classify(observation, expected=None):
    """Only an explicit rendered blocker in a confirmed new project can FAIL."""
    text_fields = ("ui_text", "ground_option", "issue", "evidence_path", "import_rejection",
                   "attachment_status", "import_status", "version_selection_status", "project_id",
                   "route_project_id", "active_project_id", "imported_version_id", "source",
                   "web_build", "stage", "exception_type", "fixture_rate_total", "screenshot_sha256")
    bool_fields = ("fresh_project", "imported_version_selected", "evidence_saved", "diagnostic_saved")
    invalid = not isinstance(observation, Mapping)
    if not invalid:
        invalid = (any(key in observation and not isinstance(observation[key], str) for key in text_fields)
                   or any(key in observation and type(observation[key]) is not bool for key in bool_fields)
                   or ("selected_labels" in observation and (
                       not isinstance(observation["selected_labels"], list)
                       or any(not isinstance(label, str) for label in observation["selected_labels"]))))
    if invalid:
        return [Step(n, "UNREADABLE", "Invalid observation field types; no scorable evidence.")
                for n in range(1, 7)]
    rejection = observation.get("import_rejection", "")
    if rejection and rejection not in IMPORT_REFUSALS:
        return [Step(n, "UNREADABLE", "Unrecognized import refusal; no scorable evidence.")
                for n in range(1, 7)]
    fresh = observation.get("fresh_project") is True
    label = observation.get("ground_option", "")
    unavailable = (isinstance(label, str)
                   and re.search(r"\bGround (?:Mount )?Physical\b", label, re.I)
                   and re.search(r"\bunavailable\b", label, re.I))
    pending = "Checking Solar steps" in observation.get("ui_text", "")
    proven = fresh and observation.get("evidence_saved") is True and not pending
    contradictory = (bool(observation.get("import_rejection"))
                     and observation.get("imported_version_selected") is True)
    if contradictory:
        verdict = "UNREADABLE"
        reason = "Contradictory import rejection and selected drawing version evidence."
    elif proven and observation.get("imported_version_selected") is True and unavailable:
        verdict = "FAIL"
        reason = ("Fresh canonical project with imported drawing version selected renders "
                  "Ground Physical unavailable; acceptance is conjunctive.")
    elif proven and observation.get("import_rejection"):
        verdict = "FAIL"
        reason = "Primary drawing import explicitly rejected by product: " + safe_text(observation["import_rejection"])
    else:
        verdict = "UNREADABLE"
        remaining = ("" if observation.get("imported_version_selected") is True
                     else "complete primary drawing import and version selection, ")
        reason = ("No confirmed rendered product blocker. Remaining: " + remaining
                  + "bind terrain and verify every step-1 acceptance clause. "
                  + safe_text(observation.get("issue", "Available option alone is insufficient.")))
    evidence = observation.get("evidence_path", "x10-ui.json / x10-ground.png when saved.")
    rows = [Step(1, verdict, reason + " Evidence: " + evidence,
                 observed=observation, expected=expected)]
    rows.extend(Step(n, "UNREADABLE", "Prerequisite: completed step 1 and " + subject
                     + "; downstream product observation unimplemented in this slice.")
                for n, subject in enumerate(DOWNSTREAM, 2))
    return rows


def _click_controls(page, labels):
    for label in labels:
        page.get_by_role("button", name=label, exact=True).or_(
            page.get_by_role("tab", name=label, exact=True)).click(timeout=15_000)


def _create_project(page, ctx, config, observation):
    observation["fresh_project"] = False
    name = config["project_name"].strip() + "-" + uuid4().hex[:12]
    field = page.get_by_role("textbox", name="Project name", exact=True)
    if not field.is_visible():
        page.get_by_role("button", name=re.compile(r"^Projects: change project\.")).click(timeout=15_000)
        field = page.get_by_role("textbox", name="New project", exact=True)
    field.fill(name, timeout=15_000)

    def created(response):
        return (response.request.method == "POST"
                and response.url == ctx.base.rstrip("/") + "/api/projects")

    prefix = ctx.base.rstrip("/") + "/api/projects/"

    def opened(response):
        return (response.request.method == "GET" and response.url.startswith(prefix)
                and re.fullmatch(r"[0-9a-fA-F-]{36}", response.url[len(prefix):]) is not None)

    with page.expect_response(opened, timeout=30_000) as active:
        with page.expect_response(created, timeout=30_000) as pending:
            field.locator("xpath=ancestor::form").get_by_role(
                "button", name="Create project", exact=True).click(timeout=15_000)
    response = pending.value
    observation["creation_status"] = response.status
    if response.status not in (200, 201):
        raise ValueError("project creation not confirmed")
    # Extract only the response's project identity. Never retain the body.
    project = response.json().get("project", {})
    identity = str(UUID(project.get("project_id") or project.get("id")))
    if project.get("name") != name:
        raise ValueError("project creation identity mismatch")
    opened_response = active.value
    observation["open_status"] = opened_response.status
    route_id = str(UUID(opened_response.url[len(prefix):]))
    opened_project = opened_response.json().get("project", {}) if opened_response.status == 200 else {}
    active_id = str(UUID(opened_project.get("project_id") or opened_project.get("id")))
    observation.update(project_id=identity, route_project_id=route_id, active_project_id=active_id)
    if route_id != identity or active_id != identity:
        raise ValueError("active project identity mismatch")
    chip = page.get_by_role("button", name="Projects: change project. Project " + name, exact=True)
    chip.wait_for(state="visible", timeout=30_000)
    page.wait_for_load_state("networkidle", timeout=30_000)
    observation["fresh_project"] = True


def _valid_png(path):
    if not 57 <= path.stat().st_size <= MAX_SCREENSHOT_BYTES:
        raise ValueError("invalid screenshot size")
    with path.open("rb") as source:
        data = source.read(MAX_SCREENSHOT_BYTES + 1)
    if len(data) > MAX_SCREENSHOT_BYTES:
        raise ValueError("screenshot byte budget exceeded")
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("invalid screenshot signature")
    offset, first, image = 8, True, False
    compressed = bytearray()
    rows = []
    palette = False
    image_ended = False
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        end = offset + 12 + length
        if (end > len(data) or re.fullmatch(rb"[A-Za-z]{4}", kind) is None
                or kind[2] & 32):
            break
        payload = data[offset + 8:end - 4]
        if zlib.crc32(kind + payload) & 0xffffffff != struct.unpack(">I", data[end - 4:end])[0]:
            break
        if first:
            if kind != b"IHDR" or length != 13:
                break
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", payload)
            depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8),
                      4: (8, 16), 6: (8, 16)}
            if (not 0 < width <= 0x7fffffff or not 0 < height <= 0x7fffffff
                    or depth not in depths.get(color, ()) or compression != 0
                    or filtering != 0 or interlace not in (0, 1)):
                break
            channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
            passes = ((0, 0, 1, 1),) if not interlace else (
                (0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
                (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
            for x, y, dx, dy in passes:
                columns = max(0, (width - x + dx - 1) // dx)
                count = max(0, (height - y + dy - 1) // dy)
                if columns and count:
                    rows.append((1 + (columns * channels * depth + 7) // 8, count))
            expected = sum(length * count for length, count in rows)
            if expected > 64 * 1024 * 1024:
                raise ValueError("screenshot decoded size exceeds limit")
        elif kind == b"IHDR":
            break
        first = False
        if kind == b"PLTE":
            if palette or image or color in (0, 4) or not length or length % 3 or length > 768:
                break
            if color == 3 and length // 3 > 2 ** depth:
                break
            palette = True
        elif kind == b"IDAT":
            if image_ended or (color == 3 and not palette):
                break
            image = True
            compressed.extend(payload)
        elif image:
            image_ended = True
        if kind == b"IEND":
            if length == 0 and end == len(data) and image:
                try:
                    decoder = zlib.decompressobj()
                    decoded = decoder.decompress(compressed, min(expected + 1, 64 * 1024 * 1024))
                except zlib.error as exc:
                    raise ValueError("invalid screenshot compressed data") from exc
                if (len(decoded) != expected or not decoder.eof
                        or decoder.unused_data or decoder.unconsumed_tail):
                    raise ValueError("invalid screenshot decoded size or stream")
                position = 0
                for stride, count in rows:
                    for _ in range(count):
                        if decoded[position] > 4:
                            raise ValueError("invalid screenshot row filter")
                        position += stride
                return data
            break
        if kind not in (b"IHDR", b"PLTE", b"IDAT") and not kind[0] & 32:
            break
        offset = end
    raise ValueError("invalid screenshot structure")


def _save(page, ctx, observation, surface=None, validate=None):
    observation["evidence_saved"] = False
    observation.pop("evidence_path", None)
    ctx.out_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".x10-staging-", dir=ctx.out_dir))
    generation = "x10-evidence-" + uuid4().hex
    evidence = generation + "/x10-ui.json / " + generation + "/x10-ground.png"
    committed = None
    try:
        if surface is None:
            surface = page.get_by_role("navigation", name="Solar design steps")
        if validate is not None:
            validate()
        surface.screenshot(path=str(staging / "x10-ground.png"), timeout=15_000)
        if validate is not None:
            validate()
        data = _valid_png(staging / "x10-ground.png")
        pin = {"screenshot_bytes": len(data), "screenshot_sha256": hashlib.sha256(data).hexdigest()}
        saved = dict(observation, **pin, evidence_saved=True, evidence_path=evidence)
        record = staging / "x10-ui.json"
        record.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
        if json.loads(record.read_text(encoding="utf-8")) != saved:
            raise ValueError("invalid evidence JSON")
        def check_pin(directory):
            actual = _valid_png(directory / "x10-ground.png")
            if (len(actual) != pin["screenshot_bytes"]
                    or hashlib.sha256(actual).hexdigest() != pin["screenshot_sha256"]):
                raise ValueError("screenshot changed during evidence commit")

        check_pin(staging)
        if validate is not None:
            validate()
        staging.rename(ctx.out_dir / generation)
        committed = ctx.out_dir / generation
        check_pin(committed)
        if json.loads((committed / "x10-ui.json").read_text(encoding="utf-8")) != saved:
            raise ValueError("invalid committed evidence JSON")
        if validate is not None:
            validate()
        observation.update(pin, evidence_saved=True, evidence_path=evidence)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if committed is not None and observation["evidence_saved"] is not True:
            shutil.rmtree(committed)


def _invalidate_evidence(ctx, observation):
    # Only the current observation's exact generation pointer may be removed.
    pointer = observation.get("evidence_path", "")
    match = re.fullmatch(r"(x10-evidence-[0-9a-f]{32})/x10-ui.json / \1/x10-ground.png", pointer)
    if match is not None:
        directory = ctx.out_dir / match.group(1)
        record = directory / "x10-ui.json"
        try:
            if (not directory.is_symlink() and not record.is_symlink()
                    and json.loads(record.read_text(encoding="utf-8")).get("evidence_path") == pointer):
                record.unlink()
                shutil.rmtree(directory)
        except Exception:
            pass
    observation["evidence_saved"] = False
    observation.pop("evidence_path", None)


def _diagnostic(ctx, observation, stage, exc):
    observation.pop("evidence_path", None)
    # Exception messages can contain URLs, provider bodies or fixture paths.
    observation.update(stage=stage, exception_type=safe_text(type(exc).__name__),
                       issue="UI/infrastructure unreadable at " + stage + ": " + safe_text(type(exc).__name__),
                       evidence_saved=False)
    try:
        ctx.out_dir.mkdir(parents=True, exist_ok=True)
        (ctx.out_dir / "x10-ui.json").write_text(
            json.dumps(observation, indent=2) + "\n", encoding="utf-8")
    except OSError:
        observation["diagnostic_saved"] = False


def _continue_workspace(page):
    button = page.get_by_role("button", name="Continue workspace", exact=True)
    if button.is_visible():
        button.click(timeout=15_000)


def _prepare_workspace(page):
    page.get_by_role("combobox", name="Workspace profile", exact=True).select_option(
        label="Solar CAD", timeout=15_000)
    _continue_workspace(page)


def _import_primary(page, ctx, config, bound, observation):
    observation["imported_version_selected"] = False
    observation.pop("import_rejection", None)
    project = page.get_by_role("button", name="Project", exact=True)
    if project.get_attribute("aria-expanded") != "true":
        project.click(timeout=15_000)
    page.get_by_role("button", name="Material", exact=True).click(timeout=15_000)
    _click_controls(page, config.get("import_controls", []))
    upload = page.get_by_label("Drawing file", exact=True)
    upload.wait_for(state="attached", timeout=30_000)
    page.wait_for_function("input => !input.disabled", arg=upload.element_handle(), timeout=30_000)
    intake = page.locator('[data-pane="material-intake"]')
    status = intake.get_by_role("status").filter(has_text=re.compile(r"^Attached "))
    failure = intake.get_by_text(re.compile(
        r"^(?:Choose the drawing again to retry the upload\.|Retry to attach this drawing to the project\.)$"))
    if status.is_visible() or failure.is_visible():
        raise ValueError("stale attachment terminal before upload")
    upload.set_input_files(str(bound["drawing"]), timeout=15_000)
    status.or_(failure).first.wait_for(state="visible", timeout=180_000)
    if failure.is_visible():
        hint = failure.inner_text(timeout=15_000)
        if hint not in ("Choose the drawing again to retry the upload.",
                        "Retry to attach this drawing to the project."):
            raise ValueError("unrecognized rendered import refusal")
        observation["import_rejection"] = hint
        observation["attachment_status"] = "Primary drawing attachment rejected; retry required."
        epoch = _current_project(page, observation)

        def validate():
            if (_current_project(page, observation) != epoch or not failure.is_visible()
                    or failure.inner_text(timeout=15_000) != hint):
                raise ValueError("project or refusal changed during capture")

        # Capture only the explicit rendered hint, excluding Diagnostic details.
        _save(page, ctx, observation, surface=failure, validate=validate)
        return False
    if not status.is_visible():
        raise ValueError("attachment terminal not visible after upload")
    # Keep the rendered attachment sentence, replacing customer names and paths.
    sentence = status.inner_text(timeout=15_000)
    version = re.fullmatch(r"Attached .+ as version ([1-9][0-9]{0,8}) to .+", sentence)
    if version is None:
        raise ValueError("invalid rendered attachment version")
    observation["attachment_status"] = ("Attached [primary drawing]"
        + " as version " + version.group(1) + " to [fresh project]")
    observation["import_status"] = "Attached primary bound drawing through material UI"
    # The board preview selects canonical project mode; Close workspace would
    # instead discard the project binding and must never be used here.
    project = page.get_by_role("button", name="Project", exact=True)
    if project.get_attribute("aria-expanded") != "true":
        project.click(timeout=15_000)
    sequence = int(version.group(1))
    marker = page.get_by_role("status").filter(
        has_text=re.compile(r"^Project drawing version selected\.$"))
    if marker.is_visible():
        raise ValueError("stale drawing version selection marker")
    preview = page.get_by_role("button", name="Preview v" + str(sequence) + " in the drawing", exact=True)
    element = preview.locator("xpath=ancestor::li[1]").get_attribute("data-element-id")
    if not isinstance(element, str) or not element.startswith("version:"):
        raise ValueError("missing preview version identity")
    identity = _uuid(element[len("version:"):])
    _current_project(page, observation)
    rows = observation.get("project_versions", [])
    matches = [row for row in rows if row["version_id"] == identity and row["seq"] == sequence]
    if len(matches) != 1:
        raise ValueError("preview is not bound to current project drawing metadata")
    drawing = matches[0]["drawing_id"]
    preview.click(timeout=30_000)
    marker.wait_for(state="visible", timeout=30_000)
    if not marker.is_visible() or marker.inner_text() != "Project drawing version selected.":
        raise ValueError("drawing version selection not confirmed")
    viewer = page.locator(".viewer-title code").filter(has_text=re.compile("^" + re.escape(identity) + "$"))
    viewer.wait_for(state="visible", timeout=30_000)
    if viewer.inner_text(timeout=15_000) != identity:
        raise ValueError("selected viewer version mismatch")
    observation["version_selection_status"] = safe_text(marker.inner_text())
    observation["imported_version_id"] = identity
    observation["imported_drawing_id"] = drawing
    observation["imported_version_selected"] = True
    observation["selected_sequence"] = sequence
    return True


def _open_workflow(page, config):
    back = page.get_by_role("button", name="Return to drawing", exact=True)
    if back.is_visible():
        back.click(timeout=15_000)
    _continue_workspace(page)
    solar = page.get_by_role("tab", name="Solar", exact=True)
    if solar.is_visible() and solar.get_attribute("aria-selected") != "true":
        solar.click(timeout=15_000)
    _click_controls(page, config.get("workflow_controls", []))
    toggle = page.get_by_role("button", name="Solar flow tools", exact=True)
    if toggle.is_visible() and toggle.get_attribute("aria-expanded") == "false":
        toggle.click(timeout=15_000)


def _uuid(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value) is None:
        raise ValueError("invalid canonical UUID")
    return str(UUID(value))


def _catalog_observer(base, observation):
    origin = urlsplit(base)
    observation["catalog_epoch"] = 0
    observation["catalog_binding"] = None
    latest = None
    pending_binding = None
    failed = False

    def exact_url(value):
        url = urlsplit(value)
        return (url.scheme, url.netloc, url.path) == (origin.scheme, origin.netloc, "/api/capabilities")

    def request_seen(request):
        nonlocal latest, pending_binding, failed
        if not exact_url(request.url):
            return
        try:
            if request.method != "GET":
                raise ValueError("catalog requires normal GET")
            url = urlsplit(request.url)
            query = parse_qs(url.query, keep_blank_values=True, max_num_fields=20)
            binding = {}
            for key in ("drawing_id", "project_id", "input_version_id"):
                values = query.get(key, [])
                if len(values) != 1:
                    raise ValueError("missing or ambiguous catalog identity")
                binding[key] = _uuid(values[0])
        except ValueError:
            # An invalid same-origin catalog binding cannot leave old proof current.
            binding = None
        latest, pending_binding = request, binding
        failed = False
        observation["catalog_epoch"] += 1
        observation["catalog_binding"] = None

    def response_seen(response):
        nonlocal failed
        if response.request is not latest or not exact_url(response.url):
            return
        failed = failed or type(response.status) is not int or not 200 <= response.status < 300
        observation["catalog_epoch"] += 1
        observation["catalog_binding"] = None if failed else pending_binding

    def request_failed(request):
        nonlocal failed
        if request is latest and exact_url(request.url):
            failed = True
            observation["catalog_epoch"] += 1
            observation["catalog_binding"] = None

    request_seen.response = response_seen
    request_seen.failed = request_failed
    return request_seen


def _project_observer(base, observation):
    origin = urlsplit(base)
    observation.update(project_epoch=0, project_binding=None, project_versions=[])
    latest, route, failed = None, None, False

    def route_id(value):
        url = urlsplit(value)
        if ((url.scheme, url.netloc) != (origin.scheme, origin.netloc)
                or url.query or url.fragment):
            return None
        match = re.fullmatch(r"/api/projects/([0-9a-fA-F-]{36})", url.path)
        if match is None:
            return None
        try:
            return _uuid(match.group(1))
        except ValueError:
            return None

    def clear():
        observation["project_epoch"] += 1
        observation.update(project_binding=None, project_versions=[])

    def request_seen(request):
        nonlocal latest, route, failed
        identity = route_id(request.url)
        if identity is None:
            return
        latest, route, failed = request, identity, request.method != "GET"
        clear()

    def response_seen(response):
        nonlocal failed
        if response.request is not latest or route_id(response.url) != route:
            return
        clear()
        failed = failed or type(response.status) is not int or response.status != 200
        if failed:
            return
        try:
            body = response.json()
            project = body["project"]
            identity = _uuid(project.get("project_id") or project.get("id"))
            if identity != route:
                raise ValueError("project identity mismatch")
            versions = body.get("drawing_versions", [])
            if not isinstance(versions, list) or len(versions) > 10000:
                raise ValueError("invalid project versions")
            rows = []
            seen = set()
            for version in versions:
                version_id = _uuid(version.get("version_id") or version.get("id"))
                drawing_id = _uuid(version.get("drawing_id"))
                seq = version.get("seq")
                if type(seq) is not int or not 1 <= seq <= 999999999 or version_id in seen:
                    raise ValueError("invalid project version identity")
                seen.add(version_id)
                rows.append(dict(project_id=route, version_id=version_id, drawing_id=drawing_id, seq=seq))
        except Exception:
            failed = True
            return
        observation.update(project_binding=identity, project_versions=rows)

    def request_failed(request):
        nonlocal failed
        if request is latest and route_id(request.url) == route:
            failed = True
            clear()

    request_seen.response = response_seen
    request_seen.failed = request_failed
    return request_seen


def _current_project(page, observation):
    project = _uuid(observation.get("project_id"))
    if observation.get("project_binding") != project or observation.get("fresh_project") is not True:
        raise ValueError("current project differs from fresh project")
    chip = page.get_by_role("button", name=re.compile(r"^Projects: change project\. Project .+"))
    if not chip.is_visible():
        raise ValueError("canonical project chip is not visible")
    return observation["project_epoch"]


def _current_identity(page, observation):
    binding = observation.get("catalog_binding")
    project = _uuid(observation.get("project_id"))
    version = _uuid(observation.get("imported_version_id"))
    drawing = _uuid(observation.get("imported_drawing_id"))
    if (not isinstance(binding, dict) or binding.get("project_id") != project
            or binding.get("input_version_id") != version or binding.get("drawing_id") != drawing):
        raise ValueError("current catalog identity differs from imported project/version")
    viewer = page.locator(".viewer-title code")
    if not viewer.is_visible() or viewer.inner_text(timeout=15_000) != version:
        raise ValueError("current viewer version differs from imported version")
    return observation.get("catalog_epoch"), _current_project(page, observation)


def _ground(page, ctx, observation):
    rail = page.get_by_role("navigation", name="Solar design steps")
    rail.get_by_text("Checking Solar steps for the selected project drawing version.", exact=True).first.wait_for(
        state="hidden", timeout=60_000)
    picker = page.get_by_role("combobox", name="Solar flow", exact=True)
    picker.wait_for(state="visible", timeout=15_000)
    picker.select_option("ground-physical", timeout=15_000)
    previous = None
    epoch = None
    for _ in range(3):
        rail.get_by_text("Checking Solar steps for the selected project drawing version.", exact=True).first.wait_for(
            state="hidden", timeout=60_000)
        current_epoch = _current_identity(page, observation)
        if epoch is not None and current_epoch != epoch:
            raise ValueError("canonical identity changed during observation")
        epoch = current_epoch
        selected = picker.locator("option:checked")
        sample = (picker.input_value(timeout=15_000), selected.inner_text(timeout=15_000),
                  rail.inner_text(timeout=15_000))
        if _current_identity(page, observation) != epoch:
            raise ValueError("canonical identity changed during observation")
        if sample[0] != "ground-physical" or "Checking Solar steps" in sample[2]:
            raise ValueError("ground catalog not ready")
        if sample == previous:
            observation["ground_option"] = safe_text(sample[1])
            observation["selected_labels"] = [safe_text(sample[1])]
            observation["ui_text"] = safe_text(sample[2])
            break
        previous = sample
    else:
        raise ValueError("ground catalog changed during observation")
    if _current_identity(page, observation) != epoch:
        raise ValueError("canonical identity changed before capture")

    def validate():
        if _current_identity(page, observation) != epoch:
            raise ValueError("canonical identity changed during capture")
        current = (picker.input_value(timeout=15_000),
                   picker.locator("option:checked").inner_text(timeout=15_000),
                   rail.inner_text(timeout=15_000))
        if (current != sample or current[0] != "ground-physical"
                or "Checking Solar steps" in current[2]
                or _current_identity(page, observation) != epoch):
            raise ValueError("ground catalog changed during capture")

    validate()
    _save(page, ctx, observation, validate=validate)


def observe(page, ctx, config, bound, session_info=None):
    observation = {"fresh_project": False, "imported_version_selected": False, "evidence_saved": False}
    stage = "session provenance"
    listeners = []
    try:
        for key in ("source", "web_build"):
            value = (session_info or {}).get(key)
            if (not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value)
                    or value != ctx.source_sha):
                raise ValueError("invalid session provenance")
        observation.update({key: session_info[key] for key in ("source", "web_build")})
        catalog = _catalog_observer(ctx.base, observation)
        project = _project_observer(ctx.base, observation)

        def callback(request):
            catalog(request)
            project(request)

        def response(response):
            catalog.response(response)
            project.response(response)

        def failed(request):
            catalog.failed(request)
            project.failed(request)

        for event, listener in (("request", callback), ("response", response),
                                ("requestfailed", failed)):
            page.on(event, listener)
            listeners.append((event, listener))
        stage = "workspace profile"
        _prepare_workspace(page)
        stage = "fresh project creation"
        _create_project(page, ctx, config, observation)
        stage = "primary drawing attachment and version selection"
        if _import_primary(page, ctx, config, bound, observation) is False:
            return observation
        stage = "selected drawing workflow navigation"
        _open_workflow(page, config)
        stage = "rendered Ground Physical evidence"
        _ground(page, ctx, observation)
    except Exception as exc:
        _diagnostic(ctx, observation, stage, exc)
    finally:
        cleanup_error = None
        for event, listener in listeners:
            try:
                page.remove_listener(event, listener)
            except Exception as exc:
                cleanup_error = cleanup_error or exc
        if cleanup_error is not None:
            _invalidate_evidence(ctx, observation)
            _diagnostic(ctx, observation, "listener cleanup", cleanup_error)
    return observation


def run(ctx):
    try:
        config, bound, _expected, total = bindings(ctx)
    except (ValueError, OSError, KeyError, TypeError, UnicodeError, InvalidOperation):
        return classify({"issue": "Missing or invalid fixture bindings/config, oracle or rates; browser not opened."})
    try:
        repo = Path(__file__).resolve().parents[3]
        with signed_in(repo, ctx.source_sha, origin=ctx.base) as (page, info):
            observation = observe(page, ctx, config, bound, session_info=info)
    except Exception as exc:
        observation = {}
        _diagnostic(ctx, observation, "authentication/browser session", exc)
    # Fixture arithmetic is retained as provenance only, never a measured outcome.
    observation["fixture_rate_total"] = str(total)
    return classify(observation, expected=_expected)
