# Saved-report pin coverage within X04 evidence review

Studio X-bench X04's offline report pin is an optional byte comparison before the saved-report consistency check. It is one boundary in the offline evidence chain. It does not establish any frozen real-drawing acceptance step on its own.

## Enforcement and what each layer proves

Paths and line numbers refer to this checkout's source.

| Layer | Enforcement | Proven scope |
| --- | --- | --- |
| Report CLI expected value | `scripts/validate_x04_review_report.py:131-133` and shared `scripts/export_x04_offline_evidence.py:38-41` | An explicitly supplied CLI value has exactly 64 hexadecimal characters. Malformed values stop before I/O. |
| Actual report buffer | `scripts/validate_x04_review_report.py:135-138`, shared `scripts/export_x04_offline_evidence.py:69-81` and `:44-48` | A supplied expected digest matches the bounded file or caller-owned stdin bytes that are decoded, including BOM and whitespace. No separate digest reopen. Input ceilings precede the digest comparison. |
| Report consistency | `scripts/validate_x04_review_report.py:63-120` | The recognized saved-report schema has consistent count arithmetic, disjoint delta memberships, batch indices and totals, and unverified integrity. This does not revalidate underlying artifacts. |
| Summary scope | `scripts/validate_x04_review_report.py:120-123` | Summary explicitly limits its claim to saved-report internal consistency, with no underlying artifact revalidation or embedded path opens. |
| Explicit assembly sources | `scripts/assemble_x04_evidence_packet.py:75-90` | `--require-source-pins` covers each explicit row. Each direct `expected_sha256` is checked against that occurrence's source buffer. The separately executed report CLI pin is not inherited. |
| Manifest and packet | `scripts/assemble_x04_evidence_packet.py:104-117`, `scripts/validate_x04_evidence_packet.py:118-125` | Optional expected values bind each respective raw buffer. A row-pin edit changes manifest bytes; decoded report whitespace does not survive into the emitted packet. |
| Packet bindings | `scripts/validate_x04_evidence_packet.py:73-105` | Embedded artifacts are validated once per call. Comparison operands and ordered batch local IDs are checked against report claims, independently of standalone report byte pins. |

## Coverage counterexamples

| Input or change | Actual behavior and limit |
| --- | --- |
| Omit `--expected-report-sha256` | Default report validation still runs. The old two-argument reader call remains. It makes no expected-byte claim. |
| Put `expected_sha256` or `expected_report_sha256` inside report JSON | These unknown fields are not the CLI option and do not trigger hashing. A hash-looking extension is not a pin. |
| Put a CLI-looking key on a manifest or packet report row | Assembly consumes only its own direct source-row `expected_sha256`; packet validation has no raw original report file buffer. No report CLI pin is implied. |
| Append report whitespace, retain expected report digest | Report CLI refuses before report checks and preserves prior output. The decoded report claims remain equivalent. |
| Refresh standalone report digest only | Standalone consistency may pass. Strict assembly still refuses the stale source-row digest. |
| Refresh source-row digest but retain expected manifest digest | Manifest byte check refuses the changed manifest. Explicit manifest-digest refresh is a separate decision. |
| Refresh report, row and manifest digests after whitespace only | Assembly emits the same decoded packet, so a retained expected packet digest still matches. The handoff test pins this boundary. |
| Supply a matching digest for contradictory arithmetic or totals | Byte comparison passes; report consistency refuses. Matching bytes do not waive semantic checks. |
| Replace report and its expected digest together | Byte comparison can pass. An internally consistent fabricated report also passes standalone validation. Neither authenticates its producer or underlying artifacts. |
| Supply a valid standalone report that contradicts packet operands | Standalone consistency can pass; packet binding still refuses. Packet-local IDs identify embedded operands, not external files. |
| Replace an internally valid packet and every separately expected value together | Byte and consistency checks may pass. No signature or independent producer trust is established. |

The pure `validate_report` function accepts decoded claims, not a raw buffer or expected digest. Callers that need a report byte check must use the CLI or the shared reader with an explicit expected value. Path labels in batch reports remain inert strings.

## Existing controls and remaining limits

The report-pin suites cover parser refusal, file/stdin byte matching, stat/open substitutions, bounded short reads, matching-hash semantic refusal and concurrent call isolation. The follow-up suites cover original cancellation identity, owned stream closure, caller stdin retention, zero report checks after hash/decode interruption, byte-before-pin-before-decode precedence, own versus cross-encoding hashes and composed report-to-packet recovery. The padding benchmark holds report memberships and batch rows fixed and records bytes, calls, time and memory without timing thresholds.

These are synthetic offline controls. Original drawing bytes, raw dependency completeness, intended design-set binding, browser geometry, actual UI selection and live exact module counts remain outside this proof. `original_bytes_integrity` stays `unverified`. Expected values need their own trusted custody. Atomic replacement is not fsync durability, source reads are successive rather than a simultaneous snapshot, and no hostile alias-race guarantee is added.
