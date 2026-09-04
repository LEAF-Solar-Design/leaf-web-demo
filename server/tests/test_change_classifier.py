"""Slice 12a: the change-to-live classifier and the /api/change-class endpoint.

The load-bearing properties, in the order they are asserted:

  1. DENIED FIRST. A malformed, traversing, absolute or backslash path is
     `denied` no matter what else is in the set, and no marathon kind can lift
     it. Fail closed is a property of the RESULT, not just of the input check.
  2. The ladder table: tenant artifacts fold, web-only prewarms, anything that
     reaches server/platform/migrations takes the relay, a marathon is fleet.
  3. Mixed sets take the WORST class, never the fastest, so the answer is never
     more optimistic than the change is.
  4. `_safe_path` stays in lockstep with the STRUCTURAL half of
     `platform_release_policy.normalize_path`: every structural refusal there
     refuses here too. Only the tenant-only lowercase convention differs.
  5. Malformed INPUT (wrong type, oversize, blank member) raises rather than
     returning a permissive class.
  6. The endpoint is bounded and fails closed with 422, and the class it
     returns is the pure function's, unchanged.

Run:
    cd server
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_change_classifier.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import change_classifier as cc  # noqa: E402
import platform_release_policy as prp  # noqa: E402


@pytest.fixture(scope="module")
def policy():
    return prp.load_policy()


@pytest.fixture(scope="module")
def release(policy):
    assert len(policy.releases) == 1, "the fixture assumes one declared release"
    return next(iter(policy.releases))


def klass(paths, kind=None, *, policy=None, release=None):
    return cc.classify_change(paths, kind, policy=policy, release_id=release)


# --------------------------------------------------------------------------- #
# 1. denied first
# --------------------------------------------------------------------------- #
DENIED_PATHS = [
    "../etc/passwd",
    "web/../../etc/passwd",
    "./web/a.js",
    "/absolute/path.py",
    "C:/windows/system32",
    "web" + chr(92) + "src" + chr(92) + "a.js",
    "web//a.js",
    "web/" + chr(0) + "a.js",
    "docs/plan.md",          # structurally fine, but no delivery surface claims it
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
    result = klass([path], policy=policy, release=release)
    assert result["klass"] == "denied", result
    assert "frozen" in result["reason"]


def test_registry_json_is_the_one_frozen_path_that_folds(policy, release):
    assert prp.classify_path(policy, release, "registry.json") == "frozen"
    assert klass(["registry.json"], policy=policy, release=release)["klass"] == "fold"


@pytest.mark.parametrize("path", FROZEN_TENANT_PATHS)
def test_one_frozen_path_denies_the_whole_set(path, policy, release):
    mixed = ["registry.json", "tools/a/tool.py", path]
    assert klass(mixed, policy=policy, release=release)["klass"] == "denied"


@pytest.mark.parametrize("path", DENIED_PATHS)
def test_denied_paths_are_denied(path, policy, release):
    result = klass([path], policy=policy, release=release)
    assert result["klass"] == "denied", result
    assert result["lands_in"] == "not allowed"
    assert result["reason"].strip(), "a denial must carry a sentence"


@pytest.mark.parametrize("path", DENIED_PATHS)
def test_one_denied_path_denies_the_whole_set(path, policy, release):
    """Denied outranks every other class, wherever it sits in the set."""
    mixed = ["tools/a/tool.py", "web/src/App.jsx", path, "server/app.py"]
    assert klass(mixed, policy=policy, release=release)["klass"] == "denied"


@pytest.mark.parametrize("path", DENIED_PATHS)
def test_a_marathon_cannot_lift_a_denied_path(path, policy, release):
    assert klass([path], "marathon", policy=policy, release=release)["klass"] == "denied"


# --------------------------------------------------------------------------- #
# 2. the ladder table
# --------------------------------------------------------------------------- #
LADDER = [
    (["registry.json"], "fold", "seconds"),
    (["tools/roof-pitch/tool.py"], "fold", "seconds"),
    (["registry.json", "tools/roof-pitch/tool.json"], "fold", "seconds"),
    (["config/surfaces.json"], "fold", "seconds"),
    (["web/src/App.jsx"], "speculative-prewarm", "minutes"),
    (["web/src/components/ReceiptsTimeline.jsx", "web/src/site/landing.css"],
     "speculative-prewarm", "minutes"),
    (["server/app.py"], "full-relay", "after the relay"),
    (["platform/api.py"], "full-relay", "after the relay"),
    (["platform/migrations/0011_x.sql"], "full-relay", "after the relay"),
    (["server/routers/tools.py", "web/src/App.jsx"], "full-relay", "after the relay"),
]


@pytest.mark.parametrize("paths,expected,lands_in", LADDER)
def test_ladder_table(paths, expected, lands_in, policy, release):
    result = klass(paths, policy=policy, release=release)
    assert result["klass"] == expected, (paths, result)
    assert result["lands_in"] == lands_in
    assert result["reason"].strip()


def test_marathon_is_fleet_whatever_the_paths(policy, release):
    for paths in ([], ["registry.json"], ["web/src/App.jsx"], ["server/app.py"]):
        result = klass(paths, "marathon", policy=policy, release=release)
        assert result["klass"] == "fleet"
        assert result["lands_in"] == "a marathon"


def test_no_paths_and_no_kind_is_denied(policy, release):
    result = klass([], policy=policy, release=release)
    assert result["klass"] == "denied"
    assert "no paths" in result["reason"]


# --------------------------------------------------------------------------- #
# 3. the worst class wins
# --------------------------------------------------------------------------- #
def test_a_mixed_set_never_reads_faster_than_its_slowest_member(policy, release):
    fold_only = ["tools/a/tool.py"]
    assert klass(fold_only, policy=policy, release=release)["klass"] == "fold"
    # adding one web file must slow it, never keep it at fold
    assert klass(fold_only + ["web/a.js"], policy=policy, release=release)["klass"] == \
        "speculative-prewarm"
    # adding one server file must slow it again
    assert klass(fold_only + ["web/a.js", "server/app.py"],
                 policy=policy, release=release)["klass"] == "full-relay"


def test_the_reason_names_the_path_that_decided_it(policy, release):
    result = klass(["tools/a/tool.py", "server/deps.py"], policy=policy, release=release)
    assert result["klass"] == "full-relay"
    assert "server/deps.py" in result["reason"]


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
def test_malformed_paths_fail_closed(bad, policy, release):
    with pytest.raises(cc.ChangeClassifierError):
        cc.classify_change(bad, None, policy=policy, release_id=release)


@pytest.mark.parametrize("bad", [7, [], {}, "NOT-A-KIND", "kind with spaces", "x" * 80])
def test_malformed_kind_fails_closed(bad, policy, release):
    with pytest.raises(cc.ChangeClassifierError):
        cc.classify_change(["web/a.js"], bad, policy=policy, release_id=release)


def test_an_unknown_release_is_refused_not_guessed(policy):
    with pytest.raises(cc.ChangeClassifierError):
        cc.classify_change(["web/a.js"], None, policy=policy, release_id="no-such-release")


def test_the_bound_is_inclusive_at_the_limit(policy, release):
    at_limit = ["web/a.js"] * cc.MAX_PATHS
    assert klass(at_limit, policy=policy, release=release)["klass"] == "speculative-prewarm"


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
    resp = _client().get("/api/change-class", params={"paths": "web/src/App.jsx,web/a.css"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["klass"] == "speculative-prewarm"
    assert body["lands_in"] == "minutes"
    assert body["paths_considered"] == 2
    assert body["contract"] == cc.CONTRACT
    assert body["reason"].strip()


def test_endpoint_classifies_repeated_query_parameters():
    resp = _client().get(
        "/api/change-class", params=[("paths", "tools/a/tool.py"), ("paths", "server/app.py")]
    )
    assert resp.status_code == 200
    assert resp.json()["klass"] == "full-relay"


def test_endpoint_denies_a_traversing_path_with_200_and_a_denied_class():
    resp = _client().get("/api/change-class", params={"paths": "../etc/passwd"})
    assert resp.status_code == 200
    assert resp.json()["klass"] == "denied"


def test_endpoint_fails_closed_on_an_oversize_request():
    too_many = ",".join(["web/a.js"] * (cc.MAX_PATHS + 5))
    resp = _client().get("/api/change-class", params={"paths": too_many})
    assert resp.status_code == 422, resp.text


def test_endpoint_fails_closed_on_a_malformed_kind():
    resp = _client().get(
        "/api/change-class", params={"paths": "web/a.js", "kind": "NOT A KIND"}
    )
    assert resp.status_code == 422


def test_endpoint_with_no_paths_is_denied_not_permissive():
    resp = _client().get("/api/change-class")
    assert resp.status_code == 200
    assert resp.json()["klass"] == "denied"
