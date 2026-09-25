# guardrails-monitoring: the guardrails never see L2 inverters

Historical finding, before Branch2025 #311. The collector now reads both lists.
For the post-fix clean-host capture and remaining runtime-data discrepancy, see
[clean-host-empty-snapshot.md](clean-host-empty-snapshot.md). The diagnosis below
is retained as the basis of Studio's historical `plugin` list mode.

**Plugin:** Branch2025 `Guardrails/UI/DesignSnapshotCollector.cs` 166-189 (the snapshot reads only the L1 inverter
list) with `DocumentEventHandler.cs` 233-313 (`GetAllInverters` rebuilds the lists when a drawing is activated, and
`AddInverter` files every L2 device in the separate L2 collector list). Filed on Branch2025 issue #281.
**Proven:** licensed AutoCAD 2025 on the VMC, test build 1.1.4.7, 2026-09-24, steps g1 (GUARDRAILS) and g2
(GUARDRAILSVALIDATE) on the inverter-chain drawing, each saved and reopened unchanged.

## Why

The drawing's 23 inverter devices are all L2 devices, and 173 strings are assigned to five of them. Because the
guardrail snapshot only reads the L1 list, every rule runs against a design with no inverters. The palette reports
HEALTHY, "2 info | 13 pass": DC/AC ratio and strings-per-MPPT capacity say their data is unavailable, and MPPT
balance passes on nothing. The design itself puts up to six strings on MPPTs that have two DC inputs, so a guardrail
that validates it has to report errors.

## Studio's behaviour, and the declared diffs

Studio ports the 14 rules, their display order and the health banner literally (`server/solar_guardrails.py`). Its
`plugin` list mode feeds them the plugin's empty L1 list and reproduces the captured verdicts row for row, which pins
the port. The evidence uses the `drawing` mode, which validates every inverter the strings name, L1 or L2: 29
strings-per-MPPT capacity errors, one MPPT balance warning, DC/AC ratio pass, status ERRORS DETECTED. Every other
verdict is the plugin's. The declared diffs are exactly the comparator's for these verdict and report rows and their
entity mapping.

## Retiring it

The divergence goes away when the plugin's snapshot collector also summarises the L2 inverters that hold strings.

## Retired

Retired 2026-09-25 by Branch2025 #323 (the inverters of a drawing open at plugin load are scanned), #330 and #331 (the cable and tag dictionaries are built and the strings linked at load and after the activation rescan), #333 (the g0 intake carries each device's Number) and this change (Studio's snapshot builds one summary per device and links strings by number, as the plugin's collector does). Recaptured on test build 5 (master 1d4f900f plus #327 and #330), the plugin summarises all 23 L2 inverters and both batch-2 guardrails receipts (g1, g2) compare with no diffs. On this fixture no device's number matches the numbers the string circuits name (step i18's fixed-L2 adoption renumbered the colliding combiner-box and inverter numbers, Solar residuals R33), so both sides link no string and take the DC/AC no-strings fallback. The signed release carrying the plugin fixes is pending the operator (R23).
