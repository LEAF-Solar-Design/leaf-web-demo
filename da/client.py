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
TOOL_ACTIVITY_PREFIX = os.environ.get("APS_TOOL_PREFIX", "LeafTool_")
ALIAS = os.environ.get("APS_ALIAS", "prod")

# Result localName the extract Activity uploads (matches da/lisp.OUT_LOCALNAME).
EXTRACT_RESULT_LOCALNAME = "result.txt"
TOOL_RESULT_LOCALNAME = "result.json"
HOSTDWG_LOCALNAME = "input.dwg"

_HTTP_TIMEOUT = 60


# --------------------------------------------------------------------------- #
# Auth (2-legged) — ports probe-aps.ps1 to Python
# --------------------------------------------------------------------------- #
_token_cache = {"access_token": None, "expires_at": 0.0}


def _load_creds() -> dict:
    if not os.path.exists(CRED_PATH):
        raise FileNotFoundError(f"APS creds missing: {CRED_PATH}")
    c = json.load(open(CRED_PATH))
    if not c.get("client_id") or c.get("client_id") == "PASTE_ME":
        raise ValueError(f"fill client_id/client_secret in {CRED_PATH}")
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
    """Deterministic bucket key (lowercase, globally unique-ish per client id).

    ROOT creates this bucket once (see setup_live.md). Override with APS_BUCKET.
    """
    if os.environ.get("APS_BUCKET"):
        return os.environ["APS_BUCKET"]
    c = _load_creds()
    # bucket keys: 3-128 chars, [-_.a-z0-9]; derive a stable suffix from the client id
    suffix = "".join(ch for ch in c["client_id"].lower() if ch.isalnum())[:16]
    return f"leaf-web-demo-{suffix}"


def activity_qualified(name: str) -> str:
    """owner.Activity+alias  (e.g. iBZF....LeafExtract+prod)."""
    return f"{nickname()}.{name}+{ALIAS}"


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


def create_bucket(policy: str = "transient") -> dict:
    """Create the OSS bucket (idempotent-ish: 409 = already exists). LIVE call."""
    r = requests.post(
        f"{APS}/oss/v2/buckets",
        headers={**_auth_headers(), "Content-Type": "application/json",
                 "x-ads-region": OSS_REGION},
        data=json.dumps({"bucketKey": bucket_key(), "policyKey": policy}),
        timeout=_HTTP_TIMEOUT)
    if r.status_code == 409:
        return {"bucketKey": bucket_key(), "existed": True}
    r.raise_for_status()
    return r.json()


def upload_object(local_path: str, object_key: str) -> str:
    """Upload a local file to OSS via signed S3 (3-step). Returns the OSS object id.

    LIVE call. Uses a single-part signed upload (demo DWGs are small).
    """
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
def _input_arg(object_key: str) -> dict:
    """Input argument referencing an OSS object; DA GETs it with the Bearer token."""
    return {"url": _oss_rest_url(object_key), "verb": "get",
            "headers": {"Authorization": f"Bearer {auth_token()}"}}


def _output_arg(object_key: str) -> dict:
    """Output argument; DA PUTs the produced file to the OSS object."""
    return {"url": _oss_rest_url(object_key), "verb": "put",
            "headers": {"Authorization": f"Bearer {auth_token()}"}}


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
                    dry_run: bool = False, poll: bool = True) -> dict:
    """Submit a WorkItem for `activity_id` with `arguments`.

    dry_run=True  -> return the exact POST body WITHOUT calling APS.
    dry_run=False -> POST, then (poll=True) poll to a terminal status and return it.
    """
    body = {"activityId": activity_id, "arguments": arguments}
    if dry_run:
        return {"_dry_run": True, "endpoint": f"POST {DA}/workitems",
                "body": _redact(body)}
    r = requests.post(f"{DA}/workitems",
                      headers={**_auth_headers(), "Content-Type": "application/json"},
                      data=json.dumps(body), timeout=_HTTP_TIMEOUT)
    r.raise_for_status()
    wi = r.json()
    if not poll:
        return wi
    return _poll_workitem(wi["id"])


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
def extract(dwg_local_path: str, dry_run: bool = False) -> dict:
    """Run the extract WorkItem on APS and return Intake JSON (§1).

    dry_run=True: mint a token, print/return the WorkItem submission body
    (referencing the real engine via the extract Activity) WITHOUT any live call.
    """
    dwg_name = os.path.basename(dwg_local_path)
    input_key = f"in/{int(time.time())}_{dwg_name}"
    output_key = f"out/{int(time.time())}_{dwg_name}.families.txt"
    activity_id = activity_qualified(EXTRACT_ACTIVITY)
    # The extract Activity's output parameter is named "Result" (localName result.txt).
    arguments = {"HostDwg": _input_arg(input_key), "Result": _output_arg(output_key)}

    if dry_run:
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
    status = submit_workitem(activity_id, arguments, dry_run=False, poll=True)
    if status.get("status") != "success":
        raise RuntimeError(f"extract WorkItem {status.get('id')} status={status.get('status')} "
                           f"report={status.get('reportUrl')}")
    finalize_upload(output_key, up_key)
    families = download_object(output_key).decode("utf-8", "replace")
    return parse_text(families, dwg_local_path)


def run_tool(dwg_local_path: str, tool: dict, params: dict,
             dry_run: bool = False) -> dict:
    """Run a tool (§2 package) on APS and return a Result envelope (§3).

    Convention: each tool has a DA Activity named  {TOOL_ACTIVITY_PREFIX}{engine_op}
    (ROOT provisions it from Lane B's script/appbundle). The Activity:
      - inputs  HostDwg (the drawing) and Params (inline data: JSON of `params`),
      - outputs Result -> result.json whose content is EITHER a full §3 envelope
        OR just the tool-specific {result, overlay?}; client wraps to full §3.
    """
    engine_op = tool.get("engine_op") or tool["name"].replace("-", "_")
    activity_id = activity_qualified(f"{TOOL_ACTIVITY_PREFIX}{engine_op}")
    dwg_name = os.path.basename(dwg_local_path)
    ts = int(time.time())
    input_key = f"in/{ts}_{dwg_name}"
    output_key = f"out/{ts}_{engine_op}.result.json"
    arguments = {
        "HostDwg": _input_arg(input_key),
        "Params": _json_arg(params or {}),
        "Result": _output_arg(output_key),
    }

    if dry_run:
        wi = submit_workitem(activity_id, arguments, dry_run=True)
        return {
            "_dry_run": True,
            "note": "run_tool: no bucket/upload/workitem performed",
            "engine": ENGINE,
            "tool": tool.get("name"),
            "activityId": activity_id,
            "input_object": input_key,
            "output_object": output_key,
            "workitem": wi,
        }

    # LIVE path — signed S3 urls for input/output; Params stays an inline data: uri
    t0 = time.time()
    upload_object(dwg_local_path, input_key)
    in_url = signed_download_url(input_key)
    up_key, out_url = signed_upload_url(output_key)
    arguments = {
        "HostDwg": {"url": in_url, "verb": "get"},
        "Params": _json_arg(params or {}),
        "Result": {"url": out_url, "verb": "put"},
    }
    status = submit_workitem(activity_id, arguments, dry_run=False, poll=True)
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


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()
