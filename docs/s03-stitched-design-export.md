# S03 restitched design export

Parent: Studio18 whole-rooftop grouping and stringing acceptance.

The existing solar-string-data read tool accepts format=design-json. It exports
one complete validated design with source/project/version identity, original
geometry and units, frame and string memberships, routes, polarity and split
metadata. Unknown module power stays0; electrical verification is not evaluated.
The default string-data report remains available. Stale, incomplete or empty
designs and outdated manual sizing evidence refuse current export.

A bounded membership correction refreshes only derived panel-centre routes,
lengths and polarity. It drops the old solver-response hash from that derived
polarity. Corrected output remains stale until the supported solve/commit path
establishes a current result; legacy manual routes are not silently regenerated.

The registered server/tests/test_solar_tool_solar_string_reports.py contains
report compatibility, manual unknown-power admission, split/restitch/native-cut,
correction and canonical artifact download cases. Its265-panel fixtures cover
mixed full/partial-beam and all-partial-beam responses, maximum14, unique full
coverage and complete graph equality. Inputs and storage use synthetic in-memory
doubles. They do not prove original-DWG, live or scored whole-rooftop acceptance.

Focused source replay from the repository root:

```text
python -P -m pytest -p no:xonsh -o pythonpath=server server/tests/test_solar_tool_solar_string_reports.py -q
```

Use the supported verification route. Selected native CI and exact-head critic
review precede canonical merge admission. Full CI is reserved for production.
Canonical staging delivery and a disposable unscored readiness pass precede the
root's scored acceptance walk on the original full drawing.

The two intermittent Project-click fixture journeys retain bounded failure-only
text diagnostics in /tmp/gate-results for normal selected CI logs. They record
click milestones, URL pathnames, console types and request timing. Headers,
queries, response bodies and console message text are excluded. These diagnostics
change no test assertion or timeout and establish no live acceptance result.
