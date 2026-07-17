# Leaf Web CAD-Tool Demo — how to run it

A browser demo: open a real DWG (rendered in our own three.js), run AI-authored CAD tools
whose real-engine work executes on **APS Design Automation**. Built 2026-07-17.

## The 90-second demo (mock mode — instant, reliable, real data)

```bash
cd C:/tmp/leaf-web-demo/web
npm install        # once
npm run dev        # http://localhost:5175
```
Open http://localhost:5175 — the rooftop (2345 real panels) renders; click a tool → Run →
result table + viewer overlay; "Author a tool" turns a plain-English description into a new
runnable tool. Mock mode computes results client-side from the REAL extracted geometry, so the
numbers are genuine (Panels = 2345), just not routed through the cloud engine.

## The proof it runs on real cloud AutoCAD (already executed)

```bash
cd C:/tmp/leaf-web-demo
export APS_LIVE=1
python da/da_extract.py data/rooftop_demo.dwg data/rooftop_live.intake.json   # real WorkItem
python measure_extract.py                                                     # cost receipt
```
Verified 2026-07-17: WorkItem status=success, output **byte-identical** to local
(4 layers / 2345 polylines, first+last geometry exact), **billable 3.3 s → $0.00554/run**
($5.54 per 1000 runs) at the confirmed AutoCAD DA rate ($6/engine-hour).

## Full stack (frontend → server → live APS)

```bash
cd C:/tmp/leaf-web-demo/server
APS_LIVE=0 python app.py        # :8130 — mock results from real data (demo-safe)
# APS_LIVE=1 routes /api/run to real WorkItems (needs per-tool Activities provisioned:
#   python da/provision_live.py --tools engine/registry.json )
```
Point the frontend at it with `VITE_MOCK=0 VITE_API_BASE=http://localhost:8130`.

## LIVE status (2026-07-17) — tool-runs now execute on APS
All three tool Activities are provisioned on APS (`LeafTool_count_by_layer`, `_measure_panel_area`,
`_highlight_panels_near_edge`, each `+prod`). Verified live, each matches the pure-python oracle:
- count-by-layer → `{Panels:2345}` (oracle 2345) · ~$0.008 · 4.06s engine
- measure-panel-area → 48718.18 sqft (oracle 48718.195) · ~$0.007 · 2.37s
- highlight-panels-near-edge(200) → 276 handles (oracle 276) · ~$0.008 · 2.71s
Full stack live-verified in-browser (LIVE badge, real extract on open, `/api/run` → real WorkItem,
UI shows `2.28s engine · ~$0.0063`). Re-provision if needed: `python da/provision_live.py --tools da/registry_live.json`.

The **Mock mode** toggle (header checkbox) flips the whole app between instant pure-python results
and real APS WorkItems — demo tip: show it real, then toggle to mock for speed.

## What's real vs staged
- REAL: DWG→JSON extraction AND all three tool-runs on APS (measured, oracle-matched), the renderer,
  the cost receipts, the frontend, the 4 tools, the author→runnable-tool flow.
- STAGED: "Author a tool" AI is templated to the select-by-layer + count/measure/highlight family
  (real + runnable, bounded) rather than open-ended codegen. Mock mode remains as the fast demo path.

## Layout
`contract/` frozen interfaces · `data/` sample + live intake JSON · `da/` APS client
(client.py, provision_live.py, da_extract.py) · `engine/` extract.scr + tools + registry +
selfcheck.py (the mock oracle) + appbundle/ (compiled-tool skeleton) · `web/` Vite+three.js
frontend · `server/` FastAPI glue.
