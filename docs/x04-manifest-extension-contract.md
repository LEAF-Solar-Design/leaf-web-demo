# X04 manifest extensions and packet propagation

Studio X-bench X04's offline assembler uses explicit local files and packet-local IDs. Manifest extensions do not increase the integrity or coverage of the evidence. The assembled packet still contains complete parsed source artifacts and reports, so ignoring a manifest field is not a data-redaction service.

The recognized top-level fields are `schema`, `artifacts`, `comparisons` and `batches`. All three sections are required arrays. Each entry requires a globally unique ASCII `id` and a nonempty string `path`. Comparison entries also supply ordered `before` and `after` references. Batch entries supply ordered `artifact_ids`; repeated occurrences are retained. References name embedded artifact IDs, never report IDs or external paths.

Unrecognized top-level and entry fields are ignored when building the packet. Every manifest container, including ignored values, is subject to the depth check first. A manifest container at depth 256 refuses before explicit artifact/report reads. A shallow unknown field can therefore have no effect on the packet, while an excessively nested unknown field refuses.

Minimal synthetic extension example, assuming `synthetic-artifact.json` is an already valid synthetic artifact:

```json
{"schema":"leaf.x04.evidence-packet-manifest.v1","artifacts":[{"id":"a","path":"synthetic-artifact.json","comment":"ignored entry note"}],"comparisons":[],"batches":[],"comment":{"note":"ignored top-level note"}}
```

Removing both `comment` fields gives the same assembled packet. Adding 256 nested arrays under top-level `comment` refuses rather than propagating the value. These examples do not create evidence of complete raw dependency coverage or authenticated drawing bytes.

| Manifest field | Assembled packet field |
|---|---|
| Manifest `schema` | Replaced by `leaf.x04.evidence-packet.v1` |
| Entry `id` | Copied to the corresponding packet entry |
| Entry `path` | Used to read the explicit local file, then omitted from that entry |
| Comparison `before`, `after` | Copied unchanged and checked against embedded artifacts |
| Batch `artifact_ids` | Copied with exact occurrence order, then checked against report rows |
| Source artifact/report JSON | Embedded as parsed, without filtering its extensions |
| Other manifest/entry fields | Omitted from packet entries after the full-manifest depth check |

Source basis: `scripts/assemble_x04_evidence_packet.py:52-60` checks schema and full-manifest depth. Lines 61-83 validate sections, IDs and paths, then construct only recognized entry fields. Lines 84-89 read each explicit source and validate the packet before returning it. `scripts/validate_x04_evidence_packet.py:50-74` checks globally unique IDs and artifact-only references before trusted validation; lines 87-104 preserve batch occurrence order. `scripts/assemble_x04_evidence_packet.py:103-108` checks the manifest/output alias, selects a call-local reader and writes only after assembly succeeds.

Relative paths resolve from the manifest directory. Absolute local paths are accepted. Path labels within saved reports are independent inert data, and may remain in the packet or normalized summary. Every explicit read counts under an optional aggregate byte ceiling, including repeated paths. Reads remain successive, without a simultaneous snapshot guarantee. Atomic replacement does not promise fsync durability. Integrity remains `unverified`; no live drawing, browser, original-byte or raw-completeness claim follows from this contract.
