# guardrails-monitoring: clean-host capture still skips drawing data

Solar parity residuals R31 and R07, SPEC-MARKER: R31B-GUARDRAILS-V2.

**Plugin reference:** Branch2025 master test build `09b5b741`, after #311.
Source paths below are relative to `C:/tmp/solar-parity/wt-b25-main`.
**Evidence:** `C:/tmp/solar-parity/b2-hs-out/g1.json` and `g2.json`, recaptured
2026-09-24 after restoring g0 host settings. Both report HEALTHY, 2 info, 13 pass.
The task packet records that the rebuilt i19 intake is byte-identical to
`docs/parity/evidence/batch2/g0-intake.json`. Card detail messages and a runtime
DesignSnapshot dump were not captured. Branch deductions below are conditional
on the recorded host settings and catalog being the values read by that build.

## Why

The old `l2-inverters-invisible.md` explains the pre-#311 collector only. It is
not the source diagnosis for this capture: `Guardrails/UI/DesignSnapshotCollector.cs:183`
now passes both lists, :192-200 enumerates both, :213 sets InverterCount, and :272
adds a summary even when `mStrings` is empty. Strings are read solely from
`inv.mStrings` (:238-268), not rediscovered from drawing entities. The collector
can also return a partial snapshot after an exception (:170-174, :275-278).

The committed intake has 23 L2 devices, 173 assigned strings, 2,345 panels and
five string-assigned inverter numbers (1, 2, 5, 6, 7). It has no drawing type
overrides. The host supplies NumMppt=12 and StringsPerMppt=2; the catalog supplies
12 trackers, 24 inputs and 250 kW AC. These are present data, not missing fields.

* **Strings per MPPT:** `LeafSolarDesign.Core/Guardrails/Rules/Design/StringsPerMpptRule.cs:28-33`
  emits INFO for empty InverterSummaries. The alternative INFO at :91-99 requires
  every summary to lack capacity. It cannot apply with recorded host capacity 2:
  :48-51 falls back to that value even if the type config is absent. Thus the
  captured INFO implies empty summaries under these inputs, not absent capacity.
* **DC/AC:** `LeafSolarDesign.Core/Guardrails/Rules/Design/DcAcRatioRule.cs:43-55`
  emits INFO when total string panels are zero and the estimate lacks a positive
  count, capacity or tracker count. With recorded capacity 2 and trackers 12, the
  missing field is InverterCount (zero). The alternative INFO at :83-87 requires
  zero AC power; the recorded 250 kW type excludes it, including when summaries
  are empty, since :76-80 uses SuggestedCount=5. The collector reads host capacity
  at `Guardrails/UI/DesignSnapshotCollector.cs:41-43` and counts both lists at :51.
* **MPPT balance:** `LeafSolarDesign.Core/Guardrails/Rules/Design/MpptBalanceRule.cs:30-33`
  returns PASS for empty summaries, consistent with the capacity branch above.
  Populated summaries with empty StringsByMppt would instead emit INFO at
  :73-76 and :166-177. Merely failing to load mStrings does not explain this capture.

This localizes the residual to unavailable runtime inverter summaries/count (or
a mismatch between recorded and consumed inputs). It does not prove why the
new collector received that state. Neither capture includes the two runtime
lists, CollectionIncomplete, or the actual snapshot. Do not claim that #311
still ignores L2, or invent missing capacity to reproduce the palette.

## Studio's behaviour and declared comparator diffs

Keep drawing mode's rules unchanged. The drawing assignments support all three
checks: DC is 1,395.275 kW against five assigned 250 kW inverters, ratio 1.11622,
so DC/AC passes. All 29 assigned MPPT buckets exceed capacity 2 (28 have six
strings, one has five). Inverter 7 has panel totals 77, 78, 84, 83, 64, giving
23.81% imbalance and one warning. Studio does not infer assignments for the
other 18 devices: the intake omits their inverter numbers and string links.

Studio gives ERRORS DETECTED, 29 error, 1 warning, 13 pass (43 verdicts).
Plugin gives HEALTHY, 2 info, 13 pass (15 verdicts). No electrical, hardware
or code-safety rule severity changes are declared.

Exact row differences, plugin to Studio, for both g1 and g2:

* `report-status.value`: HEALTHY to ERRORS DETECTED.
* Remove `report-info-count` (2); add `report-error-count` (29) and
  `report-warning-count` (1). `report-pass-count` remains 13.
* `verdict-10`: DC/AC ratio / DESIGN-DC-AC / info becomes
  Strings per MPPT capacity / DESIGN-STRINGS-MPPT / error.
* `verdict-11`: Strings per MPPT capacity remains the rule; info becomes error.
* `verdict-12`: MPPT balance / DESIGN-MPPT-BAL / pass becomes
  Strings per MPPT capacity / DESIGN-STRINGS-MPPT / error.
* `verdict-13..15`: the three code-safety pass rows become design-section
  Strings per MPPT capacity / DESIGN-STRINGS-MPPT / error rows.
* Add `verdict-16..38`, design-section Strings per MPPT capacity errors;
  `verdict-39`, design-section MPPT balance warning; `verdict-40`, design-section
  DC/AC ratio pass; `verdict-41..43`, the three code-safety pass rows in their
  original order (SAFE-NULL-STATE, SAFE-DIV-ZERO, SAFE-DB-VALUES).
* Entity mapping removes report-info-count and adds the two new report ids
  and verdict-16..43, each mapped to itself. Verdict-1..9 are unchanged.

These declarations cover row content and membership, not producer hashes,
timings, revisions or unrelated provenance differences. Historical `plugin`
list mode is unchanged and still reproduces the empty-L1-list contract.

## Retiring it

Capture the actual snapshot and both runtime lists on the restored host with
the loaded plugin identity. Retire when the collector demonstrably receives the
drawing's assignments and these checks evaluate them, or replace this finding
with the exact consumed-input mismatch. Empty string buckets alone are not a
sufficient explanation. No existing test expectations were changed; two cases
pin the committed intake verdicts and distinguish empty buckets from empty lists.
