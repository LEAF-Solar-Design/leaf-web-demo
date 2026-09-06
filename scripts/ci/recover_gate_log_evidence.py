"""One-run log-derived evidence, never a reconstructed canonical gate proof.

The native gate fetches provider records itself. A release trusts this document
only after authenticating the separate native gate's immutable AWS archive.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import urllib.request
import urllib.error
from pathlib import Path
from urllib.parse import urlparse

REPO = "LEAF-Solar-Design/leaf-web-demo"
REPO_ID = 1304548236
RUN = 34058029190
ATTEMPT = 1
TESTED = "987f722f1f0e00dff00a85352cce87fef93fa465"
SCHEMA = "leaf.github-log-recovery.v1"
FILES = frozenset(("scripts/ci/recover_gate_log_evidence.py",
    "scripts/test_gate_log_evidence.py", "scripts/ci/native_release_producer.py",
    "scripts/test_native_release_producer.py", ".codebuild/release.yml"))
PRODUCERS = ("scripts/run-all-gates.py", ".github/workflows/test-gate.yml",
             ".github/workflows/build-platform-images.yml")
FOCUSED = ("scripts/test_gate_log_evidence.py", "scripts/test_native_release_producer.py")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def helper_client(root, *, opted_in=False):
    """Only the provider's read-only helper, never a desktop credential chain.

    The fixed helper layout is qualified at runtime, not assumed available.
    No caller-selected executable or credential store is consulted.
    """
    require(opted_in is True, "CodeBuild helper transport requires explicit opt-in")
    query = f"protocol=https\nhost=github.com\npath={REPO}.git\n\n".encode()
    try:
        result = subprocess.run(["/codebuild/readonly/bin/git-credential-helper", "get"],
            input=query, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        raise ValueError("CodeBuild helper absent or unavailable") from None
    require(result.returncode == 0, "CodeBuild helper refused credentials")
    require(len(result.stdout) <= 16384, "CodeBuild helper response exceeds bound")
    try:
        fields = {}
        for line in result.stdout.decode("utf-8", errors="strict").splitlines():
            if not line:
                continue
            key, value = line.split("=", 1)
            require(key not in fields, "duplicate helper field")
            fields[key] = value
        require(set(fields) <= {"protocol", "host", "path", "username", "password"},
                "unrecognized helper field")
        for key, expected in (("protocol", "https"), ("host", "github.com"), ("path", REPO + ".git")):
            require(fields.get(key, expected) == expected, "helper returned foreign credential scope")
        require(bool(fields.get("username")) and bool(fields.get("password")), "helper returned incomplete credentials")
        return GitHub(fields["password"])
    except (ValueError, UnicodeError):
        raise ValueError("CodeBuild helper returned invalid credentials") from None


class LogRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        require(parsed.scheme == "https" and not parsed.username and not parsed.password
                and parsed.hostname is not None
                and (parsed.hostname.endswith(".blob.core.windows.net")
                     or parsed.hostname.endswith(".actions.githubusercontent.com")),
                "unrecognized log redirect origin")
        # Construct a fresh request. Never forward API authorization to storage.
        return urllib.request.Request(newurl, headers={"User-Agent": "leaf-gate-recovery"})


class GitHub:
    def __init__(self, token):
        require(isinstance(token, str) and bool(token) and not re.search(r"\s", token),
                "an explicitly admitted ephemeral GitHub token is required")
        self.token = token
        self.opener = urllib.request.build_opener(LogRedirect())

    def get(self, path, *, raw=False):
        require(path.startswith(f"repos/{REPO}/actions/") and ".." not in path,
                "API request outside fixed repository")
        request = urllib.request.Request("https://api.github.com/" + path, headers={
            "Authorization": "Bearer " + self.token,
            "Accept": "application/vnd.github+json", "User-Agent": "leaf-gate-recovery"})
        try:
            with self.opener.open(request, timeout=45) as response:
                data = response.read(8 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            status = exc.code
            raise ValueError(f"GitHub evidence access refused (HTTP {status})") from None
        except (urllib.error.URLError, OSError):
            raise ValueError("GitHub evidence transport unavailable") from None
        require(len(data) <= 8 * 1024 * 1024, "provider response exceeds bound")
        return data if raw else json.loads(data)


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, timeout=30).decode().strip()


def projection(root):
    """Whole application tree equality except the five frozen recovery files."""
    try:
        git(root, "cat-file", "-e", TESTED + "^{commit}")
    except subprocess.CalledProcessError:
        # CodeBuild may provide a shallow checkout. Fetch only the frozen public
        # commit, without changing origin, a branch, or working-tree files.
        subprocess.run(["git", "fetch", "--no-tags", "https://github.com/" + REPO + ".git", TESTED],
                       cwd=root, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, timeout=90)
    source = git(root, "rev-parse", "HEAD")
    tree = git(root, "rev-parse", "HEAD^{tree}")
    tested_tree = git(root, "rev-parse", TESTED + "^{tree}")
    changed = set(filter(None, git(root, "diff", "--name-only", "--no-renames", TESTED, source).splitlines()))
    require(changed <= FILES, "candidate changes application files outside recovery projection")
    require(not git(root, "status", "--porcelain", "--untracked-files=normal"),
            "recovery requires a clean immutable checkout")
    blobs = {p: git(root, "rev-parse", TESTED + ":" + p) for p in PRODUCERS}
    require(all(git(root, "rev-parse", source + ":" + p) == v for p, v in blobs.items()),
            "historical producer or catalog changed")
    return {"tested_source": TESTED, "tested_tree": tested_tree,
            "recovery_source": source, "recovery_tree": tree,
            "changed_files": sorted(changed), "producer_blobs": blobs}


def catalog(root):
    spec = importlib.util.spec_from_file_location("recovered_original_catalog", root / PRODUCERS[0])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    suites = module.build_suites()
    return module.catalog_fingerprint(suites), module.partition_suites(suites, 8)


def inspect_shard(job, raw, expected):
    index = int(job["name"].rsplit("-", 1)[1])
    require(job.get("run_id") == RUN and job.get("head_sha") == TESTED
            and job.get("status") == "completed" and job.get("conclusion") == "failure",
            "foreign or unfinished shard job")
    steps = job["steps"]
    tests = [s for s in steps if s["name"] == f"Run gate shard {index}"]
    require(len(tests) == 1 and tests[0]["conclusion"] == "success", "test step did not succeed")
    require([s["name"] for s in steps if s["conclusion"] not in ("success", "skipped")]
            == ["Upload shard result and logs"], "failure is not upload-only")
    text = raw.decode("utf-8", errors="strict")
    require(re.search(r"Z " + TESTED + r"\s*$", text, re.M), "checkout source absent from job log")
    require("Failed to CreateArtifact: Artifact storage quota has been hit" in text,
            "original quota failure absent")
    # Provider log timestamps must lie within the authenticated test step.
    step = tests[0]
    lines = []
    for line in text.splitlines():
        match = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?Z (.*)", line)
        if match and step["started_at"][:19] <= match[1] <= step["completed_at"][:19]:
            lines.append(match[2])
    rows = {}
    for line in lines:
        match = re.search(r"\s*\.\.\.\s+(\S+)\s+(PASS|SKIP|FAIL|UNAVAILABLE)\s+(\S+)\s+([0-9.]+)s(.*)$", line)
        if match:
            identity, status, got, seconds, note = match.groups()
            require(identity not in rows, "duplicate suite result")
            rows[identity] = {"status": status, "reported_got": got, "note": note.strip()}
    require(set(rows) == set(expected), "incomplete or foreign suite partition")
    for identity, row in rows.items():
        require(row["status"] in ("PASS", "SKIP"), "suite failed or unavailable")
        require(row["status"] != "SKIP" or expected[identity].db_gated or expected[identity].opt_in_env,
                "unexpected skipped suite")
    summaries = [re.search(r"suites: (\d+) PASS\s+(\d+) FAIL\s+(\d+) SKIP\s+(\d+) UNAVAILABLE.*test cases passed: (\d+)\s+skipped: (\d+)", line) for line in lines]
    summaries = [s for s in summaries if s]
    require(len(summaries) == 1, "ambiguous or missing scoreboard")
    passed, failed, skipped, unavailable, tests, skipped_tests = map(int, summaries[0].groups())
    require(failed == unavailable == 0 and passed == sum(r["status"] == "PASS" for r in rows.values())
            and skipped == sum(r["status"] == "SKIP" for r in rows.values()), "scoreboard differs from partition")
    return {"job_id": job["id"], "shard": index, "log_sha256": hashlib.sha256(raw).hexdigest(),
            "test_step": "success", "job_conclusion": "failure", "results": rows,
            "reported_passed_cases": tests, "reported_skipped_cases": skipped_tests}


def recover(client, fingerprint, partitions):
    prefix = f"repos/{REPO}/actions/runs/{RUN}"
    run = client.get(prefix + f"/attempts/{ATTEMPT}")
    require(all(run.get(k) == v for k, v in {"id": RUN, "run_attempt": ATTEMPT,
        "head_sha": TESTED, "event": "push", "status": "completed", "conclusion": "failure",
        "path": ".github/workflows/build-platform-images.yml"}.items()), "original run identity differs")
    for key in ("repository", "head_repository"):
        require(run.get(key, {}).get("id") == REPO_ID and run[key].get("full_name") == REPO,
                "foreign repository")
    listing = client.get(prefix + f"/attempts/{ATTEMPT}/jobs?per_page=100")
    jobs = listing["jobs"]
    require(listing["total_count"] == len(jobs) and len(jobs) <= 100,
            "job listing incomplete")
    require(len({j["id"] for j in jobs}) == len(jobs), "duplicate job identity")
    shards = [j for j in jobs if j["name"].startswith("test / gate-shard-")]
    require(len(shards) == 8 and {j["name"] for j in shards} == {f"test / gate-shard-{i}" for i in range(8)},
            "shard matrix incomplete or ambiguous")
    aggregates = [j for j in jobs if j["name"] == "test / run-all-gates"]
    require(len(aggregates) == 1 and aggregates[0].get("conclusion") == "failure"
            and aggregates[0].get("run_id") == RUN and aggregates[0].get("head_sha") == TESTED,
            "original failed aggregate not established")
    aggregate = aggregates[0]
    require([s["name"] for s in aggregate["steps"] if s["conclusion"] not in ("success", "skipped")]
            == ["Verify the complete gate"], "aggregate failed outside missing-results verification")
    aggregate_raw = client.get(f"repos/{REPO}/actions/jobs/{aggregate['id']}/logs", raw=True)
    aggregate_text = aggregate_raw.decode("utf-8", errors="strict")
    require("no shard result files (schema 1) found" in aggregate_text
            and "gate proof NOT emitted: the fan-in did not prove the gate" in aggregate_text
            and "SHARD_JOB_RESULT: failure" in aggregate_text,
            "aggregate missing-results failure absent")
    rows = []
    for job in sorted(shards, key=lambda j: j["name"]):
        index = int(job["name"].rsplit("-", 1)[1])
        raw = client.get(f"repos/{REPO}/actions/jobs/{job['id']}/logs", raw=True)
        rows.append(inspect_shard(job, raw, {s.id: s for s in partitions[index]}))
    return {"schema": SCHEMA, "repository": REPO, "repository_id": REPO_ID,
            "run_id": RUN, "run_attempt": ATTEMPT, "aggregate_conclusion": "failure",
            "canonical_proof_emitted": False, "evidence_kind": "provider-log-derived",
            "aggregate_job_id": aggregate["id"],
            "aggregate_log_sha256": hashlib.sha256(aggregate_raw).hexdigest(),
            "catalog_fingerprint": fingerprint, "shards": rows}


def produce(root, client, env):
    binding = projection(root)
    fingerprint, partitions = catalog(root)
    receipt = recover(client, fingerprint, partitions)
    # These checks cover recovery code only. They do not relabel historical tests.
    command = [sys.executable, "-P", "-m", "pytest", "-q", *[str(root / p) for p in FOCUSED]]
    result = subprocess.run(command, cwd=root / "scripts", env=env, check=True,
                            capture_output=True, timeout=180)
    require(projection(root) == binding, "source changed during recovery")
    receipt["binding"] = binding
    receipt["focused_checks"] = {"files": list(FOCUSED), "exit_code": result.returncode,
        "output_sha256": hashlib.sha256(result.stdout + result.stderr).hexdigest()}
    return receipt


def verify_bound(receipt, root):
    """Only call after independent native gate archive authentication."""
    require(receipt.get("schema") == SCHEMA and receipt.get("binding") == projection(root),
            "recovery source projection differs")
    require(receipt.get("run_id") == RUN and receipt.get("run_attempt") == ATTEMPT
            and receipt.get("repository") == REPO and receipt.get("repository_id") == REPO_ID
            and receipt.get("aggregate_conclusion") == "failure"
            and receipt.get("canonical_proof_emitted") is False
            and receipt.get("evidence_kind") == "provider-log-derived", "recovery provenance differs")
    fingerprint, partitions = catalog(root)
    require(receipt.get("catalog_fingerprint") == fingerprint, "catalog differs")
    rows = receipt.get("shards", [])
    require(len(rows) == 8 and {r["shard"] for r in rows} == set(range(8))
            and len({r["job_id"] for r in rows}) == 8, "recovery shard identities differ")
    for row in rows:
        require(set(row["results"]) == {s.id for s in partitions[row["shard"]]}
                and row["test_step"] == "success" and row["job_conclusion"] == "failure",
                "recovery partition differs")
        require(all(r["status"] in ("PASS", "SKIP") for r in row["results"].values()), "recovery has failed suites")
    checks = receipt.get("focused_checks", {})
    require(checks.get("files") == list(FOCUSED) and checks.get("exit_code") == 0
            and re.fullmatch(r"[0-9a-f]{64}", checks.get("output_sha256", "")), "focused checks absent")
