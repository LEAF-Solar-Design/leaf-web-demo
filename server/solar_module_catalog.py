"""Tenant-scoped, bounded proxy for the string sizer's CeCMod search."""
from collections import OrderedDict
from copy import deepcopy
import json
import re
import socket
import threading
import time
from typing import Literal

import requests
from pydantic import BaseModel, ConfigDict, Field

import leaf_cloud_grants as cloud

CATALOG_URL = "https://api.leafdesign.ai/string-length/modules"
MAX_RESPONSE_BYTES = 262144
QUERY_DETAIL = "q must be 2 to 64 characters with 1 to 6 search terms"
LIMIT_DETAIL = "limit must be an integer from 1 to 50"
GRANT_DETAIL = "grant_ref must match ^[a-zA-Z0-9_-]{1,64}$"
_clock = time.monotonic
_cache = OrderedDict()
_cache_lock = threading.Lock()
# A timed-out DNS operation or dripping response headers can retain a slot until
# the worker exits: requests.get exposes no connection handle before headers.
# Connect/header operations have per-operation timeouts; the body watchdog starts
# once the response exists. The caller still receives 504 at the overall deadline.
_transport_slots = threading.BoundedSemaphore(4)
DEADLINE_SECONDS = 15


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Catalog(StrictModel):
    source: Literal["pvlib.CeCMod"]
    pvlib_version: str
    count: int = Field(ge=0, le=1000000)
    sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class Module(StrictModel):
    key: str
    stc_w: float | None
    n_s: int | None
    v_oc_ref: float | None
    length_m: float | None
    width_m: float | None


class SearchResponse(StrictModel):
    schema_version: int = Field(ge=1, le=1)
    catalog: Catalog
    modules: list[Module] = Field(max_length=50)
    truncated: bool


def validate_query(q, limit, grant_ref):
    if not isinstance(q, str):
        raise ValueError(QUERY_DETAIL)
    stripped = q.strip()
    tokens = re.findall(r"[a-zA-Z0-9]+", stripped)
    if not 2 <= len(stripped) <= 64 or not 1 <= len(tokens) <= 6:
        raise ValueError(QUERY_DETAIL)
    if not isinstance(limit, str) or not re.fullmatch(r"[+-]?[0-9]+", limit.strip()):
        raise ValueError(LIMIT_DETAIL)
    digits = limit.strip().lstrip("+-").lstrip("0")
    if limit.strip().startswith("-") or len(digits) > 2 or not 1 <= int(digits or "0") <= 50:
        raise ValueError(LIMIT_DETAIL)
    if not isinstance(grant_ref, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", grant_ref):
        raise ValueError(GRANT_DETAIL)
    return " ".join(token.lower() for token in tokens), int(digits)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _fetch(q, limit, grant, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise requests.Timeout()
    with requests.get(CATALOG_URL, params={"q": q, "limit": limit},
                      headers={"Authorization": "Bearer " + grant.access_token},
                      timeout=(min(5, remaining), min(10, remaining)),
                      allow_redirects=False, stream=True) as response:
        # Capture the socket before EOF detaches it from urllib3. close() alone
        # can wait on a buffered reader's lock while iter_content is blocked.
        fp = getattr(getattr(response.raw, "_fp", None), "fp", None)
        sock = getattr(getattr(fp, "raw", None), "_sock", None)

        def interrupt():
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            try:
                response.close()
            except Exception:
                pass

        timer = threading.Timer(max(0, deadline - time.monotonic()), interrupt)
        timer.daemon = True
        timer.start()
        try:
            if response.status_code == 401:
                raise cloud.CloudError("cloud_auth_missing", 401)
            if response.status_code == 403:
                raise cloud.CloudError("cloud_tenant_unauthorized", 403)
            if response.status_code != 200:
                raise cloud.CloudError("cloud_upstream_failure", 502)
            raw = bytearray()
            chunks = iter(response.iter_content(16384))
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise requests.Timeout()
                if sock is not None:
                    try:
                        sock.settimeout(min(10, remaining))
                    except OSError:
                        # urllib3 may close the socket after yielding the last
                        # chunk; let the iterator report EOF or the read error.
                        pass
                try:
                    chunk = next(chunks)
                except StopIteration:
                    break
                if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise ValueError()
                raw.extend(chunk)
            body = SearchResponse.model_validate(json.loads(
                bytes(raw).decode("utf-8"), object_pairs_hook=_unique_object))
        finally:
            timer.cancel()
            timer.join()
    # Cleanup is part of the budget, including a deadline-induced read failure.
    if time.monotonic() >= deadline:
        raise requests.Timeout()
    return body.model_dump()


def _transport(q, limit, grant):
    """Keep sensitive transport exceptions on a bounded worker; return safe errors."""
    if not _transport_slots.acquire(blocking=False):
        raise cloud.CloudError("cloud_upstream_failure", 502)
    done = threading.Event()
    result = []
    deadline = time.monotonic() + DEADLINE_SECONDS

    def work():
        try:
            result.append((200, _fetch(q, limit, grant, deadline)))
        except cloud.CloudError as exc:
            result.append((504 if time.monotonic() >= deadline else exc.status, None))
        except requests.Timeout:
            result.append((504, None))
        except Exception:
            # Never propagate requests/parser exception text or sensitive frames.
            result.append((504 if time.monotonic() >= deadline else 502, None))
        finally:
            _transport_slots.release()
            done.set()

    try:
        threading.Thread(target=work, daemon=True).start()
    except Exception:
        _transport_slots.release()
        raise cloud.CloudError("cloud_upstream_failure", 502) from None
    if not done.wait(max(0, deadline - time.monotonic())) or time.monotonic() >= deadline:
        raise cloud.CloudError("cloud_upstream_timeout", 504)
    status, body = result[0]
    if status != 200:
        classification = {401: "cloud_auth_missing", 403: "cloud_tenant_unauthorized",
                          504: "cloud_upstream_timeout"}.get(status, "cloud_upstream_failure")
        raise cloud.CloudError(classification, status)
    return body


def search(organization_id, q, limit, grant_ref):
    normalized_q, limit = validate_query(q, limit, grant_ref)
    key = (str(organization_id), normalized_q, limit)
    with _cache_lock:
        entry = _cache.get(key)
        if entry is not None:
            inserted, body = entry
            if _clock() - inserted < 600:
                _cache.move_to_end(key)
                return deepcopy(body)
            del _cache[key]
    grant = cloud.resolve_grant(grant_ref, str(organization_id))
    # Preserve the valid stripped query on the wire: e.g. "a-" is valid,
    # while its normalized cache key "a" is shorter than the sizer's minimum.
    body = _transport(q.strip(), limit, grant)
    with _cache_lock:
        _cache[key] = (_clock(), deepcopy(body))
        _cache.move_to_end(key)
        while len(_cache) > 256:
            _cache.popitem(last=False)
    return body
