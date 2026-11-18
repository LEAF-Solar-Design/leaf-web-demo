# X03 offline replay

Parent: Studio X-bench X03, natural-language authoring on real drawings.

From the repository root, run this network-free synthetic suite:

```powershell
python -P -m pytest --disable-plugin-autoload tools/xbench/tests/test_x03.py -q -p no:cacheprovider -o pythonpath=tools/xbench
```

The suite creates temporary prompt manifests, drawings, reports and baselines. It replaces sign-in, transport and project operations with mocks. It does not need customer drawings, credentials, a browser or a running service. A passing suite proves grader behavior under those mocked contracts. It does not prove staging author quality or LIVE acceptance.

## Minimal create-versus-revision repro

Run only the existing synthetic contract case:

```powershell
python -P -m pytest --disable-plugin-autoload tools/xbench/tests/test_x03.py -q -k new_tool_authoring -p no:cacheprovider -o pythonpath=tools/xbench
```

The mock accepts the new-tool request and returns a generated tool. The test checks that the request body contains only the frozen description and build mode. It rejects any target_tool_name. AuthorLoop interprets that optional field as a request to revise an existing authored tool. A new nonexistent name therefore fails before generation. The unique request key belongs in the Idempotency-Key header. The returned tool name binds publication and execution.

## Receipts and baselines

The lifecycle cases check incremental records.json, summary.json and scores.csv. An authority refusal or timeout keeps earlier results, adds an unreadable record, stops later dispatch and cleans owned project and sign-in contexts. Only a server-issued owned turn can be cancelled. Non-finite nested execution output yields a readable failure receipt without an output hash. These incomplete runs cannot create a baseline. Record IDs must match their frozen trade and drawing before acceptance reports are written.

A synthetic PASS is source verification only. A complete LIVE run must still use the normal create, publication and bound execution paths and match the independent frozen oracles. Preserve raw historical receipts. Protocol-invalid runs that omitted authority setup or supplied a revision target cannot supply an admissible baseline. Archive their baseline bytes with a superseding adjudication. Do not change prompt bytes, oracle bytes, thresholds or regression margins to make those runs valid. The first admissible complete staging run establishes the baseline.
