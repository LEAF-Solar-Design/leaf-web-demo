# X05 offline reproduction

Parent: Studio X-bench X05 engineering-set reconciliation. These checks cover
the partial grader and independent text oracle, not staging acceptance.

From the repository root, with Python and pytest installed:

```powershell
python -P -m pytest tools/xbench/tests/test_x05.py -q -p no:cacheprovider -p no:xonsh -o pythonpath=tools/xbench
python -P -m pytest tools/xbench/tests/test_x05_oracle.py -q -p no:cacheprovider -p no:xonsh -o pythonpath=tools/xbench
```

The `-P` flag avoids the repository's `platform/` package shadowing the Python
standard library. Disabling the unrelated xonsh plugin avoids its console
requirement. Both suites can run in one process by listing both test paths.
No browser, network, provider credentials, real drawing or installed PDF reader
is required. PDF tests replace pdfplumber with in-memory page objects.

The synthetic report has 24 modules, 12 optimizers, one inverter, two strings
and 9600 W. Its two string rows contain six optimizers each. Tests construct
contradictions by appending a different explicit total, duplicate a string ID,
or substitute invalid numbers. Repeated summaries are generated in memory;
numeric-operation counts, rather than elapsed time, check linear bookkeeping.
Provenance remains limited to five snippets of at most 200 characters per field.

The mocked Studio transport uses a four-panel, two-frame design. Complete
unique associations pass steps 1 and 2. Missing or duplicate associations fail
step 2. Unsupported wire shapes, malformed fixture metadata, source drift,
authentication and unknown server codes remain UNREADABLE. Steps 3 through 7
always remain UNREADABLE in this slice, so it cannot issue a full X05 PASS.
Order permutations must preserve these verdicts.

For a no-network malformed-report and injection reproduction:

```powershell
python -P -m pytest tools/xbench/tests/test_x05.py -q -p no:cacheprovider -p no:xonsh -o pythonpath=tools/xbench -k 'unknown_report_format or server_codes_are_allowlisted or malformed_fixture_metadata'
```

Injection cases use synthetic canaries only. They write `x05-evidence.json` in
pytest's temporary directory and check that no injected text remains. Unknown
codes map to a fixed allowlisted diagnostic, never a product FAIL. Known refusal
codes keep the existing guarded behavior. Blank middle pages, later extraction
exceptions and empty documents produce named unreadable oracle outcomes.

These fixtures do not prove real PDF-layout coverage, Ground setup, accepted
saved strings, report-aware recommendations, reconciliation CSV/UI or reopen
parity. Real engineering files and their evidence stay outside the repository.
