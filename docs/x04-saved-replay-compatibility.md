# X04 saved intake replay compatibility

Parent: Studio X-bench X04 raw drawing intake. Saved replay checks the normal trusted count-modules tool using stored data. It does not reopen the original drawing or certify raw DWG dependency completeness.

The input must be a JSON object. `inserts` and `polylines` are optional; if present, each is an array of objects. Missing collections mean empty collections. Explicit null, strings or objects as collections refuse with re-import guidance. Polyline rows require a points array `pts`. Known identity fields `layer`, `name` and `handle` must be strings when supplied. Missing or empty layer defaults to `0`; missing block name and handle remain unnamed. Unidentified rows stay physically distinct.

INSERT multiplicities default to one. Explicit rows/columns must be integer1..65535; conflicting metadata for a shared normalized handle refuses the whole result. Counts above2**53-1 refuse before JSON result metadata. Polygon eligibility uses supplied straight, finite, strictly convex planar geometry and its bulge/thickness/normal metadata. Unknown geometry cannot be reconstructed from omitted fields.

Unknown JSON metadata fields at top level and candidate level are retained untouched and ignored by counting. They may contain any strict JSON value. This is an additive-field policy, not a promise that future changes to known fields are compatible. There is no saved-intake version negotiation or implicit schema migration in this tool. Known fields keep their current type and meaning even when an extension exists.

Strict JSON encoding rejects nonfinite values before saving. Truncated JSON fails at decoding. Incompatible known fields return actionable re-import guidance through the trusted loader, without partial counts or overlays. Recovery is to re-import or restore a compatible saved snapshot, list candidates again and make a fresh explicit selection. Do not derive raw dependency truth from a successful count.

Set IDs are opaque JSON-tuple strings. They must be selected exactly from the current listing. Typed invalid selectors or absent IDs refuse. The UI uses collision-free UTF-16-unit DOM encoding for arbitrary opaque strings, including lone surrogates, without replacing or normalizing their identity. Summaries require current candidate IDs, labels and per-set counts; physical union totals must lie between the largest selected count and their sum. Overlap permits a total below the sum.

Dependency reports remain literal decoded-record inventories. NFC/NFD, case and slash variants stay distinct. Saved dispositions survive JSON roundtrip, but an independent raw inventory must establish whether the converter retained all original references. Preserving source bytes or a digest does not prove decoding or browser rendering fidelity.

Run focused offline commands sequentially from the repository root:

```powershell
python -P -m pytest -p no:xonsh -o pythonpath=server server/tests/test_x04_replay_extension_fields.py server/tests/test_x04_replay_schema_failures.py server/tests/test_x04_saved_intake_replay.py -q
npm --prefix web run test:unit -- src/components/ModuleSetPicker.test.jsx
python scripts/bench_x04_saved_replay.py --out D:/agents/production-lanes-2026-11-10/XBENCH/status/X04-saved-replay.json
```

The benchmark measures strict JSON encoding, decoding, trusted listing and selection separately at100/1000/10000 synthetic parent records. Exact dispositions, weighted counts, highlight membership and unchanged stored inputs are assertions. Time and traced allocations have no thresholds. It uses no customer files or external service. These results remain offline proof; final X04 acceptance needs a source-bound staging grade and actual browser evidence.
