# X04 runtime failure replay

Studio X-bench X04's offline evidence tools refuse handled runtime failures
with exit 2, diagnostic stderr and no successful JSON emission. User
cancellation propagates as KeyboardInterrupt while temporary output is
removed. Save the worked synthetic example from
x04-offline-report-export-contract.md as example.json.

Run the injected fault tests on the allocated verification host from the
checkout root.

```sh
python -P -m pytest -p no:xonsh -o pythonpath=server server/tests/test_x04_evidence_registry_failures.py server/tests/test_x04_evidence_file_finalization.py server/tests/test_x04_evidence_cancel_cleanup.py server/tests/test_x04_evidence_early_output_refusal.py -q
```

| Injection | Expected observation |
| --- | --- |
| Missing or invalid registry JSON | Both CLIs exit 2 with a registry diagnostic |
| Missing tools array or count-modules entry | Both CLIs exit 2 with an actionable registry diagnostic |
| File flush or close failure after writing | Exit 2, previous artifact bytes retained and temporary file removed |
| KeyboardInterrupt during temporary write | Exception propagates, previous bytes retained and temporary file removed |
| KeyboardInterrupt before replacement | Exception propagates, previous bytes retained and temporary file removed |
| Tiny encoded-output ceiling | Exit 2 before publication and encoder stops after the first over-budget chunk |
| Exact encoded-output ceiling | Success is byte-identical to unlimited serialization |

Registry injections intercept the reader, so engine/registry.json remains
unchanged. Finalization injections wrap real temporary files and fail after
successful writes. Cancellation fixtures catch the propagated exception and
verify empty CLI streams. Every failure is followed by valid recovery and
checks exact count 7 and canonical highlights A and B. Cancellation follows
the invoking Python process's cancellation behavior instead of becoming a
handled exit 2 refusal.

Use the normal entry points to confirm recovery after the injected tests.

```sh
python scripts/export_x04_offline_evidence.py example.json --out recovered.json
python scripts/validate_x04_offline_evidence.py recovered.json
python scripts/export_x04_offline_evidence.py example.json --max-output-bytes 64 --out recovered.json
```

The first two commands exit 0. The final command exits 2, emits empty stdout
and leaves recovered.json unchanged. No .x04-* temporary file remains.

```sh
python scripts/bench_x04_evidence_early_output_refusal.py
```

The benchmark prepares a trusted synthetic artifact, then isolates output
serialization under a 64-byte ceiling for escaped strings and wide arrays.
It records actual encoder chunks and characters consumed, elapsed time and
traced peak memory. Construction and intake validation occur before the
measurement. Each refusal preserves prior bytes and is followed by exact-size
successful output and trusted validation. Measurements have no thresholds.
The standard encoder can produce a complete scalar string in one chunk, so
the budget stops further encoding once a chunk exceeds the limit. Original
byte integrity remains unverified. Raw dependency coverage and staging browser
acceptance retain their own evidence requirements.
