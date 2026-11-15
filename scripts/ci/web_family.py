"""Conservative, filesystem-free filtering of the selected PR web family.

Call ``filter_web_family(selected, rows, changed_paths,
protected_ids=new_ids | priority_ids, prerequisites=dependencies)`` after
priority/new-suite selection and before final prerequisite closure. Rows are
runner fingerprint dictionaries (id, cwd, command argv, kind), not Suite objects.
Pass unconditional prerequisites as protected IDs too. This module does not
replace select_tests.py's independently audited read-set selection.

Only the exact independent files below are safe. In particular, neither docs/**
nor server/tests/** is safe: web/test/check_routes.mjs reads DEMO-GOLDEN-PATH.md;
web/scripts/check_web_container_config.mjs checks CAD-ENGINE-LICENSE-REVIEW.md;
web/src/solar/SolarToolForm.test.jsx imports docs/parity evidence; and
web/src/solar/solarFlowModel.test.js reads server/tests/test_solar_solaredge_*.py.
"""

from collections.abc import Mapping


# Audited standalone prose and one backend-only pytest module. The latter uses
# monkeypatches against server routers, not browser fixtures or shared conftest
# changes. No web/harness/dispatch consumer references these exact files.
# New docs and tests require an audit; extensions/directories confer no safety.
SAFE_PATHS = frozenset({
    "docs/adn-purchase-walkthrough.md",
    "docs/adn-membership-reinstate-email-draft.md",
    "server/tests/test_catalog_digest_boundary.py",
})

# Deliberately broad dependency boundaries, not a claim of exhaustive read sets:
# web/walk/stack.mjs launches scripts/start-leaf.py, server, platform, harness;
# web/{next,vite}.config.* and vite-plugins/engineVersion.js load vendor engines;
# scripts/run-all-gates.py's root web-demo-gate invokes dispatch/run-local-ci.sh.
# Deployment/configuration, schemas, assets and unknown roots fail closed too.
DEPENDENCY_ROOTS = frozenset({
    "web", "server", "platform", "harness", "vendor", "scripts", "dispatch",
    "deploy", "auth0", "contract", "data", "da", "engine", "executor",
    "device-agent", "tools", "docs", ".codebuild", ".github",
})
ROOT_FAMILY_IDS = frozenset({"studio-walk-regressions", "web-demo-gate"})
_COLLECTIONS = (list, tuple, set, frozenset)


def _path(value):
    """Accept canonical relative POSIX file names, never normalize ambiguity."""
    return (isinstance(value, str) and bool(value)
            and "\\" not in value and ":" not in value
            and not any(ord(char) < 32 or ord(char) == 127 for char in value)
            and all(part and part not in {".", ".."}
                    and part == part.strip() for part in value.split("/")))


def _ids(values):
    return (isinstance(values, _COLLECTIONS)
            and all(isinstance(value, str) and bool(value)
                    and value == value.strip() for value in values))


def web_family_decision(selected, suites, changed_paths, *, protected_ids=(),
                        prerequisites=None):
    """Return ``(selected_set, human_readable_reason)`` without mutating inputs.

    Invalid/unavailable paths, catalog or dependencies preserve the original
    selection. Unknown paths do likewise. Protection includes explicit priority,
    new IDs and unconditional prerequisites supplied by the caller. Transitive
    prerequisites of retained suites cannot be removed, even through IDs absent
    from selected. This filter never adds IDs; the caller performs final closure.
    Lists/tuples/sets are supported; opaque iterators are unavailable evidence.
    """
    original = set(selected)

    def retain(reason):
        return original, "retained web family: " + reason

    if not _ids(selected) or not _ids(protected_ids):
        return retain("malformed selected or protected IDs")
    if not isinstance(suites, (list, tuple)) or not suites:
        return retain("missing or malformed catalog")
    catalog = {}
    for row in suites:
        if not isinstance(row, Mapping):
            return retain("malformed suite metadata")
        sid, cwd = row.get("id"), row.get("cwd")
        command, kind = row.get("command"), row.get("kind")
        # '..' is a legitimate runner fingerprint for parent-root platform
        # suites (_fingerprint_cwd in scripts/run-all-gates.py), never web.
        if (not _ids([sid]) or sid in catalog
                or not (cwd in (".", "..") or _path(cwd))
                or not isinstance(command, (list, tuple)) or not command
                or not all(isinstance(arg, str) and arg and "\0" not in arg
                           for arg in command)
                or not isinstance(kind, str) or not kind.strip()):
            return retain("malformed suite metadata")
        catalog[sid] = row
    if not original <= catalog.keys():
        return retain("selected ID absent from catalog")

    dependencies = {} if prerequisites is None else prerequisites
    if not isinstance(dependencies, Mapping):
        return retain("malformed prerequisites")
    for sid, deps in dependencies.items():
        if (not isinstance(sid, str) or sid not in catalog or not _ids(deps)
                or not set(deps) <= catalog.keys()):
            return retain("malformed prerequisites")

    if not isinstance(changed_paths, _COLLECTIONS) or not changed_paths:
        return retain("empty or unavailable changed paths")
    for path in changed_paths:
        if not _path(path):
            return retain("malformed changed path")
        if any(part.startswith(".") for part in path.split("/")):
            return retain("hidden/config path: " + path)
        if path not in SAFE_PATHS:
            category = ("dependency" if path.split("/", 1)[0] in DEPENDENCY_ROOTS
                        or "/" not in path else "unknown")
            return retain(category + " path: " + path)

    family = {sid for sid, row in catalog.items()
              if row["cwd"] == "web" or sid in ROOT_FAMILY_IDS}
    candidates = (original & family) - set(protected_ids)
    required = original - candidates
    pending = list(required)
    while pending:
        sid = pending.pop()
        for dependency in dependencies.get(sid, ()):
            if dependency not in required:
                required.add(dependency)
                pending.append(dependency)
    removed = candidates - required
    if not removed:
        return retain("no removable IDs (protected, required, or outside family)")
    return (original - removed,
            "removed proven-unaffected web family IDs: " + ", ".join(sorted(removed)))


def filter_web_family(selected, suites, changed_paths, *, protected_ids=(),
                      prerequisites=None):
    """Return the selected set with only proven-unaffected web IDs removed.

    See web_family_decision for the identical interface with a logging reason.
    Runner integration is a separate follow-up; this helper alone changes no CI.
    """
    return web_family_decision(
        selected, suites, changed_paths, protected_ids=protected_ids,
        prerequisites=prerequisites)[0]
