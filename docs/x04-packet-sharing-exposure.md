# X04 packet sharing and exposure

Studio X-bench X04 uses these offline packets to check embedded synthetic evidence. Successful validation does not redact data or authenticate original drawing bytes. Real customer drawings remain outside Git and messages under the research data root.

The assembler reads an explicit local manifest with schema `leaf.x04.evidence-packet-manifest.v1`. Each artifact entry has `id` and `path`. Each comparison adds `before` and `after`. Each batch adds ordered `artifact_ids`. For example, after creating valid synthetic artifact and report files:

```json
{"schema":"leaf.x04.evidence-packet-manifest.v1","artifacts":[{"id":"a","path":"synthetic-artifact.json"}],"comparisons":[],"batches":[]}
```

```powershell
python scripts/assemble_x04_evidence_packet.py manifest.json --out packet.json
python scripts/validate_x04_evidence_packet.py packet.json --out summary.json
```

Paths resolve from the manifest directory. The assembler reads every named file, validates the complete packet and writes the packet only after all bindings pass. It refuses an output alias to the manifest or any named input. Exit 2 means refusal; an existing output survives. `--max-input-bytes` applies separately to each file. `--max-output-bytes` bounds the complete encoded output. These reads are successive, not a simultaneous source snapshot. The assembler does not follow dependency targets or report path labels.

The following source trace uses synthetic sentinel values only. A value omitted from one summary can remain in the packet being shared.

| Synthetic input | Assembled packet | Packet validation output |
|---|---|---|
| Drawing ID `synthetic-sentinel-drawing` | Embedded artifact remains intact | Retained in artifact validations and comparison/batch reports |
| Entity handle `SENTINEL_HANDLE` and entity geometry | Complete artifact intake retained | Selected parent handles retained; entity geometry omitted |
| Selected set ID containing `SentinelLayer` and `SentinelBlock` | Request and intake names retained | Changed selected IDs retained in comparison deltas; unchanged IDs omitted from deltas |
| Literal dependency `../sentinel-font.shx` | Decoded pairs and observed dependency rows retained | Changed dependency rows retained in comparison deltas; unchanged dependencies omitted |
| Batch row path `C:/synthetic/sentinel-source.json` | Saved report label retained | Label copied into the validated batch row without opening that file |
| Opaque drawing extension `{"sentinel_note":"synthetic-private-note"}` | Retained because assembly embeds the complete parsed artifact | Omitted from the normalized artifact result; this omission does not sanitize the assembled packet |
| Opaque saved-report extension | Retained in embedded report | Normalized output omits unselected extensions; extra fields inside equality-bound claims can instead cause refusal |

Source basis: `scripts/assemble_x04_evidence_packet.py:20-53` embeds each parsed file without filtering it. `scripts/export_x04_offline_evidence.py:136-142` preserves the drawing and observed dependency report when exporting. `scripts/validate_x04_offline_evidence.py:13-28` recomputes evidence and returns drawing ID, count, selected handles and unverified integrity. `scripts/compare_x04_offline_evidence.py:16-38` emits literal membership differences, including complete dependency rows. `scripts/validate_x04_evidence_packet.py:74-110` returns normalized artifact results, copied comparison reports and batch rows with original path labels.

The nested `external_paths_opened=false` describes the packet validator's treatment of embedded labels. Assembly itself opens the manifest and its explicit input files. Neither command promises confidentiality, dependency completeness, original-byte authentication, live Studio behavior or the frozen ten-drawing acceptance. Inspect the packet and summary before sharing either. Validation success is a consistency result, not permission to disclose their contents.
