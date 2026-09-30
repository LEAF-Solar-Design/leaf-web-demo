"""TCM-05 per-tenant storage snapshots and the monthly gb-month observation.

Transparency of Leaf's real cost, never billing. Hermetic: every store lives in
tmp_path, and every root env var is set or cleared per test.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = SERVER_DIR.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from cost_meter import collect_storage_main, storage  # noqa: E402
from cost_meter.ledger import ESTIMATED, ResourcePeriod, compute_shares  # noqa: E402

UTC = timezone.utc
PERIOD = "2026-09"


def _t(month: int, day: int, hour: int = 0) -> datetime:
    return datetime(2026, month, day, hour, tzinfo=UTC)


def _write(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def _link_dir(link: Path, target: Path) -> None:
    """A directory symlink, or a junction on a Windows host without symlink rights."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        if os.name != "nt":
            raise
    import _winapi
    _winapi.CreateJunction(str(target), str(link))


def _link_file(link: Path, target: Path) -> bool:
    try:
        os.symlink(target, link)
        return True
    except (OSError, NotImplementedError):
        if os.name != "nt":
            raise
        return False  # no file-link equivalent without the symlink privilege


def _assert_no_floats(value) -> None:
    assert not isinstance(value, float), value
    if isinstance(value, dict):
        for key, item in value.items():
            _assert_no_floats(key)
            _assert_no_floats(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_no_floats(item)


def _line(moment: datetime, counts: dict, complete: bool = True) -> str:
    return json.dumps({
        "kind": storage.SNAPSHOT_KIND,
        "schema": storage.SNAPSHOT_SCHEMA,
        "taken_at": storage.format_timestamp(moment),
        "bytes": {key: str(value) for key, value in counts.items()},
        "complete": complete,
    })


def _daily(first: int, last: int, counts: dict, skip=()) -> list:
    return [_line(_t(9, day), counts) for day in range(first, last + 1) if day not in skip]


# --------------------------------------------------------------------------- #
# snapshot
# --------------------------------------------------------------------------- #
def test_snapshot_attributes_two_tenants_and_unattributed_bytes_in_every_root(tmp_path):
    uploads, store, git, runs = (tmp_path / n for n in ("uploads", "store", "git", "runs"))
    _write(uploads / "t1--d1.dwg", 100)
    _write(uploads / "t2--d2.dxf", 200)
    _write(uploads / "orphan.dwg", 7)             # no '<tenant>--' prefix
    _write(uploads / "leaf--x.dwg", 3)            # the reserved participant is never a tenant
    _write(store / "tenants" / "t1" / "drawings" / "a" / "v" / "00000001.dwg", 1000)
    _write(store / "tenants" / "t2" / "drawings" / "b" / "manifest.json", 2000)
    _write(store / "demo-cache" / "blob", 11)     # outside tenants/
    _write(git / "t1.git" / "HEAD", 10)
    _write(git / "t1.git" / "objects" / "ab" / "cd", 20)
    _write(git / "notes.txt", 5)
    _write(runs / "t2" / "run1" / "state.json", 300)
    _write(runs / "stray.json", 13)               # a file, not a tenant directory

    snap = storage.snapshot({"uploads": str(uploads), "drawings": str(store),
                             "tenant_git": str(git), "marathon_runs": str(runs)}, _t(9, 1))

    assert snap["bytes"] == {"t1|": "1130", "t2|": "2500", "leaf|unattributed": "39"}
    assert snap["total_bytes"] == "3669"
    assert snap["taken_at"] == "2026-09-01T00:00:00Z"
    assert snap["complete"] is True
    assert snap["skipped_entries"] == 0
    assert {kind: r["status"] for kind, r in snap["roots"].items()} == {
        "uploads": "measured", "drawings": "measured", "tenant_git": "measured", "marathon_runs": "measured"}
    _assert_no_floats(snap)
    assert json.loads(json.dumps(snap)) == snap


def test_snapshot_skips_links_and_never_follows_them_out_of_the_root(tmp_path):
    outside = tmp_path / "outside"
    _write(outside / "secret.bin", 10_000)
    uploads, store = tmp_path / "uploads", tmp_path / "store"
    _write(uploads / "t1--a.dwg", 50)
    _write(store / "tenants" / "t2" / "drawings" / "x" / "v.dwg", 70)
    _link_dir(uploads / "t1--link", outside)
    file_linked = _link_file(uploads / "t2--f.dwg", outside / "secret.bin")
    _link_dir(store / "tenants" / "t1", outside)          # a whole tenant dir that points out
    _link_dir(store / "tenants" / "t2" / "drawings" / "escape", outside)

    snap = storage.snapshot({"uploads": str(uploads), "drawings": str(store)}, _t(9, 1),
                            not_configured=("tenant_git", "marathon_runs"))

    assert snap["bytes"] == {"t1|": "50", "t2|": "70"}
    assert snap["roots"]["uploads"]["skipped_links"] == (2 if file_linked else 1)
    assert snap["roots"]["drawings"]["skipped_links"] == 2
    assert snap["skipped_entries"] == (4 if file_linked else 3)
    assert snap["complete"] is True  # a skipped link is intentional, not lost coverage


def test_snapshot_caps_entries_per_root_and_reports_truncation(tmp_path):
    uploads = tmp_path / "uploads"
    for i in range(5):
        _write(uploads / f"t1--d{i}.dwg", 1)

    snap = storage.snapshot({"uploads": str(uploads)}, _t(9, 1), max_entries=3)

    root = snap["roots"]["uploads"]
    assert root["status"] == "truncated"
    assert root["entries_scanned"] == 3
    assert snap["bytes"] == {"t1|": "3"}
    assert snap["complete"] is False

    full = storage.snapshot({"uploads": str(uploads)}, _t(9, 1), max_entries=5,
                            not_configured=("drawings", "tenant_git", "marathon_runs"))
    assert full["roots"]["uploads"]["status"] == "measured"
    assert full["complete"] is True


def test_snapshot_skips_a_configured_root_nested_in_another(tmp_path):
    store = tmp_path / "data"
    uploads = store / "uploads"
    _write(uploads / "t1--a.dwg", 40)
    _write(store / "tenants" / "t1" / "drawings" / "a" / "v.dwg", 60)

    snap = storage.snapshot({"uploads": str(uploads), "drawings": str(store)}, _t(9, 1))

    assert snap["bytes"] == {"t1|": "100"}  # the upload is counted once, by its own root
    assert snap["roots"]["drawings"]["skipped_nested_roots"] == 1


def test_snapshot_reports_unset_and_missing_roots(tmp_path):
    snap = storage.snapshot({"uploads": None, "drawings": str(tmp_path / "nope")}, _t(9, 1))

    assert snap["roots"]["uploads"] == {"status": "unset"}
    assert snap["roots"]["drawings"] == {"status": "missing"}
    assert snap["roots"]["tenant_git"] == {"status": "unset"}
    assert snap["bytes"] == {}
    assert snap["total_bytes"] is None
    assert snap["measured_total_bytes"] == "0"
    assert snap["complete"] is False


def test_snapshot_and_observation_refuse_bad_input(tmp_path):
    with pytest.raises(ValueError):
        storage.snapshot({"uploads": str(tmp_path)}, datetime(2026, 9, 1))  # naive
    with pytest.raises(ValueError):
        storage.snapshot({"bogus": str(tmp_path)}, _t(9, 1))
    with pytest.raises(ValueError):
        storage.snapshot({"uploads": str(tmp_path)}, _t(9, 1), max_entries=0)
    with pytest.raises(ValueError):
        storage.storage_usage_observation("2026-13", [], now=_t(10, 1))
    with pytest.raises(ValueError):
        storage.storage_usage_observation(PERIOD, [], now=datetime(2026, 10, 1))


def test_snapshot_all_roots_unset_is_incomplete(monkeypatch):
    _clear_env(monkeypatch)

    snap = storage.snapshot(storage.roots_from_env(), _t(9, 1))

    assert snap["complete"] is False
    assert snap["bytes"] == {}
    assert snap["total_bytes"] is None
    assert snap["measured_total_bytes"] == "0"
    assert all(root == {"status": "unset"} for root in snap["roots"].values())


def test_snapshot_empty_configured_root_is_complete_zero(tmp_path):
    snap = storage.snapshot({"uploads": str(tmp_path)}, _t(9, 1),
                            not_configured=("drawings", "tenant_git", "marathon_runs"))

    assert snap["complete"] is True
    assert snap["bytes"] == {}
    assert snap["total_bytes"] == "0"
    assert snap["roots"]["uploads"]["status"] == "measured"


@pytest.mark.parametrize("alias", [False, True])
def test_snapshot_duplicate_real_roots_count_bytes_once(tmp_path, alias):
    root = tmp_path / "uploads"
    _write(root / "t1--a.dwg", 40)
    second = tmp_path / "alias" if alias else root
    if alias:
        _link_dir(second, root)

    snap = storage.snapshot({"drawings": str(second), "uploads": str(root)}, _t(9, 1),
                            not_configured=("tenant_git", "marathon_runs"))

    assert snap["complete"] is True
    assert snap["bytes"] == {"t1|": "40"}
    assert snap["total_bytes"] == "40"
    assert snap["roots"]["drawings"] == {"status": "duplicate", "duplicate_of": "uploads"}


# --------------------------------------------------------------------------- #
# observation
# --------------------------------------------------------------------------- #
def test_observation_integrates_a_step_function_on_fixed_timestamps():
    # 720 h period. S0 carries in from Aug 31 12:00 and holds 24 h, S1 holds
    # 336 h, S2 holds 360 h to the period end. Byte-hours / (10^9 x 720):
    #   t1   1.5e9*24 + 3e9*336 + 2e9*360 = 1764e9  -> 2.45
    #   t2   3e9*24 + 1e9*360             =  432e9  -> 0.6
    #   leaf|unattributed 1.5e9*336      =  504e9  -> 0.7
    lines = [
        _line(_t(9, 16), {"t1|": 2_000_000_000, "t2|": 1_000_000_000}),                 # S2
        _line(_t(8, 20), {"t1|": 9_000_000_000_000}),                                   # older carry, superseded
        _line(_t(9, 2), {"t1|": 3_000_000_000, "leaf|unattributed": 1_500_000_000}),    # S1
        _line(_t(10, 1), {"t1|": 9_000_000_000_000}),                                   # at the period end: ignored
        _line(_t(8, 31, 12), {"t1|": 1_500_000_000, "t2|": 3_000_000_000}),             # S0
        _line(_t(10, 3), {"t1|": 9_000_000_000_000}),                                   # after: ignored
    ]

    obs = storage.storage_usage_observation(PERIOD, lines, now=_t(10, 5))

    assert obs["kind"] == "usage"
    assert obs["resource_id"] == "aws:amazon-elastic-file-system"
    assert obs["period"] == PERIOD
    assert obs["unit"] == "gb-month"
    assert obs["status"] == ESTIMATED
    assert obs["usages"] == {"leaf|unattributed": "0.700000000000000", "t1|": "2.450000000000000",
                             "t2|": "0.600000000000000"}
    assert obs["total_usage"] == "3.750000000000000"
    assert obs["coverage"] == "partial"  # S1 -> S2 is a 336 h gap
    assert obs["source"] == "cost_meter.storage:snapshots=3"
    _assert_no_floats(obs)

    rp = ResourcePeriod(resource_id=obs["resource_id"], period=PERIOD, unit=obs["unit"],
                        total_usage=obs["total_usage"], gross_cost_usd="10.00", credits_usd="0")
    shares = compute_shares(rp, {tuple(k.split("|")): v for k, v in obs["usages"].items()}, ESTIMATED)
    assert sum(s.usage_share for s in shares) == Decimal("1")
    assert {s.key for s in shares} == {("t1", ""), ("t2", ""), ("leaf", "unattributed")}


def test_observation_daily_samples_are_complete():
    lines = _daily(1, 30, {"t1|": 500_000_000, "t2|": 250_000_000})

    obs = storage.storage_usage_observation(PERIOD, lines, now=_t(10, 2))

    assert obs["usages"] == {"t1|": "0.500000000000000", "t2|": "0.250000000000000"}
    assert obs["total_usage"] == "0.750000000000000"
    assert obs["coverage"] == "complete"


def test_observation_gap_over_48_hours_is_partial():
    counts = {"t1|": 500_000_000}
    gapped = storage.storage_usage_observation(PERIOD, _daily(1, 30, counts, skip=(10, 11, 12)),
                                               now=_t(10, 2))
    assert gapped["coverage"] == "partial"  # Sep 9 -> Sep 13 is 96 h
    assert Decimal(gapped["usages"]["t1|"]) == Decimal("0.5")  # the step holds across the gap

    # A leading gap of exactly 48 h is still complete; 336 h / 720 h rounds half-even at 15 places.
    edge = storage.storage_usage_observation(PERIOD, _daily(3, 30, counts), now=_t(10, 2))
    assert edge["coverage"] == "complete"
    assert edge["usages"] == {"t1|": "0.466666666666667"}

    late = [_line(_t(9, 3, 1), counts)] + _daily(4, 30, counts)
    assert storage.storage_usage_observation(PERIOD, late, now=_t(10, 2))["coverage"] == "partial"


@pytest.mark.parametrize("moment", [_t(8, 31, 12), _t(9, 1)])
def test_observation_incomplete_carry_in_is_partial(moment):
    counts = {"t1|": 500_000_000}
    lines = [_line(moment, counts, complete=False)] + _daily(2, 30, counts)

    obs = storage.storage_usage_observation(PERIOD, lines, now=_t(10, 2))

    assert obs["coverage"] == "partial"
    assert obs["usages"] == {"t1|": "0.500000000000000"}


def test_observation_superseded_incomplete_carry_does_not_reduce_coverage():
    counts = {"t1|": 500_000_000}
    lines = _daily(1, 30, counts) + [_line(_t(8, 31), counts, complete=False)]

    assert storage.storage_usage_observation(PERIOD, lines, now=_t(10, 2))["coverage"] == "complete"


def test_observation_open_period_holds_last_sample_to_now():
    lines = _daily(1, 15, {"t1|": 1_440_000_000}) + [_line(_t(9, 20), {"t1|": 9_000_000_000_000})]

    obs = storage.storage_usage_observation(PERIOD, lines, now=_t(9, 16))

    assert obs["usages"] == {"t1|": "0.720000000000000"}  # 1.44e9 x 360 h / (1e9 x 720 h)
    assert obs["coverage"] == "partial"  # the period is still open


def test_observation_without_samples_is_unknown_not_zero():
    for lines, now in (([], _t(10, 2)),
                       (_daily(1, 30, {"t1|": 1}), _t(8, 15)),               # period not started
                       ([_line(_t(10, 5), {"t1|": 1})], _t(10, 9))):          # only later samples
        obs = storage.storage_usage_observation(PERIOD, lines, now=now)
        assert obs["total_usage"] is None
        assert obs["usages"] == {}
        assert obs["coverage"] == "unknown"
        assert obs["status"] == ESTIMATED


def test_observation_skips_malformed_lines_and_marks_partial():
    counts = {"t1|": 500_000_000}
    bad = [
        "not json",
        json.dumps({"kind": storage.SNAPSHOT_KIND, "taken_at": "2026-09-15T00:00:00Z", "bytes": {"t1|": 1.5}}),
        json.dumps({"kind": storage.SNAPSHOT_KIND, "taken_at": "2026-09-15T00:00:00Z", "bytes": {"t1": "5"}}),
        json.dumps({"kind": "other"}),
    ]
    clean = storage.storage_usage_observation(PERIOD, _daily(1, 30, counts), now=_t(10, 2))
    assert clean["coverage"] == "complete"

    obs = storage.storage_usage_observation(PERIOD, _daily(1, 30, counts) + bad, now=_t(10, 2))
    assert obs["usages"] == clean["usages"]
    assert obs["coverage"] == "partial"

    incomplete = _daily(1, 30, counts) + [_line(_t(9, 15, 12), counts, complete=False)]
    obs = storage.storage_usage_observation(PERIOD, incomplete, now=_t(10, 2))
    assert obs["usages"] == clean["usages"]
    assert obs["coverage"] == "partial"


def test_aws_resource_id_naming():
    assert storage.aws_resource_id("Amazon Elastic File System") == storage.RESOURCE_ID
    assert storage.aws_resource_id("AWS Lambda") == "aws:aws-lambda"
    assert storage.aws_resource_id(" Amazon EC2 Container Registry (ECR) ") == "aws:amazon-ec2-container-registry-ecr"
    with pytest.raises(ValueError):
        storage.aws_resource_id("  ")


# --------------------------------------------------------------------------- #
# the collector script
# --------------------------------------------------------------------------- #
def _load_script():
    path = REPO_DIR / "scripts" / "collect-cost-storage.py"
    spec = importlib.util.spec_from_file_location("collect_cost_storage_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _clear_env(monkeypatch):
    for var in list(storage.ROOT_ENVS.values()) + [storage.SNAPSHOTS_ENV]:
        monkeypatch.delenv(var, raising=False)


def _uploads_only_args():
    return [arg for kind in ("drawings", "tenant_git", "marathon_runs")
            for arg in ("--not-configured", kind)]


def test_script_appends_one_line_per_run_and_observes(tmp_path, monkeypatch, capsys):
    _clear_env(monkeypatch)
    uploads = tmp_path / "uploads"
    _write(uploads / "t1--a.dwg", 1000)
    snapshots = tmp_path / "ledger" / "storage.jsonl"
    monkeypatch.setenv("LEAF_UPLOADS_DIR", str(uploads))
    monkeypatch.setenv(storage.SNAPSHOTS_ENV, str(snapshots))
    script = _load_script()

    assert script.main(_uploads_only_args(), now=_t(9, 1)) == 0
    assert script.main(_uploads_only_args(), now=_t(9, 2)) == 0
    printed = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.strip()]
    stored = [json.loads(x) for x in snapshots.read_text(encoding="utf-8").splitlines()]
    assert stored == printed
    assert [s["taken_at"] for s in stored] == ["2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z"]
    assert stored[0]["bytes"] == {"t1|": "1000"}
    assert stored[0]["roots"]["drawings"] == {
        "status": "not_configured", "reason": "unset in this deployment"}

    assert script.main(["--observe", PERIOD], now=_t(10, 2)) == 0
    obs = json.loads(capsys.readouterr().out)
    assert obs["resource_id"] == "aws:amazon-elastic-file-system"
    assert Decimal(obs["usages"]["t1|"]) == Decimal("0.000001")  # 1000 B for 720 h of 720 h
    assert obs["coverage"] == "partial"  # Sep 2 -> Oct 1 has no sample
    _assert_no_floats(obs)


def test_script_is_stdout_only_when_the_snapshots_env_is_unset(tmp_path, monkeypatch, capsys):
    _clear_env(monkeypatch)
    uploads = tmp_path / "uploads"
    _write(uploads / "t2--b.dxf", 5)
    monkeypatch.setenv("LEAF_UPLOADS_DIR", str(uploads))
    script = _load_script()

    assert script.main(_uploads_only_args(), now=_t(9, 1)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["bytes"] == {"t2|": "5"}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["uploads"]  # nothing written

    assert script.main(["--observe", PERIOD], now=_t(10, 2)) == 2  # nothing to observe from


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("root_state", ["unset", "missing", "truncated"])
def test_script_incomplete_snapshot_is_not_appended(tmp_path, monkeypatch, capsys, existing, root_state):
    _clear_env(monkeypatch)
    snapshots = tmp_path / "storage.jsonl"
    original = (_line(_t(8, 31), {"t1|": 10}) + "\n").encode("utf-8")
    if existing:
        snapshots.write_bytes(original)
    monkeypatch.setenv(storage.SNAPSHOTS_ENV, str(snapshots))
    uploads = tmp_path / "uploads"
    if root_state != "unset":
        monkeypatch.setenv("LEAF_UPLOADS_DIR", str(uploads))
    if root_state == "truncated":
        _write(uploads / "t1--a.dwg", 10)
        _write(uploads / "t1--b.dwg", 20)

    assert _load_script().main(["--max-entries", "1"] + _uploads_only_args(), now=_t(9, 1)) == 3

    output = capsys.readouterr()
    snap = json.loads(output.out)
    assert snap["complete"] is False
    assert snap["total_bytes"] is None
    assert "incomplete" in output.err
    if existing:
        assert snapshots.read_bytes() == original
    else:
        assert not snapshots.exists()


def test_script_appends_empty_configured_root(tmp_path, monkeypatch, capsys):
    _clear_env(monkeypatch)
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    snapshots = tmp_path / "storage.jsonl"
    monkeypatch.setenv("LEAF_UPLOADS_DIR", str(uploads))
    monkeypatch.setenv(storage.SNAPSHOTS_ENV, str(snapshots))

    assert _load_script().main(_uploads_only_args(), now=_t(9, 1)) == 0

    snap = json.loads(snapshots.read_text(encoding="utf-8"))
    assert snap == json.loads(capsys.readouterr().out)
    assert snap["complete"] is True
    assert snap["total_bytes"] == "0"


def test_snapshot_unset_root_keeps_only_the_measured_subtotal(tmp_path):
    _write(tmp_path / "t1--a.dwg", 10)

    snap = storage.snapshot({"uploads": str(tmp_path)}, _t(9, 1))

    assert snap["complete"] is False
    assert snap["total_bytes"] is None
    assert snap["measured_total_bytes"] == "10"
    assert snap["bytes"] == {"t1|": "10"}


def test_snapshot_without_any_configured_roots_cannot_claim_measured_zero():
    snap = storage.snapshot({}, _t(9, 1), not_configured=storage.ROOT_ENVS)

    assert snap["complete"] is False
    assert snap["total_bytes"] is None
    assert snap["measured_total_bytes"] == "0"
    assert all(root["status"] == "not_configured" for root in snap["roots"].values())


def test_collect_storage_not_configured_deployment_appends_nested_roots_once(tmp_path, monkeypatch, capsys):
    _clear_env(monkeypatch)
    store = tmp_path / "store"
    uploads = store / "uploads"
    git = tmp_path / "git"
    git.mkdir()
    _write(uploads / "t1--a.dwg", 40)
    _write(store / "tenants" / "t1" / "drawings" / "a.dwg", 60)
    snapshots = tmp_path / "storage.jsonl"
    for kind, path in (("uploads", uploads), ("drawings", store), ("tenant_git", git)):
        monkeypatch.setenv(storage.ROOT_ENVS[kind], str(path))
    monkeypatch.setenv(storage.SNAPSHOTS_ENV, str(snapshots))

    assert collect_storage_main.main(["--not-configured", "marathon_runs"], now=_t(9, 1)) == 0

    lines = snapshots.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    snap = json.loads(lines[0])
    assert snap == json.loads(capsys.readouterr().out)
    assert snap["complete"] is True
    assert snap["total_bytes"] == snap["measured_total_bytes"] == "100"
    assert snap["bytes"] == {"t1|": "100"}
    assert snap["roots"]["drawings"]["skipped_nested_roots"] == 1
    assert snap["roots"]["marathon_runs"] == {
        "status": "not_configured", "reason": "unset in this deployment"}


@pytest.mark.parametrize("value", ["", "some/path"])
def test_collect_storage_rejects_not_configured_when_env_is_set(tmp_path, monkeypatch, capsys, value):
    _clear_env(monkeypatch)
    snapshots = tmp_path / "storage.jsonl"
    monkeypatch.setenv(storage.SNAPSHOTS_ENV, str(snapshots))
    monkeypatch.setenv("LEAF_MARATHON_RUNS_DIR", value)

    assert collect_storage_main.main(["--not-configured", "marathon_runs"], now=_t(9, 1)) == 2

    output = capsys.readouterr()
    assert output.out == ""
    assert "requires LEAF_MARATHON_RUNS_DIR to be unset" in output.err
    assert not snapshots.exists()


@pytest.mark.parametrize("existing", [False, True])
def test_module_collect_storage_dry_run_never_appends(tmp_path, monkeypatch, capsys, existing):
    from cost_meter import __main__ as cost_main

    _clear_env(monkeypatch)
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    snapshots = tmp_path / "storage.jsonl"
    original = b"existing snapshot\n"
    if existing:
        snapshots.write_bytes(original)
    monkeypatch.setenv("LEAF_UPLOADS_DIR", str(uploads))
    monkeypatch.setenv(storage.SNAPSHOTS_ENV, str(snapshots))

    assert cost_main.main(["collect-storage", "--dry-run"] + _uploads_only_args()) == 0

    snap = json.loads(capsys.readouterr().out)
    assert snap["complete"] is True
    assert snap["total_bytes"] == "0"
    if existing:
        assert snapshots.read_bytes() == original
    else:
        assert not snapshots.exists()


def test_collect_storage_one_unset_root_is_not_appended(tmp_path, monkeypatch, capsys):
    _clear_env(monkeypatch)
    for kind in ("uploads", "drawings", "tenant_git"):
        root = tmp_path / kind
        root.mkdir()
        monkeypatch.setenv(storage.ROOT_ENVS[kind], str(root))
    snapshots = tmp_path / "storage.jsonl"
    monkeypatch.setenv(storage.SNAPSHOTS_ENV, str(snapshots))

    assert collect_storage_main.main([], now=_t(9, 1)) == 3

    snap = json.loads(capsys.readouterr().out)
    assert snap["complete"] is False
    assert snap["total_bytes"] is None
    assert snap["roots"]["marathon_runs"] == {"status": "unset"}
    assert not snapshots.exists()


def test_collect_storage_append_failure_exits_one(tmp_path, monkeypatch, capsys):
    _clear_env(monkeypatch)
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    # An existing directory cannot be opened as a JSONL append destination.
    monkeypatch.setenv("LEAF_UPLOADS_DIR", str(uploads))
    monkeypatch.setenv(storage.SNAPSHOTS_ENV, str(tmp_path))

    assert collect_storage_main.main(_uploads_only_args(), now=_t(9, 1)) == 1

    output = capsys.readouterr()
    assert json.loads(output.out)["complete"] is True
    assert "append failed" in output.err
    assert list(tmp_path.iterdir()) == [uploads]


def test_old_script_delegates_to_the_image_collector():
    assert _load_script().main is collect_storage_main.main


@pytest.mark.parametrize("root_status", ["unset", "not_configured", "missing", "unreadable"])
def test_observation_without_measured_roots_is_unknown(root_status):
    lines = [{"kind": storage.SNAPSHOT_KIND, "taken_at": storage.format_timestamp(_t(9, day)),
              "bytes": {}, "complete": root_status == "not_configured",
              "roots": {kind: {"status": root_status} for kind in storage.ROOT_ENVS}}
             for day in range(1, 31)]

    obs = storage.storage_usage_observation(PERIOD, lines, now=_t(10, 2))

    assert obs["coverage"] == "unknown"
    assert obs["total_usage"] is None
    assert obs["usages"] == {}


def test_observation_legacy_empty_bytes_without_roots_is_unknown():
    obs = storage.storage_usage_observation(PERIOD, _daily(1, 30, {}), now=_t(10, 2))

    assert obs["coverage"] == "unknown"
    assert obs["total_usage"] is None


def test_observation_measured_empty_roots_are_genuine_zero(tmp_path):
    lines = [storage.snapshot({"uploads": str(tmp_path)}, _t(9, day),
                              not_configured=("drawings", "tenant_git", "marathon_runs"))
             for day in range(1, 31)]

    obs = storage.storage_usage_observation(PERIOD, lines, now=_t(10, 2))

    assert obs["coverage"] == "complete"
    assert Decimal(obs["total_usage"]) == 0
    assert obs["usages"] == {}


def test_observation_ignores_unmeasured_sample_and_marks_partial():
    lines = _daily(1, 30, {"t1|": 1_000_000_000})
    lines.append(storage.snapshot({}, _t(9, 15, 12)))

    obs = storage.storage_usage_observation(PERIOD, lines, now=_t(10, 2))

    assert obs["coverage"] == "partial"
    assert obs["total_usage"] == "1.000000000000000"
