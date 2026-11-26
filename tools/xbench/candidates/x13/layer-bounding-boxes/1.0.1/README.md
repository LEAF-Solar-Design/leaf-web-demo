# Layer bounding boxes 1.0.1

This offline Studio candidate reports model-space bounds and geometry counts.
It ignores polylines and 3D faces explicitly marked as paper space. It
continues to accept older intake records that omit the space marker.

The source and tool definition derive from the Studio repository's
`data/nl_author_receipt.json` (SHA-256
`1a931347cfc5cddc2b91d2c656e7503c78be35b2a6a284f32e9b5243ea6160db`).
That receipt identifies demo-tenant commit
`c534031a9cd1f02838799544adfc3ad174b44348` and records the 1.0.0 entry
SHA-256 `d1dd2439ecde5fcb562c7e29ff4fc5d1d2e5be465b58a2312ac973b2470543c0`.
The parked receipt supplies historical provenance for this new version.

The compact DXF reproduces S-04's paper-space `PS_ANNOT` line and
adds a paper line on a shared layer to test contamination of model extents
and counts. The focused tests use Studio's DXF parser and authored tool
loader, including a legacy face carrying no space marker.

From the Studio repository root, run the focused regressions with APS
disabled.

```powershell
$env:APS_LIVE = '0'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
python -P -m pytest -q -o pythonpath=server server/tests/test_x13_layer_bounding_boxes_candidate.py
```
