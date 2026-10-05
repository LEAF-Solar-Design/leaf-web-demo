"""SYNTHETIC, TEST ONLY: recorded String Sizer replay, not live sizing.

These tests use real sizing/grant modules, loopback HTTP, and disposable fake
launchers. They do not start the product stack or a browser.
"""
import argparse
import copy
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import platform
import queue
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest
import requests

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
import proof_string_sizer as replay

client = replay.sizing_module()
from leaf_cloud_grants import CloudError, CloudGrant, resolve_grant

REQUEST_SHA = "c2adcc2616b290a64eacbdc9f3857447d5de567a3d297bb23b87d7bdabe269b5"
RESPONSE_SHA = "5d44f0bde6ec7ff27873b4902db5e07fb6004e0d5281db92ce00fa57c27b12ed"
MISMATCH_SHA = "3072e0d8befff2d1032b360fdfcedd7cb05d7c6005e0ea758846299311aa07fa"
CANONICAL_SHA = "c26514380548f96333945e9cc448eec4ffeb9d38e5c6b88623c197183d43ea8d"
FIXTURE_PATH = REPO / "server/tests/fixtures/w1_string_length_recorded_response.json"
RUNNER = REPO / "web/scripts/run_unified_local_proof.ps1"


@pytest.fixture
def fixture():
    return json.loads(FIXTURE_PATH.read_bytes())


@pytest.fixture
def wire(fixture):
    return client.wire_bytes(client.SizingRequest.model_validate(fixture["request"]))


@pytest.fixture
def server():
    listener = replay.ReplayServer(replay.load_manifest(replay.MANIFEST, REPO), "test-token")
    try:
        yield listener
    finally:
        listener.close()


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    monkeypatch.delenv("LEAF_RUNTIME_ENV", raising=False)
    monkeypatch.delenv("LEAF_ENV", raising=False)
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def post(server, body, *, path="/string-length", method="POST", headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server.server_port, timeout=3)
    try:
        connection.request(method, path, body=body, headers=headers if headers is not None else {
            "Authorization": "Bearer test-token", "Content-Type": "application/json"})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def client_error(monkeypatch, server, fixture, classification, status, *, path=None,
                 token="test-token", content_type=None, request=None):
    """Alter only outbound transport fields; keep the real sender's error mapping."""
    original_post = requests.post

    def send(url, **kwargs):
        if content_type is not None:
            kwargs["headers"] = dict(kwargs["headers"], **{"Content-Type": content_type})
        # A Session avoids inherited proxy/netrc state even in transport refusal tests.
        session = requests.Session()
        session.trust_env = False
        response = session.post(url, **kwargs)
        original_close = response.close

        def close():
            original_close()
            session.close()

        response.close = close
        return response

    monkeypatch.setattr(client, "requests", SimpleNamespace(post=send,
                                                            RequestException=requests.RequestException))
    monkeypatch.setattr(client, "SIZING_URL", server.url if path is None else
                        server.url.replace("/string-length", path))
    value = client.SizingRequest.model_validate(request or fixture["request"])
    with pytest.raises(CloudError) as caught:
        client.post_string_length(value, CloudGrant("demo-tenant", token))
    assert caught.value.classification == classification
    assert caught.value.status == status
    assert requests.post is original_post


def grant(tmp_path, monkeypatch):
    path, token = replay.write_grant_file(tmp_path, ttl_s=60)
    monkeypatch.setenv("LEAF_CLOUD_GRANTS_FILE", str(path))
    return path, token


def change_grant(path, **changes):
    value = json.loads(path.read_bytes())
    value[replay.GRANT_REF].update(changes)
    path.write_text(json.dumps(value), encoding="utf-8")


def grant_error(reference, tenant, classification, status):
    with pytest.raises(CloudError) as caught:
        resolve_grant(reference, tenant)
    assert caught.value.classification == classification
    assert caught.value.status == status


def fake_launcher(tmp_path, *, mode="normal", roles=False, http_events=False):
    """Build a real subprocess launcher with explicit owned-child cleanup."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    directory = tmp_path / "server"
    directory.mkdir()
    (directory / "solar_sizing_client.py").write_text(
        "SIZING_URL = 'unpatched'\nimport requests\n", encoding="utf-8")
    entry = (
        "import json, os\nfrom pathlib import Path\nimport solar_sizing_client as sizing\n"
        "Path(__file__).with_suffix('.observed.json').write_text(json.dumps({"
        "'url': sizing.SIZING_URL, 'grant': os.environ.get('LEAF_CLOUD_GRANTS_FILE'),"
        "'database': os.environ.get('DATABASE_URL')}))\n")
    for role in ("broker", "app", "other"):
        (directory / (role + ".py")).write_text(entry, encoding="utf-8")
    launcher = tmp_path / "launcher.py"
    source = (
        "import json, os, subprocess, sys, time\nfrom pathlib import Path\n"
        "children = []\nroot = Path(__file__).parent\n"
        "def spawn(name, cmd, cwd, env):\n"
        "    proc = subprocess.Popen(cmd, cwd=cwd, env=env)\n"
        "    children.append(proc)\n"
        "    return proc\n"
        "def cleanup():\n"
        "    for proc in children:\n"
        "        if proc.poll() is None: proc.kill()\n"
        "        proc.wait(timeout=5)\n"
        "    (root / 'cleaned.json').write_text(json.dumps([p.returncode for p in children]))\n"
        "def main():\n"
        "    server = root / 'server'\n"
        "    env = dict(os.environ, DATABASE_URL='remote-database', LEAF_CLOUD_GRANTS_FILE='ambient')\n")
    if roles:
        source += (
            "    for role in ('broker', 'app', 'other'):\n"
            "        proc = spawn(role, [sys.executable, role + '.py'], server, env)\n"
            "        if proc.wait(timeout=10) != 0: raise RuntimeError('child failed')\n")
    else:
        source += (
            "    proc = spawn('sleeper', [sys.executable, '-B', '-c', 'import time; time.sleep(60)'], server, env)\n"
            "    (root / 'child-pid').write_text(str(proc.pid))\n")
    if http_events:
        source += (
            "    import http.client\n"
            "    receipt = json.loads(Path(os.environ['TEST_RECEIPT']).read_text())\n"
            "    port = int(receipt['endpoint'].split(':')[2].split('/')[0])\n"
            "    mapping = json.loads((Path(os.environ['TEST_RECEIPT']).parent / 'synthetic-cloud-grants.json').read_text())\n"
            "    token = mapping['synthetic-sizing-g2']['access_token']\n"
            "    body = bytes.fromhex(os.environ['TEST_WIRE_HEX'])\n"
            "    for raw in (body, body.replace(b'44224', b'44225')):\n"
            "        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=3)\n"
            "        conn.request('POST', '/string-length', raw, {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})\n"
            "        conn.getresponse().read()\n"
            "        conn.close()\n")
    if mode == "exception":
        source += "    raise RuntimeError('intentional main failure')\n"
    elif mode == "stop":
        source += ("    try:\n"
                   "        raise KeyboardInterrupt()\n"
                   "    except KeyboardInterrupt:\n"
                   "        return 0\n")
    elif mode == "ttl":
        source += "    while True: time.sleep(0.05)\n"
    elif mode == "graceful":
        source += (
            "    import signal\n"
            "    stopped = False\n"
            "    def request_stop(signum, frame):\n"
            "        nonlocal stopped\n"
            "        stopped = True\n"
            "    signal.signal(signal.SIGINT, request_stop)\n"
            "    (root / 'ready').touch()\n"
            "    while not stopped: time.sleep(0.05)\n"
            "    return 0\n")
    else:
        source += "    return 0\n"
    launcher.write_text(source, encoding="utf-8")
    return launcher, directory


def supervise(tmp_path, launcher, directory, *, max_seconds=30, run_id="test-run"):
    args = argparse.Namespace(run_root=tmp_path / "run", run_id=run_id, manifest=None,
                              max_seconds=max_seconds, launcher=launcher,
                              server_dir=directory, launcher_args=["--"])
    result = replay.supervise(args)
    receipt_path = args.run_root / "synthetic-sizing-receipt.json"
    return result, receipt_path, json.loads(receipt_path.read_bytes())


def assert_listener_closed(endpoint):
    port = int(endpoint.split(":")[2].split("/")[0])
    with pytest.raises(OSError):
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass


def test_g2a_01_fixture_wire_digest(wire):
    assert len(wire) == 350
    assert digest(wire) == REQUEST_SHA
    assert digest(FIXTURE_PATH.read_bytes()) == "6e807695b9b36bbaf9007f3322d3570ea748853c746c33dcefcfdfe07c1c299a"


def test_g2a_02_replay_returns_recorded_bytes(server, wire):
    status, raw = post(server, wire)
    assert status == 200
    assert raw == replay.response_span(FIXTURE_PATH.read_bytes())
    assert len(raw) == 3862 and digest(raw) == RESPONSE_SHA
    assert server.events == [{"method": "POST", "path": "/string-length", "status": 200,
                              "request_sha256": REQUEST_SHA,
                              "fixture_id": "w1-string-length-recorded", "response_sha256": RESPONSE_SHA}]
    assert not server.failed


def test_g2a_03_real_size_through_replay(tmp_path, monkeypatch, fixture, wire):
    _, token = grant(tmp_path, monkeypatch)
    server = replay.ReplayServer(replay.load_manifest(replay.MANIFEST, REPO), token)
    monkeypatch.setattr(client, "SIZING_URL", client.SIZING_URL)
    monkeypatch.setattr(client, "requests", client.requests)
    try:
        replay.prepare_child("broker", server.url)
        result = client.size({"grant_ref": replay.GRANT_REF, "request": fixture["request"]},
                             "demo-tenant", "test-job")
        assert result["sizing"]["panels_in_sequence"] == 27
        cold = result["sizing"]["voc_cold"]
        assert cold["passes"] is True
        assert cold["string_voltage"] == pytest.approx(1471.352, abs=.001)
        assert cold["max_dc_voltage"] == 1500
        assert result["endpoint"] == server.url
        assert result["request_sha256"] == CANONICAL_SHA != digest(wire)
    finally:
        server.close()


def test_g2a_04_one_byte_mismatch_refused_and_recorded(server, wire, fixture, monkeypatch):
    changed = wire.replace(b"44224", b"44225")
    assert sum(a != b for a, b in zip(wire, changed)) == 1
    status, raw = post(server, changed)
    assert status == 409
    body = json.loads(raw)
    assert body["classification"] == "SYNTHETIC_REPLAY_REQUEST_MISMATCH"
    assert body["expected_sha256"] == [REQUEST_SHA]
    assert body["actual_sha256"] == MISMATCH_SHA
    assert server.events[-1]["request_sha256"] == MISMATCH_SHA
    assert server.events[-1]["status"] == 409 and server.failed
    request = dict(fixture["request"], zip_code="44225")
    client_error(monkeypatch, server, fixture, "cloud_upstream_failure", 502, request=request)


def test_g2a_05_wrong_path_404(server, wire, fixture, monkeypatch):
    assert post(server, wire, path="/wrong")[0] == 404
    client_error(monkeypatch, server, fixture, "cloud_upstream_failure", 502, path="/wrong")


def test_g2a_06_wrong_method_405(server):
    assert post(server, b"", method="GET")[0] == 405
    assert server.events[-1]["method"] == "GET" and server.failed


def test_g2a_07_missing_bearer_401(server, wire, fixture, monkeypatch):
    assert post(server, wire, headers={"Content-Type": "application/json"})[0] == 401
    # Remove the header at the facade boundary; the real sender still maps the response.
    class MissingBearer:
        RequestException = requests.RequestException

        @staticmethod
        def post(url, **kwargs):
            kwargs["headers"].pop("Authorization")
            session = requests.Session()
            session.trust_env = False
            from contextlib import contextmanager

            @contextmanager
            def response():
                with session:
                    with session.post(url, **kwargs) as value:
                        yield value
            return response()

    monkeypatch.setattr(client, "requests", MissingBearer)
    monkeypatch.setattr(client, "SIZING_URL", server.url)
    with pytest.raises(CloudError) as caught:
        client.post_string_length(client.SizingRequest.model_validate(fixture["request"]),
                                  CloudGrant("demo-tenant", "test-token"))
    assert (caught.value.classification, caught.value.status) == ("cloud_auth_missing", 401)


def test_g2a_08_wrong_bearer_401(server, wire, fixture, monkeypatch):
    assert post(server, wire, headers={"Authorization": "Bearer wrong", "Content-Type": "application/json"})[0] == 401
    client_error(monkeypatch, server, fixture, "cloud_auth_missing", 401, token="wrong")


def test_g2a_09_wrong_content_type_415(server, wire, fixture, monkeypatch):
    assert post(server, wire, headers={"Authorization": "Bearer test-token", "Content-Type": "text/plain"})[0] == 415
    client_error(monkeypatch, server, fixture, "cloud_upstream_failure", 502, content_type="text/plain")


def test_g2a_10_synthetic_grant_accepted(tmp_path, monkeypatch):
    path, token = grant(tmp_path, monkeypatch)
    resolved = resolve_grant(replay.GRANT_REF, "demo-tenant")
    assert resolved.access_token == token and resolved.tenant_id == "demo-tenant"
    value = json.loads(path.read_bytes())[replay.GRANT_REF]
    assert type(value["expires_at"]) is int
    assert value["expires_at"] >= int(time.time()) + 359
    assert value["audience"] == "https://api.leafdesign.ai"
    assert not any(c.isspace() for c in token)


def test_g2a_11_grant_audience_loopback_refused(tmp_path, monkeypatch):
    path, _ = grant(tmp_path, monkeypatch)
    change_grant(path, audience="http://127.0.0.1:1234/string-length")
    grant_error(replay.GRANT_REF, "demo-tenant", "cloud_auth_missing", 401)


def test_g2a_12_grant_expired_or_boolean_refused(tmp_path, monkeypatch):
    path, _ = grant(tmp_path, monkeypatch)
    for value in (int(time.time()) - 1, True):
        change_grant(path, expires_at=value)
        grant_error(replay.GRANT_REF, "demo-tenant", "cloud_auth_missing", 401)


def test_g2a_13_grant_tenant_mismatch_refused(tmp_path, monkeypatch):
    grant(tmp_path, monkeypatch)
    grant_error(replay.GRANT_REF, "another-tenant", "cloud_tenant_unauthorized", 403)


def test_g2a_14_grant_reference_invalid_refused(tmp_path, monkeypatch):
    grant(tmp_path, monkeypatch)
    grant_error("bad.ref", "demo-tenant", "cloud_auth_missing", 401)


def test_g2a_15_manifest_or_fixture_drift_refused(tmp_path, monkeypatch):
    original = json.loads(replay.MANIFEST.read_bytes())
    fixture = tmp_path / "fixture.json"
    fixture.write_bytes(FIXTURE_PATH.read_bytes())
    base = copy.deepcopy(original)
    base["fixtures"][0]["path"] = "fixture.json"
    path = tmp_path / "manifest.json"
    cases = []
    for pin in ("fixture_sha256", "request_wire_sha256", "response_wire_sha256"):
        value = copy.deepcopy(base)
        value["fixtures"][0][pin] = "0" * 64
        cases.append((value, pin))
    for key, value in (("schema", "wrong"), ("label", "wrong")):
        changed = copy.deepcopy(base)
        changed[key] = value
        cases.append((changed, key))
    for key, value in (("extra", "key"), ("path", "../fixture.json"), ("id", "INVALID"),
                       ("response_encoding", "reserialized"), ("path", str(fixture))):
        changed = copy.deepcopy(base)
        changed["fixtures"][0][key] = value
        cases.append((changed, "keys" if key == "extra" else key))
    changed = copy.deepcopy(base)
    changed["fixtures"][0]["id"] = 1
    cases.append((changed, "id"))
    for entries in ([], base["fixtures"] * 17):
        changed = copy.deepcopy(base)
        changed["fixtures"] = entries
        cases.append((changed, "fixtures"))

    def no_socket(*args, **kwargs):
        pytest.fail("manifest validation opened a socket")

    monkeypatch.setattr(replay, "ThreadingHTTPServer", no_socket)
    for value, field in cases:
        path.write_text(json.dumps(value), encoding="utf-8")
        before = path.read_bytes()
        with pytest.raises(replay.ReplayConfigError, match=field):
            replay.load_manifest(path, tmp_path)
        assert path.read_bytes() == before
    path.write_text(json.dumps(base), encoding="utf-8")
    before = path.read_bytes()
    fixture.write_bytes(fixture.read_bytes() + b"\n")
    with pytest.raises(replay.ReplayConfigError, match="fixture_sha256"):
        replay.load_manifest(path, tmp_path)
    assert path.read_bytes() == before


def test_g2a_16_fixture_set_dispatch_by_digest(tmp_path, fixture, wire):
    first = tmp_path / "first.json"
    first.write_bytes(FIXTURE_PATH.read_bytes())
    changed = copy.deepcopy(fixture)
    changed["request"]["zip_code"] = "44225"
    changed["response"]["voc"] += 1
    second = tmp_path / "second.json"
    second.write_text(json.dumps(changed, indent=2), encoding="utf-8")
    manifest = json.loads(replay.MANIFEST.read_bytes())
    manifest["fixtures"][0]["path"] = "first.json"
    second_body = client.wire_bytes(client.SizingRequest.model_validate(changed["request"]))
    entry = dict(manifest["fixtures"][0], id="second-fixture", path="second.json",
                 fixture_sha256=digest(second.read_bytes()), request_wire_sha256=digest(second_body),
                 response_wire_sha256=digest(replay.response_span(second.read_bytes())))
    manifest["fixtures"].append(entry)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    mapping = replay.load_manifest(path, tmp_path)
    listener = replay.ReplayServer(mapping, "test-token")
    try:
        assert post(listener, wire) == (200, replay.response_span(first.read_bytes()))
        assert post(listener, second_body) == (200, replay.response_span(second.read_bytes()))
        assert [event["fixture_id"] for event in listener.events] == ["w1-string-length-recorded", "second-fixture"]
        assert post(listener, b"unknown")[0] == 409
    finally:
        listener.close()
    manifest["fixtures"][1] = dict(manifest["fixtures"][0], id="duplicate-body")
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(replay.ReplayConfigError, match="duplicate request digest"):
        replay.load_manifest(path, tmp_path)


def test_g2a_17_parent_posture_refused(tmp_path, monkeypatch):
    for key, value in (("LEAF_RUNTIME_ENV", "production"), ("LEAF_ENV", "staging"),
                       ("LEAF_RUNTIME_ENV", "unknown")):
        env = dict(os.environ, LEAF_AUTH_LIVE="0", LEAF_RUNTIME_ENV="", LEAF_ENV="")
        env[key] = value
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "proof_string_sizer.py"),
                                 "supervise", "--run-root", str(tmp_path), "--run-id", "refused"],
                                env=env, capture_output=True, text=True, timeout=10)
        assert result.returncode == 2
        assert result.stderr.strip() == "Synthetic string sizing refuses a deployed runtime posture"
        assert not (tmp_path / "synthetic-cloud-grants.json").exists()
        assert not (tmp_path / "synthetic-sizing-receipt.json").exists()


def test_g2a_18_guest_mode_refused(tmp_path):
    result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "proof_string_sizer.py"),
                             "supervise", "--run-root", str(tmp_path), "--run-id", "guest"],
                            env=dict(os.environ, LEAF_AUTH_LIVE="1"), capture_output=True,
                            text=True, timeout=10)
    assert result.returncode == 2
    assert result.stderr.strip() == "Synthetic string sizing runs in account mode only"
    assert not list(tmp_path.iterdir())
    text = RUNNER.read_text(encoding="utf-8")
    throw = text.index("throw 'Synthetic string sizing runs in account mode only'")
    assert throw < text.index("Invoke-ProofRetentionPrune") < text.index("New-Item")
    assert throw < text.index("foreach ($port")
    assert "[switch]$SyntheticStringSizing" in text


def test_g2a_19_switch_absent_launch_unchanged():
    text = RUNNER.read_text(encoding="utf-8")
    expected = """  $launcherArgs = @(
    'scripts/start-leaf.py', '--with-harness',
    '--broker-port', $BrokerPort,
    '--app-port', $AppPort,
    '--harness-port', $HarnessPort,
    '--web-port', $WebPort
  )"""
    assert expected in text
    removal = text.index("Remove-Item Env:LEAF_CLOUD_GRANTS_FILE")
    assert text.rfind("} else {", 0, removal) > text.index(expected)
    assert removal < text.index("$launcher = Start-Process")
    assert "'scripts/proof_string_sizer.py', 'supervise'" in text
    assert "@('--strict-ports')" in text
    assert "taskkill /PID $launcher.Id /T /F" in text
    verify = text.index("verify-receipt --receipt $sizingReceipt --run-id $runId")
    failure = text.index("if ($LASTEXITCODE -ne 0 -and $proofExitCode -eq 0) { $proofExitCode = 1 }", verify)
    missing = text.index("} elseif ($proofExitCode -eq 0) {", verify)
    assert verify < failure < missing
    branch_end = text.index("}", missing + 1)
    assert "$proofExitCode = 1" in text[missing:branch_end]


def test_g2a_20_bootstrap_patches_both_roles_grant_broker_only(tmp_path):
    launcher, directory = fake_launcher(tmp_path, roles=True)
    code, path, receipt = supervise(tmp_path, launcher, directory)
    assert code == 0 and receipt["bootstrap_roles"] == ["broker", "app"]
    observed = {role: json.loads((directory / (role + ".observed.json")).read_bytes())
                for role in ("broker", "app", "other")}
    assert observed["broker"]["url"] == receipt["endpoint"]
    assert observed["app"]["url"] == receipt["endpoint"]
    assert observed["other"]["url"] == "unpatched"
    assert observed["broker"]["grant"] == str(path.parent / "synthetic-cloud-grants.json")
    assert observed["app"]["grant"] is None and observed["other"]["grant"] is None
    assert all(value["database"] is None for value in observed.values())
    assert_listener_closed(receipt["endpoint"])


def test_g2a_21_proxy_and_netrc_isolated(tmp_path, monkeypatch, fixture, wire):
    netrc = tmp_path / "netrc"
    netrc.write_text("machine 127.0.0.1 login wrong password wrong-password\n", encoding="utf-8")
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NETRC", str(netrc))
    server = replay.ReplayServer(replay.load_manifest(replay.MANIFEST, REPO), "test-token")
    original_post, original_session = requests.post, requests.Session
    monkeypatch.setattr(client, "SIZING_URL", client.SIZING_URL)
    monkeypatch.setattr(client, "requests", client.requests)
    try:
        entry = replay.prepare_child("app", server.url)
        assert entry == REPO / "server/app.py"
        raw = client.post_string_length(client.SizingRequest.model_validate(fixture["request"]),
                                        CloudGrant("demo-tenant", "test-token"))
        assert raw == replay.response_span(FIXTURE_PATH.read_bytes())
        assert server.events[-1]["request_sha256"] == digest(wire)
        assert requests.post is original_post and requests.Session is original_session
        assert requests.Session().trust_env is True
    finally:
        server.close()
    with pytest.raises(replay.ReplayConfigError, match="sizing-url"):
        replay.prepare_child("broker", "https://example.invalid/string-length")


def test_g2a_22_receipt_bound_to_run(tmp_path, monkeypatch):
    launcher, directory = fake_launcher(tmp_path)
    root = tmp_path / "run"
    root.mkdir()
    path = root / "synthetic-sizing-receipt.json"
    path.write_text('{"run_id":"stale","secret":"stale-marker"}', encoding="utf-8")
    original = replay.load_manifest

    def loading(*args):
        assert not path.exists(), "stale receipt must disappear before manifest loading"
        return original(*args)

    monkeypatch.setattr(replay, "load_manifest", loading)
    code, path, receipt = supervise(tmp_path, launcher, directory)
    assert code == 0 and b"stale-marker" not in path.read_bytes()
    assert replay.verify_receipt(path, "test-run") == 0
    assert replay.verify_receipt(path, "another-run") == 1
    for changed in (dict(receipt, label="wrong"), dict(receipt, ok=False)):
        path.write_text(json.dumps(changed), encoding="utf-8")
        assert replay.verify_receipt(path, "test-run") == 1
    path.write_text("{broken", encoding="utf-8")
    assert replay.verify_receipt(path, "test-run") == 1


def test_g2a_23_lifecycle_kills_owned_children(tmp_path):
    for mode, expected in (("stop", 0), ("exception", 1), ("ttl", 1)):
        root = tmp_path / mode
        launcher, directory = fake_launcher(root, mode=mode)
        code, _, receipt = supervise(root, launcher, directory, max_seconds=1 if mode == "ttl" else 30)
        assert code == expected
        cleaned = json.loads((root / "cleaned.json").read_bytes())
        assert len(cleaned) == 1 and cleaned[0] is not None
        # cleanup waits on the actual Popen; this also proves the recorded child is gone.
        assert int((root / "child-pid").read_text()) > 0
        assert_listener_closed(receipt["endpoint"])
        assert receipt["ttl_expired"] is (mode == "ttl")
        if mode == "ttl":
            assert receipt["ok"] is False


def test_g2a_24_packaging_exclusion():
    for name in ("broker", "app", "canonical-worker"):
        text = (REPO / "deploy" / ("Dockerfile." + name)).read_text(encoding="utf-8")
        # Join continuation lines before inspecting COPY source operands.
        text = re.sub(r"\\\r?\n", " ", text)
        for line in text.splitlines():
            if not re.match(r"\s*COPY\s", line, re.I) or "--from=" in line.lower():
                continue
            assert "proof_string_sizer" not in line and "string_sizer_replay_manifest" not in line
            operands = line.strip().split(None, 1)[1]
            if operands.startswith("["):
                sources = json.loads(operands)[:-1]
            else:
                sources = [word for word in operands.split()[:-1] if not word.startswith("--")]
            assert not set(sources) & {".", "./", "scripts", "scripts/", "scripts/*"}
    for path in (REPO / "server").rglob("*.py"):
        assert "proof_string_sizer" not in path.read_text(encoding="utf-8")


def test_g2a_25_receipt_label_and_redaction(tmp_path, monkeypatch, wire):
    launcher, directory = fake_launcher(tmp_path, http_events=True)
    monkeypatch.setenv("TEST_RECEIPT", str(tmp_path / "run/synthetic-sizing-receipt.json"))
    monkeypatch.setenv("TEST_WIRE_HEX", wire.hex())
    code, path, receipt = supervise(tmp_path, launcher, directory)
    assert code == 1 and receipt["ok"] is False
    assert receipt["label"] == replay.LABEL
    assert receipt["schema"] == replay.RECEIPT_SCHEMA
    assert receipt["record"] == "sf-w3-sizing-replay"
    assert [event["status"] for event in receipt["events"]] == [200, 409]
    assert receipt["events"][0]["response_sha256"] == RESPONSE_SHA
    assert receipt["events"][1]["request_sha256"] == MISMATCH_SHA
    token = json.loads((path.parent / "synthetic-cloud-grants.json").read_bytes())[replay.GRANT_REF]["access_token"]
    raw = path.read_bytes()
    assert token.encode() not in raw and b"Bearer" not in raw
    assert wire not in raw and wire.replace(b"44224", b"44225") not in raw
    assert "access_token" not in receipt and "Authorization" not in receipt
    assert replay.LABEL in receipt["limitations"]
    assert "synthetic fixture, not a live-service capture" in receipt["limitations"]
    assert "no browser walk (G2b)" in receipt["limitations"]
    assert "no staging sizing (G3)" in receipt["limitations"]
    assert "a drawing sized under one replay port does not re-verify under another" in receipt["limitations"]


def test_g2a_26_bounded_malformed_transport(wire):
    server = replay.ReplayServer(replay.load_manifest(replay.MANIFEST, REPO), "test-token",
                                 read_timeout_s=.5, max_body_bytes=400, max_events=4)
    prefix = (b"POST /string-length HTTP/1.1\r\nHost: 127.0.0.1\r\n"
              b"Authorization: Bearer test-token\r\nContent-Type: application/json\r\n")

    def transport(suffix):
        with socket.create_connection(("127.0.0.1", server.server.server_port), timeout=3) as connection:
            connection.sendall(prefix + suffix)
            chunks = []
            while True:
                value = connection.recv(4096)
                if not value:
                    return b"".join(chunks)
                chunks.append(value)

    try:
        # No body is sent: an answer proves oversize refusal did not await/read it.
        assert b" 413 " in transport(b"Content-Length: 999999\r\n\r\n")
        assert b" 411 " in transport(b"\r\n")
        assert b" 411 " in transport(b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n")
        with socket.create_connection(("127.0.0.1", server.server.server_port), timeout=1.5) as connection:
            connection.sendall(prefix + b"Content-Length: 20\r\n\r\na")
            started = time.monotonic()
            stop = threading.Event()

            def trickle():
                while not stop.wait(.25):
                    try:
                        connection.sendall(b"a")
                    except OSError:
                        return

            sender = threading.Thread(target=trickle)
            sender.start()
            try:
                assert connection.recv(4096) == b""
                assert time.monotonic() - started < 1.0
            finally:
                stop.set()
                sender.join()
        wait_for_events(server, 4)
        assert server.events[-1]["status"] == "timeout"
        assert len(server.events) == 4 and server.failed
        server.failed = False
        assert post(server, wire)[0] == 200
        assert server.failed and len(server.events) == 5
        assert server.events[-1]["status"] == 200
        assert transport(b"Content-Length: 20\r\n\r\nshort") == b""
        wait_for_events(server, 6)
        assert server.events[-1]["status"] == "timeout"
    finally:
        server.close()


def wait_for_events(server, count):
    deadline = time.monotonic() + .3
    while len(server.events) < count and time.monotonic() < deadline:
        time.sleep(.005)
    assert len(server.events) == count


def raw_transport(server, request):
    with socket.create_connection(("127.0.0.1", server.server.server_port), timeout=1.5) as connection:
        connection.sendall(request)
        chunks = []
        while True:
            try:
                value = connection.recv(4096)
            except ConnectionResetError:
                if chunks:
                    return b"".join(chunks)
                raise
            if not value:
                return b"".join(chunks)
            chunks.append(value)


def test_g2a_27_header_deadline_closes_and_records():
    baseline = threading.active_count()
    server = replay.ReplayServer(replay.load_manifest(replay.MANIFEST, REPO), "test-token",
                                 read_timeout_s=.5)
    prefix = b"POST /string-length HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Incomplete:"
    try:
        for trickling in (False, True):
            with socket.create_connection(("127.0.0.1", server.server.server_port), timeout=1.5) as connection:
                started = time.monotonic()
                connection.sendall(prefix)
                stop = threading.Event()

                def trickle():
                    while not stop.wait(.3):
                        try:
                            connection.sendall(b"a")
                        except OSError:
                            return

                sender = threading.Thread(target=trickle)
                if trickling:
                    sender.start()
                try:
                    assert connection.recv(4096) == b""
                    assert time.monotonic() - started < 1.0
                finally:
                    stop.set()
                    if trickling:
                        sender.join()
            wait_for_events(server, 2 if trickling else 1)
            assert server.events[-1]["status"] == "timeout" and server.failed
        with socket.create_connection(("127.0.0.1", server.server.server_port), timeout=1.5):
            pass
        # A silent idle connection must also expire without an event.
        server.failed = False
        with socket.create_connection(("127.0.0.1", server.server.server_port), timeout=1.5) as connection:
            assert connection.recv(4096) == b""
    finally:
        server.close()
    assert len(server.events) == 2 and not server.failed
    deadline = time.monotonic() + 1
    while threading.active_count() > baseline and time.monotonic() < deadline:
        time.sleep(.005)
    assert threading.active_count() <= baseline


def test_g2a_28_request_target_and_method_never_recorded(tmp_path, monkeypatch):
    token = secrets.token_urlsafe(24)
    server = replay.ReplayServer(replay.load_manifest(replay.MANIFEST, REPO), "test-token")
    try:
        assert b" 404 " in raw_transport(server, ("POST /" + token + " HTTP/1.1\r\nHost: localhost\r\n\r\n").encode())
        assert server.events[-1]["path"] == "other"
        response = raw_transport(server, (token + " /string-length HTTP/1.1\r\nHost: localhost\r\n\r\n").encode())
        assert b" 405 " in response
        assert server.events[-1]["method"] == "other"
        assert token not in json.dumps(server.events)
        launcher, directory = fake_launcher(tmp_path)
        monkeypatch.setattr(replay, "ReplayServer", lambda *args, **kwargs: server)
        code, path, receipt = supervise(tmp_path, launcher, directory)
        assert code == 1 and receipt["events"] == server.events
        assert token.encode() not in path.read_bytes()
        assert replay.verify_receipt(path, "test-run") == 1
        replay.atomic_json(path, dict(receipt, ok=True))
        assert replay.verify_receipt(path, "test-run") == 0
    finally:
        server.close()


def test_g2a_29_parser_refusals_recorded_and_fail(server):
    for request, status in ((b"POST /string-length HTTP/9.9\r\nHost: localhost\r\n\r\n", 505),
                            (b"not-an-http-request\r\n", 400),
                            (b"POST /string-length HTTP/1.1\r\nX: " + b"a" * 65537 + b"\r\n\r\n", 431),
                            (b"POST /" + b"a" * 65537 + b" HTTP/1.1\r\n\r\n", 414)):
        count = len(server.events)
        response = raw_transport(server, request)
        # HTTP/9.9 and malformed request lines receive the stdlib's HTTP/0.9 body-only response.
        assert (b'"status": ' + str(status).encode()) in response
        wait_for_events(server, count + 1)
        assert server.events[-1]["status"] == status and server.failed
    assert server.events[1]["method"] is None and server.events[1]["path"] is None


def test_g2a_30_exact_raw_target_only(server, wire):
    # The 404 is decided before the body is read, so these requests declare the pinned length and send no
    # body: a server that closes with unread request bytes resets the connection and can drop the answer.
    for target in ("//string-length", "/string-length?x=1", "/STRING-LENGTH"):
        request = ("POST " + target + " HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer test-token\r\n"
                   "Content-Type: application/json\r\nContent-Length: " + str(len(wire)) + "\r\n\r\n").encode()
        assert b" 404 " in raw_transport(server, request)
        assert server.events[-1]["path"] == "other"
    assert post(server, wire) == (200, replay.response_span(FIXTURE_PATH.read_bytes()))
    assert server.events[-1]["path"] == "/string-length"
    assert [event["status"] for event in server.events] == [404, 404, 404, 200]



class InertTimer:
    """A watchdog that never fires, so only the body read's own deadline can end the read."""

    def __init__(self, *args, **kwargs):
        self.daemon = True

    def start(self):
        pass

    def cancel(self):
        pass

    def join(self, timeout=None):
        pass


def test_g2a_32_body_deadline_alone_cuts_a_trickle(monkeypatch):
    monkeypatch.setattr(replay.threading, "Timer", InertTimer)
    server = replay.ReplayServer(replay.load_manifest(replay.MANIFEST, REPO), "test-token", read_timeout_s=.5)
    request = (b"POST /string-length HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer test-token\r\n"
               b"Content-Type: application/json\r\nContent-Length: 20\r\n\r\na")
    try:
        with socket.create_connection(("127.0.0.1", server.server.server_port), timeout=1.5) as connection:
            started = time.monotonic()
            connection.sendall(request)
            stop = threading.Event()

            def trickle():
                while not stop.wait(.25):
                    try:
                        connection.sendall(b"a")
                    except OSError:
                        return

            sender = threading.Thread(target=trickle)
            sender.start()
            try:
                assert connection.recv(4096) == b""
                assert time.monotonic() - started < 1.0
            finally:
                stop.set()
                sender.join()
        wait_for_events(server, 1)
        assert server.events[-1]["status"] == "timeout" and server.failed
    finally:
        server.close()

def test_g2a_31_manifest_top_level_keys_exact(tmp_path, monkeypatch):
    manifest = json.loads(replay.MANIFEST.read_bytes())
    missing = dict(manifest)
    missing.pop("label")
    path = tmp_path / "manifest.json"

    def no_socket(*args, **kwargs):
        pytest.fail("manifest validation opened a socket")

    monkeypatch.setattr(replay, "ThreadingHTTPServer", no_socket)
    for value in (dict(manifest, extra="x"), missing):
        path.write_text(json.dumps(value), encoding="utf-8")
        before = path.read_bytes()
        with pytest.raises(replay.ReplayConfigError, match="manifest keys"):
            replay.load_manifest(path, REPO)
        assert path.read_bytes() == before


_SIGINT_IGNORED_CHILD = r"""
import json, signal, sys
from pathlib import Path
signal.signal(signal.SIGINT, signal.SIG_IGN)
sys.path.insert(0, sys.argv[1])
import test_proof_string_sizer as rows
root = Path(sys.argv[2])
launcher, directory = rows.fake_launcher(root, mode="ttl")
code, _, receipt = rows.supervise(root, launcher, directory, max_seconds=1)
print(json.dumps({"code": code, "ttl_expired": receipt["ttl_expired"], "ok": receipt["ok"],
                  "restored": signal.getsignal(signal.SIGINT) == signal.SIG_IGN}))
"""


def test_g2a_33_ttl_fires_while_sigint_is_ignored(tmp_path):
    # A CI job inherits SIGINT as ignored; the watchdog's interrupt must still stop the run.
    env = dict(os.environ, LEAF_AUTH_LIVE="0", LEAF_RUNTIME_ENV="", LEAF_ENV="")
    try:
        done = subprocess.run([sys.executable, "-B", "-c", _SIGINT_IGNORED_CHILD, str(SCRIPTS),
                               str(tmp_path / "ignored")], env=env, capture_output=True,
                              text=True, timeout=60)
    except subprocess.TimeoutExpired:
        pytest.fail("the TTL watchdog did not stop a run whose SIGINT was ignored")
    assert done.returncode == 0, done.stderr[-2000:]
    result = json.loads(done.stdout.strip().splitlines()[-1])
    assert result == {"code": 1, "ttl_expired": True, "ok": False, "restored": True}
    cleaned = json.loads((tmp_path / "ignored" / "cleaned.json").read_bytes())
    assert len(cleaned) == 1 and cleaned[0] is not None


# SP-21A adds a separate, explicitly selected synthetic profile. These rows
# measure loopback transport and source/build contexts, never built images.
SP_PACKAGE = SCRIPTS / "fixtures/rooftop_solve_replay.json"
SP_SIZING = REPO / "server/tests/fixtures/w1_rooftop_unsplit_sizing_response.json"
SP_SIZING_SHA = "840bb09ca947f80c274ad4beda315b7ecd26aecd889c4467ae06f8acbd6efd41"
SP_PACKAGE_SHA = "53ed4b7b1f8d5c22c9a5e991d0eafd100e6fb598fe702114b091d3f7d66c4618"
SP_PINS = [[12,21300,"7be3421b5834ca07ad32ad340d403ad628649cd3d490d532946503d035a515bb",23162,"9e30176bcfb9d8cbf127ccbdbb3dff6ba5d737d3db394e5c6df517c5b24b0110",[[14,13],[7,1]],8],[13,20141,"495a015e052494930c929e69bf2dd04523a65ea74dd9cfc661b6c9b0a4f3ecab",21959,"673f016c46ead20fe15cdd4c04773e75d04d2dab52e0d668165f0a1035ee1cd0",[[14,13],[0,8]],8],[16,18152,"b7b3139ac495d8b50a986e196b54cda65c3856c6ae1080480b864e10c9af741d",19915,"09009716285ee7304e5dca54b68606c149eb508b86fabf7a6c41d7e51103e50f",[[13,12],[3,5]],8],[19,22296,"edcc9f287a3456f3585b8dddcf0e9bb14df9e20ca62c5b6aa6f39c3367b4c1fd",24408,"0501d452148aa892adad922ae576c02c8375f445932023e38766a41956594c2d",[[14,13],[7,3]],10],[20,30096,"5f43f72c977fbc48ad8db34b9b57b7f0c2c56962d6dc67eaae0adea3073f5c18",32228,"80f45979bd6da379adadbe72960f20470ecc744ae07b1400bf0ee852dbab865f",[[14,13],[4,6]],10],[21,35306,"27747c602e70236fae1d38f1a6fa4279d84ae810dffb95cfb123e59f76f48bdd",37384,"6d59868565dd397b3b455e555a15d3b38307d02a475b0a70a1d75ad647a78c7a",[[14,13],[6,3]],9],[22,28622,"bf739a8eb93c699d1ed3f04bb95283ae12de573a241bd0af1f19257af1293c27",31093,"2e21188a3a85864125516d54a576ea200d9fee5ac65f8aa958117778d022e0c4",[[14,13],[5,8]],13]]
SP_PIECES = [row[0] for row in SP_PINS]
SP_ASSETS = [
    "scripts/proof_string_sizer.py", "scripts/test_proof_string_sizer.py",
    "scripts/fixtures/string_sizer_replay_manifest.json",
    "scripts/fixtures/rooftop_string_sizer_replay_manifest.json",
    "scripts/fixtures/rooftop_solve_replay_manifest.json",
    "scripts/fixtures/rooftop_solve_replay.json",
]


def sp_package():
    return json.loads(SP_PACKAGE.read_bytes())


def sp_pair(piece):
    return sp_package()["fixtures"][SP_PIECES.index(piece)]


def sp_reconstruct():
    """Rebuild from source independently of the replay package and manifests."""
    from uuid import UUID
    from intake_dxf import intake_to_dxf
    from dxf_intake import parse_dxf_bytes
    cloud = replay.solve_module()
    raw = (REPO / "data/rooftop_unsplit.intake.json").read_bytes()
    assert digest(raw) == "52506008e5cde459c8d3f695eb787a8021f2cacf4a02f499ea390cdc4d891a9a"
    dxf = intake_to_dxf(json.loads(raw))
    assert len(dxf) == 159118
    assert digest(dxf) == "942433cd03fb585263060e552871bafe00316520136605352072e9d027d7e75d"
    intake = parse_dxf_bytes(dxf, source_name="rooftop-unsplit.dxf")
    upload = json.dumps(intake).encode()
    assert len(upload) == 177010
    source = digest(upload)
    assert source == "9990fdd62a048fdd7f559d819a05ed2250bcca4f4e212a150d2c272597050a04"
    recorded = (REPO / "server/tests/fixtures/w1_rooftop_unsplit_solve.json").read_bytes()
    assert digest(recorded) == "4044d38eb28f442266e5b43372d5090f6f951c7d8e180fe60c0ef4114991fc87"
    pairs = []
    for group in json.loads(recorded)["groups"]:
        response = copy.deepcopy(group["response"])
        for row in response["data"]["final_grid"]["Rows"]:
            for panel in row["Panels"]:
                if panel["Code"] == 1:
                    seed = source + ":panel:" + panel["Id"].upper().lstrip("0")
                    panel["Id"] = "leaf:panel:" + str(UUID(
                        bytes=hashlib.sha256(seed.encode()).digest()[:16], version=4))
        request = {"grid": copy.deepcopy(response["data"]["final_grid"])}
        for row in request["grid"]["Rows"]:
            for panel in row["Panels"]:
                panel["Seq"] = 0
        request = cloud.StringerRequest.model_validate(request).wire_payload()
        pairs.append({"id": "rooftop-piece-" + str(int(group["piece"])),
                      "request": request, "response": response})
    package = {"schema": "leaf.synthetic-rooftop-solve-fixtures.v1",
               "label": replay.ROOFTOP_LABEL, "source_intake_sha256": digest(raw),
               "upload_intake_sha256": source, "source_solve_fixture_sha256": digest(recorded),
               "fixtures": pairs}
    return cloud.canonical_bytes(package) + b"\n"


@pytest.fixture
def sp_listeners():
    # Both manifests are validated before the first socket exists.
    sizing = replay.load_manifest(replay.ROOFTOP_SIZING_MANIFEST, REPO, label=replay.ROOFTOP_LABEL)
    solves = replay.load_solve_manifest(replay.ROOFTOP_SOLVE_MANIFEST, REPO)
    listeners = []
    try:
        listeners.append(replay.ReplayServer(sizing, "test-token", label=replay.ROOFTOP_LABEL,
                                            max_body_bytes=client.MAX_RESPONSE_BYTES))
        listeners.append(replay.ReplayServer(solves, "test-token", endpoint="/api/ml/",
                                            label=replay.ROOFTOP_LABEL,
                                            max_body_bytes=replay.solve_module().MAX_RESPONSE_BYTES))
        yield listeners
    finally:
        for listener in listeners:
            listener.close()


def sp_post(listener, body, *, path=None, method="POST", headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", listener.server.server_port, timeout=3)
    try:
        connection.request(method, path or listener.endpoint, body=body, headers=headers if headers is not None else {
            "Authorization": "Bearer test-token", "Content-Type": "application/json"})
        response = connection.getresponse()
        return response.status, response.read(), response.getheader("X-Leaf-Synthetic-Replay")
    finally:
        connection.close()


def sp_supervise(tmp_path, launcher, directory, *, max_seconds=30):
    args = argparse.Namespace(run_root=tmp_path / "run", run_id="test-run", manifest=None,
                              profile="rooftop", max_seconds=max_seconds, launcher=launcher,
                              server_dir=directory, launcher_args=["--"])
    code = replay.supervise(args)
    path = args.run_root / "synthetic-sizing-receipt.json"
    return code, path, json.loads(path.read_bytes())


def sp_launcher(tmp_path, *, events=False, mode="normal"):
    launcher, directory = fake_launcher(tmp_path, roles=True, mode=mode)
    (directory / "leaf_cloud_client.py").write_text(
        "SOLVER_URL = 'unpatched'\nimport requests\n", encoding="utf-8")
    entry = (
        "import json, os\nfrom pathlib import Path\n"
        "import solar_sizing_client as sizing\nimport leaf_cloud_client as cloud\n"
        "Path(__file__).with_suffix('.observed.json').write_text(json.dumps({"
        "'sizing': sizing.SIZING_URL, 'solver': cloud.SOLVER_URL,"
        "'isolated': sizing.requests is cloud.requests and not hasattr(sizing.requests, 'Session'),"
        "'grant': os.environ.get('LEAF_CLOUD_GRANTS_FILE'),"
        "'database': os.environ.get('DATABASE_URL')}))\n")
    for role in ("broker", "app", "other"):
        (directory / (role + ".py")).write_text(entry, encoding="utf-8")
    if events:
        source = launcher.read_text(encoding="utf-8")
        source = source.replace("    return 0\n", """
    import http.client
    receipt = json.loads((root / 'run/synthetic-sizing-receipt.json').read_text())
    token = json.loads((root / 'run/synthetic-cloud-grants.json').read_text())['synthetic-sizing-g2']['access_token']
    for endpoint, wire in json.loads(Path(os.environ['SP_TEST_WIRES_FILE']).read_bytes()):
        port = int(receipt['endpoints'][endpoint].split(':')[2].split('/')[0])
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
        connection.request('POST', endpoint, bytes.fromhex(wire),
                           {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        if connection.getresponse().status != 200: raise RuntimeError('replay failed')
        connection.close()
    return 0
""")
        launcher.write_text(source, encoding="utf-8")
    return launcher, directory


def test_sp21a_01_package_bytes_reconstructed():
    rebuilt = sp_reconstruct()
    assert rebuilt == SP_PACKAGE.read_bytes()
    assert len(rebuilt) == 366852 and digest(rebuilt) == SP_PACKAGE_SHA
    package = json.loads(rebuilt)
    assert set(package) == {"schema", "label", "source_intake_sha256", "upload_intake_sha256",
                            "source_solve_fixture_sha256", "fixtures"}
    assert [pair["id"] for pair in package["fixtures"]] == ["rooftop-piece-" + str(p) for p in SP_PIECES]
    assert all(set(pair) == {"id", "request", "response"} for pair in package["fixtures"])


def test_sp21a_02_sizing_rooftop_profile_returns_14(sp_listeners, monkeypatch, tmp_path):
    fixture = json.loads(SP_SIZING.read_bytes())
    assert digest(SP_SIZING.read_bytes()) == "5eaf2977819d43d5a6321893535a4902712f5c7b4d427ee53d6fb5bd4c2ddb1a"
    wire = client.wire_bytes(client.SizingRequest.model_validate(fixture["request"]))
    assert (len(wire), digest(wire)) == (350, REQUEST_SHA)
    status, raw, label = sp_post(sp_listeners[0], wire)
    assert status == 200 and label == replay.ROOFTOP_LABEL
    assert raw == replay.response_span(SP_SIZING.read_bytes())
    assert (len(raw), digest(raw)) == (718, SP_SIZING_SHA)
    monkeypatch.setattr(client, "SIZING_URL", client.SIZING_URL)
    monkeypatch.setattr(client, "requests", client.requests)
    replay.prepare_child("broker", sp_listeners[0].url)
    monkeypatch.setattr(client, "resolve_grant", lambda *args: CloudGrant("demo-tenant", "test-token"))
    result = client.size({"grant_ref": replay.GRANT_REF, "request": fixture["request"]},
                         "demo-tenant", "sizing-proof")
    assert result["job_id"] == "sizing-proof" and result["tenant_id"] == "demo-tenant"
    assert result["wire_response_sha256"] == SP_SIZING_SHA
    assert result["response"]["pmp"] == 595
    sizing = result["sizing"]
    assert sizing["panels_in_sequence"] == 14
    assert sizing["voc_cold"] == {
        "passes": True, "override_accepted": False, "suggested_string_length": 0,
        "per_module": 54.49452456029573, "string_voltage": 762.9233438441402,
        "max_dc_voltage": 1500.0}


@pytest.mark.parametrize("piece", SP_PIECES)
def test_sp21a_03_solve_requests_pinned(piece):
    cloud = replay.solve_module()
    pair = sp_pair(piece)
    pin = SP_PINS[SP_PIECES.index(piece)]
    request = cloud.StringerRequest.model_validate(pair["request"])
    wire = cloud.canonical_bytes(request.wire_payload())
    assert (len(wire), digest(wire)) == (pin[1], pin[2])
    assert request.grid.Dwgname == "rooftop_demo.dwg"
    assert all(p.Seq == 0 for row in request.grid.Rows for p in row.Panels)
    manifest = json.loads(replay.ROOFTOP_SOLVE_MANIFEST.read_bytes())
    loaded = replay.load_solve_manifest(replay.ROOFTOP_SOLVE_MANIFEST, REPO)
    assert set(loaded) == {entry["request_wire_sha256"] for entry in manifest["fixtures"]}
    assert set(loaded) == {row[2] for row in SP_PINS}
    for entry in manifest["fixtures"]:
        value = loaded[entry["request_wire_sha256"]]
        assert value[0] == entry["id"] and value[2] == entry["response_wire_sha256"]


@pytest.mark.parametrize("piece", SP_PIECES)
def test_sp21a_04_solve_responses_pinned_and_validated(piece, sp_listeners, monkeypatch):
    cloud = replay.solve_module()
    pair, pin = sp_pair(piece), SP_PINS[SP_PIECES.index(piece)]
    wire = cloud.canonical_bytes(pair["request"])
    status, raw, label = sp_post(sp_listeners[1], wire)
    assert status == 200 and label == replay.ROOFTOP_LABEL
    assert raw == cloud.canonical_bytes(pair["response"])
    assert (len(raw), digest(raw)) == (pin[3], pin[4])
    response = cloud.StringerResponse.model_validate_json(raw)
    request = cloud.StringerRequest.model_validate(pair["request"])
    response.check_echo(request)
    path = response.original_visited_path(request)
    assert response.data.final_grid.Sequences == pin[5]
    assert len(response.data.best_result.info.sequence_length) == pin[6]
    assert len(path) == sum(a * b for a, b in zip(*pin[5]))
    assert response.job_id == response.data.best_result.grid_id
    monkeypatch.setattr(client, "SIZING_URL", client.SIZING_URL)
    monkeypatch.setattr(client, "requests", client.requests)
    monkeypatch.setattr(cloud, "SOLVER_URL", cloud.SOLVER_URL)
    monkeypatch.setattr(cloud, "requests", cloud.requests)
    replay.prepare_child("broker", sp_listeners[0].url, solver_url=sp_listeners[1].url)
    monkeypatch.setattr(cloud, "resolve_grant", lambda *args: CloudGrant("demo-tenant", "test-token"))
    params = {"grant_ref": replay.GRANT_REF, "request": pair["request"]}
    job = "proof-piece-" + str(piece)
    result = cloud.proposal(params, "demo-tenant", job)
    assert result["response_sha256"] == pin[4] and result["request_sha256"] == pin[2]
    assert result["visited_path"] == path
    assert result["job_id"] == job and result["tenant_id"] == "demo-tenant"
    assert cloud.proposal_provenance(result, params, "demo-tenant", job)["solver"]["endpoint"] == sp_listeners[1].url
    bad = copy.deepcopy(pair["response"])
    bad["job_id"] += "-wrong"
    with pytest.raises(ValueError, match="does not match"):
        cloud.StringerResponse.model_validate(bad).original_visited_path(request)
    bad = copy.deepcopy(pair["response"])
    bad["data"]["final_grid"]["Dwgname"] += "-wrong"
    with pytest.raises(ValueError, match="does not match"):
        cloud.StringerResponse.model_validate(bad).original_visited_path(request)
    bad = copy.deepcopy(pair["response"])
    bad["data"]["best_result"]["info"]["visited_path"][0] = bad["data"]["best_result"]["info"]["visited_path"][1]
    with pytest.raises(ValueError, match="visited path"):
        cloud.StringerResponse.model_validate(bad).original_visited_path(request)


def test_sp21a_05_synthetic_header_on_success_and_refusal(sp_listeners, wire):
    cloud = replay.solve_module()
    for listener, body in zip(sp_listeners, [wire, cloud.canonical_bytes(sp_pair(12)["request"])]):
        status, raw, label = sp_post(listener, body)
        assert status == 200 and label == replay.ROOFTOP_LABEL
        assert raw == listener.fixture_map[digest(body)][1]
        for method, path, headers, changed, expected in (
            ("POST", listener.endpoint, {}, b"", 401),
            ("POST", listener.endpoint, {"Authorization": "Bearer test-token", "Content-Type": "text/plain"}, b"", 415),
            ("GET", listener.endpoint, None, b"", 405),
            ("POST", "/other", None, b"", 404),
            ("POST", listener.endpoint, None, b"{}", 409),
        ):
            status, _, label = sp_post(listener, changed, method=method, path=path, headers=headers)
            assert status == expected and label == replay.ROOFTOP_LABEL
        prefix = ("POST " + listener.endpoint + " HTTP/1.1\r\nHost: localhost\r\n"
                  "Authorization: Bearer test-token\r\nContent-Type: application/json\r\n").encode()
        for suffix, expected in ((b"\r\n", 411), (b"Content-Length: 99999999\r\n\r\n", 413)):
            answer = raw_transport(listener, prefix + suffix)
            assert b" " + str(expected).encode() + b" " in answer
            assert ("X-Leaf-Synthetic-Replay: " + replay.ROOFTOP_LABEL).encode() in answer
        for malformed in (b"not-an-http-request\r\n",
                          ("POST " + listener.endpoint + " HTTP/9.9\r\nHost: localhost\r\n\r\n").encode()):
            answer = raw_transport(listener, malformed)
            assert ("X-Leaf-Synthetic-Replay: " + replay.ROOFTOP_LABEL).encode() in answer


def test_sp21a_06_changed_zip_sizing_refused(sp_listeners, wire):
    changed = wire.replace(b"44224", b"44225")
    assert digest(changed) == MISMATCH_SHA
    status, raw, _ = sp_post(sp_listeners[0], changed)
    assert status == 409 and json.loads(raw)["classification"] == "SYNTHETIC_REPLAY_REQUEST_MISMATCH"


def test_sp21a_07_changed_drawing_label_solve_refused(sp_listeners):
    wire = replay.solve_module().canonical_bytes(sp_pair(12)["request"])
    changed = wire.replace(b"rooftop_demo.dwg", b"rooftop_dema.dwg")
    assert digest(changed) == "7c4dc3dab2d9b76a181d84e66d5d74124f3d3f4567b0881b5342c459c08c258e"
    assert sp_post(sp_listeners[1], changed)[0] == 409


def test_sp21a_08_reordered_wire_bytes_refused(sp_listeners, wire):
    cloud = replay.solve_module()
    for listener, original in zip(sp_listeners, [wire, cloud.canonical_bytes(sp_pair(12)["request"])]):
        obj = json.loads(original)
        reordered = dict(reversed(list(obj.items())))
        for changed in (json.dumps(obj, indent=1).encode(), json.dumps(reordered).encode()):
            assert json.loads(changed) == obj and changed != original
            assert sp_post(listener, changed)[0] == 409


def test_sp21a_09_bearer_refusals(sp_listeners):
    for listener in sp_listeners:
        for token in (None, "wrong"):
            headers = {"Content-Type": "application/json"}
            if token is not None:
                headers["Authorization"] = "Bearer " + token
            assert sp_post(listener, b"", headers=headers)[0] == 401
        duplicate = ("POST " + listener.endpoint + " HTTP/1.1\r\nHost: localhost\r\n"
                     "Authorization: Bearer test-token\r\nAuthorization: Bearer test-token\r\n"
                     "Content-Type: application/json\r\nContent-Length: 0\r\n\r\n").encode()
        assert b" 401 " in raw_transport(listener, duplicate)


def test_sp21a_10_wrong_content_type_415(sp_listeners):
    for listener in sp_listeners:
        for content in ("text/plain", "application/json; charset=utf-8", "APPLICATION/JSON"):
            assert sp_post(listener, b"", headers={
                "Authorization": "Bearer test-token", "Content-Type": content})[0] == 415


def test_sp21a_11_framing_and_oversize(sp_listeners):
    for listener, limit in zip(sp_listeners, [client.MAX_RESPONSE_BYTES, replay.solve_module().MAX_RESPONSE_BYTES]):
        prefix = ("POST " + listener.endpoint + " HTTP/1.1\r\nHost: localhost\r\n"
                  "Authorization: Bearer test-token\r\nContent-Type: application/json\r\n").encode()
        for suffix in (b"\r\n", b"Content-Length: -1\r\n\r\n",
                       b"Content-Length: 0\r\nContent-Length: 0\r\n\r\n",
                       b"Transfer-Encoding: chunked\r\n\r\n"):
            assert b" 411 " in raw_transport(listener, prefix + suffix)
        for length in (limit + 1, 10 ** 30):
            answer = raw_transport(listener, prefix + ("Content-Length: " + str(length) + "\r\n\r\n").encode())
            assert b" 413 " in answer
        body = b"x" * limit
        assert sp_post(listener, body)[0] == 409


def test_sp21a_12_wrong_endpoint_or_method_refused(sp_listeners):
    for listener in sp_listeners:
        for path in ("/api/ml", "/string-length/", "//string-length", "/api/ml/?x=1", "/STRING-LENGTH"):
            assert sp_post(listener, b"", path=path)[0] == 404
        opposite = "/api/ml/" if listener.endpoint == "/string-length" else "/string-length"
        assert sp_post(listener, b"", path=opposite)[0] == 404
        for method in ("GET", "PUT", "DELETE", "UNKNOWN"):
            assert sp_post(listener, b"", method=method)[0] == 405
        assert listener.failed


def test_sp21a_13_startup_refusals(tmp_path, monkeypatch):
    args = argparse.Namespace(run_root=tmp_path / "run", run_id="refused", manifest=None,
                              profile="rooftop", max_seconds=30, launcher=None,
                              server_dir=None, launcher_args=[])
    def forbidden(*args, **kwargs):
        pytest.fail("invalid manifest reached a listener or grant/child launch")
    monkeypatch.setattr(replay, "ReplayServer", forbidden)
    monkeypatch.setattr(replay, "write_grant_file", forbidden)
    original_sizing = replay.ROOFTOP_SIZING_MANIFEST
    original_solve = replay.ROOFTOP_SOLVE_MANIFEST
    sizing = json.loads(original_sizing.read_bytes())
    solve = json.loads(original_solve.read_bytes())
    bad_sizing = copy.deepcopy(sizing)
    bad_sizing["fixtures"][0]["fixture_sha256"] = "0" * 64
    bad_package = copy.deepcopy(solve)
    bad_package["package"]["sha256"] = "0" * 64
    escaped = copy.deepcopy(solve)
    escaped["package"]["path"] = "../rooftop_solve_replay.json"
    duplicate = copy.deepcopy(solve)
    duplicate["fixtures"][1]["request_wire_sha256"] = duplicate["fixtures"][0]["request_wire_sha256"]
    for index, (kind, value) in enumerate((("sizing", bad_sizing), ("solve", bad_package),
                                          ("solve", escaped), ("solve", duplicate))):
        path = tmp_path / ("manifest-" + str(index) + ".json")
        path.write_text(json.dumps(value), encoding="utf-8")
        monkeypatch.setattr(replay, "ROOFTOP_SIZING_MANIFEST", path if kind == "sizing" else original_sizing)
        monkeypatch.setattr(replay, "ROOFTOP_SOLVE_MANIFEST", path if kind == "solve" else original_solve)
        with pytest.raises(replay.ReplayConfigError):
            replay.supervise(args)
        assert not (args.run_root / "synthetic-cloud-grants.json").exists()
        assert not (args.run_root / "synthetic-sizing-receipt.json").exists()
    corrupt_root = tmp_path / "corrupt"
    package_path = corrupt_root / solve["package"]["path"]
    package_path.parent.mkdir(parents=True)
    package_path.write_bytes(SP_PACKAGE.read_bytes().replace(b"rooftop_demo", b"rooftop_dema", 1))
    with pytest.raises(replay.ReplayConfigError, match="pin mismatch"):
        replay.load_solve_manifest(original_solve, corrupt_root)
    alias_root = tmp_path / "alias"
    alias_path = alias_root / "data/aliased-package.json"
    alias_path.parent.mkdir(parents=True)
    alias_path.write_bytes(SP_PACKAGE.read_bytes())
    aliased = copy.deepcopy(solve)
    aliased["package"]["path"] = "data/aliased-package.json"
    alias_manifest = tmp_path / "aliased-manifest.json"
    alias_manifest.write_text(json.dumps(aliased), encoding="utf-8")
    monkeypatch.setattr(replay, "REPO", alias_root)
    monkeypatch.setattr(replay, "ROOFTOP_SIZING_MANIFEST", original_sizing)
    monkeypatch.setattr(replay, "ROOFTOP_SOLVE_MANIFEST", alias_manifest)
    monkeypatch.setattr(replay, "load_manifest", lambda *args, **kwargs: {})
    with pytest.raises(replay.ReplayConfigError, match="path: outside scripts/fixtures"):
        replay.supervise(args)
    assert not (args.run_root / "synthetic-cloud-grants.json").exists()
    assert not (args.run_root / "synthetic-sizing-receipt.json").exists()


def test_sp21a_14_default_profile_unchanged(server, wire):
    status, raw, label = sp_post(server, wire)
    assert status == 200 and label == replay.LABEL
    assert len(raw) == 3862 and digest(raw) == RESPONSE_SHA
    assert client.validate_response(json.loads(raw))["panels_in_sequence"] == 27
    assert digest(wire) == REQUEST_SHA


def sp_runner_source():
    text = RUNNER.read_text(encoding="utf-8")
    return re.sub(r"#[^\n]*", "", re.sub(r"<#.*?#>", "", text, flags=re.DOTALL))


def test_sp21a_15_both_switches_refused():
    text = sp_runner_source()
    assert "[switch]$SyntheticRooftop" in text
    condition = text.index("if ($SyntheticStringSizing -and $SyntheticRooftop)")
    refusal = text.index("throw 'SyntheticRooftop and SyntheticStringSizing are mutually exclusive'")
    assert condition < refusal < text.index("foreach ($port")
    assert refusal < text.index("New-Item") < text.index("$launcher = Start-Process")


def test_sp21a_16_no_switch_no_replay():
    text = sp_runner_source()
    defaults = text[:text.index("$ErrorActionPreference")]
    assert re.search(r"\[switch\]\$SyntheticRooftop\s*(,|\))", defaults)
    assert "[switch]$SyntheticRooftop =" not in defaults
    assert "[switch]$SyntheticStringSizing =" not in defaults
    start = text.index("  if ($SyntheticRooftop) {", text.index("$launcherArgs = @("))
    end = text.index("$launcher = Start-Process", start)
    selected = text[start:end]
    assert "} elseif ($SyntheticStringSizing)" in selected
    assert selected.endswith("  }\n  ")
    assert "} else {\n    Remove-Item Env:LEAF_CLOUD_GRANTS_FILE" in selected
    assert text[:start].count("'scripts/start-leaf.py', '--with-harness'") == 1


def test_sp21a_17_live_auth_posture_refused(tmp_path, monkeypatch):
    args = argparse.Namespace(run_root=tmp_path / "run", run_id="refused", manifest=None,
                              profile="rooftop", max_seconds=30, launcher=None,
                              server_dir=None, launcher_args=[])
    def forbidden(*args, **kwargs):
        pytest.fail("live posture reached replay configuration or launch")
    monkeypatch.setattr(replay, "load_manifest", forbidden)
    for auth, posture in (("1", ""), ("0", "production"), ("0", "staging"), ("0", "unknown")):
        monkeypatch.setenv("LEAF_AUTH_LIVE", auth)
        monkeypatch.setenv("LEAF_RUNTIME_ENV", posture)
        assert replay.supervise(args) == 2
        assert not args.run_root.exists()


def test_sp21a_18_browser_json_round_trip_keeps_hashes():
    cloud = replay.solve_module()
    # Equivalent JSON object round trip: ECMAScript stringify emits integral
    # doubles as integer tokens. The real strict model restores float fields.
    def number(value):
        parsed = float(value)
        return int(parsed) if parsed.is_integer() else parsed
    for piece, _, expected, *_ in SP_PINS:
        browser_json = json.dumps(sp_pair(piece)["request"], separators=(",", ":"))
        browser_object = json.loads(browser_json, parse_float=number)
        final = cloud.canonical_bytes(cloud.StringerRequest.model_validate(browser_object).wire_payload())
        assert digest(final) == expected
        assert final == cloud.canonical_bytes(sp_pair(piece)["request"])


def test_sp21a_19_bootstrap_points_both_clients_grant_broker_only(tmp_path, monkeypatch, sp_listeners):
    cloud = replay.solve_module()
    trust_env_at_send = []
    original_session = requests.Session

    def tracked_session():
        session = original_session()
        original_post = session.post

        def send(*args, **kwargs):
            trust_env_at_send.append(session.trust_env)
            return original_post(*args, **kwargs)

        session.post = send
        return session

    monkeypatch.setattr(requests, "Session", tracked_session)
    for role in ("app", "broker"):
        monkeypatch.setattr(client, "SIZING_URL", client.SIZING_URL)
        monkeypatch.setattr(client, "requests", client.requests)
        monkeypatch.setattr(cloud, "SOLVER_URL", cloud.SOLVER_URL)
        monkeypatch.setattr(cloud, "requests", cloud.requests)
        assert replay.prepare_child(role, sp_listeners[0].url, solver_url=sp_listeners[1].url) == REPO / ("server/" + role + ".py")
        assert client.SIZING_URL == sp_listeners[0].url and cloud.SOLVER_URL == sp_listeners[1].url
        assert client.requests is cloud.requests
        assert client.requests is not requests
        sizing_request = client.SizingRequest.model_validate(json.loads(SP_SIZING.read_bytes())["request"])
        assert digest(client.post_string_length(sizing_request, CloudGrant("demo-tenant", "test-token"))) == SP_SIZING_SHA
        with cloud.requests.post(sp_listeners[1].url,
                                 data=cloud.canonical_bytes(sp_pair(12)["request"]),
                                 headers={"Authorization": "Bearer test-token", "Content-Type": "application/json"},
                                 timeout=3, allow_redirects=False, stream=True) as response:
            assert response.status_code == 200 and response.content
    assert trust_env_at_send == [False, False, False, False]
    with pytest.raises(replay.ReplayConfigError, match="solver-url"):
        replay.prepare_child("app", sp_listeners[0].url, solver_url="https://api.leafdesign.ai/api/ml/")
    launcher, directory = sp_launcher(tmp_path)
    code, receipt_path, receipt = sp_supervise(tmp_path, launcher, directory)
    assert code == 0 and receipt["bootstrap_roles"] == ["broker", "app"]
    assert replay.verify_receipt(receipt_path, "test-run", profile="rooftop") == 0
    observed = {role: json.loads((directory / (role + ".observed.json")).read_bytes())
                for role in ("broker", "app", "other")}
    for role in ("app", "broker"):
        assert observed[role]["sizing"] == receipt["endpoints"]["/string-length"]
        assert observed[role]["solver"] == receipt["endpoints"]["/api/ml/"]
        assert observed[role]["isolated"] is True
    assert observed["broker"]["grant"] == str(receipt_path.parent / "synthetic-cloud-grants.json")
    assert observed["app"]["grant"] is None and observed["other"]["grant"] is None
    assert observed["other"]["sizing"] == observed["other"]["solver"] == "unpatched"
    assert all(value["database"] is None for value in observed.values())
    for endpoint in receipt["endpoints"].values():
        assert_listener_closed(endpoint)


def test_sp21a_20_lifecycle_cleanup(tmp_path):
    for mode, expected in (("ttl", 1), ("exception", 1), ("stop", 0)):
        root = tmp_path / mode
        launcher, directory = fake_launcher(root, mode=mode)
        code, _, receipt = sp_supervise(root, launcher, directory, max_seconds=1 if mode == "ttl" else 30)
        assert code == expected and receipt["cleanup_complete"] is True
        cleaned = json.loads((root / "cleaned.json").read_bytes())
        assert len(cleaned) == 1 and cleaned[0] is not None
        assert int((root / "child-pid").read_text()) > 0
        assert receipt["ttl_expired"] is (mode == "ttl")
        assert receipt["ok"] is (mode == "stop")
        for endpoint in receipt["endpoints"].values():
            assert_listener_closed(endpoint)


def sp_docker_excluded(asset, lines):
    """Apply ordered Docker ignore globs to the file and its parent directories."""
    excluded = False
    candidates = ["/".join(asset.split("/")[:i]) for i in range(1, len(asset.split("/")) + 1)]
    for line in lines:
        if line.startswith("#"):
            continue
        pattern = line.strip()
        if not pattern or pattern == ".":
            continue
        negate = pattern.startswith("!")
        pattern = pattern[1:] if negate else pattern
        pattern = pattern.strip("/")
        regex, pos = "", 0
        while pos < len(pattern):
            if pattern[pos:pos + 3] == "**/":
                regex += "(?:.*/)?"
                pos += 3
            elif pattern[pos:pos + 2] == "**":
                regex += ".*"
                pos += 2
            elif pattern[pos] == "*":
                regex += "[^/]*"
                pos += 1
            elif pattern[pos] == "?":
                regex += "[^/]"
                pos += 1
            elif pattern[pos] == "[":
                end = pattern.index("]", pos + 1)
                regex += pattern[pos:end + 1]
                pos = end + 1
            else:
                regex += re.escape(pattern[pos])
                pos += 1
        if any(re.fullmatch(regex, candidate) for candidate in candidates):
            excluded = not negate
    return excluded


def test_sp21a_21_packaging_all_seven_recipes():
    # Source/build-context evidence only: this row neither builds nor inspects images.
    ignores = (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert all(sp_docker_excluded(asset, ignores) for asset in SP_ASSETS)
    assert not sp_docker_excluded("scripts/proof_string_sizer.py",
                                  ignores + ["!scripts/proof_string_sizer.py"])
    recipes = ["deploy/Dockerfile." + name for name in
               ("app", "broker", "canonical-worker", "harness", "instant-execution", "web")]
    recipes.append("e2b.Dockerfile")
    for recipe in recipes:
        path = REPO / recipe
        assert path.is_file()
        # Dockerfile-specific ignores replace the root file; they must preserve these exclusions.
        override = Path(str(path) + ".dockerignore")
        if override.exists():
            override_lines = override.read_text(encoding="utf-8").splitlines()
            assert all(sp_docker_excluded(asset, override_lines) for asset in SP_ASSETS)
        text = path.read_text(encoding="utf-8")
        assert "proof_string_sizer" not in text and "rooftop_solve_replay" not in text
        for asset in SP_ASSETS:
            assert asset not in text
    for directory in ("server", "harness", "platform"):
        for path in (REPO / directory).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "proof_string_sizer" not in text and "rooftop_solve_replay" not in text
    for path in (REPO / "web/src").rglob("*"):
        if path.is_file() and path.suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs"}:
            text = path.read_text(encoding="utf-8")
            assert "proof_string_sizer" not in text and "rooftop_solve_replay" not in text
    launcher = (SCRIPTS / "start-leaf.py").read_text(encoding="utf-8")
    assert "proof_string_sizer" not in launcher and "rooftop_solve_replay" not in launcher


def test_sp21a_22_receipt_profile_pins_events(tmp_path, monkeypatch, wire):
    cloud = replay.solve_module()
    wires = [["/string-length", wire.hex()]] + [
        ["/api/ml/", cloud.canonical_bytes(sp_pair(piece)["request"]).hex()] for piece in SP_PIECES]
    # Windows limits a single environment variable to 32767 characters; only
    # a disposable stimulus-file path enters the fake launcher's environment.
    wires_path = tmp_path / "request-wires.json"
    wires_path.write_text(json.dumps(wires), encoding="utf-8")
    monkeypatch.setenv("SP_TEST_WIRES_FILE", str(wires_path))
    launcher, directory = sp_launcher(tmp_path, events=True)
    code, path, receipt = sp_supervise(tmp_path, launcher, directory)
    assert code == 0 and replay.verify_receipt(path, "test-run", profile="rooftop") == 0
    assert receipt["profile"] == "rooftop" and receipt["pins"] == replay.rooftop_pins()
    assert receipt["pins"]["package_sha256"] == SP_PACKAGE_SHA
    assert len(receipt["events"]) == 8  # This test stimulus, not the supervisor's generic success condition.
    assert [event["path"] for event in receipt["events"]] == ["/string-length"] + ["/api/ml/"] * 7
    assert all(event["endpoint"] == receipt["endpoints"][event["path"]] for event in receipt["events"])
    token = json.loads((path.parent / "synthetic-cloud-grants.json").read_bytes())[replay.GRANT_REF]["access_token"]
    assert token.encode() not in path.read_bytes() and b"Bearer" not in path.read_bytes()
    assert receipt["cleanup_complete"] is True
    variants = [
        dict(receipt, profile="wrong"), dict(receipt, label="wrong"), dict(receipt, ok=False),
        dict(receipt, cleanup_complete=False), dict(receipt, run_id="other"),
        dict(receipt, manifest_sha256="0" * 64), dict(receipt, fixtures=[]),
        dict(receipt, solve_fixtures=[]),
        dict(receipt, pins=dict(receipt["pins"], package_sha256="0" * 64)),
        dict(receipt, endpoints=dict(receipt["endpoints"], **{"/api/ml/": "https://example.invalid/api/ml/"})),
    ]
    variants.append({key: value for key, value in receipt.items() if key != "profile"})
    for key, value in (("method", "GET"), ("status", 409), ("request_sha256", "0" * 64),
                       ("response_sha256", "0" * 64), ("fixture_id", "wrong"),
                       ("endpoint", "http://127.0.0.1:1/api/ml/"), ("path", "/other")):
        changed = copy.deepcopy(receipt)
        changed["events"][1][key] = value
        variants.append(changed)
    for changed in variants:
        replay.atomic_json(path, changed)
        assert replay.verify_receipt(path, "test-run", profile="rooftop") == 1
    replay.atomic_json(path, receipt)
    assert replay.verify_receipt(path, "test-run", profile="rooftop") == 0


def test_sp21a_23_stop_request_completes_receipt(tmp_path):
    launcher, directory = sp_launcher(tmp_path, mode="graceful")
    errors = []

    def request_stop():
        deadline = time.monotonic() + 15
        while not (tmp_path / "ready").exists():
            if time.monotonic() >= deadline:
                errors.append("launcher never became ready")
                return
            time.sleep(0.05)
        (tmp_path / "run/stop-request").touch()

    helper = threading.Thread(target=request_stop)
    started = time.monotonic()
    helper.start()
    try:
        code, path, receipt = sp_supervise(tmp_path, launcher, directory, max_seconds=30)
    finally:
        helper.join(timeout=16)
    assert not helper.is_alive() and not errors
    assert time.monotonic() - started < 20
    assert code == 0 and receipt["bootstrap_roles"] == ["broker", "app"]
    assert receipt["cleanup_complete"] is True
    assert receipt["ttl_expired"] is False and receipt["ok"] is True
    assert replay.verify_receipt(path, "test-run", profile="rooftop") == 0
    assert (tmp_path / "cleaned.json").is_file()
    for endpoint in receipt["endpoints"].values():
        assert_listener_closed(endpoint)


def test_sp21a_24_receipt_profile_is_the_callers(tmp_path, monkeypatch, wire):
    wires = [["/string-length", wire.hex()]]
    wires_path = tmp_path / "request-wires.json"
    wires_path.write_text(json.dumps(wires), encoding="utf-8")
    monkeypatch.setenv("SP_TEST_WIRES_FILE", str(wires_path))
    rooftop_root = tmp_path / "rooftop"
    launcher, directory = sp_launcher(rooftop_root, events=True)
    code, path, receipt = sp_supervise(rooftop_root, launcher, directory)
    assert code == 0 and replay.verify_receipt(path, "test-run", profile="rooftop") == 0
    assert replay.verify_receipt(path, "test-run", profile="string-sizing") == 1
    assert replay.verify_receipt(path, "test-run", profile="unknown") == 1
    changed = copy.deepcopy(receipt)
    changed["events"][0]["status"] = 409
    changed.update(cleanup_complete=False, pins={}, profile="wrong", label=replay.LABEL)
    replay.atomic_json(path, changed)
    for profile in ("rooftop", "string-sizing"):
        assert replay.verify_receipt(path, "test-run", profile=profile) == 1
    old_root = tmp_path / "old"
    launcher, directory = fake_launcher(old_root)
    code, path, receipt = supervise(old_root, launcher, directory)
    assert code == 0 and "profile" not in receipt
    assert replay.verify_receipt(path, "test-run") == 0
    assert replay.verify_receipt(path, "test-run", profile="rooftop") == 1


def test_sp21a_25_runner_requests_a_graceful_stop():
    text = sp_runner_source()
    final = text[text.index("} finally {\n  if ($launcher"):]
    synthetic = final[final.index("if ($SyntheticStringSizing -or $SyntheticRooftop)"):
                      final.index("    } else {")]
    assert synthetic.index("Join-Path $runRoot 'stop-request'") < synthetic.index("New-Item")
    assert re.search(r"New-Item\s+-ItemType File\s+-Path \$stopRequest\s+-Force", synthetic)
    assert synthetic.index("New-Item") < synthetic.index("WaitForExit(60000)") < synthetic.index("taskkill")
    assert "verify-receipt --receipt $sizingReceipt --run-id $runId --profile $receiptProfile" in final
    assert "$receiptProfile = if ($SyntheticRooftop) { 'rooftop' } else { 'string-sizing' }" in final
