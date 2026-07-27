import { describe, expect, it } from "vitest";

import { makeApsTestRun } from "../src/agent/tools/apsTestRun.js";
import { FakeBrokerApsClient } from "../src/ports/fakes/fakeBrokerApsClient.js";
import type { ToolPackage } from "../src/ports/index.js";

function tool(capability: "drawing.read" | "drawing.write"): ToolPackage {
  return {
    name: "panel-layout",
    version: "1.0.0",
    description: "Deterministic panel layout",
    kind: "script",
    engine_op: "panel_layout",
    params: { type: "object", properties: {} },
    returns: { type: "object" },
    capabilities: [capability],
    provenance: {
      author: "agent",
      created: "2026-07-25T00:00:00Z",
    },
  };
}

describe("design-time APS test runs", () => {
  it("forces drawing.write candidates into dry-run mode", async () => {
    const broker = new FakeBrokerApsClient();
    const run = makeApsTestRun(broker, "demo-tenant");

    await run(tool("drawing.write"), {
      drawing_id: "cat-workbench",
      dry_run: false,
    });

    expect(broker.calls[0]!.params).toEqual({
      drawing_id: "cat-workbench",
      dry_run: true,
    });
  });

  it("leaves drawing.read params unchanged", async () => {
    const broker = new FakeBrokerApsClient();
    const run = makeApsTestRun(broker, "demo-tenant");

    await run(tool("drawing.read"), { layer: "Panels" });

    expect(broker.calls[0]!.params).toEqual({ layer: "Panels" });
  });
});
