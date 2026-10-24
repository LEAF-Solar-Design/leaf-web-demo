"""The tenant grant harness adds only its configured hostname to broker egress."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

import broker


HARNESS_HOST = "harness.leaf-platform-staging.local"


@pytest.fixture(autouse=True)
def _isolate_allowlist(monkeypatch):
    monkeypatch.delenv("LEAF_AUTHOR_HARNESS_URL", raising=False)
    monkeypatch.setattr(broker, "ALLOWED_SUFFIXES", [".amazonaws.com"])


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_configured_harness_matches_exact_hostname_case_insensitively(monkeypatch, scheme):
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", f"{scheme}://{HARNESS_HOST}:8150")
    assert broker._egress_allowed(HARNESS_HOST)
    assert broker._egress_allowed(HARNESS_HOST.upper())
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", f"{scheme}://{HARNESS_HOST.upper()}:8150")
    assert broker._egress_allowed(HARNESS_HOST)
    for host in ("evil.example.com", f"{HARNESS_HOST}.evil.com", f"x{HARNESS_HOST}"):
        assert not broker._egress_allowed(host)


@pytest.mark.parametrize("value", [
    None, "", "ftp://harness.example", "harness.example:8150", "http://",
    "http://[", "http://[not-an-ipv6-address]",
])
def test_invalid_harness_configuration_adds_nothing(monkeypatch, value):
    if value is not None:
        monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", value)
    assert not broker._egress_allowed("harness.example")
    assert not broker._egress_allowed(HARNESS_HOST)
    assert not broker._egress_allowed("evil.example.com")


def test_harness_configuration_is_read_at_call_time(monkeypatch):
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", f"http://{HARNESS_HOST}:8150")
    assert broker._egress_allowed(HARNESS_HOST)
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "https://replacement.example/grants")
    assert not broker._egress_allowed(HARNESS_HOST)
    assert broker._egress_allowed("replacement.example")
    monkeypatch.delenv("LEAF_AUTHOR_HARNESS_URL")
    assert not broker._egress_allowed("replacement.example")


def test_other_url_parts_do_not_allow_hosts(monkeypatch):
    monkeypatch.setenv(
        "LEAF_AUTHOR_HARNESS_URL",
        f"https://user:password@{HARNESS_HOST}:8150/evil.example.com?next=evil.example.com#evil.example.com",
    )
    assert broker._egress_allowed(HARNESS_HOST)
    assert not broker._egress_allowed("evil.example.com")


@pytest.mark.parametrize("host", [
    "developer.api.autodesk.com", "api.leafdesign.ai", "s3.us-east-1.amazonaws.com",
    "127.0.0.1", "localhost",
])
def test_existing_allowances_survive_invalid_harness_url(monkeypatch, host):
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://[")
    assert broker._egress_allowed(host)


def test_extra_suffix_and_installed_guard_without_network(monkeypatch):
    # A fresh import preserves the existing import-time BROKER_EGRESS_EXTRA behavior.
    monkeypatch.setenv("BROKER_EGRESS_EXTRA", ".extra.example")
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", f"http://{HARNESS_HOST}:8150")
    script = r'''
import socket
import sys
sys.path.insert(0, sys.argv[1])

def no_network(*args, **kwargs):
    raise AssertionError("real network attempted")

socket.socket.connect = no_network
socket.create_connection = no_network
import requests
from requests.adapters import HTTPAdapter
HTTPAdapter.send = no_network
import broker
assert broker._egress_allowed("service.extra.example")
assert not broker._egress_allowed("evil.example.com")
with requests.Session() as session:
    session.trust_env = False
    try:
        session.get("https://evil.example.com/")
    except broker.EgressBlocked:
        pass
    else:
        raise AssertionError("broker allowed arbitrary egress")
'''
    result = subprocess.run(
        [sys.executable, "-B", "-P", "-c", script, str(Path(__file__).resolve().parent.parent)],
        env=dict(os.environ), capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
