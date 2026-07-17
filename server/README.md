# Lane D — Demo backend glue (FastAPI)

Wires the frontend (Lane C) to tool results. Serves the FROZEN HTTP API
(CONTRACT section 4). Default mode uses the cached sample intake + pure-python
tool logic so the demo works **before** APS is wired; flip one env var to route
through the Lane A APS Design Automation client instead. The frontend never
knows the difference.

## Run

```bash
cd C:/tmp/leaf-web-demo/server

# demo mode (default) — cached data + pure-python tools, no APS needed
APS_LIVE=0 uvicorn app:app --port 8130
#   or simply:  python app.py

# live mode — routes /api/session + /api/run through ../da/client.py (Lane A)
APS_LIVE=1 uvicorn app:app --port 8130
```

PowerShell equivalent:

```powershell
$env:APS_LIVE=0; python -m uvicorn app:app --port 8130
```

Port **8130**. CORS is permissive (`allow_origins=["*"]`) for localhost dev.

## Endpoints (CONTRACT §4)

| Method | Path | Behaviour |
|---|---|---|
| GET  | `/api/session?dwg=rooftop_demo` | `{intake: <§1>}`. APS_LIVE=0 → cached `../data/rooftop_demo.intake.json`; APS_LIVE=1 → `da.client.extract()`. |
| GET  | `/api/tools` | `{tools:[<§2>]}` — engine registry (or built-in defaults) merged with authored tools. |
| POST | `/api/run` | `{tool, params, dwg}` → `<§3 envelope>`. APS_LIVE=0 → pure-python compute; APS_LIVE=1 → `da.client.run_tool()`. |
| POST | `/api/author` | `{description}` → `{tool:<§2>, code, preview}`. Registers the tool so it appears in `/api/tools`. |
| GET  | `/api/health` | Diagnostics: mode, which sibling lanes are present, tool counts. |

### Quick verify

```bash
curl localhost:8130/api/session?dwg=rooftop_demo         # real 2345-polyline intake
curl -XPOST localhost:8130/api/run -H "Content-Type: application/json" \
     -d '{"tool":"count-by-layer","params":{},"dwg":"rooftop_demo"}'
# -> {"ok":true,"tool":"count-by-layer",...,"result":{"counts":{"Panels":2345},"total":2345},...}
```

## Built-in tools (demo)

Three read-only tools ship as defaults (also mirror what Lane B's registry will carry):

- `count-by-layer` — entity counts per layer. `{counts, total}`.
- `measure-panel-area` — shoelace area of closed polylines on a layer. `{count, total_area, avg_area}`.
- `highlight-panels-near-edge` — panels within a margin of the layout bbox edge; returns an
  `overlay.highlight_handles` list (+ markers) for the viewer. Spatial tool for MVP step 3.

## Tool authoring (`/api/author`)

Templated (no LLM) for the constrained family **"select entities on layer L and
count / measure / highlight"** (CONTRACT §6 permits this stub, marked as such).
The description is parsed with regex to detect the operation verb and layer, fills a
tool template, and returns the package + generated `code` + a `preview` string.
Authored tools are **runnable immediately** — the server dispatches them to the same
three primitives keyed by `engine_op`, using the detected layer as the default param.

Optional real LLM authoring is gated behind `LEAF_AUTHOR_LLM=1` and falls back to
templating (no provider/secret is wired here — do not hardcode secrets).

## Dependencies

- Python 3.13, `fastapi`, `uvicorn`, `pydantic` (already present on this host).
  Install if needed: `pip install fastapi uvicorn`.

## Imports from sibling lanes (all graceful)

- **`../data/rooftop_demo.intake.json`** (root) — cached intake for APS_LIVE=0. Required for demo mode.
- **`../engine/registry.json`** (Lane B) — tool registry; falls back to built-in `DEFAULT_TOOLS` if absent.
- **`../engine/selfcheck.py`** (Lane B) — if present and it exposes `run_op` / `run_tool` / `run`,
  it's preferred for APS_LIVE=0 compute; otherwise `tools_fallback.py` (this lane) is authoritative.
- **`../da/client.py`** (Lane A) — used only when APS_LIVE=1. If absent, APS_LIVE=1 returns a clear
  error and **APS_LIVE=0 still works fully**.

## Files

- `app.py` — FastAPI app, endpoint handlers, sibling-lane import wiring.
- `tools_fallback.py` — pure-python tool logic, built-in registry, and the authoring templater.
- `authored_tools.json` — created on first `/api/author` call; persists authored tools additively
  (kept in this lane rather than editing Lane B's `engine/registry.json`; merged into `/api/tools`).

## Notes / boundaries

- This lane writes only under `server/`. Authored tools persist to `server/authored_tools.json`
  and are merged with the engine registry at read time — Lane B's `engine/` is never mutated.
- No live/mutating APS calls are made here; that is root's responsibility. APS_LIVE=1 only
  *calls into* Lane A's client, which owns any WorkItem execution.
