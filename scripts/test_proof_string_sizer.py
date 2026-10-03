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
