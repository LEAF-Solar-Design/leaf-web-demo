"""Trusted, offline test selection. Inputs are supplied by the trusted loader."""

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile


DECISION_SCHEMA = "leaf.ci.selection-decision.v1"
MAP_PATH = "scripts/ci/test-selection-map.json"
POLICY_PATH = "scripts/ci/test-selection-policy.json"
WINDOW_PATH = "scripts/ci/selection-window.json"
MINIMUM_FULL_RUNS = 3  # [frozen] distinct complete producer runs, never retries
WINDOW_DAYS = 7  # [frozen] fixed before activation


class InvalidInput(ValueError):
    """A diagnosed pre-execution condition which requires full execution."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidInput("duplicate_json_key")
        result[key] = value
    return result


def load_json(path):
    try:
        raw = Path(path).read_bytes()
        value = json.loads(raw, object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(
                               InvalidInput("nonfinite_json")))
        if not isinstance(value, dict):
            raise InvalidInput("invalid_json_object")
        return value, raw
    except (OSError, ValueError, TypeError) as exc:
        raise InvalidInput("unreadable_input") from exc


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_json(path, value):
    atomic_write(path, canonical(value) + b"\n")


def repo_path(value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise InvalidInput("invalid_path")
    if any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise InvalidInput("invalid_path")
    parts = value.split("/")
    if any(p in ("", ".", "..") for p in parts) or PurePosixPath(value).is_absolute():
        raise InvalidInput("invalid_path")
    return value


def strings(value, reason="invalid_ids"):
    if not isinstance(value, list) or any(not isinstance(s, str) or not s or
                                          "\x00" in s for s in value):
        raise InvalidInput(reason)
    if len(set(value)) != len(value):
        raise InvalidInput(reason)
    return value


class Git:
    def __init__(self, repo):
        self.repo = str(Path(repo).absolute())

    def run(self, *args):
        # Do not inherit replacement/config/object-directory injections. No network,
        # candidate imports, shell, textconv or external diff can occur here.
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1")
        try:
            proc = subprocess.run(
                ["git", "--no-pager", "--no-replace-objects", "-C", self.repo,
                 *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InvalidInput("git_unreadable") from exc
        if proc.returncode:
            raise InvalidInput("git_unreadable")
        return proc.stdout

    def resolve(self, name):
        if not isinstance(name, str) or name.startswith("-") or "\x00" in name:
            raise InvalidInput("invalid_revision")
        result = self.run("rev-parse", "--verify", "--end-of-options", name)
        text = result.decode("ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", text):
            raise InvalidInput("invalid_revision")
        return text

    def commit(self, sha):
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
            raise InvalidInput("invalid_commit")
        if self.resolve(sha + "^{commit}") != sha:
            raise InvalidInput("invalid_commit")
        return sha

    def tree(self, sha):
        raw = self.run("ls-tree", "-r", "-z", "--full-tree", sha)
        if raw and not raw.endswith(b"\x00"):
            raise InvalidInput("malformed_tree")
        result = {}
        for record in raw.split(b"\x00")[:-1]:
            try:
                header, path = record.split(b"\t", 1)
                mode, kind, oid = header.decode("ascii").split(" ")
                path = repo_path(path.decode("utf-8", "strict"))
            except (ValueError, UnicodeError) as exc:
                raise InvalidInput("malformed_tree") from exc
            if path in result:
                raise InvalidInput("malformed_tree")
            result[path] = (mode, kind, oid)
        return result


def bind_blob(raw, tree, path):
    """Check extracted policy bytes against the frozen commit's actual blob ID."""
    entry = tree.get(repo_path(path))
    if not entry or entry[0] not in ("100644", "100755") or entry[1] != "blob":
        raise InvalidInput("untrusted_input")
    algorithm = "sha256" if len(entry[2]) == 64 else "sha1"
    blob = b"blob " + str(len(raw)).encode("ascii") + b"\x00" + raw
    if hashlib.new(algorithm, blob).hexdigest() != entry[2]:
        raise InvalidInput("untrusted_input")


def changed_paths(raw):
    if not raw.endswith(b"\x00") and raw:
        raise InvalidInput("malformed_diff")
    tokens = raw.split(b"\x00")[:-1]
    result = set()
    i = 0
    while i < len(tokens):
        status = tokens[i].decode("ascii", "strict")
        i += 1
        rename = re.fullmatch(r"R([0-9]{1,3})", status)
        count = 2 if rename and int(rename.group(1)) <= 100 else 1
        if count == 1 and status not in ("A", "M", "D"):
            raise InvalidInput("unsupported_status")
        if i + count > len(tokens):
            raise InvalidInput("malformed_diff")
        for token in tokens[i:i + count]:
            result.add(repo_path(token.decode("utf-8", "strict")))
        i += count
    return sorted(result)


def catalog_info(catalog):
    if catalog.get("schema") != "leaf.ci.test-catalog.v1":
        raise InvalidInput("invalid_catalog")
    rows = catalog.get("suites")
    if not isinstance(rows, list) or not rows:
        raise InvalidInput("invalid_catalog")
    entries = {}
    for row in rows:
        if not isinstance(row, dict):
            raise InvalidInput("invalid_catalog")
        sid = row.get("id")
        if not isinstance(sid, str) or not sid or any(ord(c) < 32 for c in sid):
            raise InvalidInput("invalid_catalog")
        if sid in entries:
            raise InvalidInput("duplicate_catalog_id")
        strings(row.get("test_ids"), "invalid_collection")
        if row.get("module") is not None:
            repo_path(row["module"])
        entries[sid] = row
    payload = {k: v for k, v in catalog.items() if k != "catalog_sha256"}
    fingerprint = digest(payload)
    if catalog.get("catalog_sha256", fingerprint) != fingerprint:
        raise InvalidInput("catalog_digest_mismatch")
    return entries, fingerprint


def collection_digest(ids):
    return digest(sorted(strings(list(ids), "invalid_collection")))


def expand_only(values, catalog_ids):
    values = strings(values, "unmatched_only")
    expanded = set()
    for value in values:
        matches = {sid for sid in catalog_ids if value in sid}
        if not matches:
            raise InvalidInput("unmatched_only")
        expanded.update(matches)
    if not expanded:
        raise InvalidInput("empty_selection")
    return sorted(expanded)


def protected_path(path, extra_rules=()):
    lower = path.lower()
    name = lower.rsplit("/", 1)[-1]
    prefixes = (".github/", ".codebuild/", "scripts/ci/", "selector/", "ci/")
    names = {"conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "setup.py",
             "tox.ini", "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
             "uv.lock", "poetry.lock", "pipfile", "pipfile.lock", "cargo.toml", "cargo.lock",
             "go.mod", "go.sum", "gemfile", "gemfile.lock", "makefile", "cmakelists.txt",
             "aspects.yaml", ".gitmodules", ".gitattributes", ".gitignore", "ci.sh",
             "tf-plan.sh", "run-all-gates.py", "select_tests.py", "trace_reads.py",
             "pytest_selection.py", "build_map.py", "sitecustomize.py"}
    return (lower.startswith(prefixes) or name in names or
            name.startswith(("dockerfile", "buildspec", "requirements")) or
            name.endswith((".lock", ".lock.hcl")) or
            any(word in lower for word in ("quarantine", "benchmark", "shared_fixture",
                                          "shared-config", "shared_config", "test-selection")) or
            any(fnmatch.fnmatchcase(path, rule) for rule in extra_rules))


def timestamp(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone required")
        return parsed.astimezone(dt.timezone.utc)
    except (AttributeError, TypeError, ValueError) as exc:
        raise InvalidInput("invalid_window") from exc


def validate_event(event, head, repo_slug, pr_number):
    refs = [event.get("head_ref", ""), event.get("source_version", ""),
            event.get("target_ref", "")]
    if any(isinstance(ref, str) and ("gh-readonly-queue/" in ref or
                                    "merge-group" in ref) for ref in refs):
        raise InvalidInput("merge_group")
    if event.get("event") not in ("PULL_REQUEST_CREATED", "PULL_REQUEST_UPDATED",
                                  "PULL_REQUEST_REOPENED"):
        raise InvalidInput("non_pr_event")
    if (event.get("provider") != "github" or event.get("initiator") != "GitHub-Hook" or
            event.get("provider_bound") is not True or not event.get("build_id") or
            not event.get("evidence_ref")):
        raise InvalidInput("unverified_event")
    number = event.get("pr_number")
    if type(number) is not int or number <= 0:
        raise InvalidInput("invalid_pr_identity")
    slug = event.get("repo")
    if not isinstance(slug, str) or not re.fullmatch(r"[\w.-]+/[\w.-]+", slug):
        raise InvalidInput("invalid_pr_identity")
    if ((repo_slug is not None and repo_slug != slug) or
            (pr_number is not None and int(pr_number) != number) or
            event.get("source_version") != "pr/" + str(number) or
            event.get("head_sha") != head):
        raise InvalidInput("contradictory_event")
    for key in ("head_ref", "target_ref"):
        ref = event.get(key)
        if (not isinstance(ref, str) or not ref.startswith("refs/heads/") or
                not ref[len("refs/heads/"):] or any(c in ref for c in " ~^:?*[\\") or
                ".." in ref or "@{" in ref or ref.endswith(("/", ".", ".lock"))):
            raise InvalidInput("invalid_ref")
    if event["head_ref"] == event["target_ref"]:
        raise InvalidInput("contradictory_event")
    bucket = hashlib.sha256((slug + ":" + str(number)).encode("utf-8")).digest()[0] % 2
    return slug, number, bucket


def full(decision, reason):
    decision.update(selection_mode="full", execution_mode="full", apply_filter=False)
    decision["reasons"] = sorted(set(decision.get("reasons", []) + [reason]))
    decision["executed_suite_ids"] = list(decision.get("catalog_suite_ids", []))
    return decision


def _eligible_suite(row):
    runs = row.get("full_run_ids", [])
    if not isinstance(runs, list) or any(not isinstance(r, str) or not r for r in runs):
        return False
    return (row.get("mappable") is True and len(set(runs)) >= MINIMUM_FULL_RUNS and
            isinstance(row.get("test_ids"), list) and bool(row["test_ids"]) and
            bool(row.get("evidence_refs")) and
            all(row.get(key) is True for key in
                ("collection_stable", "children_complete", "capture_complete",
                 "generated_inputs_resolved", "shared_state_closure_resolved")) and
            not row.get("capture_errors") and not row.get("incomplete_reasons"))


def decide(repo, trusted_sha, head_sha, map_path, event_evidence=None, policy_path=None,
           catalog_path=None, repo_slug=None, pr_number=None, window_path=None,
           environ=None, now=None):
    decision = {
        "schema": DECISION_SCHEMA, "selection_mode": "full", "execution_mode": "full",
        "assigned_arm": None, "assignment_bucket": None, "assignment_rule": "sha256-pr-v1",
        "window_id": None, "reasons": [], "apply_filter": False,
        "proposed_suite_ids": [], "expanded_suite_ids": [], "changed_paths": [],
        "merge_base_sha": None, "catalog_sha256": None, "catalog_suite_ids": [],
        "trusted_sha": trusted_sha, "head_sha": head_sha,
        "collection_complete": False, "execution_complete": False,
    }
    environ = os.environ if environ is None else environ
    now = dt.datetime.now(dt.timezone.utc) if now is None else now
    try:
        # Assignment is determined before safety fallbacks, including bad map inputs.
        event, _ = load_json(event_evidence)
        slug, number, bucket = validate_event(event, head_sha, repo_slug, pr_number)
        decision.update(repo=slug, pr_number=number, assignment_bucket=bucket,
                        assigned_arm="selected" if bucket else "control", build_id=event["build_id"])
        git = Git(repo)
        git.commit(trusted_sha)
        git.commit(head_sha)
        trusted_tree = git.tree(trusted_sha)
        mapping, map_raw = load_json(map_path)
        bind_blob(map_raw, trusted_tree, MAP_PATH)
        decision["map_sha"] = trusted_tree[MAP_PATH][2]
        if mapping.get("schema") != "leaf.ci.test-selection-map.v1" or mapping.get("repo") != slug:
            raise InvalidInput("invalid_map")
        policy = mapping
        if policy_path is not None:
            policy, raw = load_json(policy_path)
            bind_blob(raw, trusted_tree, POLICY_PATH)
        window = policy.get("window", {})
        if window_path is not None:
            window, raw = load_json(window_path)
            bind_blob(raw, trusted_tree, WINDOW_PATH)
        if not isinstance(window, dict):
            raise InvalidInput("invalid_window")
        decision["window_id"] = window.get("id")
        catalog, _ = load_json(catalog_path)
        entries, fingerprint = catalog_info(catalog)
        decision.update(catalog_sha256=fingerprint, catalog_suite_ids=sorted(entries),
                        executed_suite_ids=sorted(entries), catalog_kind=catalog.get("kind"))
        if mapping.get("catalog_sha256") != fingerprint:
            raise InvalidInput("catalog_change")
        if catalog.get("kind") not in ("pytest", "web"):
            raise InvalidInput("invalid_catalog")
        if catalog["kind"] == "pytest" and ("PR_BASE_SHA" in environ or "BASE_SHA" in environ):
            raise InvalidInput("base_override_env")
        gate = event.get("merge_gate", {})
        if (not isinstance(gate, dict) or gate.get("provider_bound") is not True or
                gate.get("kind") != "github-merge-queue" or
                any(gate.get(k) is not True for k in ("queue_protected", "webhook_filter", "full_status")) or
                not gate.get("evidence_ref") or gate.get("head_sha") != head_sha or
                gate.get("repo") != slug or gate.get("map_sha") != decision["map_sha"]):
            raise InvalidInput("merge_gate_unverified")
        revoked = gate.get("revoked_map_shas")
        strings(revoked, "revocation_unknown")
        if decision["map_sha"] in revoked:
            raise InvalidInput("map_revoked")
        target = git.resolve(event["target_ref"] + "^{commit}")
        if event.get("target_sha") != target:
            raise InvalidInput("target_mismatch")
        bases = git.run("merge-base", "--all", target, head_sha).decode("ascii").splitlines()
        if len(bases) != 1:
            raise InvalidInput("missing_or_ambiguous_base")
        base = git.commit(bases[0])
        decision.update(merge_base_sha=base, target_sha=target, target_ref=event["target_ref"],
                        head_tree=git.resolve(head_sha + "^{tree}"))
        base_tree, head_tree = git.tree(base), git.tree(head_sha)
        raw = git.run("diff-tree", "--no-commit-id", "-r", "-z", "--name-status", "-M",
                      "--no-ext-diff", "--no-textconv", base, head_sha, "--")
        paths = changed_paths(raw)
        decision["changed_paths"] = paths
        if not paths:
            raise InvalidInput("empty_diff")
        for path in paths:
            old, new = base_tree.get(path), head_tree.get(path)
            if not old and not new:
                raise InvalidInput("unreadable_changed_path")
            if any(entry and (entry[0] not in ("100644", "100755") or entry[1] != "blob")
                   for entry in (old, new)):
                raise InvalidInput("submodule_or_symlink")
        rules = strings(policy.get("force_full_rules", []), "invalid_policy")
        if any(protected_path(path, rules) for path in paths):
            raise InvalidInput("policy_path_changed")
        if any(protected_path(path, rules) and trusted_tree.get(path) != head_tree.get(path)
               for path in set(trusted_tree) | set(head_tree)):
            raise InvalidInput("trusted_policy_tree_drift")
        runner = catalog.get("runner_path")
        if runner:
            runner = repo_path(runner)
            if (head_tree.get(runner) != trusted_tree.get(runner) or
                    not trusted_tree.get(runner) or
                    catalog.get("runner_blob_sha") != trusted_tree[runner][2]):
                raise InvalidInput("catalog_runner_change")
        if catalog["kind"] == "web" and not runner:
            raise InvalidInput("catalog_runner_unbound")
        suites = mapping.get("suites")
        if not isinstance(suites, dict) or set(suites) - set(entries):
            raise InvalidInput("invalid_map")
        mandatory = set(strings(mapping.get("mandatory_suite_ids")))
        mandatory.update(strings(policy.get("mandatory_suite_ids")))
        required = mandatory | set(strings(mapping.get("always_select_suite_ids", [])))
        if not mandatory or not required <= set(entries):
            raise InvalidInput("missing_mandatory_id")
        decision["mandatory_suite_ids"] = sorted(mandatory)
        decision["known_read_paths"] = {}
        edges = {}
        shared = {}
        for sid, entry in entries.items():
            row = suites.get(sid, {})
            if not isinstance(row, dict):
                raise InvalidInput("invalid_map")
            if (not _eligible_suite(row) or entry.get("trace_kind") != "python" or
                    entry.get("python_only") is not True or
                    entry.get("classification") not in ("mapped", "mandatory") or
                    entry.get("classification") == "mandatory" or entry.get("contract_assertions")):
                required.add(sid)
            if row.get("test_ids") is not None and sorted(strings(row["test_ids"])) != sorted(entry["test_ids"]):
                raise InvalidInput("collection_drift")
            reads = set(repo_path(p) for p in strings(row.get("read_paths", [])))
            generated = row.get("generated_rules", [])
            if not isinstance(generated, list):
                raise InvalidInput("generated_closure_incomplete")
            for rule in generated:
                if not isinstance(rule, dict) or not rule.get("evidence_ref"):
                    raise InvalidInput("generated_closure_incomplete")
                repo_path(rule.get("output"))
                inputs = strings(rule.get("inputs"), "generated_closure_incomplete")
                if not inputs:
                    raise InvalidInput("generated_closure_incomplete")
                reads.update(repo_path(p) for p in inputs)
            for path in reads:
                if path not in base_tree and path not in head_tree:
                    raise InvalidInput("unresolved_read_path")
                if any(tree.get(path) and tree[path][0] not in ("100644", "100755")
                       for tree in (base_tree, head_tree)):
                    raise InvalidInput("ambiguous_read_path")
                edges.setdefault(path, set()).add(sid)
            decision["known_read_paths"][sid] = sorted(reads)
            shared[sid] = set(strings(row.get("shared_state_dependencies", [])))
            if not shared[sid] <= set(entries):
                raise InvalidInput("shared_closure_incomplete")
        for path in paths:
            if path not in edges:
                raise InvalidInput("unknown_path")
            required.update(edges[path])
        # Shared state is conservatively undirected: either endpoint requires both.
        while True:
            previous = set(required)
            for sid, dependencies in shared.items():
                if sid in required or dependencies & required:
                    required.add(sid)
                    required.update(dependencies)
            if previous == required:
                break
        if not required:
            raise InvalidInput("empty_selection")
        proposed = sorted(required)
        expanded = expand_only(proposed, entries) if catalog["kind"] == "web" else proposed
        all_tests = [tid for entry in entries.values() for tid in entry["test_ids"]]
        decision.update(proposed_suite_ids=proposed, expanded_suite_ids=expanded,
                        collection_ids_sha256=collection_digest(all_tests),
                        expected_collection_ids=sorted(all_tests),
                        selected_modules=sorted({entries[sid]["module"] for sid in expanded
                                                 if entries[sid].get("module")}),
                        selected_test_ids=sorted(tid for sid in expanded for tid in entries[sid]["test_ids"]))
        if catalog["kind"] == "pytest" and any(not entries[sid].get("module") for sid in entries):
            raise InvalidInput("module_identity_missing")
        decision["reasons"] = ["readset_match"]
        phase = policy.get("phase")
        if type(policy.get("selection_enabled")) is not bool or phase not in ("shadow", "active"):
            raise InvalidInput("invalid_policy")
        if phase == "shadow":
            decision.update(selection_mode="shadow", execution_mode="full", apply_filter=False,
                            reasons=["shadow_policy"])
            return decision
        if policy["selection_enabled"] is not True:
            return full(decision, "policy_disabled")
        start, end = timestamp(window.get("start")), timestamp(window.get("end"))
        if (not isinstance(window.get("id"), str) or
                not re.fullmatch(r"[A-Za-z0-9._-]+", window["id"]) or
                end - start != dt.timedelta(days=WINDOW_DAYS) or
                timestamp(window.get("fixed_at")) > start or not start <= now < end):
            raise InvalidInput("activation_window_expired_or_invalid")
        if decision["assigned_arm"] == "control":
            decision.update(selection_mode="shadow", execution_mode="full", apply_filter=False,
                            reasons=["control_arm"])
        else:
            decision.update(selection_mode="selected", execution_mode="selected", apply_filter=True,
                            executed_suite_ids=expanded)
        return decision
    except (InvalidInput, UnicodeError) as exc:
        return full(decision, str(exc) if isinstance(exc, InvalidInput) else "invalid_encoding")


def validated_web_args(decision, catalog):
    entries, fingerprint = catalog_info(catalog)
    if (decision.get("schema") != DECISION_SCHEMA or catalog.get("kind") != "web" or
            decision.get("catalog_sha256") != fingerprint):
        raise InvalidInput("catalog_change")
    if decision.get("apply_filter") is not True:
        if decision.get("execution_mode") != "full":
            raise InvalidInput("invalid_decision")
        return b""
    if (decision.get("selection_mode") != "selected" or
            decision.get("execution_mode") != "selected" or decision.get("assigned_arm") != "selected"):
        raise InvalidInput("invalid_decision")
    proposed = strings(decision.get("proposed_suite_ids"))
    expanded = expand_only(proposed, entries)
    required = set(strings(decision.get("mandatory_suite_ids"))) | set(proposed)
    if (not required <= set(expanded) or set(proposed) - set(entries) or
            expanded != decision.get("expanded_suite_ids")):
        raise InvalidInput("incomplete_selection_closure")
    return b"".join(b"--only\x00" + sid.encode("utf-8") + b"\x00" for sid in proposed)


def emit_web_args(decision_path, catalog_path, out):
    # Remove stale arguments before parsing. An adapter must still check the exit code.
    atomic_write(out, b"")
    decision, _ = load_json(decision_path)
    try:
        catalog, _ = load_json(catalog_path)
        data = validated_web_args(decision, catalog)
    except InvalidInput as exc:
        full(decision, str(exc))
        write_json(decision_path, decision)
        return decision
    atomic_write(out, data)
    return decision


def decision_lines(decision, stream):
    mode = decision["selection_mode"]
    total = len(decision.get("catalog_suite_ids", []))
    selected = len(decision.get("expanded_suite_ids", [])) if mode != "full" else total
    detail = dict(decision, schema="leaf.ci.selection.v1")
    print("LEAF_SELECTION " + canonical(detail).decode("ascii"), file=stream)
    print("SELECTION {} selected={} of {} reasons={}".format(
        mode, selected, total, ",".join(sorted(decision["reasons"]))), file=stream)
    print("SELECTION_ARM assigned={} bucket={} rule=sha256-pr-v1 window={}".format(
        decision.get("assigned_arm") or "none", decision.get("assignment_bucket"),
        decision.get("window_id") or "none"), file=stream)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    choose = commands.add_parser("decide")
    for option in ("--repo", "--trusted-sha", "--head-sha", "--map", "--out-dir"):
        choose.add_argument(option, required=True)
    for name in ("event-evidence", "policy", "catalog", "repo-slug", "pr-number", "window"):
        choose.add_argument("--" + name)
    emit = commands.add_parser("emit-web-args")
    for name in ("decision", "catalog", "out"):
        emit.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "emit-web-args":
            emit_web_args(args.decision, args.catalog, args.out)
            return 0
        out = Path(args.out_dir)
        atomic_write(out / "only-args.nul", b"")
        decision = decide(args.repo, args.trusted_sha, args.head_sha, args.map,
                          args.event_evidence, args.policy, args.catalog, args.repo_slug,
                          args.pr_number, args.window)
        if decision.get("catalog_kind") == "web" and decision["apply_filter"]:
            catalog, _ = load_json(args.catalog)
            atomic_write(out / "only-args.nul", validated_web_args(decision, catalog))
        write_json(out / "decision.json", decision)
        decision_lines(decision, sys.stderr if decision.get("catalog_kind") == "pytest" else sys.stdout)
        return 0
    except Exception as exc:
        # Exceptions are not a valid successful decision. Clear filter state and
        # leave a diagnostic fallback when possible; the loader owns recovery.
        if args.command == "decide":
            fallback = full({"schema": DECISION_SCHEMA}, "selector_exception")
            try:
                atomic_write(Path(args.out_dir) / "only-args.nul", b"")
                write_json(Path(args.out_dir) / "decision.json", fallback)
            except OSError:
                pass
        print("selector_exception:" + type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
