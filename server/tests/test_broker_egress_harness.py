"""The harness and broker URLs add only their configured hostname to egress."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

import broker


HARNESS_HOST = "harness.leaf-platform-staging.local"
BROKER_HOST = "broker.leaf-platform-staging.local"


@pytest.fixture(autouse=True)
def _isolate_allowlist(monkeypatch):
    monkeypatch.delenv("LEAF_AUTHOR_HARNESS_URL", raising=False)
    monkeypatch.delenv("BROKER_URL", raising=False)
    monkeypatch.setattr(broker, "ALLOWED_SUFFIXES", [".amazonaws.com"])


@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize("env_name", ["LEAF_AUTHOR_HARNESS_URL", "BROKER_URL"])
def test_configured_harness_matches_exact_hostname_case_insensitively(monkeypatch, scheme, env_name):
    configured_host = BROKER_HOST if env_name == "BROKER_URL" else HARNESS_HOST
    port = 8140 if env_name == "BROKER_URL" else 8150
    monkeypatch.setenv(env_name, f"{scheme}://{configured_host}:{port}")
    assert broker._egress_allowed(configured_host)
    assert broker._egress_allowed(configured_host.upper())
    monkeypatch.setenv(env_name, f"{scheme}://{configured_host.upper()}:{port}")
    assert broker._egress_allowed(configured_host)
    for host in (
        "evil.example.com", f"{configured_host}.evil.com", f"x{configured_host}",
        f"evil.{configured_host}", "leaf-platform-staging.local",
    ):
        assert not broker._egress_allowed(host)


@pytest.mark.parametrize("value", [
    None, "", "ftp://harness.example", "harness.example:8150", "http://",
    "http://[", "http://[not-an-ipv6-address]",
    "broker.leaf-platform-staging.local", "ftp://x", "http://:8140", "http://h:notaport",
])
@pytest.mark.parametrize("env_name", ["LEAF_AUTHOR_HARNESS_URL", "BROKER_URL"])
def test_invalid_harness_configuration_adds_nothing(monkeypatch, value, env_name):
    if value is not None:
        monkeypatch.setenv(env_name, value)
    assert not broker._egress_allowed("harness.example")
    assert not broker._egress_allowed(HARNESS_HOST)
    assert not broker._egress_allowed(BROKER_HOST)
    assert not broker._egress_allowed("x")
    assert not broker._egress_allowed("h")
    assert not broker._egress_allowed("evil.example.com")
    for host in ("developer.api.autodesk.com", "api.leafdesign.ai",
                 "s3.us-east-1.amazonaws.com", "127.0.0.1", "localhost"):
        assert broker._egress_allowed(host)


@pytest.mark.parametrize("port, allowed", [
    ("invalid", False), ("99999", False), ("8150", True),
])
@pytest.mark.parametrize("env_name", ["LEAF_AUTHOR_HARNESS_URL", "BROKER_URL"])
def test_harness_port_validation(monkeypatch, port, allowed, env_name):
    monkeypatch.setenv(env_name, f"http://harness.example:{port}")
    assert broker._egress_allowed("harness.example") is allowed


@pytest.mark.parametrize("env_name", ["LEAF_AUTHOR_HARNESS_URL", "BROKER_URL"])
def test_harness_configuration_is_read_at_call_time(monkeypatch, env_name):
    monkeypatch.setenv(env_name, f"http://{HARNESS_HOST}:8150")
    assert broker._egress_allowed(HARNESS_HOST)
    monkeypatch.setenv(env_name, "https://replacement.example/grants")
    assert not broker._egress_allowed(HARNESS_HOST)
    assert broker._egress_allowed("replacement.example")
    monkeypatch.delenv(env_name)
    assert not broker._egress_allowed("replacement.example")


@pytest.mark.parametrize("env_name", ["LEAF_AUTHOR_HARNESS_URL", "BROKER_URL"])
def test_other_url_parts_do_not_allow_hosts(monkeypatch, env_name):
    monkeypatch.setenv(
        env_name,
        f"https://user:password@{HARNESS_HOST}:8150/evil.example.com?next=evil.example.com#evil.example.com",
    )
    assert broker._egress_allowed(HARNESS_HOST)
    assert not broker._egress_allowed("evil.example.com")


@pytest.mark.parametrize("host", [
    "developer.api.autodesk.com", "api.leafdesign.ai", "s3.us-east-1.amazonaws.com",
    "127.0.0.1", "localhost",
])
@pytest.mark.parametrize("env_name", ["LEAF_AUTHOR_HARNESS_URL", "BROKER_URL"])
def test_existing_allowances_survive_invalid_harness_url(monkeypatch, host, env_name):
    monkeypatch.setenv(env_name, "http://[")
    assert broker._egress_allowed(host)


@pytest.mark.parametrize("env_name", ["LEAF_AUTHOR_HARNESS_URL", "BROKER_URL"])
def test_extra_suffix_and_installed_guard_without_network(monkeypatch, env_name):
    # A fresh import preserves the existing import-time BROKER_EGRESS_EXTRA behavior.
    monkeypatch.setenv("BROKER_EGRESS_EXTRA", ".extra.example")
    configured_host = BROKER_HOST if env_name == "BROKER_URL" else HARNESS_HOST
    port = 8140 if env_name == "BROKER_URL" else 8150
    monkeypatch.setenv(env_name, f"http://{configured_host}:{port}")
    script = r'''
import os
import socket
import sys
sys.path.insert(0, sys.argv[1])

def no_network(*args, **kwargs):
    raise AssertionError("real network attempted")

socket.socket.connect = no_network
socket.create_connection = no_network
import requests
from requests.adapters import HTTPAdapter
sent_urls = []

def fake_send(self, request, *args, **kwargs):
    sent_urls.append(request.url)
    response = requests.Response()
    response.status_code = 200
    return response

HTTPAdapter.send = fake_send
import broker
assert broker._egress_allowed("service.extra.example")
assert not broker._egress_allowed("evil.example.com")
with requests.Session() as session:
    session.trust_env = False
    configured_url = os.environ[sys.argv[2]] + "/"
    assert session.get(configured_url).status_code == 200
    assert sent_urls == [configured_url]
    try:
        session.get("https://evil.example.com/")
    except broker.EgressBlocked:
        pass
    else:
        raise AssertionError("broker allowed arbitrary egress")
    assert sent_urls == [configured_url]
'''
    result = subprocess.run(
        [sys.executable, "-B", "-P", "-c", script, str(Path(__file__).resolve().parent.parent), env_name],
        env=dict(os.environ), capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
