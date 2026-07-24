"""da/client.py — APS Design Automation client for the Leaf web demo.

Implements the FROZEN §5 interface:
    auth_token() -> str                         # 2-legged, creds at ~/.aps/credentials.json
    extract(dwg_local_path) -> dict             # Intake JSON §1 (runs the extract WorkItem)
    run_tool(dwg_local_path, tool, params) -> dict   # Result envelope §3
plus the low-level primitive the task asked for:
    submit_workitem(activity_id, arguments) -> dict  # raw WorkItem status JSON

Engine work runs on APS Design Automation (AutoCAD). The EXTRACT Activity runs the
exact LISP from the proven local extractor (da/lisp.py) headless, emits the same
families text, and client.py parses it to Intake JSON IDENTICALLY (da/intake_parse.py).

DOCS USED (2026-07):
  Design Automation v3 overview / concepts:
    https://aps.autodesk.com/en/docs/design-automation/v3/developers_guide/overview/
  POST appbundles / activities / workitems (v3 HTTP reference):
    https://aps.autodesk.com/en/docs/design-automation/v3/reference/http/appbundles-POST/
    https://aps.autodesk.com/en/docs/design-automation/v3/reference/http/activities-POST/
    https://aps.autodesk.com/en/docs/design-automation/v3/reference/http/workitems-POST/
  Execute WorkItem tutorial (arguments: input verb get, output verb put):
    https://get-started.aps.autodesk.com/tutorials/design-automation/execute-workitem/
  AutoCAD tutorial tasks (bucket -> appbundle -> activity -> workitem):
    https://aps.autodesk.com/en/docs/design-automation/v3/tutorials/autocad/
  OSS Direct-to-S3 upload/download (signeds3upload / signeds3download):
    https://aps.autodesk.com/blog/data-management-oss-object-storage-service-migrating-direct-s3-approach
    https://aps.autodesk.com/en/docs/data/v2/tutorials/app-managed-bucket/

NOTHING in this module creates buckets/appbundles/activities/workitems unless you
call the LIVE high-level functions (extract / run_tool with dry_run=False). The
one-time provisioning ROOT must do is in da/setup_live.md.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.parse
from datetime import datetime, timezone

import requests

import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # resolve sibling imports when imported from elsewhere
from intake_parse import parse_text

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
APS = "https://developer.api.autodesk.com"
DA = f"{APS}/da/us-east/v3"                      # Design Automation region host
CRED_PATH = os.path.expanduser(os.environ.get("APS_CRED", "~/.aps/credentials.json"))
SCOPES = "code:all data:read data:write bucket:create bucket:read"
ENGINE = os.environ.get("APS_ENGINE", "Autodesk.AutoCAD+26_0")  # confirmed live 2026-07
OSS_REGION = os.environ.get("APS_OSS_REGION", "US")

# Names ROOT provisions once (see da/setup_live.md). Overridable via env.
EXTRACT_ACTIVITY = os.environ.get("APS_EXTRACT_ACTIVITY", "LeafExtract")
# Separate Activity for DXF input. A DA Activity's parameter localName is FIXED
# in the Activity definition and a WorkItem cannot override it, so one Activity
# cannot serve both extensions — hence a second one rather than a flag.
EXTRACT_DXF_ACTIVITY = os.environ.get("APS_EXTRACT_DXF_ACTIVITY", "LeafExtractDxf")
TOOL_ACTIVITY_PREFIX = os.environ.get("APS_TOOL_PREFIX", "LeafTool_")
ALIAS = os.environ.get("APS_ALIAS", "prod")

# Result localName the extract Activity uploads (matches da/lisp.OUT_LOCALNAME).
EXTRACT_RESULT_LOCALNAME = "result.txt"
TOOL_RESULT_LOCALNAME = "result.json"
HOSTDWG_LOCALNAME = "input.dwg"
# The extension is the WHOLE point: accoreconsole dispatches its reader on it.
# DXF bytes delivered as "input.dwg" are rejected outright ("Drawing file is not
# valid", ErrorStatus=434) — that was the live guest-upload bug, reproduced
# locally against a real AutoCAD 2026 accoreconsole on 2026-07-24.
HOSTDXF_LOCALNAME = "input.dxf"

_HTTP_TIMEOUT = 60


# --------------------------------------------------------------------------- #
# Auth (2-legged) — ports probe-aps.ps1 to Python
# --------------------------------------------------------------------------- #
_token_cache = {"access_token": None, "expires_at": 0.0}


def _load_creds() -> dict:
    # ECS injects the complete JSON object from Secrets Manager into this
    # process only. Local/operator runs keep using the established credential
    # file. Never log either source or its values.
    inline = os.environ.get("APS_CREDENTIALS_JSON", "").strip()
    if inline:
        try:
            c = json.loads(inline)
        except json.JSONDecodeError as exc:
            raise ValueError("APS_CREDENTIALS_JSON is not valid JSON") from exc
    else:
        if not os.path.exists(CRED_PATH):
            raise FileNotFoundError(f"APS creds missing: {CRED_PATH}")
        with open(CRED_PATH, encoding="utf-8") as handle:
            c = json.load(handle)
    if (
        not c.get("client_id")
        or c.get("client_id") == "PASTE_ME"
        or not c.get("client_secret")
        or c.get("client_secret") == "PASTE_ME"
    ):
        raise ValueError("APS credentials must include client_id and client_secret")
    return c


def auth_token() -> str:
    """Mint (and cache) a 2-legged APS token. Returns the bearer access_token."""
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]
    c = _load_creds()
    pair = f"{c['client_id']}:{c['client_secret']}"
    b64 = base64.b64encode(pair.encode()).decode()
    r = requests.post(
        f"{APS}/authentication/v2/token",
        headers={"Authorization": f"Basic {b64}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data=f"grant_type=client_credentials&scope={SCOPES}",
        timeout=_HTTP_TIMEOUT)
    r.raise_for_status()
    tok = r.json()
    _token_cache["access_token"] = tok["access_token"]
    _token_cache["expires_at"] = now + float(tok.get("expires_in", 3600))
    return tok["access_token"]


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {auth_token()}"}


def nickname() -> str:
    """App nickname (owner id) used to qualify activity ids. Cached per process."""
    if not hasattr(nickname, "_v"):
        r = requests.get(f"{DA}/forgeapps/me", headers=_auth_headers(), timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        nickname._v = r.text.strip().strip('"')
    return nickname._v


def bucket_key() -> str:
    """Deterministic bucket key for the PERSISTENT drawing store.

    Stem is `leaf-web-store-` (was `leaf-web-demo-` for the old TRANSIENT bucket).
    OSS bucket policy is immutable at creation, so the persistent store gets a
    FRESH key rather than colliding (409) with the abandoned transient bucket —
    the old `leaf-web-demo-*` bucket is intentionally left to expire on its own.
    Override with APS_BUCKET. Bucket keys: 3-128 chars, [-_.a-z0-9].
    """
    if os.environ.get("APS_BUCKET"):
        return os.environ["APS_BUCKET"]
    c = _load_creds()
    # derive a stable suffix from the client id
    suffix = "".join(ch for ch in c["client_id"].lower() if ch.isalnum())[:16]
    return f"leaf-web-store-{suffix}"


def activity_qualified(name: str) -> str:
    """owner.Activity+alias  (e.g. iBZF....LeafExtract+prod)."""
    return f"{nickname()}.{name}+{ALIAS}"


# --------------------------------------------------------------------------- #
# Per-tenant ephemeral (per-run scratch) object keys — ADDITIVE.
#
# tenant_id=None reproduces today's exact single-tenant keys (`in/...`, `out/...`)
# BYTE-FOR-BYTE. A tenant scopes throwaway run keys under `t/<tenant>/` (see
# da/tenant.py). This is a DIFFERENT namespace from the persistent versioned
# drawing store (da/store.py: `tenants/<t>/drawings/...`) — no collision.
# --------------------------------------------------------------------------- #
def _ephemeral_prefix(tenant_id: str | None) -> str:
    if tenant_id is None:
        return ""
    import tenant as _tenant  # sibling da/tenant.py — pure, no network/creds
    return _tenant.tenant_key_prefix(tenant_id)


def ephemeral_input_key(dwg_name: str, tenant_id: str | None = None,
                        ts: int | None = None) -> str:
    """Per-run throwaway INPUT key. None -> `in/<ts>_<dwg>` (unchanged);
    tenant -> `t/<tenant>/in/<ts>_<dwg>`."""
    ts = int(time.time()) if ts is None else int(ts)
    return f"{_ephemeral_prefix(tenant_id)}in/{ts}_{dwg_name}"


def ephemeral_output_key(name: str, tenant_id: str | None = None,
                         ts: int | None = None, suffix: str = ".result.json") -> str:
    """Per-run throwaway OUTPUT key. None -> `out/<ts>_<name><suffix>` (unchanged);
    tenant -> `t/<tenant>/out/<ts>_<name><suffix>`."""
    ts = int(time.time()) if ts is None else int(ts)
    return f"{_ephemeral_prefix(tenant_id)}out/{ts}_{name}{suffix}"


# lazy loader for the sibling submit QUEUE (da/queue.py). Loaded by PATH under a
# distinct module name so it never depends on `import queue` resolving to our
# module (the stdlib `queue` may already be cached in this process).
_leaf_queue_mod = None


def _leaf_queue():
    global _leaf_queue_mod
    if _leaf_queue_mod is None:
        import importlib.util
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "queue.py")
        spec = importlib.util.spec_from_file_location("leaf_aps_queue", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        _leaf_queue_mod = mod
    return _leaf_queue_mod


# --------------------------------------------------------------------------- #
# OSS (Object Storage) — Direct-to-S3 upload / download
# --------------------------------------------------------------------------- #
def _enc(object_key: str) -> str:
    """URL-encode an OSS object key for use as a single path segment.

    Keys like 'in/1784319563_rooftop_demo.dwg' contain '/', which OSS requires
    percent-encoded in the URL path (otherwise the route 404s).
    """
    return urllib.parse.quote(object_key, safe="")


def _oss_rest_url(object_key: str) -> str:
    """The OSS REST object URL DA resolves natively (with a Bearer header)."""
    return f"{APS}/oss/v2/buckets/{bucket_key()}/objects/{_enc(object_key)}"


def create_bucket(policy: str = "persistent") -> dict:
    """Create the OSS bucket (idempotent-ish: 409 = already exists). LIVE call.

    Default policy is PERSISTENT (objects do NOT expire) — this bucket backs the
    per-tenant, per-drawing versioned drawing store. The old TRANSIENT bucket is
    abandoned (OSS policy is immutable at creation; a fresh key is used instead).
    """
    body = {"bucketKey": bucket_key(), "policyKey": policy}
    r = requests.post(
        f"{APS}/oss/v2/buckets",
        headers={**_auth_headers(), "Content-Type": "application/json",
                 "x-ads-region": OSS_REGION},
        data=json.dumps(body),
        timeout=_HTTP_TIMEOUT)
    if r.status_code == 409:
        return {"bucketKey": bucket_key(), "existed": True}
    r.raise_for_status()
    return r.json()


def upload_object(local_path: str, object_key: str, tenant_id: str | None = None) -> str:
    """Upload a local file to OSS via signed S3 (3-step). Returns the OSS object id.

    LIVE call. Uses a single-part signed upload (demo DWGs are small). ADDITIVE:
    if `tenant_id` is given and `object_key` is not already tenant-scoped, the key
    is prefixed with `t/<tenant>/` so the upload lands in the tenant's scratch
    namespace. Existing callers pass a fully-built key and no tenant_id -> byte
    identical behavior.
    """
    if tenant_id is not None and not object_key.startswith("t/"):
        object_key = _ephemeral_prefix(tenant_id) + object_key
    H = _auth_headers()
    # 1) request a signed S3 upload url
    g = requests.get(
        f"{APS}/oss/v2/buckets/{bucket_key()}/objects/{_enc(object_key)}/signeds3upload",
        headers=H, timeout=_HTTP_TIMEOUT)
    g.raise_for_status()
    j = g.json()
    upload_key = j["uploadKey"]
    put_url = j["urls"][0]
    # 2) PUT the bytes directly to S3 (no auth header on the signed url)
    with open(local_path, "rb") as fh:
        p = requests.put(put_url, data=fh, timeout=300)
    p.raise_for_status()
    # 3) finalize with OSS so the object is registered
    f = requests.post(
        f"{APS}/oss/v2/buckets/{bucket_key()}/objects/{_enc(object_key)}/signeds3upload",
        headers={**H, "Content-Type": "application/json"},
        data=json.dumps({"uploadKey": upload_key}),
        timeout=_HTTP_TIMEOUT)
    f.raise_for_status()
    return f.json().get("objectId", _oss_rest_url(object_key))


def download_object(object_key: str) -> bytes:
    """Download an OSS object via signed S3 download. Returns raw bytes. LIVE call."""
    H = _auth_headers()
    g = requests.get(
        f"{APS}/oss/v2/buckets/{bucket_key()}/objects/{_enc(object_key)}/signeds3download",
        headers=H, timeout=_HTTP_TIMEOUT)
    g.raise_for_status()
    url = g.json()["url"]
    d = requests.get(url, timeout=300)
    d.raise_for_status()
    return d.content


def signed_download_url(object_key: str, minutes: int = 60) -> str:
    """A self-authenticating presigned S3 GET url DA can fetch (no Bearer header).

    The legacy /oss/v2/.../objects/<key> direct GET is deprecated (403); DA must be
    handed a signed S3 url instead.
    """
    g = requests.get(
        f"{APS}/oss/v2/buckets/{bucket_key()}/objects/{_enc(object_key)}"
        f"/signeds3download?minutesExpiration={minutes}",
        headers=_auth_headers(), timeout=_HTTP_TIMEOUT)
    g.raise_for_status()
    return g.json()["url"]


def signed_upload_url(object_key: str, minutes: int = 60) -> tuple[str, str]:
    """(uploadKey, presigned S3 PUT url) DA can PUT its output to. Finalize after."""
    g = requests.get(
        f"{APS}/oss/v2/buckets/{bucket_key()}/objects/{_enc(object_key)}"
        f"/signeds3upload?minutesExpiration={minutes}",
        headers=_auth_headers(), timeout=_HTTP_TIMEOUT)
    g.raise_for_status()
    j = g.json()
    return j["uploadKey"], j["urls"][0]


def finalize_upload(object_key: str, upload_key: str) -> dict:
    """Register a signed-S3-PUT object with OSS so it can be downloaded back."""
    f = requests.post(
        f"{APS}/oss/v2/buckets/{bucket_key()}/objects/{_enc(object_key)}/signeds3upload",
        headers={**_auth_headers(), "Content-Type": "application/json"},
        data=json.dumps({"uploadKey": upload_key}), timeout=_HTTP_TIMEOUT)
    f.raise_for_status()
    return f.json()


# --------------------------------------------------------------------------- #
# WorkItem argument builders
# --------------------------------------------------------------------------- #
def _input_arg(object_key: str, *, live: bool = True) -> dict:
    """Input argument referencing an OSS object; DA GETs it with the Bearer token.

    live=False builds the identical body shape WITHOUT minting a token (used by
    dry_run so a dry run makes no live call — the header is redacted anyway).
    """
    auth = f"Bearer {auth_token()}" if live else "Bearer <minted at run time>"
    return {"url": _oss_rest_url(object_key), "verb": "get",
            "headers": {"Authorization": auth}}


def _output_arg(object_key: str, *, live: bool = True) -> dict:
    """Output argument; DA PUTs the produced file to the OSS object."""
    auth = f"Bearer {auth_token()}" if live else "Bearer <minted at run time>"
    return {"url": _oss_rest_url(object_key), "verb": "put",
            "headers": {"Authorization": auth}}


def _json_arg(obj) -> dict:
    """Inline JSON argument via data: URI (DA saves it as a local JSON file)."""
    return {"url": "data:application/json," + json.dumps(obj, separators=(",", ":"))}


def _redact(obj):
    """Deep-copy with Authorization bearer tokens masked (for safe dry-run printing)."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "Authorization" and isinstance(v, str) and v.startswith("Bearer "):
                out[k] = "Bearer <REDACTED 2-legged token, minted at run time>"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


# --------------------------------------------------------------------------- #
# WorkItem submit + poll (low level)
# --------------------------------------------------------------------------- #
def submit_workitem(activity_id: str, arguments: dict,
                    dry_run: bool = False, poll: bool = True,
                    tenant_id: str | None = None) -> dict:
    """Submit a WorkItem for `activity_id` with `arguments`.

    dry_run=True  -> return the exact POST body WITHOUT calling APS.
    dry_run=False -> POST, then (poll=True) poll to a terminal status and return it.

    The LIVE submit (dry_run=False) is the head-of-line-blocking point, so it is
    routed through the FAIR account Flex gate (da/queue.fair_admit): at most
    APS_MAX_CONCURRENCY WorkItems are in flight across all tenants in this
    process, AND slots go round-robin per tenant so none can starve another.
    With poll=True the slot is held for the WorkItem's whole lifetime
    (submit -> terminal), which is exactly what the ceiling should count. dry_run
    returns BEFORE the gate, so APS_LIVE=0 / dry-run paths are unaffected.
    """
    body = {"activityId": activity_id, "arguments": arguments}
    if dry_run:
        return {"_dry_run": True, "endpoint": f"POST {DA}/workitems",
                "body": _redact(body)}
    _q = _leaf_queue()
    with _q.fair_admit(tenant_id):
        r = requests.post(f"{DA}/workitems",
                          headers={**_auth_headers(), "Content-Type": "application/json"},
                          data=json.dumps(body), timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        wi = r.json()
        if not poll:
            return wi
        return _poll_workitem(wi["id"])


def cancel_workitem(workitem_id: str, dry_run: bool = False) -> dict:
    """Cancel a running WorkItem: DELETE /workitems/{id} (orphan reaping).

    dry_run=True returns the request descriptor WITHOUT calling APS. Otherwise a
    LIVE DELETE is issued (reachable only when a caller explicitly asks — the
    reaper does so only behind APS_LIVE + BROKER_REAP_LIVE). 404 is tolerated
    (the WorkItem already finished/vanished). Mirrors submit_workitem's dry_run
    discipline so no unguarded live call exists.
    """
    endpoint = f"{DA}/workitems/{workitem_id}"
    if dry_run:
        return {"_dry_run": True, "endpoint": f"DELETE {endpoint}",
                "workitem_id": workitem_id}
    r = requests.delete(endpoint, headers=_auth_headers(), timeout=_HTTP_TIMEOUT)
    cancelled = r.status_code in (200, 202, 204, 404)
    return {"workitem_id": workitem_id, "status_code": r.status_code,
            "cancelled": cancelled}


def _poll_workitem(workitem_id: str, timeout_s: int = 900, interval_s: float = 2.0) -> dict:
    t0 = time.time()
    while True:
        r = requests.get(f"{DA}/workitems/{workitem_id}",
                         headers=_auth_headers(), timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        st = r.json()
        status = st.get("status", "")
        if status not in ("pending", "inprogress"):
            return st
        if time.time() - t0 > timeout_s:
            st["_timeout"] = True
            return st
        time.sleep(interval_s)


def _engine_seconds(status: dict) -> float | None:
    """Best-effort engine seconds from a WorkItem status 'stats' block."""
    stats = status.get("stats") or {}
    # DA reports timeQueued/timeDownloadStarted/timeInstructionsStarted/timeInstructionsEnded/timeUploadEnded
    a = stats.get("timeInstructionsStarted")
    b = stats.get("timeInstructionsEnded")
    if a and b:
        try:
            fa = datetime.fromisoformat(a.replace("Z", "+00:00"))
            fb = datetime.fromisoformat(b.replace("Z", "+00:00"))
            return round((fb - fa).total_seconds(), 2)
        except Exception:
            return None
    return None


# --------------------------------------------------------------------------- #
# High-level §5 interface
# --------------------------------------------------------------------------- #
def _resolve_store_key(tenant_id: str, drawing_id: str, version, backend, dry_run: bool):
    """Resolve a (version_int, store_object_key) for the version-aware code path.

    - With a backend (or LIVE with no backend -> default OSSBackend): consult the
      real manifest via store.resolve_version (so "head"/"latest" mean what they say).
    - dry_run with no backend (offline body preview): int/digit versions resolve
      directly; "head"/"latest" fall back to v1 as a body-shape placeholder (the
      key still matches the versioned scheme, /v/00000001.dwg).
    """
    import store as _store  # lazy (avoids import cycle: store imports client)
    if backend is None and not dry_run:
        backend = _store.OSSBackend()
    if backend is not None:
        return _store.resolve_version(backend, tenant_id, drawing_id, version)
    if isinstance(version, int):
        v = version
    elif str(version).isdigit():
        v = int(version)
    else:  # "head"/"latest" without a manifest to consult
        v = 1
    return v, _store.drawing_version_key(tenant_id, drawing_id, v)


def extract_activity_for(local_path: str) -> str:
    """Un-aliased Activity id for this input's extension.

    `.dxf` -> the DXF twin; anything else -> the DWG Activity. Case-insensitive
    because the staged name comes from a user-supplied filename.
    """
    return (EXTRACT_DXF_ACTIVITY
            if os.path.splitext(local_path)[1].lower() == ".dxf"
            else EXTRACT_ACTIVITY)


def extract(dwg_local_path: str, dry_run: bool = False, *,
            tenant_id: str | None = None, drawing_id: str | None = None,
            version="head", backend=None) -> dict:
    """Run the extract WorkItem on APS and return Intake JSON (§1).

    FROZEN §5: extract(dwg_local_path) still works exactly as before. The
    tenant_id/drawing_id/version/backend kwargs are purely ADDITIVE: when
    tenant_id AND drawing_id are supplied, the persistent, versioned store object
    is referenced as HostDwg (no throwaway re-upload); otherwise the legacy
    single-tenant demo path (upload a throwaway `in/<ts>_` object) is used.

    dry_run=True returns the WorkItem submission body WITHOUT any live call.

    The Activity is chosen by the INPUT EXTENSION: a DA Activity's parameter
    localName is fixed at definition time, and accoreconsole dispatches its
    reader off that name's extension, so DXF must go to the DXF twin or it is
    rejected as an invalid drawing. Everything else about the run is identical.
    """
    activity_id = activity_qualified(extract_activity_for(dwg_local_path))
    version_aware = tenant_id is not None and drawing_id is not None
    dwg_name = os.path.basename(dwg_local_path)
    # per-run scratch OUTPUT key; tenant-scoped when a tenant is supplied
    output_key = f"{_ephemeral_prefix(tenant_id)}out/{int(time.time())}_{dwg_name}.families.txt"

    if version_aware:
        v, input_key = _resolve_store_key(tenant_id, drawing_id, version, backend, dry_run)
        if dry_run:
            arguments = {"HostDwg": _input_arg(input_key, live=False),
                         "Result": _output_arg(output_key, live=False)}
            wi = submit_workitem(activity_id, arguments, dry_run=True)
            return {
                "_dry_run": True,
                "note": "extract: version-aware; references stored version, no re-upload",
                "engine": ENGINE,
                "activityId": activity_id,
                "tenant_id": tenant_id, "drawing_id": drawing_id, "store_version": v,
                "input_object": input_key,
                "output_object": output_key,
                "workitem": wi,
            }
        # LIVE version-aware — reference the persistent version's signed url (no upload)
        in_url = signed_download_url(input_key)
        up_key, out_url = signed_upload_url(output_key)
        arguments = {"HostDwg": {"url": in_url, "verb": "get"},
                     "Result": {"url": out_url, "verb": "put"}}
        status = submit_workitem(activity_id, arguments, dry_run=False, poll=True,
                                 tenant_id=tenant_id)
        if status.get("status") != "success":
            raise RuntimeError(f"extract WorkItem {status.get('id')} status={status.get('status')} "
                               f"report={status.get('reportUrl')}")
        finalize_upload(output_key, up_key)
        families = download_object(output_key).decode("utf-8", "replace")
        return parse_text(families, dwg_local_path)

    # ------- legacy single-tenant demo path (FROZEN behavior) -------
    input_key = f"{_ephemeral_prefix(tenant_id)}in/{int(time.time())}_{dwg_name}"

    if dry_run:
        arguments = {"HostDwg": _input_arg(input_key, live=False),
                     "Result": _output_arg(output_key, live=False)}
        wi = submit_workitem(activity_id, arguments, dry_run=True)
        return {
            "_dry_run": True,
            "note": "extract: no bucket/upload/workitem performed",
            "engine": ENGINE,
            "activityId": activity_id,
            "input_object": input_key,
            "output_object": output_key,
            "workitem": wi,
        }

    # LIVE path — hand DA signed S3 urls (legacy OSS REST GET/PUT is deprecated -> 403)
    upload_object(dwg_local_path, input_key)
    in_url = signed_download_url(input_key)
    up_key, out_url = signed_upload_url(output_key)
    arguments = {"HostDwg": {"url": in_url, "verb": "get"},
                 "Result": {"url": out_url, "verb": "put"}}
    status = submit_workitem(activity_id, arguments, dry_run=False, poll=True,
                             tenant_id=tenant_id)
    if status.get("status") != "success":
        raise RuntimeError(f"extract WorkItem {status.get('id')} status={status.get('status')} "
                           f"report={status.get('reportUrl')}")
    finalize_upload(output_key, up_key)
    families = download_object(output_key).decode("utf-8", "replace")
    return parse_text(families, dwg_local_path)


def run_tool(dwg_local_path: str, tool: dict, params: dict,
             dry_run: bool = False, *,
             tenant_id: str | None = None, drawing_id: str | None = None,
             version="head", backend=None) -> dict:
    """Run a tool (§2 package) on APS and return a Result envelope (§3).

    FROZEN §5: run_tool(dwg_local_path, tool, params) is unchanged. The
    tenant_id/drawing_id/version/backend kwargs are purely ADDITIVE: when
    tenant_id AND drawing_id are supplied, HostDwg references the persistent,
    versioned store object instead of re-uploading a throwaway `in/<ts>_` object.
    The Result output stays ephemeral (`out/<ts>_...result.json`) for read tools;
    only the write path (out of scope) turns a result into a new store version.

    Convention: each tool has a DA Activity named {TOOL_ACTIVITY_PREFIX}{engine_op}.
    """
    engine_op = tool.get("engine_op") or tool["name"].replace("-", "_")
    activity_id = activity_qualified(f"{TOOL_ACTIVITY_PREFIX}{engine_op}")
    dwg_name = os.path.basename(dwg_local_path)
    ts = int(time.time())
    # per-run scratch OUTPUT key; tenant-scoped when a tenant is supplied
    output_key = ephemeral_output_key(engine_op, tenant_id, ts)
    version_aware = tenant_id is not None and drawing_id is not None

    if version_aware:
        v, input_key = _resolve_store_key(tenant_id, drawing_id, version, backend, dry_run)
    else:
        input_key = ephemeral_input_key(dwg_name, tenant_id, ts)

    if dry_run:
        arguments = {
            "HostDwg": _input_arg(input_key, live=False),
            "Params": _json_arg(params or {}),
            "Result": _output_arg(output_key, live=False),
        }
        wi = submit_workitem(activity_id, arguments, dry_run=True)
        out = {
            "_dry_run": True,
            "note": ("run_tool: version-aware; references stored version, no re-upload"
                     if version_aware else "run_tool: no bucket/upload/workitem performed"),
            "engine": ENGINE,
            "tool": tool.get("name"),
            "activityId": activity_id,
            "input_object": input_key,
            "output_object": output_key,
            "workitem": wi,
        }
        if version_aware:
            out.update({"tenant_id": tenant_id, "drawing_id": drawing_id, "store_version": v})
        return out

    # LIVE path — signed S3 urls for input/output; Params stays an inline data: uri
    t0 = time.time()
    if version_aware:
        # input_key already resolved to the persistent version above (no re-upload)
        in_url = signed_download_url(input_key)
    else:
        upload_object(dwg_local_path, input_key)
        in_url = signed_download_url(input_key)
    up_key, out_url = signed_upload_url(output_key)
    arguments = {
        "HostDwg": {"url": in_url, "verb": "get"},
        "Params": _json_arg(params or {}),
        "Result": {"url": out_url, "verb": "put"},
    }
    status = submit_workitem(activity_id, arguments, dry_run=False, poll=True,
                             tenant_id=tenant_id)
    timing_ms = int((time.time() - t0) * 1000)
    if status.get("status") != "success":
        return {
            "ok": False, "tool": tool.get("name"), "version": tool.get("version"),
            "result": None, "overlay": None, "timing_ms": timing_ms, "cost": None,
            "error": f"WorkItem {status.get('id')} status={status.get('status')} "
                     f"report={status.get('reportUrl')}",
        }
    finalize_upload(output_key, up_key)
    raw = download_object(output_key).decode("utf-8", "replace")
    try:
        payload = json.loads(raw)
    except Exception as e:
        return {"ok": False, "tool": tool.get("name"), "version": tool.get("version"),
                "result": None, "overlay": None, "timing_ms": timing_ms, "cost": None,
                "error": f"result.json not JSON: {e}: {raw[:200]}"}
    eng_s = _engine_seconds(status)
    # If the tool already emitted a full envelope, trust its result/overlay/error.
    result = payload.get("result", payload if "ok" not in payload else payload.get("result"))
    overlay = payload.get("overlay")
    return {
        "ok": True,
        "tool": tool.get("name"),
        "version": tool.get("version"),
        "result": result,
        "overlay": overlay,
        "timing_ms": timing_ms,
        "cost": None if eng_s is None else {
            "engine_seconds": eng_s,
            "usd_est": round(eng_s / 3600.0 * float(os.environ.get("APS_USD_PER_HR", "10")), 4),
        },
        "error": None,
    }


# --------------------------------------------------------------------------- #
# Activity definitions ROOT provisions once (used by setup_live.md / dry-run print)
# --------------------------------------------------------------------------- #
def extract_activity_spec() -> dict:
    """The POST /activities body for the extract Activity (pure-LISP, no appbundle)."""
    from lisp import build_scr, OUT_LOCALNAME
    return {
        "id": EXTRACT_ACTIVITY,
        "engine": ENGINE,
        "commandLine": [
            r'$(engine.path)\accoreconsole.exe /i "$(args[HostDwg].path)" '
            r'/s "$(settings[script].path)"'
        ],
        "parameters": {
            "HostDwg": {"verb": "get", "required": True, "localName": HOSTDWG_LOCALNAME},
            "Result": {"verb": "put", "required": True, "localName": OUT_LOCALNAME},
        },
        "settings": {"script": {"value": build_scr()}},
        "description": "Leaf headless DWG intake extraction (LISP families dump).",
    }


def extract_dxf_activity_spec() -> dict:
    """POST /activities body for the DXF twin of the extract Activity.

    Identical engine, command line and LISP body to extract_activity_spec —
    same fidelity, same Result contract — differing in exactly two places, both
    forced by measurements against a real accoreconsole (see da/lisp.py):

      * HostDwg localName is `input.dxf`, so accoreconsole picks its DXF reader
        instead of rejecting the bytes as an invalid drawing (ErrorStatus=434).
      * the script ends with lisp.QUIT_SAVED, because a DXF-opened drawing has
        no source .dwg to save back to and a plain QUIT blocks on a SAVEAS
        prompt until the WorkItem times out — discarding a result the LISP had
        already written.

    The parameter is still NAMED HostDwg: renaming it would change the WorkItem
    argument contract for no gain, and the name is only a key.
    """
    from lisp import build_scr, OUT_LOCALNAME, QUIT_SAVED
    return {
        "id": EXTRACT_DXF_ACTIVITY,
        "engine": ENGINE,
        "commandLine": [
            r'$(engine.path)\accoreconsole.exe /i "$(args[HostDwg].path)" '
            r'/s "$(settings[script].path)"'
        ],
        "parameters": {
            "HostDwg": {"verb": "get", "required": True, "localName": HOSTDXF_LOCALNAME},
            "Result": {"verb": "put", "required": True, "localName": OUT_LOCALNAME},
        },
        "settings": {"script": {"value": build_scr(quit_form=QUIT_SAVED)}},
        "description": "Leaf headless DXF intake extraction (LISP families dump).",
    }


def tool_activity_spec(tool: dict) -> dict:
    """POST /activities body for a tool's LeafTool_<engine_op> Activity.

    Built from the tool's LISP source: `engine_script` if present, else the
    contents of its `.lsp` `script`. Pure-LISP via accoreconsole, same shape as
    the extract Activity, plus a Params (get) input for the tool's arguments.

    A relative `.lsp` `script` path resolves against the PROJECT ROOT ONLY, so
    registries must declare root-relative paths (engine/registry.json declares
    `engine/tools/<name>.lsp`). Deliberately no fallback root: one would let a
    tool from one registry silently load another registry's script on a path
    collision. An unresolvable path yields an EMPTY script (never a raise) so
    live-path guards can fail closed on it.
    """
    engine_op = tool.get("engine_op") or tool["name"].replace("-", "_")
    script_src = tool.get("engine_script")
    if not script_src:
        sp = tool.get("script")
        if sp and str(sp).endswith(".lsp"):
            base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            path = sp if os.path.isabs(sp) else os.path.join(base, sp)
            try:
                with open(path, encoding="utf-8") as fh:
                    script_src = fh.read()
            except OSError:
                script_src = ""
    return {
        "id": f"{TOOL_ACTIVITY_PREFIX}{engine_op}",
        "engine": ENGINE,
        "commandLine": [
            r'$(engine.path)\accoreconsole.exe /i "$(args[HostDwg].path)" '
            r'/s "$(settings[script].path)"'
        ],
        "parameters": {
            "HostDwg": {"verb": "get", "required": True, "localName": HOSTDWG_LOCALNAME},
            "Params": {"verb": "get", "required": False, "localName": "params.json"},
            "Result": {"verb": "put", "required": True, "localName": TOOL_RESULT_LOCALNAME},
        },
        "settings": {"script": {"value": script_src or ""}},
        "description": f"Leaf authored tool {tool.get('name')} (engine_op={engine_op}).",
    }


def ensure_tool_activity(tool: dict, dry_run: bool = False) -> dict:
    """Idempotently provision the tool's DA Activity + `prod` alias. LIVE call.

    Called before submitting a WorkItem for a tool whose Activity may not exist
    yet (e.g. a newly authored tool). 409 on POST /activities == already exists.
    ADDITIVE: does not change any frozen §5 signature (auth_token/extract/run_tool).
    """
    engine_op = tool.get("engine_op") or tool["name"].replace("-", "_")
    activity_id = f"{TOOL_ACTIVITY_PREFIX}{engine_op}"
    spec = tool_activity_spec(tool)
    if dry_run:
        return {"_dry_run": True, "endpoint": f"POST {DA}/activities",
                "activity": activity_id, "alias": ALIAS, "body": spec}
    headers = {**_auth_headers(), "Content-Type": "application/json"}
    r = requests.post(f"{DA}/activities", headers=headers, data=json.dumps(spec),
                      timeout=_HTTP_TIMEOUT)
    if r.status_code == 409:
        created = False  # already exists
    else:
        r.raise_for_status()
        created = True
    # ensure the `prod` alias -> version 1 (409 == alias already exists)
    a = requests.post(f"{DA}/activities/{activity_id}/aliases", headers=headers,
                      data=json.dumps({"id": ALIAS, "version": 1}), timeout=_HTTP_TIMEOUT)
    alias_ok = a.status_code in (200, 201, 409)
    return {"activity": activity_id, "created": created, "alias": ALIAS, "alias_ok": alias_ok}


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()
