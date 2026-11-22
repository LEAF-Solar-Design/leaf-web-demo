# X04 packet validation cost

Parent: Studio X-bench X04 offline packet checking. These are source-derived estimates plus existing local measurement files. No benchmark or suite ran for this document. Source lines refer to local base `56fccc9e7d002bea0a9dc4dc3dea53a17560ca2a`.

Let `A` be embedded artifact entries, `C` comparison entries and `R` total batch occurrences. Let `S` be parsed packet size, `V` the total cached artifact-result size, `D` the total derived comparison-result size and `B` the size of all copied batch rows. These symbols describe data size, not physical module counts.

For a successful packet, trusted artifact-validator calls equal **A + 2C**. The packet validates every artifact at line 74 of `scripts/validate_x04_evidence_packet.py`. Each comparison invokes `compare_evidence` at line 80; that function validates both operands at `scripts/compare_x04_offline_evidence.py:26`. Repeating the same operand does not remove those calls. Batch occurrences use `checked[identity]` at packet line 93 and add no artifact-validator calls. Each trusted validation calls the exporter, which reads the registry and runs the selected count tool (`scripts/validate_x04_offline_evidence.py:13`; `scripts/export_x04_offline_evidence.py:121-131`). This call count is not a count of filesystem opens or a runtime prediction.

Early rejection changes the formula. Invalid references refuse before line 74, so trusted calls are zero. A bad artifact stops during the up-front pass. A malformed saved report can refuse after all `A` calls but before its comparison operands are revalidated. Do not use A + 2C as a universal failure-path count.

## Existing observations

The measurement files below live outside git under `D:/agents/production-lanes-2026-11-10/XBENCH/status/`. They are earlier leaf-svc observations, not fresh measurements of this documentation change.

| Recorded cohort | Calls and encoded bytes | Time and measured peak |
|---|---|---|
| `X04-packet-validation.json`: A=50, C=1, R=50, repeated/distributed | Both 52 calls and 49,893 bytes | 0.103789 / 0.103012 s; both 99,980 bytes peak |
| Same file: A=50, C=1, R=200, repeated/distributed | Both 52 calls and 88,245 bytes | 0.109487 / 0.112535 s; both 176,684 bytes peak |
| `X04-packet-unused-artifacts.json`: A=101, C=1, R=1 | 103 calls | 0.308160 s; 149,110 bytes peak |

Instrumentation replaces both validator aliases during the measured call (`scripts/bench_x04_packet_validation.py:41-57`; `scripts/bench_x04_packet_unused_artifacts.py:33-48`). Fixture construction, the input-preservation copy, serialization used to count input bytes and later recovery checks are outside the timed interval. The reported peaks use Python `tracemalloc`, not process RSS or host-wide memory. Repeated/distributed controls contain identical artifact content, so they do not measure heterogeneous drawing complexity. Their equal-byte assertion is at `scripts/bench_x04_packet_validation.py:75`.

## Retained data and work

The packet keeps the caller's full parsed input plus its ID maps and `checked` results. It retains all derived comparisons, then deep-copies every batch occurrence result (`scripts/validate_x04_evidence_packet.py:61,74,83,99-105`). A useful retained-data estimate is O(S + A + C + R + V + D + B). Repeated batch references save recomputation, but their output rows still consume space proportional to occurrence count and each result's highlights. Comparison membership normalization also builds sorted JSON strings (`scripts/validate_x04_evidence_packet.py:28-34`).

Artifact validation temporarily serializes its input, copies drawing/dependency data and builds a count result (`scripts/export_x04_offline_evidence.py:90,136-141`). Packet and saved-report checks also serialize their inputs. These overlapping transient allocations are not a fixed memory multiplier. CLI output serialization retains chunks and a joined output string (`scripts/export_x04_offline_evidence.py:154-163`), adding O(encoded output size). The byte ceilings are not exact RAM limits.

## Bounded optimization proposal at the inspected base

At the base named above this was a proposal, not an implementation. A later worktree change may implement it; the historical call counts and observations above still describe that base.

Extract a pure comparison builder that accepts two already validated results plus their embedded selection/dependency data. Keep the existing public comparison function validating its operands. Let the packet call the builder with its call-local `checked` map. Successful packet calls would then fall from A + 2C to A, while every artifact, including unused ones, still validates once. Repeated batch behavior need not change.

The proposal needs separate source work and evidence for identical claims, mismatch refusal, local ID isolation and unchanged inputs. It must not introduce a process-wide cache keyed only by local IDs. It does not reduce full-packet scans, saved-report checks, deep-copied batch rows or output size. No speedup, memory reduction or acceptance result is claimed here.
