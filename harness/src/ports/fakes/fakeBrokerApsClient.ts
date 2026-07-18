/**
 * Fake BrokerApsClient - returns a real, schema-valid CONTRACT section 3 envelope
 * computed deterministically from a tiny embedded intake. NO network, NO APS. It
 * mirrors the shape the real broker returns (POST /broker/run) closely enough to
 * exercise the run + test-run paths hermetically.
 *
 * It also records every call (with the requested apsLive flag) so a test can prove
 * the run path went through the broker and stayed hermetic.
 */

import type { BrokerApsClient, BrokerRunRequest, ResultEnvelope } from "../index.js";

/** Minimal stand-in for the golden rooftop intake (2 layers, a few entities). */
const EMBEDDED_INTAKE = {
  layers: ["Panels", "0"],
  polylines: [
    { layer: "Panels", closed: true, handle: "9A2" },
    { layer: "Panels", closed: true, handle: "9A3" },
    { layer: "Panels", closed: true, handle: "9A4" },
    { layer: "0", closed: false, handle: "1B0" },
  ],
  inserts: [] as { layer: string }[],
  faces3d: [] as { layer: string }[],
};

function countByLayer(): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const coll of ["polylines", "inserts", "faces3d"] as const) {
    for (const e of EMBEDDED_INTAKE[coll]) {
      counts[e.layer] = (counts[e.layer] ?? 0) + 1;
    }
  }
  return counts;
}

export class FakeBrokerApsClient implements BrokerApsClient {
  readonly calls: BrokerRunRequest[] = [];

  async runTool(req: BrokerRunRequest): Promise<ResultEnvelope> {
    this.calls.push(req);
    const tool = req.tool;
    const t0 = Date.now();

    let result: Record<string, unknown>;
    switch (tool.engine_op) {
      case "count_by_layer":
        result = { counts: countByLayer() };
        break;
      case "list_layers":
        result = { layers: EMBEDDED_INTAKE.layers };
        break;
      default:
        // Deterministic, still a valid envelope for any engine_op.
        result = { note: `mock run of engine_op ${tool.engine_op}`, params: req.params };
    }

    return {
      ok: true,
      tool: tool.name,
      version: tool.version ?? "1.0.0",
      result,
      overlay: null,
      timing_ms: Math.max(1, Date.now() - t0),
      cost: null, // null on the mock path (no real APS run)
      error: null,
      degraded_mode: false,
    };
  }
}
