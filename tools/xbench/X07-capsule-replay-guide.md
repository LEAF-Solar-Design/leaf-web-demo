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

For a retained actual JSX capture, run from the checkout root and supply a new private output path:

```powershell
python tools/xbench/x07_jsx_failure_capsule.py --check x07_receipt_audit_jsx_journey.cjs --deps C:/tmp/x07-web-check-20261115/node_modules --out D:/agent-data/leaf-research/studio-xbench-2026-11/runs/X07/new-capsule.json --timeout 90
```

The artifact records command, source commit, dirty state, file digests, exit, timeout, bounded streams and process settlement. Source-change refusals record separately labelled before/after observations. Existing artifacts are never replaced. Read-only refusal is not a staging acceptance receipt. The staging incident hold continues until E3 posts a coherent recovery receipt.
