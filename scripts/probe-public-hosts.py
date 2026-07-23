#!/usr/bin/env python3
"""Credential-free public-host redirect contract probe.

Default mode validates the frozen contract without network access. ``--live``
performs one no-follow HTTP request per host and reports violations. No mode
contains a mutation path.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit

REPO = Path(__file__).resolve().parent.parent
DEFAULT_CONTRACT = REPO / "contract" / "PUBLIC-HOSTS.json"
REDIRECT_STATUSES = {301, 302, 307, 308}
MAX_HOSTS = 16
MAX_OUTPUT_BYTES = 32768


@dataclass(frozen=True)
class ProbeResponse:
    status: int
    locations: tuple[str, ...]
    body_bytes: int


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _locations(headers: Mapping[str, str]) -> tuple[str, ...]:
    get_all = getattr(headers, "get_all", None)
    if callable(get_all):
        return tuple(get_all("Location") or ())
    value = headers.get("Location")
    return (value,) if value else ()


def fetch_once(url: str, timeout: float, body_limit: int) -> ProbeResponse:
    # Ignore ambient proxy settings so the credential-free probe cannot attach
    # proxy credentials from the operator environment.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirect)
    request = urllib.request.Request(
        url, method="GET", headers={"User-Agent": "leaf-public-host-probe/1"})
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        body = response.read(body_limit + 1)
        return ProbeResponse(
            int(response.status), _locations(response.headers), len(body))


def load_contract(path: Path = DEFAULT_CONTRACT) -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    validate_contract(data)
    return data


def validate_contract(contract: Mapping[str, Any]) -> None:
    if contract.get("schema") != "leaf.public-host-contract.v1":
        raise ValueError("unsupported public-host contract schema")
    if contract.get("max_hops_to_https") != 1:
        raise ValueError("public-host contract must allow exactly one hop to HTTPS")
    timeout = contract.get("timeout_seconds")
    body_limit = contract.get("max_body_bytes")
    if not isinstance(timeout, (int, float)) or not 0.1 <= float(timeout) <= 10:
        raise ValueError("timeout_seconds must be between 0.1 and 10")
    if not isinstance(body_limit, int) or not 0 <= body_limit <= 16384:
        raise ValueError("max_body_bytes must be between 0 and 16384")
    hosts = contract.get("hosts")
    if not isinstance(hosts, list) or not 1 <= len(hosts) <= MAX_HOSTS:
        raise ValueError("hosts must be a bounded nonempty list")
    names = set()
    for item in hosts:
        if not isinstance(item, dict):
            raise ValueError("each host contract must be an object")
        if item.get("name") in names:
            raise ValueError("host contract names must be unique")
        names.add(item.get("name"))
        request = urlsplit(str(item.get("http_url", "")))
        target = urlsplit(str(item.get("location", "")))
        if request.scheme != "http" or request.hostname != item.get("host"):
            raise ValueError("http_url must be HTTP on the declared host")
        if request.username or request.password or request.query or request.fragment:
            raise ValueError("http_url must not contain credentials, query, or fragment")
        if target.scheme != "https" or target.hostname != item.get("host"):
            raise ValueError("location must be same-host HTTPS")
        if target.username or target.password or target.query or target.fragment:
            raise ValueError("location must not contain credentials, query, or fragment")
        if item.get("status") not in REDIRECT_STATUSES:
            raise ValueError("status must be an HTTP redirect")


def _safe_location(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        parsed = urlsplit(value[:2048])
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError:
        return "<invalid-location>"
    path = parsed.path[:256] or "/"
    return urlunsplit((parsed.scheme, host + port, path, "", ""))


def _failure(
        name: str, check: str, expected: Any, actual: Any) -> Dict[str, Any]:
    return {
        "host": name,
        "check": check,
        "expected": expected,
        "actual": actual,
    }


def probe_contract(
        contract: Mapping[str, Any],
        requester: Optional[Callable[[str, float, int], ProbeResponse]] = None,
) -> Dict[str, Any]:
    validate_contract(contract)
    requester = fetch_once if requester is None else requester
    timeout = float(contract["timeout_seconds"])
    body_limit = int(contract["max_body_bytes"])
    failures = []
    observations = []
    for item in contract["hosts"]:
        name = item["name"]
        try:
            response = requester(item["http_url"], timeout, body_limit)
        except Exception as exc:
            failures.append(_failure(
                name, "request", "bounded HTTP response", type(exc).__name__))
            continue
        safe_locations = tuple(_safe_location(value) for value in response.locations)
        observations.append({
            "host": name,
            "status": response.status,
            "location": safe_locations[0] if len(safe_locations) == 1 else None,
        })
        if response.status == 200:
            failures.append(_failure(name, "http_not_200", "redirect", 200))
        if response.status != item["status"]:
            failures.append(_failure(
                name, "status", item["status"], response.status))
        if len(response.locations) != 1:
            failures.append(_failure(
                name, "location_count", 1, len(response.locations)))
            continue
        raw_location = response.locations[0]
        try:
            target = urlsplit(raw_location)
            target_host = target.hostname
        except ValueError:
            failures.append(_failure(
                name, "location_parse", "valid absolute URL", "<invalid-location>"))
            continue
        if target.scheme != "https":
            failures.append(_failure(
                name, "https_in_one_hop", "https", target.scheme or None))
        if target_host != item["host"]:
            failures.append(_failure(
                name, "same_host", item["host"], target_host))
        if raw_location == item["http_url"]:
            failures.append(_failure(name, "no_loop", False, True))
        if raw_location != item["location"]:
            failures.append(_failure(
                name, "location", item["location"], _safe_location(raw_location)))
        if response.body_bytes > body_limit:
            failures.append(_failure(
                name, "body_limit", body_limit, response.body_bytes))
    return {
        "schema": "leaf.public-host-probe-result.v1",
        "ok": not failures,
        "mode": "live",
        "observations": observations,
        "failures": failures,
    }


def contract_report(contract: Mapping[str, Any]) -> Dict[str, Any]:
    validate_contract(contract)
    return {
        "schema": "leaf.public-host-probe-result.v1",
        "ok": True,
        "mode": "contract",
        "hosts": [item["name"] for item in contract["hosts"]],
        "network_requests": 0,
        "mutations": 0,
    }


def _emit(report: Mapping[str, Any]) -> None:
    encoded = json.dumps(report, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_OUTPUT_BYTES:
        encoded = json.dumps({
            "schema": "leaf.public-host-probe-result.v1",
            "ok": False,
            "mode": report.get("mode"),
            "failures": [{
                "host": "probe",
                "check": "output_limit",
                "expected": MAX_OUTPUT_BYTES,
                "actual": "exceeded",
            }],
        }, sort_keys=True, separators=(",", ":"))
    print(encoded)


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate the credential-free public-host redirect contract")
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument(
        "--live", action="store_true",
        help="perform bounded read-only HTTP requests; never change host configuration")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        contract = load_contract(args.contract)
        report = probe_contract(contract) if args.live else contract_report(contract)
    except Exception as exc:
        report = {
            "schema": "leaf.public-host-probe-result.v1",
            "ok": False,
            "mode": "live" if args.live else "contract",
            "failures": [_failure(
                "probe", "contract_or_request", "valid bounded probe",
                type(exc).__name__)],
        }
    _emit(report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
