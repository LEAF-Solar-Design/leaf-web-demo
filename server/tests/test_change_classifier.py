"""Slice 12a: the change-to-live classifier and the /api/change-class endpoint.

The load-bearing properties, in the order they are asserted:

  0. ONE NAMESPACE PER ANSWER. `repo` is required and never defaulted. The
     tenant and platform repositories share path shapes -- `tools/**` is a
     tenant fold rule AND a real tracked platform build input -- so a flat path
     space would answer `fold`/`seconds` for a change that needs the image build
     and the relay. That is the one OPTIMISTIC error worst-rank-wins exists to
     prevent, and the collision is asserted directly.
  1. DENIED FIRST. A malformed, traversing, absolute or backslash path is
     `denied` in BOTH namespaces no matter what else is in the set, and no
     marathon kind can lift it. Fail closed is a property of the RESULT, not
     just of the input check.
  2. The ladder tables, one per repository: tenant artifacts fold and everything
     else a tenant cannot land is denied; in the platform repo web-only
     prewarms, EVERY other real surface takes the relay, and `denied` means
     malformed and nothing else.
  3. Mixed sets take the WORST class, never the fastest, so the answer is never
     more optimistic than the change is.
  4. `_safe_path` stays in lockstep with the STRUCTURAL half of
     `platform_release_policy.normalize_path`: every structural refusal there
     refuses here too. Only the tenant-only lowercase convention differs.
  5. Malformed INPUT (wrong type, oversize, blank member, absent repo) raises
     rather than returning a permissive class.
  6. The endpoint is bounded and fails closed with 422, and the class it
     returns is the pure function's, unchanged.

Run:
    cd server
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_change_classifier.py -q
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import change_classifier as cc  # noqa: E402
import platform_release_policy as prp  # noqa: E402

TENANT = cc.REPO_TENANT
PLATFORM = cc.REPO_PLATFORM


@pytest.fixture(scope="module")
def policy():
    return prp.load_policy()


@pytest.fixture(scope="module")
def release(policy):
    assert len(policy.releases) == 1, "the fixture assumes one declared release"
    return next(iter(policy.releases))


def klass(paths, kind=None, *, repo, policy=None, release=None):
    return cc.classify_change(paths, kind, repo=repo, policy=policy, release_id=release)


# --------------------------------------------------------------------------- #
# 0. the two namespaces are not one namespace
# --------------------------------------------------------------------------- #
def test_repo_is_required_and_never_guessed(policy, release):
    """Absent `repo` raises. A default would be a guess, and the guess that a
    flat path space made was the optimistic one."""
    with pytest.raises(cc.ChangeClassifierError):
        cc.classify_change(["tools/a/tool.py"], None, policy=policy, release_id=release)


@pytest.mark.parametrize("bad", ["", "TENANT", "both", "leaf-web-demo", 7, None, ["tenant"]])
def test_an_unknown_repo_is_refused(bad, policy, release):
    with pytest.raises(cc.ChangeClassifierError):
        cc.classify_change(["tools/a/tool.py"], None, repo=bad,
                           policy=policy, release_id=release)


def test_the_tools_collision_is_answered_per_repository(policy, release):
    """THE defect this discriminator closes.

    `leaf-web-demo` really does track a top-level `tools/` directory, so
    `tools/skills-bundle/build.mjs` matches the tenant `tools/**` fold rule AND
    is a platform build input. One flat answer had to be wrong for one of them,
    and it was wrong in the fast direction: `fold` / `seconds` for a change that
    needs the image build and the relay.
    """
    path = "tools/skills-bundle/build.mjs"
    tenant = klass([path], repo=TENANT, policy=policy, release=release)
    platform = klass([path], repo=PLATFORM, policy=policy, release=release)
    assert tenant["klass"] == "fold" and tenant["lands_in"] == "seconds"
    assert platform["klass"] == "full-relay"
    assert platform["lands_in"] == "after the relay"
    assert tenant["repo"] == TENANT and platform["repo"] == PLATFORM


def test_the_platform_tools_directory_is_really_tracked():
    """The collision is a fact about this repository, not a hypothetical.

    If `tools/` ever stops being tracked here the collision is gone and this
    test says so out loud rather than leaving the discriminator unexplained.
    """
    root = SERVER_DIR.parent
    listing = subprocess.run(
        ["git", "ls-files", "tools"], cwd=str(root),
        capture_output=True, text=True, timeout=60,
    )
    if listing.returncode != 0:
        pytest.skip("not a git checkout, so the tracked path set cannot be read")
    tracked = [line for line in listing.stdout.splitlines() if line.strip()]
    assert tracked, "tools/ is tracked in this repository; that is the collision"


def test_every_answer_names_the_repository_it_answered_for(policy, release):
    for repo in (TENANT, PLATFORM):
        assert klass(["registry.json"], repo=repo,
                     policy=policy, release=release)["repo"] == repo


# --------------------------------------------------------------------------- #
# 1. denied first
# --------------------------------------------------------------------------- #
# Structurally malformed: denied in BOTH namespaces, always.
MALFORMED_PATHS = [
    "../etc/passwd",
    "web/../../etc/passwd",
    "./web/a.js",
    "/absolute/path.py",
    "C:/windows/system32",
    "web" + chr(92) + "src" + chr(92) + "a.js",
    "web//a.js",
    "web/" + chr(0) + "a.js",
]

# Frozen by the platform release policy INSIDE the tenant repository. The stage
# gate refuses these with `frozen_path_changed`, so the class must refuse them
# too rather than advertising a fold that cannot happen.
FROZEN_TENANT_PATHS = [
    ".github/workflows/x.yml",
    ".aps/config.json",
    "credentials/token.json",
    "requirements.txt",
    "pyproject.toml",
    "package-lock.json",
    "platform_release_policy.json",
]


@pytest.mark.parametrize("path", FROZEN_TENANT_PATHS)
def test_a_frozen_tenant_path_is_denied_not_folded(path, policy, release):
    result = klass([path], repo=TENANT, policy=policy, release=release)
    assert result["klass"] == "denied", result
    assert "frozen" in result["reason"]


def test_registry_json_is_the_one_frozen_path_that_folds(policy, release):
    assert prp.classify_path(policy, release, "registry.json") == "frozen"
    assert klass(["registry.json"], repo=TENANT,
                 policy=policy, release=release)["klass"] == "fold"


@pytest.mark.parametrize("path", FROZEN_TENANT_PATHS)
def test_one_frozen_path_denies_the_whole_set(path, policy, release):
    mixed = ["registry.json", "tools/a/tool.py", path]
    assert klass(mixed, repo=TENANT, policy=policy, release=release)["klass"] == "denied"


@pytest.mark.parametrize("path", MALFORMED_PATHS)
@pytest.mark.parametrize("repo", [TENANT, PLATFORM])
def test_a_malformed_path_is_denied_in_both_repositories(path, repo, policy, release):
    result = klass([path], repo=repo, policy=policy, release=release)
    assert result["klass"] == "denied", result
    assert result["lands_in"] == "not allowed"
    assert result["reason"].strip(), "a denial must carry a sentence"


@pytest.mark.parametrize("path", MALFORMED_PATHS)
@pytest.mark.parametrize("repo", [TENANT, PLATFORM])
def test_one_denied_path_denies_the_whole_set(path, repo, policy, release):
    """Denied outranks every other class, wherever it sits in the set."""
    mixed = ["tools/a/tool.py", path, "registry.json"]
    assert klass(mixed, repo=repo, policy=policy, release=release)["klass"] == "denied"


@pytest.mark.parametrize("path", MALFORMED_PATHS)
@pytest.mark.parametrize("repo", [TENANT, PLATFORM])
def test_a_marathon_cannot_lift_a_denied_path(path, repo, policy, release):
    assert klass([path], "marathon", repo=repo,
                 policy=policy, release=release)["klass"] == "denied"


# --------------------------------------------------------------------------- #
# 2. the ladder tables, one per repository
# --------------------------------------------------------------------------- #
TENANT_LADDER = [
    (["registry.json"], "fold", "seconds"),
    (["tools/roof-pitch/tool.py"], "fold", "seconds"),
    (["registry.json", "tools/roof-pitch/tool.json"], "fold", "seconds"),
    (["config/surfaces.json"], "fold", "seconds"),
    # A tenant cannot change platform code, whatever it is named. This is a
    # statement about the TENANT repository, not about `server/app.py` the file.
    (["server/app.py"], "denied", "not allowed"),
    (["web/src/App.jsx"], "denied", "not allowed"),
    (["docs/plan.md"], "denied", "not allowed"),
]

PLATFORM_LADDER = [
    (["web/src/App.jsx"], "speculative-prewarm", "minutes"),
    (["web/src/components/ReceiptsTimeline.jsx", "web/src/site/landing.css"],
     "speculative-prewarm", "minutes"),
    (["server/app.py"], "full-relay", "after the relay"),
    (["platform/api.py"], "full-relay", "after the relay"),
    (["platform/migrations/0011_x.sql"], "full-relay", "after the relay"),
    (["web/migrations/0002_x.sql"], "full-relay", "after the relay"),
    (["server/routers/tools.py", "web/src/App.jsx"], "full-relay", "after the relay"),
]


@pytest.mark.parametrize("paths,expected,lands_in", TENANT_LADDER)
def test_tenant_ladder_table(paths, expected, lands_in, policy, release):
    result = klass(paths, repo=TENANT, policy=policy, release=release)
    assert result["klass"] == expected, (paths, result)
    assert result["lands_in"] == lands_in
    assert result["reason"].strip()


@pytest.mark.parametrize("paths,expected,lands_in", PLATFORM_LADDER)
def test_platform_ladder_table(paths, expected, lands_in, policy, release):
    result = klass(paths, repo=PLATFORM, policy=policy, release=release)
    assert result["klass"] == expected, (paths, result)
    assert result["lands_in"] == lands_in
    assert result["reason"].strip()


# Real, tracked surfaces of THIS repository outside web/server/platform. A flat
# path space answered "denied / not allowed" for every one of them, which is a
# lie about changes that ship every day. In the platform namespace they land,
# pessimistically, on the relay.
REAL_PLATFORM_SURFACES = [
    "engine/acadrust_adapter.py",
    "scripts/run-all-gates.py",
    "harness/x.py",
    "e2e/a.spec.mjs",
    "docs/ADMIN-SELF-EDIT-LANE.md",
    "requirements.txt",
    "package-lock.json",
    ".github/workflows/test-gate.yml",
    "tools/skills-bundle/build.mjs",
    "contract/CONTRACT.md",
    "README.md",
]


@pytest.mark.parametrize("path", REAL_PLATFORM_SURFACES)
def test_no_real_platform_surface_reads_as_unable_to_land(path, policy, release):
    result = klass([path], repo=PLATFORM, policy=policy, release=release)
    assert result["klass"] != "denied", result
    assert result["lands_in"] != "not allowed"


def test_in_the_platform_repo_denied_means_malformed_and_nothing_else(policy, release):
    """The only `denied` a platform path can earn is a structural one."""
    for path in REAL_PLATFORM_SURFACES:
        assert cc._safe_path(path) is None
        assert klass([path], repo=PLATFORM,
                     policy=policy, release=release)["klass"] != "denied"
    for path in MALFORMED_PATHS:
        assert cc._safe_path(path) is not None
        assert klass([path], repo=PLATFORM,
                     policy=policy, release=release)["klass"] == "denied"


def test_a_platform_classification_needs_no_policy_at_all(monkeypatch):
    """The platform ladder never consults the tenant vocabulary, so it does no
    I/O -- asserted by making the policy loader explode."""
    def explode(*args, **kwargs):
        raise AssertionError("the platform ladder must not load the tenant policy")

    monkeypatch.setattr(prp, "load_policy", explode)
    monkeypatch.setattr(cc.platform_release_policy, "classify_path", explode)
    result = cc.classify_change(["server/app.py", "web/a.js"], None, repo=PLATFORM)
    assert result["klass"] == "full-relay"


@pytest.mark.parametrize("repo", [TENANT, PLATFORM])
def test_marathon_is_fleet_whatever_the_paths(repo, policy, release):
    for paths in ([], ["registry.json"], ["tools/a/tool.py"]):
        result = klass(paths, "marathon", repo=repo, policy=policy, release=release)
        assert result["klass"] == "fleet"
        assert result["lands_in"] == "a marathon"


@pytest.mark.parametrize("repo", [TENANT, PLATFORM])
def test_no_paths_and_no_kind_is_denied(repo, policy, release):
    result = klass([], repo=repo, policy=policy, release=release)
    assert result["klass"] == "denied"
    assert "no paths" in result["reason"]


# --------------------------------------------------------------------------- #
# 3. the worst class wins
# --------------------------------------------------------------------------- #
def test_a_mixed_platform_set_never_reads_faster_than_its_slowest_member(policy, release):
    web_only = ["web/a.js"]
    assert klass(web_only, repo=PLATFORM,
                 policy=policy, release=release)["klass"] == "speculative-prewarm"
    # adding one server file must slow it, never keep it at prewarm
    assert klass(web_only + ["server/app.py"], repo=PLATFORM,
                 policy=policy, release=release)["klass"] == "full-relay"
    # and a build input is not a fold here, whatever the tenant policy says
    assert klass(web_only + ["tools/skills-bundle/build.mjs"], repo=PLATFORM,
                 policy=policy, release=release)["klass"] == "full-relay"


def test_a_mixed_tenant_set_never_reads_faster_than_its_slowest_member(policy, release):
    fold_only = ["tools/a/tool.py"]
    assert klass(fold_only, repo=TENANT, policy=policy, release=release)["klass"] == "fold"
    assert klass(fold_only + ["requirements.txt"], repo=TENANT,
                 policy=policy, release=release)["klass"] == "denied"


def test_the_reason_names_the_path_that_decided_it(policy, release):
    result = klass(["web/a.js", "server/deps.py"], repo=PLATFORM,
                   policy=policy, release=release)
    assert result["klass"] == "full-relay"
    assert "server/deps.py" in result["reason"]


def test_every_reason_belongs_to_a_path_that_is_really_in_the_set(policy, release):
    """No seeded class: a set is never described by a class no member produced."""
    result = klass(["web/a.js", "web/b.css"], repo=PLATFORM,
                   policy=policy, release=release)
    assert result["klass"] == "speculative-prewarm"
    assert "web/a.js" in result["reason"] or "web/b.css" in result["reason"]


# --------------------------------------------------------------------------- #
# 4. _safe_path stays in lockstep with the policy's structural refusals
# --------------------------------------------------------------------------- #
STRUCTURAL_REFUSALS = [
    "../x",
    "a/../b",
    "./a",
    "/a",
    "C:/a",
    "a" + chr(92) + "b",
    "a//b",
    "",
]


@pytest.mark.parametrize("path", STRUCTURAL_REFUSALS)
def test_safe_path_refuses_everything_normalize_path_structurally_refuses(path):
    """Only the tenant-only lowercase convention may differ between the two."""
    with pytest.raises(prp.PlatformReleasePolicyError):
        prp.normalize_path(path or "x/../y")
    assert cc._safe_path(path or "x/../y") is not None


def test_safe_path_accepts_the_uppercase_paths_the_policy_refuses_by_convention():
    """The whole reason `_safe_path` exists: an uppercase segment is a tenant
    NAMING rule, not a safety property, and denying it would call every real
    web and server change "not allowed"."""
    for path in ("web/src/App.jsx", "server/CONTRACT-ADDENDUM.md", "platform/API.py"):
        with pytest.raises(prp.PlatformReleasePolicyError):
            prp.normalize_path(path)
        assert cc._safe_path(path) is None


# --------------------------------------------------------------------------- #
# 5. malformed input raises rather than classifying
# --------------------------------------------------------------------------- #
MALFORMED_INPUTS = [
    "web/a.js",                       # a bare string is not a path list
    b"web/a.js",
    None,
    123,
    {"paths": ["web/a.js"]},
    ["web/a.js", None],
    ["web/a.js", ""],
    ["web/a.js", 7],
    ["a" * (cc.MAX_PATH_LENGTH + 1)],
    ["web/a.js"] * (cc.MAX_PATHS + 1),
]


@pytest.mark.parametrize("bad", MALFORMED_INPUTS)
@pytest.mark.parametrize("repo", [TENANT, PLATFORM])
def test_malformed_paths_fail_closed(bad, repo, policy, release):
    with pytest.raises(cc.ChangeClassifierError):
        cc.classify_change(bad, None, repo=repo, policy=policy, release_id=release)


@pytest.mark.parametrize("bad", [7, [], {}, "NOT-A-KIND", "kind with spaces", "x" * 80])
@pytest.mark.parametrize("repo", [TENANT, PLATFORM])
def test_malformed_kind_fails_closed(bad, repo, policy, release):
    with pytest.raises(cc.ChangeClassifierError):
        cc.classify_change(["web/a.js"], bad, repo=repo,
                           policy=policy, release_id=release)


def test_an_unknown_release_is_refused_not_guessed(policy):
    """`release_id` selects a TENANT policy release, so it is checked there."""
    with pytest.raises(cc.ChangeClassifierError):
        cc.classify_change(["tools/a/tool.py"], None, repo=TENANT,
                           policy=policy, release_id="no-such-release")


def test_the_bound_is_inclusive_at_the_limit(policy, release):
    at_limit = ["web/a.js"] * cc.MAX_PATHS
    assert klass(at_limit, repo=PLATFORM,
                 policy=policy, release=release)["klass"] == "speculative-prewarm"


def test_lands_in_covers_every_class():
    assert set(cc.LANDS_IN) == {
        "fold", "speculative-prewarm", "full-relay", "fleet", "denied",
    }


def test_the_docstring_says_the_class_is_not_a_promise():
    """The honesty requirement is enforced, not just intended."""
    doc = " ".join((cc.__doc__ or "").split())
    assert "never a promise" in doc
    assert "never as an ETA the platform owes anybody" in doc
    assert "never a promise" in " ".join((cc.classify_change.__doc__ or "").split()).lower()


def test_the_docstring_states_the_namespace_collision():
    """The reason `repo` exists is written down, not folk knowledge."""
    doc = " ".join((cc.__doc__ or "").split())
    assert "tools/skills-bundle/build.mjs" in doc
    assert "REQUIRED" in doc


# --------------------------------------------------------------------------- #
# 6. the endpoint
# --------------------------------------------------------------------------- #
def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from envelopes import install_error_handlers
    import routers.change_to_live as router_module

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router_module.router)
    return TestClient(app, raise_server_exceptions=False)


def test_endpoint_classifies_a_comma_joined_query():
    resp = _client().get("/api/change-class", params={
        "repo": PLATFORM, "paths": "web/src/App.jsx,web/a.css"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["klass"] == "speculative-prewarm"
    assert body["lands_in"] == "minutes"
    assert body["repo"] == PLATFORM
    assert body["paths_considered"] == 2
    assert body["contract"] == cc.CONTRACT
    assert body["reason"].strip()


def test_endpoint_classifies_repeated_query_parameters():
    resp = _client().get("/api/change-class", params=[
        ("repo", PLATFORM), ("paths", "tools/a/tool.py"), ("paths", "server/app.py")])
    assert resp.status_code == 200
    assert resp.json()["klass"] == "full-relay"


def test_the_endpoint_answers_the_collision_per_repository():
    client = _client()
    params = {"paths": "tools/skills-bundle/build.mjs"}
    tenant = client.get("/api/change-class", params={**params, "repo": TENANT}).json()
    platform = client.get("/api/change-class", params={**params, "repo": PLATFORM}).json()
    assert tenant["klass"] == "fold"
    assert platform["klass"] == "full-relay", "a build input must not read as seconds"


def test_endpoint_requires_a_repo():
    assert _client().get(
        "/api/change-class", params={"paths": "web/a.js"}).status_code == 422


def test_endpoint_fails_closed_on_an_unknown_repo():
    resp = _client().get("/api/change-class", params={"repo": "both", "paths": "web/a.js"})
    assert resp.status_code == 422, resp.text


def test_endpoint_denies_a_traversing_path_with_200_and_a_denied_class():
    resp = _client().get("/api/change-class", params={
        "repo": PLATFORM, "paths": "../etc/passwd"})
    assert resp.status_code == 200
    assert resp.json()["klass"] == "denied"


def test_endpoint_fails_closed_on_an_oversize_request():
    too_many = ",".join(["web/a.js"] * (cc.MAX_PATHS + 5))
    resp = _client().get("/api/change-class", params={
        "repo": PLATFORM, "paths": too_many})
    assert resp.status_code == 422, resp.text


def test_endpoint_fails_closed_on_a_malformed_kind():
    resp = _client().get("/api/change-class", params={
        "repo": PLATFORM, "paths": "web/a.js", "kind": "NOT A KIND"})
    assert resp.status_code == 422


def test_endpoint_with_no_paths_is_denied_not_permissive():
    resp = _client().get("/api/change-class", params={"repo": PLATFORM})
    assert resp.status_code == 200
    assert resp.json()["klass"] == "denied"
