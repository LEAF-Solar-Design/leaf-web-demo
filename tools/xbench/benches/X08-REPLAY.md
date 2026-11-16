# X08 offline evidence replay

From the repository root, using only local sample files:

```sh
PYTHONPATH=tools/xbench python -m benches.x08_replay --samples /path/to/samples.json --expected-modules 12 --out /path/to/x08-diagnostics.json
python -P -m pytest -p no:xonsh -o pythonpath=tools/xbench tools/xbench/tests/test_x08_frame_evidence.py tools/xbench/tests/test_x08_replay.py -q -p no:cacheprovider
```

Replay imports the existing pure `x08_metrics.score`, serializes its six `Step`
dataclasses (`n`, `verdict`, `evidence`, `observed`, `expected`), and emits a
`x08_offline_replay_diagnostic_v1` object with `steps` and aggregate `verdict`.
Exit status is 0 for all PASS, 1 for readable FAIL without UNREADABLE, and 4 for
any UNREADABLE or IO/JSON error. Invalid JSON, non-object roots, NaN, Infinity,
and numeric overflow refuse. Output is replaced atomically from a temporary
sibling. Resolved path, symlink, and same-inode input aliases refuse; sample
bytes are never rewritten. IO/JSON errors are reported on stderr without a traceback.

The scorer input is its existing mapping: `geometry` with integer
`rendered_module_count`, `marker_module_count`, `omitted_module_count`;
`single_select_ms`; `window_select` rows with `latency_ms`, independent oracle
`expected_handles`, `selected_handles`, and `oracle_independent: true`;
`job_ack` rows with `latency_ms`, `http_status: 202`, `rendered: true`;
`frame_deltas_ms`; `sequence`; `cold_run_discarded: true`; and `reference`.
Window handle sets must match exactly, with 490–510 unique nonempty oracle handles.
Timing minima are 20 single selections, 5 window selections, 10 job acknowledgements,
and 600 frames. Timings must be finite and strictly positive. Strict nearest-rank
p95 is sorted element `ceil(0.95*n)` (one-based), with budgets **<100 ms** for
single/window selection, **<200 ms** for acknowledgement, **<33 ms** for frames.
Cold runs must already be excluded and explicitly marked discarded; replay never
discards or repairs samples. Missing metadata is UNREADABLE; 599 frames refuse.

`validate_frame_evidence(snapshot, cleanup=...)` is a separate fail-closed adapter.
The current collector emits `complete`, `visible`, `visibility`, `error`, absolute
`start_ms`/`finish_ms`, `duration_ms`, `actions`, `raw_events`, `frame_deltas_ms`,
and `sequence`. Each action has `kind`, recording-relative `start_ms`/`finish_ms`,
`success`, and `within_recording`. All four pans, ten zoom-ins, ten zoom-outs, and
one window selection must appear in that order within 0–10000 ms. Sequence fields
are `pan_drags`, `zoom_in_steps`, `zoom_out_steps`, and snapshot `duration_ms`.
Visible history must stay visible, recording duration must be at least 10000 ms,
and frame sums must fit the actual interval with at most 1 ms tolerance.
`raw_events` contains `trusted` and `untrusted` nonnegative integer counters for
pointerdown/up/move and wheel; only trusted counts qualify (minima 5/5/5/20).
The collector does **not** emit cleanup metadata. A separate observation mapping
must explicitly assert `recorder_removed`, `element_disposed`, and `mouse_released`
all true. Missing cleanup or window action evidence refuses even if complete is true.
The adapter returns copied scorer fields only and does not validate selection geometry.

`validate_provenance(fixture, reference, expected_fixture_sha256=...,
expected_source_sha=...)` requires explicit strict 64/40 hexadecimal hashes.
Both receipts must contain matching `fixture_sha256`, `source_sha`, and
`source_binding_verified: true`. Optional `sha256` must agree; fixture `bytes`
must be a positive integer. `reference['reference']` must carry the scorer's
step-six fields: hostname, os, cpu, nonempty gpu list, positive integer
free_ram_bytes, chromium_version, boolean headed, positive integer viewport
width/height, and positive device_scale_factor. Hardware mismatch remains a
reference diagnostic, not a provenance PASS claim. No filename/git/health inference occurs.

The CLI consumes scorer samples directly: it does **not** invoke either adapter,
invent raw collector metadata, or validate provenance (`provenance_validated: false`).
Every report has `qualified_live_grade: false`; `synthetic: true` labels it
`synthetic_non_scored`, otherwise `offline_non_scored`. Synthetic and offline
PASS diagnostics never qualify a scored live run, establish readiness, or replace
real drawing evidence. No browser, network, or collector runs during replay.
