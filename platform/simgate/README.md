# Simulator gate

`run.py` replays the committed gold set through the working copy of
`platform/replay.py` and `platform/hashing.py`. It exits zero only when every
record matches. A digest divergence or a skipped malformed record exits
nonzero because `ReplayReport.ok` treats both as failures.

Run the gate locally:

```text
python platform/simgate/run.py
python platform/simgate/run.py --self-test
```

The gold set is stable by design. `payloads.json` contains deterministic,
JSON-native inputs only. Do not add timestamps, random values, machine paths,
or environment-derived data. The frozen hash resolution is `1e-4` metres.

Regenerate `gold-set.json` only when the intended hashing contract changes and
the new digests have been reviewed. Mint it through the replay core, then rerun
the gate and inspect the diff:

```text
python platform/simgate/run.py --mint platform/simgate/payloads.json --write-gold platform/simgate/gold-set.json
python platform/simgate/run.py
```

Changing the hash or replay behavior without deliberately reminting this file
must make the gate fail. This is the CI canary for behavioral drift.
