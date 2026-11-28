# X12 offline evidence replay

X12 checks whether a saved Studio drawing opens in desktop AutoCAD and preserves the source drawing's CAD standards. These comparison functions accept offline evidence. Each returns a list of parity differences and raises `ValueError` with the field location when the supplied rows are invalid.

From the repository root with Python 3.11 or newer and pytest installed, run:

```text
python -P -m pytest --disable-plugin-autoload -q -o pythonpath=. tools/xbench/tests/test_x12_layers.py tools/xbench/tests/test_x12_text_styles.py tools/xbench/tests/test_x12_title_blocks.py tools/xbench/tests/test_x12_plot_styles.py tools/xbench/tests/test_x12_entity_counts.py
```

The malformed-evidence test reads `tools/xbench/fixtures/x12/malformed-evidence.json`. It expects the missing plot flag, duplicate layer identity and negative entity count to fail with field diagnostics. The truncated example is stored as a string inside valid JSON. The decoder must refuse that string before any comparison starts.

## Evidence inputs

| Input | Row fields |
| --- | --- |
| Layers | `name`, integer `color`, `linetype`, boolean `plot` |
| Text styles | `name`, `font_file` including the full path when recorded |
| Title blocks | A snapshot with `definitions` (each with `name` and ordered `geometry`) and `inserts` (each with stable `id`, `block` and ordered `attributes` with `tag` and `value`) |
| Layouts | `name`, `plot_style_table` |
| Model-space counts | `type`, `layer`, integer `count`, with explicit lists of original and Studio-added layers supplied to the comparison |

Use full snapshots from the same evidence producer and format. Keep layer names and insert identities stable across snapshots. A missing original layer can signal a deletion, rename or merge. Layer rows alone cannot distinguish those causes. A matching recorded font path does not establish which font AutoCAD loaded.

## Frozen acceptance

Run all seven criteria for Copenhagen, Bensenville and Hope D Wall on the approved licensed host.

1. The normal Studio action opens the current exported version in desktop AutoCAD within 60 seconds with zero modal dialogs. This requires a desktop observation.
2. Every original layer keeps its name, color, linetype and plot flag. Use the layer comparison against source and reopened drawing dumps.
3. Every original text style keeps its font file. Use the text style comparison and observe AutoCAD for SHX substitution.
4. The title-block definition and insert attribute values match. Use a source and reopened drawing dump with stable insert IDs.
5. The plot style table is unchanged and plotting the first layout succeeds. The offline comparison covers table names. Plot success requires AutoCAD.
6. Model-space counts match by type and layer except approved Studio additions. Use the count comparison with the original layer inventory and explicit Studio layer names.
7. AutoCAD AUDIT reports zero errors. Keep the native AUDIT result.

Synthetic rows exercise the comparisons. A scored result requires evidence tied to all three frozen drawings, the actual Studio action and the reopened AutoCAD drawings.
