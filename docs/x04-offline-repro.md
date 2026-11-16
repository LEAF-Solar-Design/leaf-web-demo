# X04 offline reproduction

Parent: Studio X-bench X04 raw drawing intake. These checks use synthetic data only.
They do not establish acceptance for the ten held-out drawings or any staging source.

Run from the module-count worktree root in PowerShell. Python 3.11 or later and the
repository Python requirements are needed. Disabling the unrelated global xonsh
plugin avoids its Windows console requirement. No browser or Mac is used.

```powershell
python -P -m pytest -p no:xonsh -o pythonpath=server server/tests/test_count_modules.py server/tests/test_x04_module_selection_failures.py server/tests/test_drawing_dependency_report.py server/tests/test_x04_dependency_record_boundaries.py -q
python scripts/bench_x04_module_count.py --out D:/agents/production-lanes-2026-11-10/XBENCH/status/X04-module-benchmark.json
```

The tests construct INSERT dictionaries and closed four-corner polylines in memory.
They reverse and rotate entity order. Both orders must list identical set IDs and
return the same selected count. Highlight handle order can differ; membership must
stay equal. The reordered selection contains four synthetic modules.

The trusted-loader tests obtain count-modules from the normal registry. A malformed
set_ids value must return BAD_PARAMS. A selection mixing a known and unknown set
must fail with guidance to list the sets again. Neither failure may return a partial
count or highlight overlay.

Dependency fixtures are DXF group-code pairs. UNC, absolute and traversal paths are
reported as supplied while filesystem open functions are intercepted. Xrefs are
detached, fonts map to sans-serif, images and underlays are omitted. Truncated records
must not borrow names or paths from the next record. An IMAGEDEF without a filename
can report its own handle. An unnamed record without a usable reference is omitted.

The benchmark builds 1,000, 10,000 and 100,000 synthetic INSERTs across four layers.
Each row must report the exact selected count and that many unique highlighted
handles. It measures listing and selected-counting time and peak traced allocation
separately. Input construction is outside the measurement. Timing and memory values
are observations, with no speed threshold or timing-based pass gate.

Local evidence covers these synthetic code paths. It does not prove raw DWG metadata
completeness, rendered INSERT geometry, upload deadlines, modal absence or exact
real-drawing oracle counts. Those require the pinned raw dependency inventory, the
shared full X04 grader and canonical staging delivery. The current selected-CI,
staging and grading receipts are tracked in XBENCH/status/X04-status.md.
