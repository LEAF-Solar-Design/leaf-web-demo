# S03 restitched design export

Parent: Studio18 whole-rooftop grouping/stringing acceptance. This branch combines
the published split/correction stack at c0c1abb4 with the exact four server/test
file deltas from exporter4e66c589. It adds two synthetic compatibility cases in
the existing exporter test file; it introduces no new production behavior.

The cases use265 synthetic panels, mixed full/partial-beam or all-partial-beam
responses, the real client receipt/provenance and native restitch/cut code, then
the canonical project read and artifact download. They verify complete unique
coverage, maximum14, full graph equality, source version/intake/graph/content
hashes, routes, polarity, split metadata and unknown0 power. A later correction
remains stale and is refused for current export.

The source import and storage are in-memory test doubles. No provider, licensed
native host, existing project, original drawing, historical solver response or
scored/LIVE walk is used. This is not original whole-rooftop acceptance and does
not replace the frozen full-drawing requirement. The independent grouping
producer7a07a947 still needs its normal integration and LIVE proof.

Focused replay:

```text
python -P -m pytest -p no:xonsh -o pythonpath=server server/tests/test_s03_design_export.py server/tests/test_s03_manual_split_compat.py server/tests/test_solar_tool_solar_string_reports.py -q
```

Use the normal offhost verification helper for execution. Full native CI,
canonical merge admission and root-owned staging/scored acceptance remain
separate stages.
