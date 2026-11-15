"""Manifest validation, evidence collection, and atomic grading receipts."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_UNREADABLE = 4
EXIT_USAGE = 5
BENCH_IDS = ("X01", "X02", "X03", "X04", "X05", "X06", "X07", "X08", "X09", "X10")
MANIFEST_SCHEMA = "leaf.xbench.manifest.v1"
GRADE_SCHEMA = "leaf.xbench.grade.v1"
MAX_MANIFEST_BYTES = 1_048_576
MAX_FIXTURES = 200
HASH_CHUNK = 1_048_576
HEALTH_USER_AGENT = "Mozilla/5.0 leaf-xbench-grader"
_VERDICTS = ("PASS", "FAIL", "UNREADABLE")


class ManifestError(ValueError):
    """The manifest cannot be read or does not satisfy the contract."""


class HealthError(RuntimeError):
    """Staging did not provide a usable source SHA."""


def _hex(value, length):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{%d}" % length, value) is not None


@dataclass(frozen=True)
class Step:
    n: int
    verdict: str
    evidence: str
    observed: object = None
    expected: object = None

    def __post_init__(self):
        if type(self.n) is not int or self.n < 1:
            raise ValueError("step n must be an integer >= 1")
        if self.verdict not in _VERDICTS:
            raise ValueError("invalid step verdict")
        object.__setattr__(self, "evidence", self.evidence[:2000])


def load_manifest(path):
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_MANIFEST_BYTES + 1)
    except OSError as exc:
        raise ManifestError(str(exc)) from exc
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ManifestError("manifest exceeds size limit")
    try:
        manifest = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ManifestError("invalid manifest JSON") from exc
    if not isinstance(manifest, dict):
        raise ManifestError("manifest must be an object")
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ManifestError("invalid manifest schema")
    if manifest.get("bench") not in BENCH_IDS:
        raise ManifestError("invalid manifest bench")
    fixtures = manifest.get("fixtures")
    if not isinstance(fixtures, list) or len(fixtures) > MAX_FIXTURES:
        raise ManifestError("invalid fixtures list")
    ids = set()
    for fixture in fixtures:
        if not isinstance(fixture, dict) or not (
            isinstance(fixture.get("id"), str)
            and isinstance(fixture.get("path"), str)
            and type(fixture.get("bytes")) is int
            and fixture["bytes"] >= 0
            and _hex(fixture.get("sha256"), 64)
        ):
            raise ManifestError("invalid fixture")
        if fixture["id"] in ids:
            raise ManifestError("duplicate fixture id: " + fixture["id"])
        ids.add(fixture["id"])
    manifest["manifest_sha256"] = hashlib.sha256(raw).hexdigest()
    return manifest


def verify_fixtures(manifest):
    rows = []
    for fixture in manifest["fixtures"]:
        row = {key: fixture[key] for key in ("id", "path", "bytes", "sha256")}
        row.update(ok=False, reason=None, observed_bytes=None, observed_sha256=None)
        path = Path(fixture["path"])
        try:
            if not path.is_file():
                row["reason"] = "missing"
            else:
                row["observed_bytes"] = path.stat().st_size
                if row["observed_bytes"] != fixture["bytes"]:
                    row["reason"] = "bytes_mismatch"
                else:
                    digest = hashlib.sha256()
                    with path.open("rb") as stream:
                        for chunk in iter(lambda: stream.read(HASH_CHUNK), b""):
                            digest.update(chunk)
                    row["observed_sha256"] = digest.hexdigest()
                    if row["observed_sha256"] != fixture["sha256"]:
                        row["reason"] = "sha256_mismatch"
                    else:
                        row["ok"] = True
        except OSError:
            row["reason"] = "missing"
        rows.append(row)
    return rows


def aggregate(steps):
    steps = list(steps)
    if len({step.n for step in steps}) != len(steps):
        raise ValueError("duplicate step n")
    verdicts = {step.verdict for step in steps}
    if "FAIL" in verdicts:
        return "FAIL"
    if not steps or "UNREADABLE" in verdicts:
        return "UNREADABLE"
    return "PASS"


def exit_code(verdict):
    if verdict not in _VERDICTS:
        raise ValueError("invalid verdict")
    return {"PASS": EXIT_PASS, "FAIL": EXIT_FAIL, "UNREADABLE": EXIT_UNREADABLE}[verdict]


def read_health(base, timeout=10.0, opener=None):
    try:
        url = base.rstrip("/") + "/api/health"
        if opener is None:
            request = Request(url, headers={"User-Agent": HEALTH_USER_AGENT,
                                            "Accept": "application/json"})
            with urlopen(request, timeout=timeout) as response:
                raw = response.read(1_048_576)
        else:
            raw = opener(url, timeout)
        payload = json.loads(raw)
        sha = payload.get("source_sha") if isinstance(payload, dict) else None
        if not _hex(sha, 40):
            raise ValueError("source_sha must be 40 lowercase hex characters")
        return sha
    except Exception as exc:
        raise HealthError(str(exc)) from exc


def validate_base(base):
    try:
        parsed = urlsplit(base)
        port = parsed.port
        if (not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or any(char.isspace() for char in base)):
            raise ValueError("invalid base URL")
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")
        ):
            raise ValueError("base must use HTTPS or local HTTP")
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("invalid base port")
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("invalid base URL: " + str(base)) from exc
    return base.rstrip("/")


def write_json_atomic(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(obj, stream, indent=2, sort_keys=False, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def _json_value(value):
    try:
        json.dumps(value)
    except (TypeError, ValueError, OverflowError):
        return repr(value)[:2000]
    return value


def write_grade(out_dir, *, bench, verdict, source_sha, manifest_sha256,
                started_at, finished_at, steps, fixtures, limits=""):
    if bench not in BENCH_IDS or verdict not in _VERDICTS:
        raise ValueError("invalid bench or verdict")
    if not _hex(source_sha, 40) and not (source_sha is None and verdict == "UNREADABLE"):
        raise ValueError("invalid source_sha")
    payload = {
        "schema": GRADE_SCHEMA,
        "bench": bench,
        "verdict": verdict,
        "source_sha": source_sha,
        "manifest_sha256": manifest_sha256,
        "started_at": started_at,
        "finished_at": finished_at,
        "steps": [dict(n=step.n, verdict=step.verdict, evidence=step.evidence,
                       observed=_json_value(step.observed), expected=_json_value(step.expected))
                  for step in sorted(steps, key=lambda step: step.n)],
        "fixtures": fixtures,
        "limits": limits,
    }
    return write_json_atomic(Path(out_dir) / "grade.json", payload)


def write_readiness(readiness_dir, *, bench, sha, receipt):
    if bench not in BENCH_IDS or not _hex(sha, 40):
        raise ValueError("invalid bench or sha")
    return write_json_atomic(Path(readiness_dir) / (bench + ".json"), {
        "bench": bench, "sha": sha, "verdict": "READINESS_PASS",
        "receipt": str(receipt), "recorded_at": utc_now(),
    })


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class BenchContext:
    bench: str
    manifest: dict
    base: str
    out_dir: Path
    source_sha: str
    fixtures: list
