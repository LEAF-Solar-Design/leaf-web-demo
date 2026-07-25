"""Test-only helpers for positive catalog-backed run requests."""
from __future__ import annotations

from typing import Any, Dict, Optional

import requests


def _confirmed_payload(catalog: Dict[str, Any], tool: str,
                       params: Optional[Dict[str, Any]] = None,
                       dwg: Optional[str] = None) -> Dict[str, Any]:
    match = next((item for item in catalog.get("tools", [])
                  if item.get("name") == tool), None)
    if match is None:
        raise AssertionError(f"catalog did not issue tool {tool!r}")
    digest = match.get("catalog_digest")
    if not isinstance(digest, str) or not digest.startswith("sha256:") \
            or len(digest) != 71:
        raise AssertionError(f"catalog did not issue a strong digest for {tool!r}")
    payload: Dict[str, Any] = {
        "tool": tool,
        "params": params or {},
        "catalog_digest": digest,
    }
    if dwg is not None:
        payload["dwg"] = dwg
    return payload


def confirmed_requests_payload(app_url: str, tool: str,
                               params: Optional[Dict[str, Any]] = None,
                               dwg: Optional[str] = None,
                               headers: Optional[Dict[str, str]] = None,
                               timeout: float = 15) -> Dict[str, Any]:
    response = requests.get(
        f"{app_url.rstrip('/')}/api/tools", headers=headers or {}, timeout=timeout)
    response.raise_for_status()
    return _confirmed_payload(response.json(), tool, params, dwg)


def confirmed_client_payload(client: Any, tool: str,
                             params: Optional[Dict[str, Any]] = None,
                             dwg: Optional[str] = None,
                             headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    response = client.get("/api/tools", headers=headers or {})
    if response.status_code != 200:
        raise AssertionError(
            f"catalog request failed with {response.status_code}: {response.text}")
    return _confirmed_payload(response.json(), tool, params, dwg)
