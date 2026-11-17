# Expected report kind within X04's offline evidence chain

Studio X-bench X04 uses an optional expected kind to route saved comparison or batch reports. It is distinct from exact-byte matching, internal report consistency and embedded packet binding. None of these offline checks alone proves a frozen live real-drawing acceptance step.

## Enforcement order

| Boundary | Current source | What is checked |
| --- | --- | --- |
| Arguments | `scripts/validate_x04_review_report.py:125-135` | Exact flag spelling, comparison/batch choices and optional 64-hex digest parse before I/O. `allow_abbrev=False` refuses shortened options. |
| Output aliases | `scripts/validate_x04_review_report.py:137` | Existing source/output alias checks occur before input reads. |
| Bounded actual input | `scripts/validate_x04_review_report.py:138-139`, `scripts/export_x04_offline_evidence.py:69-81` | The file or binary stdin buffer is read under the byte ceiling. File stat is a precheck, not the data used for kind or digest decisions. |
| Digest then decode | `scripts/export_x04_offline_evidence.py:44-66` | A supplied expected digest compares the exact buffer before strict JSON decoding, including BOM and whitespace. The reader does not reopen a source to hash it. |
| Expected kind | `scripts/validate_x04_review_report.py:140-143` | A supplied CLI kind maps to the recognized comparison or batch schema and compares the decoded top-level schema. Wrong kind stops before report semantics. |
| Report consistency | `scripts/validate_x04_review_report.py:144` and `:63-123` | The unchanged validator checks arithmetic, memberships, batch totals/indices and unverified integrity. The returned scope explicitly excludes underlying artifact revalidation and embedded path opens. |
| Publication | `scripts/validate_x04_review_report.py:145-147` | Existing bounded encoding and atomic replacement run only after accepted kind and internal consistency. Handled refusal emits a diagnostic and preserves prior output. |
| Packet binding | `scripts/validate_x04_evidence_packet.py:73-105` | Embedded artifact results determine expected comparison and ordered batch claims. Packet-local references do not open outside targets. This is independent of a standalone CLI routing expectation. |

## Counterexamples and retained defaults

| Example | Actual scope |
| --- | --- |
| Omit `--expected-report-schema` | Either recognized report kind still reaches the original consistency validator. There is no routing-kind expectation. |
| Put `expected_report_schema`, `expected-report-schema` or `expected_kind` in report JSON | Such extension keys do not set the CLI option. The recognized top-level `schema` still determines the actual report kind. |
| Put a similar key on a manifest row or packet report row | The standalone CLI expectation is not inherited. Assembly source-row pins and packet reference bindings retain their separate contracts. |
| Rename a comparison's schema field to the batch schema and request batch | A field match alone does not pass. Required batch rows and totals still fail consistency. |
| Supply correct comparison kind and digest with `before=6`, `after=1`, `delta=0` | Kind and bytes match; arithmetic still refuses. |
| Supply correct batch kind and digest with three rows but `total=4` | Kind and bytes match; batch totals still refuse. |
| Supply the wrong expected kind with a correct byte digest | Reading, hashing and decoding succeed, then routing refuses before report semantics or output replacement. |
| Replace a file between stat and open with a batch report | Kind and optional digest both inspect the single actual decoded batch buffer. A retained comparison digest refuses before kind; a refreshed digest plus expected comparison refuses at kind. |
| Supply an internally consistent invented report of the expected kind | Standalone validation can pass. No underlying artifact or producer truth is established. |
| Supply a standalone-valid report that contradicts embedded packet operands | Standalone consistency can pass while packet binding refuses. The expected kind cannot waive packet checks. |
| Replace report, expected digest and expected kind together | These checks may pass. Expected values require independent custody; they are not signatures or producer authentication. |

The pure `validate_report` API accepts decoded reports and has no expected-kind parameter. The optional CLI guard is call-local. The handoff fixture explicitly runs standalone kind guards before assembly and packet validation and stops its test-owned sequence on refusal. It does not establish an automatic production workflow that performs those steps.

## Evidence and limits

Focused controls cover valid/wrong/omitted kinds, matching digest semantic refusal, parsing before I/O, refusal order and call isolation. Follow-up controls cover reordered keys, escaped Unicode, BOM/CRLF, optimized Python and hash seeds under network-denied file/stdin subprocesses, replaced short-read buffers and an explicit pinned report-to-packet sequence. The byte-volume benchmark holds comparison memberships and batch rows fixed while growing ignored padding, records reads and semantic calls, and uses no timing threshold.

All fixtures are synthetic and offline. Original-byte integrity stays `unverified`; source completeness, raw dependency inventory, intended design-set binding, browser geometry/selection and live exact module counts remain unproved here. Atomic replacement does not add fsync durability or hostile alias-race protection. Successive reads do not promise a simultaneous snapshot. A trusted expected kind is routing intent, not authenticity.
