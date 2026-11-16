# X04 evidence diagnostics

Studio X-bench X04 track 2 provides these diagnostics for its synthetic
portable evidence CLIs. Save the worked JSON from
x04-offline-report-export-contract.md as example.json. Run commands from the
checkout on the allocated verification host.

```sh
python scripts/export_x04_offline_evidence.py example.json --out evidence.json
python scripts/validate_x04_offline_evidence.py evidence.json
```

Export with --out exits 0 with empty stdout and stderr. Validation without
--out exits 0 with one strict JSON document and empty stderr. Schema is the
version discriminator and must equal leaf.x04.offline-evidence.v1 exactly.
The validated count claim must be an integer. Export recomputes derived claims.
Both CLIs allow up to 256 nested containers, counting the root object. The
limit applies to opaque extensions as well as known fields, independently of
the interpreter's recursion setting. Literal brackets inside strings have no
effect on the container depth.

| Failure category | Minimal synthetic input or trigger | Expected result |
| --- | --- | --- |
| JSON syntax | Save `{invalid` as bad.json | Exit 2, empty stdout, refusal diagnostic on stderr |
| Duplicate keys | Save `{"synthetic":true,"synthetic":false}` | Exit 2 before artifact output |
| Nonfinite number | Add an opaque field with NaN or Infinity | Exit 2 before artifact output |
| Deep nesting | Add an opaque array nested 5000 levels | Exit 2 with a nesting diagnostic and no traceback |
| Schema type | Set schema to true, 1, "1" or 1.5 | Exit 2 with required-schema diagnostic |
| Count type | Set result.count to true, "7" or 7.0 | Validation exits 2 with count-claim diagnostic |
| Unicode decoding | Save invalid UTF-8 bytes | Exit 2 with decoding diagnostic |
| Unicode scalar | Add an escaped lone surrogate to an opaque key or value | Exit 2 before output |
| Drawing identity | Change request.drawing_id only | Exit 2 with identity diagnostic |
| Derived claim | Change result.count to 999 | Validation exits 2 with count-claim diagnostic |
| Dependency claim | Change a dependency name or coverage_note only | Exit 2 with decoded-record mismatch diagnostic |
| Input access | Use a missing or unreadable input | Exit 2 with filesystem diagnostic |
| Output alias | Use example.json as both input and --out | Exit 2 before replacement |
| Output destination | Use a directory or missing parent as --out | Exit 2 with filesystem diagnostic and temporary cleanup |
| Closed stream | Inject stdout or stderr write failure | Exit 2, with diagnostics when stderr is usable |

Run either CLI with bad.json and --out evidence.json to reproduce malformed
input refusal. Existing evidence.json bytes survive each refusal. These
commands cover both command entry points.

```sh
python scripts/export_x04_offline_evidence.py bad.json --out evidence.json
python scripts/validate_x04_offline_evidence.py bad.json --out evidence.json
```

UTF-8 with an initial BOM is accepted. Successful JSON output is UTF-8 safe.
Replacing an output symlink to an unrelated sentinel replaces the symlink
entry with a complete artifact and preserves the sentinel's bytes. An output
alias to the input is refused.

The focused tests construct deep nesting, scalar substitutions and byte
sequences without requiring manual file edits.

```sh
python -P -m pytest -p no:xonsh -o pythonpath=server server/tests/test_x04_evidence_nesting_limits.py server/tests/test_x04_evidence_scalar_type_confusion.py server/tests/test_x04_evidence_encoding_boundaries.py server/tests/test_x04_evidence_destination_types.py -q
```

Recover by restoring the original example and choosing a writable output path
that differs from the input. The nesting test follows refusal with a valid
request in the same process and checks exact count 7 and highlights A and B.

```sh
python scripts/export_x04_offline_evidence.py example.json --out recovered.json
python scripts/validate_x04_offline_evidence.py recovered.json
python scripts/bench_x04_evidence_extension_payload.py
```

The benchmark retains 128 parents and one literal dependency while growing
accepted opaque extension objects. It checks trusted count and highlights,
unchanged inputs and retained extensions, then records artifact size, time and
traced peak memory without thresholds. Validation proves consistency with
embedded synthetic inputs. Original-byte integrity remains unverified, and
raw DWG completeness and staging browser acceptance require their own evidence.
