/**
 * Fake AgentRunner - a SCRIPTED stand-in for the real Agent SDK session. It drives
 * the SAME three tools the real session would (read-only fsTenantRepo,
 * submitTool, apsTestRun), deterministically, with NO network and NO Anthropic auth:
 *
 *   1. classify the description into a deterministic engine_op + template;
 *   2. submit source + manifest metadata through the trusted structured writer;
 *   3. receive its exact-byte validation receipt;
 *   4. test-run it once via aps-test-run (broker only);
 *   5. return { tool, code, preview, files }.
 *
 * `calls` counts run() invocations so a test can assert the run path never
 * constructs the SDK (design-time-only invariant).
 */

import type {
  AgentRunInput,
  AgentRunResult,
  AgentRunner,
  ToolPackage,
} from "../index.js";

// --------------------------------------------------------------------------- //
// deterministic NL -> engine_op template (mirrors the demo's constrained family)
// --------------------------------------------------------------------------- //
interface Template {
  engineOp: string;
  code: string;
  params: ToolPackage["params"];
  returns: ToolPackage["returns"];
  previewVerb: string;
  kind?: "script" | "view";
}

function toKebab(text: string): string {
  const k = text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .replace(/-{2,}/g, "-");
  return k.length > 0 ? k.slice(0, 60).replace(/-+$/g, "") : "authored-tool";
}

const COUNT_CODE = `"""Authored tool (deterministic; runs with ZERO LLM against the extracted Intake JSON)."""


def run(intake, params):
    params = params or {}
    layer = params.get("layer")
    counts = {}
    for key in ("polylines", "inserts", "faces3d"):
        for ent in intake.get(key) or []:
            lay = ent.get("layer", "0")
            if layer and lay != layer:
                continue
            counts[lay] = counts.get(lay, 0) + 1
    return ({"counts": counts, "total": sum(counts.values())}, None)
`;

const LIST_CODE = `"""Authored tool (deterministic; runs with ZERO LLM against the extracted Intake JSON)."""


def run(intake, params):
    params = params or {}
    prefix = params.get("prefix")
    layers = list(intake.get("layers") or [])
    if prefix:
        layers = [l for l in layers if l.startswith(prefix)]
    return ({"layers": layers, "count": len(layers)}, None)
`;

const MEASURE_CODE = `"""Authored tool (deterministic; runs with ZERO LLM against the extracted Intake JSON)."""


def _shoelace(pts):
    n = len(pts)
    if n < 3:
        return 0.0
    a = 0.0
    for i in range(n):
        x1, y1 = pts[i][0], pts[i][1]
        x2, y2 = pts[(i + 1) % n][0], pts[(i + 1) % n][1]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


def run(intake, params):
    params = params or {}
    layer = params.get("layer")
    total_in2 = 0.0
    for pl in intake.get("polylines") or []:
        if not pl.get("closed"):
            continue
        if layer and pl.get("layer") != layer:
            continue
        total_in2 += _shoelace(pl.get("pts") or [])
    return ({"area_sqft": round(total_in2 / 144.0, 3),
             "units_assumption": "1 drawing unit = 1 inch; sqft = in2 / 144"}, None)
`;

// The canned VIEW template: an HTML fragment (never a document) that renders
// its data contract as themed bars. It must itself pass checkViewFragment —
// theme tokens only, no external origins, no position:fixed — because it goes
// through the SAME submit gate as everything else. Exported so the CI render
// checker self-tests against the exact template the loop authors.
export const FAKE_VIEW_TEMPLATE = `<div class="mv">
  <div class="mv-title">status</div>
  <div class="mv-bars"></div>
  <div class="mv-foot"><span class="mv-total"></span><span>
    <button type="button" class="mv-refresh">refresh from tool</button>
    <button type="button" class="mv-ask">ask in chat</button>
  </span></div>
</div>
<style>
  .mv { font: 14px/1.5 system-ui, sans-serif; color: var(--ink); }
  .mv-title { font-weight: 600; margin-bottom: 8px; }
  .mv-row { display: flex; align-items: center; gap: 8px; margin: 4px 0; }
  .mv-label { flex: 0 0 30%; font: 12px var(--mono); color: var(--muted); overflow-wrap: anywhere; }
  .mv-track { flex: 1; display: block; background: var(--bg); border: 1px solid var(--edge);
    border-radius: 5px; height: 14px; }
  .mv-fill { display: block; background: var(--accent); height: 100%; border-radius: 4px; }
  .mv-n { flex: 0 0 2.5em; text-align: right; font: 12px var(--mono); }
  .mv-foot { display: flex; align-items: center; justify-content: space-between; margin-top: 10px; }
  .mv-total { color: var(--muted); font-size: 12.5px; }
  .mv-empty { color: var(--muted); font-size: 13px; }
  .mv button { background: transparent; color: var(--accent); border: 1px solid var(--accent);
    border-radius: 6px; padding: 3px 10px; font-size: 12.5px; cursor: pointer; margin-left: 6px; }
</style>
<script>
  (function () {
    var d = window.MUSHY_DATA || {};
    var el = document.querySelector(".mv");
    function render(data) {
      var counts = data.counts || {};
      var keys = Object.keys(counts).sort();
      var max = 0;
      keys.forEach(function (k) { max = Math.max(max, Number(counts[k]) || 0); });
      el.querySelector(".mv-title").textContent = data.title || "status";
      var bars = el.querySelector(".mv-bars");
      bars.textContent = "";
      if (!keys.length) {
        var e = document.createElement("div");
        e.className = "mv-empty";
        e.textContent = "no data yet - pass counts in the view data";
        bars.appendChild(e);
      }
      keys.forEach(function (k) {
        var row = document.createElement("div"); row.className = "mv-row";
        var lab = document.createElement("span"); lab.className = "mv-label"; lab.textContent = k;
        var track = document.createElement("span"); track.className = "mv-track";
        var fill = document.createElement("span"); fill.className = "mv-fill";
        fill.style.width = (max ? Math.round(100 * (Number(counts[k]) || 0) / max) : 0) + "%";
        track.appendChild(fill);
        var n = document.createElement("span"); n.className = "mv-n"; n.textContent = String(counts[k]);
        row.appendChild(lab); row.appendChild(track); row.appendChild(n);
        bars.appendChild(row);
      });
      var total = data.total;
      if (total === undefined) {
        total = 0;
        keys.forEach(function (k) { total += Number(counts[k]) || 0; });
      }
      el.querySelector(".mv-total").textContent = "total " + total;
    }
    render(d);
    var refresh = el.querySelector(".mv-refresh");
    if (d.source_tool && window.mushy) {
      refresh.onclick = function () {
        window.mushy.callTool(d.source_tool, {}).then(function (out) {
          var r = (out && out.result) || {};
          render({ title: d.title, counts: r.counts || {}, total: r.total });
        });
      };
    } else { refresh.style.display = "none"; }
    var ask = el.querySelector(".mv-ask");
    if (window.mushy) {
      ask.onclick = function () {
        window.mushy.sendPrompt(d.prompt || "what does this panel show?");
      };
    } else { ask.style.display = "none"; }
  })();
</script>
`;

function classify(description: string, views: boolean): Template {
  const d = description.toLowerCase();
  if (views && /\b(widget|view|chart|status|diagram)\b/.test(d)) {
    return {
      kind: "view",
      engineOp: "render_view",
      code: FAKE_VIEW_TEMPLATE,
      params: {
        type: "object",
        properties: {
          title: { type: "string", description: "panel heading" },
          counts: { type: "object", description: "label -> number, drawn as themed bars" },
          total: { type: "number", description: "optional; computed from counts when absent" },
          source_tool: { type: "string", description: "catalog tool the refresh button re-queries via callTool (no model)" },
          prompt: { type: "string", description: "chat prompt the ask button sends via sendPrompt" },
        },
        required: [],
      },
      returns: { type: "object" },
      previewVerb: "renders a themed status panel from its data contract",
    };
  }
  if (/\b(area|measure|square|sq\.?\s?ft|footage)\b/.test(d)) {
    return {
      engineOp: "measure_area_by_layer",
      code: MEASURE_CODE,
      params: {
        type: "object",
        properties: { layer: { type: "string", description: "optional layer filter" } },
        required: [],
      },
      returns: { type: "object" },
      previewVerb: "measures closed-polyline area (sq ft)",
    };
  }
  if (/\b(list|names?)\b/.test(d) && !/\bcount|how many|number of\b/.test(d)) {
    return {
      engineOp: "list_layers",
      code: LIST_CODE,
      params: {
        type: "object",
        properties: { prefix: { type: "string", description: "optional layer-name prefix" } },
        required: [],
      },
      returns: { type: "object" },
      previewVerb: "lists layer names",
    };
  }
  // default: count per layer
  return {
    engineOp: "count_by_layer",
    code: COUNT_CODE,
    params: {
      type: "object",
      properties: { layer: { type: "string", description: "optional layer filter" } },
      required: [],
    },
    returns: { type: "object" },
    previewVerb: "counts entities per layer",
  };
}

export class FakeAgentRunner implements AgentRunner {
  /** Number of times run() has been called (must stay 0 on the run path). */
  calls = 0;

  /**
   * DORMANT BY DEFAULT. This runner can author `kind: "view"` packages, but it
   * only does so when explicitly switched on.
   *
   * The reason is a half-working window, not tidiness. The validator, schema and
   * fold in this change are complete, but NO consumer shell renders a view yet.
   * Left on, the demo's DEFAULT fake mode would author a view for any prompt
   * matching /widget|view|chart|status|diagram/, commit it, list it in the
   * catalog, and then fail it at run time with an unresolved entry. Off by
   * default, the capability lands fully tested and never fires until a renderer
   * exists and turns it on.
   *
   * The production AgentSdkRunner cannot author a view at all today: its
   * validate_tool schema carries no `kind` field, so submitToolProposal's
   * `proposal.kind ?? "script"` makes every real proposal a script. Closing that
   * belongs with the renderer, and it is why this flag is currently the only
   * path that can produce a view.
   */
  constructor(private readonly views = false) {}

  async run(input: AgentRunInput): Promise<AgentRunResult> {
    this.calls += 1;
    const tmpl = classify(input.description, this.views);
    const name = toKebab(input.description);
    const kind = tmpl.kind ?? "script";
    const submitted = input.toolset.submitTool({
      name,
      description: input.description,
      engine_op: tmpl.engineOp,
      params: tmpl.params,
      returns: tmpl.returns,
      capabilities: kind === "view" ? [] : ["drawing.read"],
      source: tmpl.code,
      session: "fake-agent-runner",
      ...(kind === "view" ? { kind } : {}),
    });

    // (4) test-run once through the broker (broker only, aps_live=false).
    // Views never execute on the engine, so there is nothing to test-run.
    if (kind !== "view") {
      await input.toolset.apsTestRun(submitted.tool, {});
    }

    const preview = kind === "view"
      ? `View "${name}" ${tmpl.previewVerb} (kind=view, data contract in params, rendered by the shell with zero LLM).`
      : `Tool "${name}" ${tmpl.previewVerb} (engine_op=${tmpl.engineOp}, kind=script, zero-LLM at runtime).`;
    return {
      tool: submitted.tool,
      code: submitted.code,
      preview,
      files: submitted.files,
      sourceReceipt: submitted.receipt,
    };
  }
}
