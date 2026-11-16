# X04 portable evidence replay

Studio X-bench X04 track 2 exports synthetic evidence that carries its saved
entities, decoded dependency pairs and selected IDs. Validation can recompute
the result after the original synthetic input is removed.

Save the JSON example from x04-offline-report-export-contract.md as
scratch/source.json. Set REPO to this checkout's absolute path and SCRATCH to
an empty scratch directory. Run these commands on the allocated verification
host, using synthetic data.

```sh
python "$REPO/scripts/export_x04_offline_evidence.py" "$SCRATCH/source.json" --out "$SCRATCH/evidence.json"
mkdir "$SCRATCH/moved" "$SCRATCH/unrelated"
mv "$SCRATCH/evidence.json" "$SCRATCH/moved/evidence.json"
rm "$SCRATCH/source.json"
cd "$SCRATCH/unrelated"
python -P "$REPO/scripts/validate_x04_offline_evidence.py" "$SCRATCH/moved/evidence.json"
```

Export exits 0 with empty stdout and stderr. Validation exits 0 with one JSON
document and empty stderr. The example returns valid true, count 7 and
canonical highlighted handles A and B. original_bytes_integrity stays
unverified. The moved file's bytes remain identical after validation.

Both CLIs refuse identical, normalized-equivalent, symlink and hardlink
input/output aliases with exit 2 before output replacement. Concurrent
exports to one destination use unique temporary files. The last successful
replacement supplies one complete artifact, with its drawing identity,
selection and entity-derived count from the same input.

Unknown envelope fields are ignored by export. Unknown drawing and intake
fields are retained as opaque input. Unknown request and result fields are
ignored when recomputing known claims. Validation still checks count,
highlights and byte-integrity claims. Dependency reports and their rows must
match the report recomputed from embedded decoded pairs exactly, so unknown
fields there receive a refusal diagnostic.

A closed stdout causes exit 2. A closed stderr can suppress the diagnostic
while preserving exit 2. Failed input and output operations preserve existing
artifact bytes. Dependency targets remain unopened.

Run the focused replay on the verification host from the checkout root.

```sh
python -P -m pytest -p no:xonsh -o pythonpath=server server/tests/test_x04_evidence_path_aliases.py server/tests/test_x04_evidence_concurrent_writers.py server/tests/test_x04_evidence_extension_policy.py server/tests/test_x04_evidence_broken_pipe.py -q
python scripts/bench_x04_evidence_reference_payload.py
```

The benchmark fixes 128 physical parents while increasing four literal
dependency names. It checks exact names, count and highlights, then records
JSON size, time and traced peak memory without thresholds. These observations
cover synthetic consistency. Raw DWG completeness, original-byte proof and
the staging browser walk retain their separate evidence requirements.
