# X04 packet binding counterexamples

Parent: Studio X-bench X04 offline evidence. This document extracts small mutations from existing source tests at base `56fccc9e7d002bea0a9dc4dc3dea53a17560ca2a`. Verdicts below are manual expectations from source inspection. No example, suite or benchmark ran for this document.

Start with `packet()` in `server/tests/test_x04_packet_cross_binding.py:11-19`. It embeds artifact `a` with count 6 and artifact `b` with count 1. Its comparison is a -> b, counts 6/1/delta -5. Batch IDs are `[a,b,a]` with row counts 6/1/6. The synthetic artifact variants are defined at `server/tests/test_x04_comparison_delta_algebra.py:10-20`; variant 2 counts 7.

| Minimal mutation of that control | Exact contradiction and manual expected boundary | Corrected control |
|---|---|---|
| Set comparison `before=b`, `after=a`; keep its saved report. | Saved before/after/delta are 6/1/-5; ordered recomputation is 1/6/+5; selected-ID, highlight and dependency delta directions also reverse. Refuse at `scripts/validate_x04_evidence_packet.py:81-82`. Mutation: `server/tests/test_x04_packet_cross_binding.py:34`. | Restore references a,b, or regenerate the comparison for b,a. |
| Set batch `artifact_ids=[b,a,a]`; keep row order. | Claimed counts 6/1/6 disagree with expected 1/6/6; the first two highlight sets also exchange A and B. Row 0 refuses at packet lines 94-98. Mutation: cross-binding test line 35. | Restore `[a,b,a]`, or rebuild all occurrence rows against `[b,a,a]`. Changing a display path is not a correction. |
| Replace the saved comparison with `compare_evidence(a,a)`; keep references a,b. | The report is internally valid at 6/6/0 with empty memberships; the expected 6/1/-5 report changes selected IDs, highlights and dependencies. Refuse at packet lines 81-82. Mutation: cross-binding test line 36. | Regenerate `compare_evidence(a,b)`. |
| Replace embedded a with valid variant 2; keep saved reports. | New a counts 7, so the comparison should be 7/1/-6 and batch counts 7/1/7. The added selected-ID/highlight memberships and removed dependency identity also change; batch rows 0/2 gain B. Comparison refuses first. Mutation: cross-binding test line 37. | Restore the original a, or regenerate both reports from the new embedded artifacts. |
| Set comparison `before=missing`. | Label syntax is valid, but no artifact owns that ID. Refuse at packet line 64 before trusted validation. Mutation: `server/tests/test_x04_packet_reference_failures.py:11`. | Reference existing artifact a. |
| Set both comparison operands to its own report ID c. | Report IDs cannot be artifact references. This is rejected membership, not recursive graph traversal. Refuse at packet line 64. Mutation: reference-failure test line 12. | Use embedded artifact IDs a,b. |
| Set comparison entry ID to a. | Global ID uniqueness fails before reference checks at packet line 57. Mutation: reference-failure test line 10. | Use a distinct report ID c. |
| Set an artifact ID to a URL-like string. | Local label syntax fails at packet lines 22-24 before any resolution. Mutation: reference-failure test line 14. | Use a local ASCII label and update any matching references. |

The cross-binding test first asserts that each report and artifact is individually valid, then requires packet refusal and unchanged input (`server/tests/test_x04_packet_cross_binding.py:41-48`). This separates cross-binding from an arithmetic-only failure. Reference tests deny file opens and trusted validation during the bad call, then recover with the original packet (`server/tests/test_x04_packet_reference_failures.py:24-33`).

CLI refusal maps controlled validation errors to exit 2 (`scripts/validate_x04_evidence_packet.py:121-122`). The source-deletion replay provides a separate substitution sentinel and valid retry (`server/tests/test_x04_packet_replay_chain.py:29-42`). Its initial counts differ from the 6/1 fixture because it starts with both synthetic sets selected. Do not mix those fixture counts.

A consistent artifact edit followed by matching regenerated reports can pass. That is the intended consistency boundary, not byte authentication or proof of unchanged drawing content. These manual cases establish no real-drawing, browser or staging verdict.
