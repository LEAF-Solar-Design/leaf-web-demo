# X07 capsule replay

Parent: Studio X-bench X07 offline customer-profile evidence. These recipes use disposable synthetic Node programs. They use no browser, network, customer drawings or live application.

From the checkout's `server` directory, run one recipe at a time:

```powershell
python -P -m pytest ../tools/xbench/tests/test_x07_capsule_replay_guide.py -o pythonpath=../tools/xbench -q -k assertion_recipe
python -P -m pytest ../tools/xbench/tests/test_x07_capsule_replay_guide.py -o pythonpath=../tools/xbench -q -k timeout_recipe
python -P -m pytest ../tools/xbench/tests/test_x07_capsule_replay_guide.py -o pythonpath=../tools/xbench -q -k overflow_recipe
python -P -m pytest ../tools/xbench/tests/test_x07_capsule_replay_guide.py -o pythonpath=../tools/xbench -q -k source_recipe
```

Each recipe creates a temporary check and a new artifact, checks the actual CLI result and artifact fields, then removes only its fixtures. The source recipe changes check bytes after observing its output. The other recipes assert, hang or emit one byte beyond the stream cap. Node must be installed. A dependency skip does not prove a recipe ran.

| CLI exit | Artifact | Retry decision |
| --- | --- | --- |
| 0 | Child passed, streams complete, source unchanged and workers settled | Keep the receipt. No retry needed. |
| 1 | Child failed, timed out or exceeded a stream cap | Read the complete diagnostic where available. A truncated stream cannot establish success. Correct that failure, then use a fresh output path. |
| 2, SOURCE_CHANGED | Before/after observations differ; no successful source attestation | Preserve both observations. Settle source edits and rerun into a fresh artifact. |
| 2, CAPSULE_UNAVAILABLE | No completed capture; preflight or startup failed | Resolve the named local prerequisite through its owner. An existing output remains untouched. The invocation removes only its own failed empty reservation. |

A timeout receipt remains evidence of a timeout. It does not show that the check passed. Endpoint source revalidation detects changed, added, removed or unreadable measured dependencies; it does not claim to detect a hostile edit restored to identical bytes between observations.

Cancellation returns CLI2 with `CAPTURE_CANCELLED` and removes only its owned failed reservation after worker cleanup. Reader failure returns CLI2 with `READER_FAILED`, incomplete diagnostics and settled-resource metadata. Its retained prefix is evidence of failure, never a successful check.

Use `read_capture(path)` from the capsule module to consume current captures. It requires complete newline-terminated JSON, the current schema, settlement metadata and a source binding or labelled source refusal. A producer killed during publication can leave partial bytes. Preserve those bytes and any foreign artifacts; retry into a fresh output path. Do not infer success from a file merely existing, and do not overwrite a partial file during retry. Historical artifacts keep their original bytes and producer-version semantics.

The consumer admits regular files up to 8 MiB, JSON nesting depth 32 and 20,000 nodes. Nodes include containers, member keys and scalar values. It checks file size before opening and scans depth and nodes before allocating the JSON object tree. Duplicate decoded keys, nonfinite numbers, trailing JSON, BOMs, invalid UTF-8 and lone surrogates refuse. These bounds are inclusive.

Current captures record per-stream received and retained byte counts, EOF flags and retained-byte SHA256 digests. Binary diagnostics retain canonical base64 alongside replacement-decoded display text. The consumer cross-checks these fields, overall completeness, reader errors, process settlement and exact scalar types. These checks prove internal consistency, not authenticity against an author who can rewrite every field. Missing fields in older captures refuse; consume those with their original producer version rather than adding attestations afterward.

For a retained actual JSX capture, run from the checkout root and supply a new private output path:

```powershell
python tools/xbench/x07_jsx_failure_capsule.py --check x07_receipt_audit_jsx_journey.cjs --deps C:/tmp/x07-web-check-20261115/node_modules --out D:/agent-data/leaf-research/studio-xbench-2026-11/runs/X07/new-capsule.json --timeout 90
```

The artifact records command, source commit, dirty state, file digests, exit, timeout, bounded streams and process settlement. Source-change refusals record separately labelled before/after observations. Existing artifacts are never replaced. Read-only refusal is not a staging acceptance receipt. Follow the current E3 delivery receipt and drawing-upload hold for staging work.
