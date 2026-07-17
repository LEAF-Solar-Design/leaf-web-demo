# Leaf Web Demo — FROZEN CONTRACT v1

**Goal:** a browser demo where a person opens a real DWG (rendered in *our* three.js, not Autodesk's), runs AI-authored CAD tools whose engine work executes on **APS Design Automation** (occasional WorkItems), and sees results — proving the web lane of "build your own CAD tools with AI."

**Golden sample data (already real, use it):** `C:/tmp/leaf-web-demo/data/rooftop_demo.intake.json` — produced by the proven extractor from `rooftop_demo.dwg` (2345 polylines / 4 layers, extracted headless in 6 s). Every lane builds against THIS file so the demo works before APS is wired.

**Ownership (DISJOINT — do not write outside your dir):**
- Lane A → `C:/tmp/leaf-web-demo/da/` (APS Design Automation client)
- Lane B → `C:/tmp/leaf-web-demo/engine/` (extraction script, tools, registry)
- Lane C → `C:/tmp/leaf-web-demo/web/` (frontend)
- Lane D → `C:/tmp/leaf-web-demo/server/` (demo backend glue)
- Root owns `contract/`, `data/`, and ALL live APS calls (bucket/upload/appbundle/activity/workitem).

---

## 1. Intake JSON (extractor output → frontend render input) — FROZEN

```jsonc
{
  "dwg": "string (source path/name)",
  "layers": ["Panels", "0", ...],              // string[]
  "polylines": [                                // the main geometry — 2345 in the sample
    {
      "layer": "Panels",
      "closed": true,
      "pts": [[x,y,z], [x,y,z], ...],           // WCS coords, numbers; z often ~constant
      "xdata": null,                            // or object of app->string[]
      "handle": "9A2"                           // DWG entity handle (stable id)
    }
  ],
  "inserts":   [ {"name","layer","pt":[x,y,z],"rot","scale":[x,y,z],"handle"} ],  // may be []
  "faces3d":   [ {"layer","p1":[x,y,z],"p2","p3","p4"} ],                          // may be []
  "blockdefs": {},                              // object; may be empty
  "geodata":   ["none"] or [ {dxf pairs} ],
  "images": [], "imageNames": []
}
```
Frontend MUST handle empty inserts/faces3d (the sample has only polylines). Render polylines as
closed/open paths in a top-down 2D view (this is a rooftop plan), color-by-layer, fit-to-bounds, pan+zoom.

## 2. Tool package (registry entry) — FROZEN

```jsonc
{
  "name": "count-by-layer",                     // kebab-case, unique, = MCP tool suffix
  "version": "1.0.0",
  "description": "Counts entities per layer.",
  "kind": "script",                             // "script" (LISP/scr on DA) | "appbundle" (compiled)
  "engine_op": "count_by_layer",                // activity/command id the DA layer runs
  "params": { "type": "object", "properties": { }, "required": [] },  // JSON Schema
  "returns": { "type": "object" },
  "capabilities": ["drawing.read"],             // drawing.read | drawing.write
  "provenance": { "author": "agent|user", "created": "<iso8601>" }
}
```
Registry file: `engine/registry.json` = `{ "tools": [ <tool package>, ... ] }`.

## 3. Result envelope (WorkItem output → frontend) — FROZEN

Every tool run (mock OR real APS) returns EXACTLY this shape:
```jsonc
{
  "ok": true,
  "tool": "count-by-layer",
  "version": "1.0.0",
  "result": { /* tool-specific data, e.g. {"counts": {"Panels": 2345}} */ },
  "overlay": {                                  // OPTIONAL — how the frontend shows the effect
    "highlight_handles": ["9A2","9A3"],         // entity handles to emphasize in the viewer
    "markers": [ {"pt":[x,y], "label":"gap"} ], // points to draw
    "polylines": [ {"pts":[[x,y]], "color":"#f00"} ]  // extra geometry to overlay
  },
  "timing_ms": 412,
  "cost": { "engine_seconds": 3.1, "usd_est": 0.005 },  // null until real APS run
  "error": null
}
```

## 4. HTTP API (frontend ↔ server) — FROZEN

- `GET  /api/session?dwg=rooftop_demo` → `{ intake: <Intake JSON §1> }` (server extracts or serves cached sample)
- `GET  /api/tools` → `{ tools: [ <tool package §2> ] }`
- `POST /api/run` `{ "tool": "count-by-layer", "params": {}, "dwg": "rooftop_demo" }` → `<Result envelope §3>`
- `POST /api/author` `{ "description": "count panels within 18in of the roof edge" }` → `{ tool: <tool package §2>, code: "<generated script>", preview: "..." }`

Server MUST work with the sample data + mock tool results when APS is not yet wired (env `APS_LIVE=0`),
and switch to the Lane A DA client when `APS_LIVE=1`. Frontend never knows the difference.

## 5. DA client interface (server → Lane A) — FROZEN

Python module `da/client.py` exposing:
- `extract(dwg_local_path: str) -> dict`  # returns Intake JSON §1 (runs the extract WorkItem)
- `run_tool(dwg_local_path: str, tool: dict, params: dict) -> dict`  # returns Result envelope §3
- `auth_token() -> str`  # 2-legged; creds at `~/.aps/credentials.json`

Reference (working auth + engine list): `C:/tmp/aps-spike/probe-aps.ps1`.
Extraction recipe to port to a DA activity: the LISP block in
`C:/Users/ehaug/OneDrive/Documents/GitHub/utility-estimation/extracts/dwg_intake.py`.
APS AutoCAD engines available (confirmed): `Autodesk.AutoCAD+24_3` (net48), `+25_1`, `+26_0` (net8).

## 6. MVP demo scope (what must work end to end)

1. Open `rooftop_demo` → see 2345 panels rendered, colored by layer, pan/zoom. (Lane C + sample data)
2. Run **count-by-layer** → table of counts. (read-only, safest first tool)
3. Run a **spatial** tool (e.g. **highlight-panels-near-edge** or **measure-panel-area**) → viewer overlays the result. (Lane B tool + Lane C overlay)
4. **Author** a tool from a text description → new tool appears in the list and is runnable. (Lane D generate + Lane B template)
5. At least one tool run executed for REAL on APS with a measured cost receipt. (Root, live)

Stub allowed for the demo: real LLM tool-gen may be templated for a constrained family (select-by-layer + measure/count); mark it. Everything else must be real.
