# p1: LEAFPLATFORM (leaf-platform-webview)

`p0-intake.json` is written by the Branch2025 adapter `tools/parity/w4_evidence.py` from the 2026-09-24 VMC capture. It
holds the two facts the palette reads before it navigates: whether `LEAF_PLATFORM_ORIGIN` overrode the platform origin
(it did not: the shipped configuration) and whether the drawing already carried a platform binding (it did not).

The plugin's palette showed a page it could not reach and committed nothing. Studio commits nothing when it is opened.
The two `report` rows only the plugin emits are the declared divergence
`docs/parity/divergences/leaf-platform-webview/palette-partner-unreachable.md`; the receipt is
`docs/parity/receipts/leaf-platform-webview/w4-p1.json`.
