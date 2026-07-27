import { createHash } from "node:crypto";
import {
  existsSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  MAX_TOOL_SOURCE_BYTES,
  submitToolProposal,
} from "../src/agent/tools/submitToolProposal.js";
import { AUTHOR_FS_ACTIONS } from "../src/ports/impl/agentSdkRunner.js";
import type { ToolSourceProposal } from "../src/ports/index.js";

const CAT_SOURCE = `def run(intake, params):
    panels = sorted(
        [p for p in intake.get("polylines", []) if p.get("layer") == "Panels"],
        key=lambda panel: panel.get("handle", ""),
    )
    transforms = []
    for index, panel in enumerate(panels):
        pts = panel.get("pts") or []
        cx = sum(pt[0] for pt in pts) / len(pts)
        cy = sum(pt[1] for pt in pts) / len(pts)
        target_x = (index % 50) * 12.0
        target_y = (index // 50) * 8.0
        transforms.append({
            "handle": panel["handle"],
            "dx": target_x - cx,
            "dy": target_y - cy,
            "rotation_deg": 0.0,
        })
    return ({"mutations": {"transforms": transforms}, "panel_count": len(panels)}, None)
`;

function proposal(overrides: Partial<ToolSourceProposal> = {}): ToolSourceProposal {
  return {
    name: "arrange-panels-as-cat",
    description: "Rearrange every panel into a deterministic sitting-cat silhouette.",
    engine_op: "arrange_panels_as_cat",
    params: {
      type: "object",
      properties: {
        drawing_id: { type: "string", default: "cat-workbench" },
        dry_run: { type: "boolean", default: false },
      },
      required: [],
    },
    returns: { type: "object" },
    capabilities: ["drawing.write"],
    source: CAT_SOURCE,
    session: "test-session",
    ...overrides,
  };
}

describe("structured tool proposal boundary", () => {
  let root: string;

  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), "leaf-tool-proposal-"));
    writeFileSync(join(root, "registry.json"), '{"tools":[]}\n', "utf8");
  });

  it("mounts no model-controlled repository write action", () => {
    expect(AUTHOR_FS_ACTIONS).toEqual(["read", "list", "exists"]);
    expect(AUTHOR_FS_ACTIONS).not.toContain("write");
  });

  afterEach(() => {
    rmSync(root, { recursive: true, force: true });
  });

  it("writes one novel drawing.write package and returns exact-byte receipts", () => {
    const submitted = submitToolProposal(
      root,
      proposal(),
      new Date("2026-07-26T12:00:00.000Z"),
    );

    expect(submitted.tool).toMatchObject({
      name: "arrange-panels-as-cat",
      engine_op: "arrange_panels_as_cat",
      capabilities: ["drawing.write"],
      entry: "tools/arrange-panels-as-cat/tool.py",
    });
    expect(submitted.code).toBe(CAT_SOURCE);
    expect(submitted.files).toEqual([
      "tools/arrange-panels-as-cat/tool.json",
      "tools/arrange-panels-as-cat/tool.py",
    ]);

    const source = readFileSync(join(root, submitted.receipt.entry));
    const manifest = readFileSync(join(root, submitted.receipt.manifest));
    expect(source.toString("utf8")).toBe(CAT_SOURCE);
    expect(submitted.receipt.source_sha256)
      .toBe(createHash("sha256").update(source).digest("hex"));
    expect(submitted.receipt.manifest_sha256)
      .toBe(createHash("sha256").update(manifest).digest("hex"));
    expect(submitted.receipt.source_bytes).toBe(source.byteLength);
    expect(submitted.receipt.manifest_bytes).toBe(manifest.byteLength);
    expect(JSON.parse(manifest.toString("utf8"))).toMatchObject({
      name: "arrange-panels-as-cat",
      entry: "tool.py",
    });
  });

  it("rejects unsafe names, missing write controls, invalid source, and oversized source before writing", () => {
    const cases = [
      proposal({ name: "../escape" }),
      proposal({
        params: { type: "object", properties: {}, required: [] },
      }),
      proposal({ source: "print('not a tool')\n" }),
      proposal({
        source: `def run(intake, params):\n    return ({}, None)\n#${"x".repeat(MAX_TOOL_SOURCE_BYTES)}`,
      }),
    ];

    for (const candidate of cases) {
      expect(() => submitToolProposal(root, candidate)).toThrow(
        /tool proposal rejected/,
      );
    }
    expect(existsSync(join(root, "tools", "arrange-panels-as-cat"))).toBe(false);
    expect(existsSync(join(root, "escape"))).toBe(false);
  });

  it("cannot overwrite an existing package or registry entry", () => {
    submitToolProposal(root, proposal());
    expect(() => submitToolProposal(root, proposal({ source: CAT_SOURCE + "\n# changed\n" })))
      .toThrow(/already exists/);
    expect(readFileSync(join(root, "tools", "arrange-panels-as-cat", "tool.py"), "utf8"))
      .toBe(CAT_SOURCE);
  });

  it("replaces only the same uncommitted package when the prior receipt matches exactly", () => {
    const first = submitToolProposal(
      root,
      proposal(),
      new Date("2026-07-26T12:00:00.000Z"),
    );
    const changed = `${CAT_SOURCE}\n# broker-test correction\n`;
    const second = submitToolProposal(
      root,
      proposal({ source: changed }),
      new Date("2026-07-26T12:01:00.000Z"),
      first.receipt,
    );

    expect(readFileSync(join(root, second.receipt.entry), "utf8")).toBe(changed);
    expect(second.receipt.source_sha256).not.toBe(first.receipt.source_sha256);
    expect(JSON.parse(readFileSync(join(root, second.receipt.manifest), "utf8")))
      .toMatchObject({
        provenance: {
          created: "2026-07-26T12:00:00.000Z",
          modified: "2026-07-26T12:01:00.000Z",
        },
      });
    expect(readdirSync(join(root, "tools"))).toEqual(["arrange-panels-as-cat"]);
  });

  it("refuses replacement when the receipt does not match the current bytes", () => {
    const first = submitToolProposal(root, proposal());
    const forged = { ...first.receipt, source_sha256: "0".repeat(64) };
    expect(() => submitToolProposal(
      root,
      proposal({ source: `${CAT_SOURCE}\n# forged replacement\n` }),
      new Date(),
      forged,
    )).toThrow(/receipt does not match existing bytes/);
    expect(readFileSync(join(root, first.receipt.entry), "utf8")).toBe(CAT_SOURCE);
  });
});
