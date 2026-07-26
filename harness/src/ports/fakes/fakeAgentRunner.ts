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

function classify(description: string): Template {
  const d = description.toLowerCase();
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

  async run(input: AgentRunInput): Promise<AgentRunResult> {
    this.calls += 1;
    const tmpl = classify(input.description);
    const name = toKebab(input.description);
    const submitted = input.toolset.submitTool({
      name,
      description: input.description,
      engine_op: tmpl.engineOp,
      params: tmpl.params,
      returns: tmpl.returns,
      capabilities: ["drawing.read"],
      source: tmpl.code,
      session: "fake-agent-runner",
    });

    // (4) test-run once through the broker (broker only, aps_live=false).
    await input.toolset.apsTestRun(submitted.tool, {});

    const preview = `Tool "${name}" ${tmpl.previewVerb} (engine_op=${tmpl.engineOp}, kind=script, zero-LLM at runtime).`;
    return {
      tool: submitted.tool,
      code: submitted.code,
      preview,
      files: submitted.files,
      sourceReceipt: submitted.receipt,
    };
  }
}
