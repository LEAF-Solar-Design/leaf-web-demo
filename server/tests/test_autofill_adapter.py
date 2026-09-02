import json
import os
from pathlib import Path

import pytest

from solver_adapters import autofill


STAGING_SOLVER_REVISION = "760d3888018f762e0ab8dbe289dae8a2871216ea"
STAGING_SOLVER_SHA256 = "a70f1e323da0f7a972c530d1101fc1eca82f2c58dc5b0141d5d98fba0f188cd2"


def test_trusted_manifest_is_limited_to_the_deployed_solver() -> None:
    manifest = json.loads(autofill.TRUSTED_SOURCES_MANIFEST.read_text(encoding="utf-8"))

    assert manifest == {STAGING_SOLVER_REVISION: STAGING_SOLVER_SHA256}


def _write_self_attestation(root: Path, revision: str) -> None:
    source_sha256 = autofill._source_sha256(root)
    (root / autofill.SOURCE_ATTESTATION).write_text(
        '{"revision":"' + revision + '","source_sha256":"' + source_sha256 + '"}\n')


def _seal_trusted_source(monkeypatch, root: Path, revision: str) -> Path:
    manifest = root / "trusted-sources.json"
    manifest.write_text(json.dumps({revision: autofill._source_sha256(root)}) + "\n")
    monkeypatch.setattr(autofill, "TRUSTED_SOURCES_MANIFEST", manifest)
    autofill.attest_source(root, revision, manifest)
    (root / autofill.SOURCE_REVISION_MARKER).write_text(revision + "\n")
    return manifest


def test_descriptor_uses_trusted_manifest_and_image_revision(monkeypatch, tmp_path):
    (tmp_path / "solver.py").write_text("def solve_targets(*args, **kwargs): return {}\n")
    revision = "a" * 40
    _seal_trusted_source(monkeypatch, tmp_path, revision)
    monkeypatch.setenv("AUTOFILL_SOLVER_REVISION", revision.upper())

    assert autofill.descriptor(solver_root=tmp_path)["source_revision"] == revision


def test_staging_solver_manifest_entry_authorizes_matching_worker_bytes(monkeypatch, tmp_path):
    (tmp_path / "solver.py").write_text("def solve_targets(*args, **kwargs): return {}\n")
    (tmp_path / autofill.SOURCE_ATTESTATION).write_text(json.dumps({
        "revision": STAGING_SOLVER_REVISION,
        "source_sha256": STAGING_SOLVER_SHA256,
    }) + "\n")
    (tmp_path / autofill.SOURCE_REVISION_MARKER).write_text(STAGING_SOLVER_REVISION + "\n")
    monkeypatch.setattr(autofill, "_source_sha256", lambda _root: STAGING_SOLVER_SHA256)
    monkeypatch.setattr(autofill, "_git_source_revision", lambda _root: None)

    source = autofill.descriptor(solver_root=tmp_path)

    assert source["source_revision"] == STAGING_SOLVER_REVISION
    assert source["source_sha256"] == STAGING_SOLVER_SHA256


def test_staging_solver_manifest_entry_rejects_mismatched_worker_bytes(monkeypatch, tmp_path):
    (tmp_path / "solver.py").write_text("def solve_targets(*args, **kwargs): return {}\n")
    (tmp_path / autofill.SOURCE_ATTESTATION).write_text(json.dumps({
        "revision": STAGING_SOLVER_REVISION,
        "source_sha256": STAGING_SOLVER_SHA256,
    }) + "\n")
    (tmp_path / autofill.SOURCE_REVISION_MARKER).write_text(STAGING_SOLVER_REVISION + "\n")
    monkeypatch.setattr(autofill, "_source_sha256", lambda _root: "0" * 64)
    monkeypatch.setattr(autofill, "_git_source_revision", lambda _root: None)

    with pytest.raises(RuntimeError, match="bytes do not match their build attestation"):
        autofill.descriptor(solver_root=tmp_path)


def test_descriptor_rejects_invalid_supplied_solver_revision(monkeypatch, tmp_path):
    (tmp_path / "solver.py").write_text("def solve_targets(*args, **kwargs): return {}\n")
    monkeypatch.setenv("AUTOFILL_SOLVER_REVISION", "latest")

    with pytest.raises(RuntimeError, match="exact Git commit"):
        autofill.descriptor(solver_root=tmp_path)


def test_descriptor_rejects_untrusted_self_attestation(monkeypatch, tmp_path):
    (tmp_path / "solver.py").write_text("def solve_targets(*args, **kwargs): return {}\n")
    revision = "a" * 40
    _write_self_attestation(tmp_path, revision)
    (tmp_path / autofill.SOURCE_REVISION_MARKER).write_text(revision + "\n")
    manifest = tmp_path / "trusted-sources.json"
    manifest.write_text("{}\n")
    monkeypatch.setattr(autofill, "TRUSTED_SOURCES_MANIFEST", manifest)

    with pytest.raises(RuntimeError, match="no trusted autofill source digest"):
        autofill.descriptor(solver_root=tmp_path)


def test_descriptor_rejects_attestation_with_wrong_image_revision(monkeypatch, tmp_path):
    (tmp_path / "solver.py").write_text("def solve_targets(*args, **kwargs): return {}\n")
    revision = "a" * 40
    _seal_trusted_source(monkeypatch, tmp_path, revision)
    (tmp_path / autofill.SOURCE_REVISION_MARKER).write_text(("b" * 40) + "\n")

    with pytest.raises(RuntimeError, match="image revision does not match"):
        autofill.descriptor(solver_root=tmp_path)


def test_descriptor_rejects_source_changed_after_trusted_attestation(monkeypatch, tmp_path):
    solver = tmp_path / "solver.py"
    solver.write_text("def solve_targets(*args, **kwargs): return {}\n")
    revision = "a" * 40
    _seal_trusted_source(monkeypatch, tmp_path, revision)
    monkeypatch.setenv("AUTOFILL_SOLVER_REVISION", revision)
    solver.write_text("def solve_targets(*args, **kwargs): return {'changed': True}\n")

    with pytest.raises(RuntimeError, match="bytes do not match their build attestation"):
        autofill.descriptor(solver_root=tmp_path)


def test_build_attestation_rejects_revision_source_digest_mismatch(tmp_path):
    (tmp_path / "solver.py").write_text("def solve_targets(*args, **kwargs): return {}\n")
    fake_revision = "0" * 40
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"' + fake_revision + '":"' + ("f" * 64) + '"}\n')

    with pytest.raises(RuntimeError, match="source does not match revision"):
        autofill.attest_source(tmp_path, fake_revision, manifest)


# The adapter's default assumes leaf-web-demo and autofill-solver are sibling
# checkouts. AUTOFILL_SOLVER_ROOT overrides for isolated worktrees and must be
# valid when set. The smoke is REQUIRED by default: an unresolvable solver
# source fails the suite so a resolution bug can never skip-launder past the
# gate floor. Hosts that genuinely lack the source opt out explicitly with
# LEAF_AUTOFILL_SOLVER_ABSENT_OK=1, which produces a visible skip.
def _main_checkout_root():
    """Root of the PRIMARY checkout, even when this file runs from a linked
    worktree. A fixed parent hop from server/tests is only correct in the
    primary tree: under .claude/worktrees/<name> the same hop lands on the
    worktrees directory, so the sibling probe below could never find a solver
    checkout that is present on the host, and the required resolution-policy
    test failed on every worktree session. In a linked worktree `.git` is a
    file holding `gitdir: <main>/.git/worktrees/<name>`, which names the
    primary tree exactly."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        dot_git = parent / ".git"
        if dot_git.is_dir():
            return parent
        if dot_git.is_file():
            # Every branch below falls back to `parent`, which IS a checkout
            # root: this runs at import, so a raise here would error the whole
            # module rather than fail one test.
            try:
                raw = dot_git.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError):
                return parent
            if not raw.startswith("gitdir:"):
                return parent
            gitdir = Path(raw[len("gitdir:"):].strip())
            if not gitdir.is_absolute():
                gitdir = (parent / gitdir).resolve()
            # Only a LINKED WORKTREE points at <main>/.git/worktrees/<name>,
            # and only for it is the primary tree somewhere else. A submodule
            # points at <super>/.git/modules/<name>, where hopping three
            # parents would hand back the superproject; its own checkout root
            # is `parent`.
            if gitdir.parent.name == "worktrees" and len(gitdir.parents) >= 3:
                return gitdir.parents[2]
            return parent
    return here.parents[2]


def _resolve_solver_root():
    env_root = os.environ.get("AUTOFILL_SOLVER_ROOT")
    if env_root:
        root = Path(env_root)
        if not (root / "solver.py").is_file():
            return None, f"AUTOFILL_SOLVER_ROOT is set but invalid: {env_root!r}"
        return root, None
    sibling = _main_checkout_root().parent / "autofill-solver"
    if (sibling / "solver.py").is_file():
        return sibling, None
    return None, None


SOLVER_ROOT, _RESOLUTION_ERROR = _resolve_solver_root()
_ABSENT_OK = os.environ.get("LEAF_AUTOFILL_SOLVER_ABSENT_OK") == "1"


def test_solver_source_resolution_policy():
    if _RESOLUTION_ERROR:
        pytest.fail(_RESOLUTION_ERROR)
    if SOLVER_ROOT is None and not _ABSENT_OK:
        pytest.fail(
            "autofill-solver source not found (no sibling checkout, no "
            "AUTOFILL_SOLVER_ROOT). Set AUTOFILL_SOLVER_ROOT, or acknowledge "
            "a source-less host explicitly with LEAF_AUTOFILL_SOLVER_ABSENT_OK=1.")


SMOKE_INPUT = {
    "groups": [
        {"handle": "A", "name": "A", "count": 25, "centroidX": 0.0,
         "centroidY": 0.0, "electricalZone": "Z", "elevationZone": ""},
        {"handle": "B", "name": "B", "count": 15, "centroidX": 10.0,
         "centroidY": 0.0, "electricalZone": "Z", "elevationZone": ""},
    ],
    "panelsPerString": 10,
    "options": {"drainThreshold": 23, "drainDiscount": 0.0,
                "activeGroupPenalty": 10, "concentrationBias": 0.15,
                "clusterMarginPitches": 2.0},
}


@pytest.mark.skipif(
    SOLVER_ROOT is None,
    reason="autofill-solver source absent, acknowledged via LEAF_AUTOFILL_SOLVER_ABSENT_OK=1",
)
def test_real_autofill_solver_smoke_is_deterministic():
    first = autofill.run(SMOKE_INPUT, solver_root=SOLVER_ROOT)
    second = autofill.run(SMOKE_INPUT, solver_root=SOLVER_ROOT)
    assert first == second
    assert first["solver_result"] == {
        "clusters": [["A", "B"]], "diagnostics": [], "feasible": True,
        "groupTargets": {"A": 40, "B": 0}, "totalDisruption": 30,
    }
    assert first["result_sha256"] == "525e2d417d916ab896ab25525352783302c98f6f436631777731a4c08bb1ed59"
    assert first["request_sha256"]
    assert len(first["source_sha256"]) == 64
    assert first["solver_input"]["panelsPerString"] == 10
    assert first["solver_revision"]
    assert first["runtime"].startswith("python-")


def test_legacy_solver_accepts_defaults_but_rejects_geometry(tmp_path):
    (tmp_path / "solver.py").write_text(
        "def solve_targets(groups, n, options=None): return {'ok': True}\n")

    result = autofill.run(SMOKE_INPUT, solver_root=tmp_path)
    assert result["solver_result"] == {"ok": True}

    with pytest.raises(RuntimeError, match="does not support panel geometry"):
        autofill.run({**SMOKE_INPUT, "panelAngle": 1.0}, solver_root=tmp_path)


def test_adapter_rejects_non_json_and_invalid_panel_count():
    for invalid in ({}, {"groups": [{}], "panelsPerString": True},
                    {"groups": [{}], "panelsPerString": 2},
                    {"groups": [{}], "panelsPerString": 10, "options": []}):
        try:
            autofill.run(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid adapter input was accepted: {invalid!r}")


def test_adapter_matches_pinned_three_argument_solver_contract(tmp_path):
    (tmp_path / "solver.py").write_text(
        "def solve_targets(groups, n, options=None):\n"
        "    return {'groups': len(groups), 'n': n, 'options': options or {}}\n",
        encoding="utf-8",
    )

    result = autofill.run(SMOKE_INPUT, solver_root=tmp_path)

    assert result["solver_result"] == {
        "groups": 2,
        "n": 10,
        "options": SMOKE_INPUT["options"],
    }
