import { createHash } from "node:crypto";
import {
  existsSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  statSync,
  utimesSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  MAX_TOOL_SOURCE_BYTES,
  revisionSchemaChange,
  revisionVersion,
  REVISION_ERROR,
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
      version: "1.0.0",
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

const J = JSON.stringify;
const B = {
  type: "object", properties: { x: { type: "string" } }, required: ["x"],
};
const O = { type: "object" };
const A = { ...B, properties: { ...B.properties, y: true } };
const P = (x: unknown) => ({ type: "object", properties: { x } });
const N = (depth: number): unknown => {
  let value: unknown = null;
  while (depth-- > 0) value = [value];
  return value;
};
const add = (b: Record<string, unknown>, y: unknown = true) => ({
  ...b, properties: { ...(b.properties as object | undefined), y },
});
const T = {
  type: "object", properties: { x: true, z: true }, required: ["x", "z"],
};
const numeric = (token: string) =>
  `{"type":"object","properties":{"x":{"default":${token}}}}`;

describe("lossless server revision classification", () => {
  it("D2b1 C01", () => {
    expect(revisionSchemaChange(J(B), J(B)))
      .toBe("same");
  });

  it("D2b1 C02", () => {
    expect(revisionSchemaChange(J(B), J(A)))
      .toBe("additive");
  });

  it("D2b1 C03", () => {
    expect(revisionSchemaChange(J(B), J({ ...B, properties: {} })))
      .toBe("breaking");
  });

  it("D2b1 C04", () => {
    expect(revisionSchemaChange(J(B), J({ ...B, properties: { x: { type: "number" } } })))
      .toBe("breaking");
  });

  it("D2b1 C05", () => {
    expect(revisionSchemaChange(J(B), J({ ...A, required: ["x", "y"] })))
      .toBe("breaking");
  });

  it("D2b1 C06", () => {
    expect(revisionSchemaChange(J(P({ default: { a: 1 } })), J(P({ default: { a: 2 } }))))
      .toBe("breaking");
  });

  it("D2b1 C07", () => {
    expect(revisionSchemaChange(J(B), J({ type: "object", properties: B.properties })))
      .toBe("breaking");
  });

  it("D2b1 C08", () => {
    expect(revisionSchemaChange(J(T), J({ ...T, required: ["z", "x"] })))
      .toBe("breaking");
  });

  it("D2b1 C09", () => {
    expect(revisionSchemaChange(J(O), J({ type: "object", properties: { y: true }, required: [] })))
      .toBe("breaking");
  });

  it("D2b1 C10", () => {
    expect(revisionSchemaChange(J(B), J({ ...A, additionalProperties: false })))
      .toBe("breaking");
  });

  it("D2b1 C11", () => {
    expect(revisionSchemaChange(J(O), J(add(O))))
      .toBe("additive");
  });

  it("D2b1 C12", () => {
    expect(revisionSchemaChange(J(O), J({ ...O, properties: {} })))
      .toBe("breaking");
  });

  it("D2b1 C13", () => {
    expect(revisionSchemaChange(J(B), J(add(B, false))))
      .toBe("additive");
  });

  it("D2b1 C14", () => {
    expect(revisionSchemaChange(J(B), J(add(B, { nested: N(129) }))))
      .toBe("additive");
  });

  it("D2b1 C15", () => {
    expect(revisionSchemaChange(J(B), J({ ...B, properties: [] })))
      .toBe("breaking");
  });

  it("D2b1 C16", () => {
    expect(revisionSchemaChange(J({ ...B, properties: [] }), J(A)))
      .toBe("breaking");
  });

  it("D2b1 C17", () => {
    expect(revisionSchemaChange(J({ ...B, required: ["x", "x"] }), J(add({ ...B, required: ["x", "x"] }))))
      .toBe("breaking");
  });

  it("D2b1 C18", () => {
    expect(revisionSchemaChange(J({ ...B, required: ["missing"] }), J(add({ ...B, required: ["missing"] }))))
      .toBe("breaking");
  });

  it("D2b1 C19", () => {
    expect(revisionSchemaChange(J({ ...B, required: [1] }), J(add({ ...B, required: [1] }))))
      .toBe("breaking");
  });

  it("D2b1 C20", () => {
    expect(revisionSchemaChange(J({ ...B, required: "x" }), J(add({ ...B, required: "x" }))))
      .toBe("breaking");
  });

  it("D2b1 C21", () => {
    expect(revisionSchemaChange(J(B), J(add(B, 1))))
      .toBe("breaking");
  });

  it("D2b1 C22", () => {
    expect(revisionSchemaChange(J(P({ default: true })), J(P({ default: 1 }))))
      .toBe("breaking");
  });

  it("D2b1 C23", () => {
    expect(revisionSchemaChange(numeric("1"), numeric("1.0")))
      .toBe("breaking");
  });

  it("D2b1 C24", () => {
    expect(revisionSchemaChange(numeric("1.0"), numeric("1")))
      .toBe("breaking");
  });

  it("D2b1 C25", () => {
    expect(revisionSchemaChange(numeric("-0.0"), numeric("0.0")))
      .toBe("breaking");
  });

  it("D2b1 C26", () => {
    expect(revisionSchemaChange(numeric("9007199254740992"), numeric("9007199254740993")))
      .toBe("breaking");
  });

  it("D2b1 C27", () => {
    expect(revisionSchemaChange(numeric("9007199254740993"), numeric("9007199254740993")))
      .toBe("same");
  });

  it("D2b1 C28", () => {
    expect(revisionSchemaChange(J(B), J(Object.fromEntries(Object.entries(B).reverse()))))
      .toBe("same");
  });

  it("D2b1 C29", () => {
    expect(revisionSchemaChange(J(B), J(Object.fromEntries(Object.entries(A).reverse()))))
      .toBe("additive");
  });

  it("D2b1 C30", () => {
    expect(revisionSchemaChange(J(N(128)), J(N(128))))
      .toBe("same");
  });

  it("D2b1 C31", () => {
    expect(revisionSchemaChange(J(N(129)), J(N(129))))
      .toBe("breaking");
  });

  it("D2b1 C32", () => {
    expect(revisionSchemaChange(J(P(N(128))), J(add(P(N(128))))))
      .toBe("additive");
  });

  it("D2b1 C33", () => {
    expect(revisionSchemaChange(J(P(N(129))), J(add(P(N(129))))))
      .toBe("breaking");
  });

  it("D2b1 C34", () => {
    expect(revisionSchemaChange(J({ ...B, required: ["missing"] }), J({ ...B, required: ["missing"] })))
      .toBe("same");
  });

  it("D2b1 C35", () => {
    expect(revisionSchemaChange('{"default":1.0}', '{"default":1e0}'))
      .toBe("same");
  });

  it("D2b1 C36", () => {
    expect(revisionSchemaChange('{"default":1.00000000000000001}', '{"default":1.0}'))
      .toBe("same");
  });

  it("D2b1 C37", () => {
    expect(revisionSchemaChange('{"default":1e400}', '{"default":2e400}'))
      .toBe("same");
  });

  it("D2b1 C38", () => {
    expect(revisionSchemaChange('{"default":-0}', '{"default":0}'))
      .toBe("same");
  });

  it("D2b1 C39", () => {
    expect(revisionSchemaChange(J(P({ enum: [1, 2] })), J(P({ enum: [2, 1] }))))
      .toBe("breaking");
  });

  it("D2b1 C40", () => {
    expect(revisionSchemaChange('{"x":1,"x":2}', '{"x":2}'))
      .toBe("same");
  });

  it("D2b1 C41", () => {
    expect(revisionSchemaChange('{"__proto__":{"x":1}}', '{"__proto__":{"x":2}}'))
      .toBe("breaking");
  });

});

describe("exact revision versions", () => {
  it("D2b1 V01", () => {
    const rawRegistry = J({ tools: [{ name: "t", version: "1.2.3", params: B, returns: O }] });
    const candidateJson = J({ params: B, returns: O });
    expect(revisionVersion(rawRegistry, "t", candidateJson)).toBe("1.2.4");
  });

  it("D2b1 V02", () => {
    const rawRegistry = J({ tools: [{ name: "t", version: "1.2.3", params: B, returns: O }] });
    const candidateJson = J({ params: A, returns: O });
    expect(revisionVersion(rawRegistry, "t", candidateJson)).toBe("1.3.0");
  });

  it("D2b1 V03", () => {
    const rawRegistry = J({ tools: [{ name: "t", version: "1.9.4", params: B, returns: O }] });
    const candidateJson = J({ params: A, returns: O });
    expect(revisionVersion(rawRegistry, "t", candidateJson)).toBe("1.10.0");
  });

  it("D2b1 V04", () => {
    const rawRegistry = J({ tools: [{ name: "t", version: "01.09.004", params: B, returns: O }] });
    const candidateJson = J({ params: B, returns: O });
    expect(revisionVersion(rawRegistry, "t", candidateJson)).toBe("01.09.5");
  });

  it("D2b1 V05", () => {
    const rawRegistry = J({ tools: [{ name: "t", version: "01.09.004", params: B, returns: O }] });
    const candidateJson = J({ params: A, returns: O });
    expect(revisionVersion(rawRegistry, "t", candidateJson)).toBe("01.10.0");
  });

  it("D2b1 V06", () => {
    const rawRegistry = J({ tools: [{ name: "t", version: "1.2.9007199254740991", params: B, returns: O }] });
    const candidateJson = J({ params: B, returns: O });
    expect(revisionVersion(rawRegistry, "t", candidateJson)).toBe("1.2.9007199254740992");
  });

  it("D2b1 V07", () => {
    const rawRegistry = J({ tools: [{ name: "t", version: "1.2.9007199254740992", params: B, returns: O }] });
    const candidateJson = J({ params: B, returns: O });
    expect(revisionVersion(rawRegistry, "t", candidateJson)).toBe("1.2.9007199254740993");
  });

  it("D2b1 V08", () => {
    const rawRegistry = J({ tools: [{ name: "t", version: "1.9007199254740992.9", params: B, returns: O }] });
    const candidateJson = J({ params: A, returns: O });
    expect(revisionVersion(rawRegistry, "t", candidateJson)).toBe("1.9007199254740993.0");
  });

  it("D2b1 V09", () => {
    const rawRegistry = J({ tools: [{ name: "t", version: "999999999999999999999.2.3", params: B, returns: O }] });
    const candidateJson = J({ params: B, returns: O });
    expect(revisionVersion(rawRegistry, "t", candidateJson)).toBe("999999999999999999999.2.4");
  });

  it("D2b1 V10", () => {
    // A non-ASCII decimal digit is refused: Python and Node disagree on which
    // code points are digits (see D2b1 V16), so the producer reads ASCII only.
    const rawRegistry = J({ tools: [{ name: "t", version: "1.٢.3", params: B, returns: O }] });
    const candidateJson = J({ params: A, returns: O });
    expect(() => revisionVersion(rawRegistry, "t", candidateJson))
      .toThrow(new Error(REVISION_ERROR));
  });

  it("D2b1 V16", () => {
    // U+10D40 is a decimal digit to Node 22 (Unicode 16) and not to Python 3.13
    // (Unicode 15.1), whose re.fullmatch refuses the base version. The preserved
    // major/minor group cases pin the ASCII rule without reaching BigInt.
    const rawRegistry = J({ tools: [{ name: "t", version: "1.2.\u{10D40}", params: B, returns: O }] });
    const candidateJson = J({ params: B, returns: O });
    expect(() => revisionVersion(rawRegistry, "t", candidateJson))
      .toThrow(new Error(REVISION_ERROR));

    const majorRegistry = J({ tools: [{ name: "t", version: "\u{10D40}.2.3", params: B, returns: O }] });
    expect(() => revisionVersion(majorRegistry, "t", candidateJson))
      .toThrow(new Error(REVISION_ERROR));

    const minorRegistry = J({ tools: [{ name: "t", version: "1.\u{10D40}.3", params: B, returns: O }] });
    expect(() => revisionVersion(minorRegistry, "t", candidateJson))
      .toThrow(new Error(REVISION_ERROR));

    const arabicIndicRegistry = J({ tools: [{ name: "t", version: "٢.2.3", params: B, returns: O }] });
    expect(() => revisionVersion(arabicIndicRegistry, "t", candidateJson))
      .toThrow(new Error(REVISION_ERROR));
  });

  it("D2b1 V17", () => {
    // Patch: the incremented group may reach 4300 digits and no further.
    const candidateJson = J({ params: B, returns: O });
    const fits = J({ tools: [{ name: "t", version: "1.2." + "9".repeat(4299), params: B, returns: O }] });
    expect(revisionVersion(fits, "t", candidateJson)).toBe("1.2.1" + "0".repeat(4299));
    const over = J({ tools: [{ name: "t", version: "1.2." + "9".repeat(4300), params: B, returns: O }] });
    expect(() => revisionVersion(over, "t", candidateJson))
      .toThrow(new Error(REVISION_ERROR));
  });

  it("D2b1 V18", () => {
    // Additive: the incremented minor group may reach 4300 digits and no further.
    const candidateJson = J({ params: A, returns: O });
    const fits = J({ tools: [{ name: "t", version: "1." + "9".repeat(4299) + ".3", params: B, returns: O }] });
    expect(revisionVersion(fits, "t", candidateJson)).toBe("1.1" + "0".repeat(4299) + ".0");
    const over = J({ tools: [{ name: "t", version: "1." + "9".repeat(4300) + ".3", params: B, returns: O }] });
    expect(() => revisionVersion(over, "t", candidateJson))
      .toThrow(new Error(REVISION_ERROR));
  });

  it("D2b1 V19", () => {
    // A base group longer than 4300 digits is refused in every position,
    // leading zeros included (CPython's limit counts them).
    const long = "0".repeat(4300) + "1";
    const candidateJson = J({ params: B, returns: O });
    for (const version of [`${long}.2.3`, `1.${long}.3`, `1.2.${long}`]) {
      const rawRegistry = J({ tools: [{ name: "t", version, params: B, returns: O }] });
      expect(() => revisionVersion(rawRegistry, "t", candidateJson))
        .toThrow(new Error(REVISION_ERROR));
    }
  });

  it("D2b1 V11", () => {
    const rawRegistry = J({ tools: [{ name: "t", version: "1.2.3-beta", params: B, returns: O }] });
    const candidateJson = J({ params: B, returns: O });
    expect(() => revisionVersion(rawRegistry, "t", candidateJson))
      .toThrow(new Error(REVISION_ERROR));
  });

  it("D2b1 V12", () => {
    const rawRegistry = J({ tools: [{ name: "t", version: "1.2.3+build", params: B, returns: O }] });
    const candidateJson = J({ params: A, returns: O });
    expect(() => revisionVersion(rawRegistry, "t", candidateJson))
      .toThrow(new Error(REVISION_ERROR));
  });

  it("D2b1 V13", () => {
    const rawRegistry = J({ tools: [{ name: "t", version: "1.2.3\n", params: B, returns: O }] });
    const candidateJson = J({ params: B, returns: O });
    expect(() => revisionVersion(rawRegistry, "t", candidateJson))
      .toThrow(new Error(REVISION_ERROR));
  });

  it("D2b1 V14", () => {
    const rawRegistry = J({ tools: [{ name: "t", version: " 1.2.3", params: B, returns: O }] });
    const candidateJson = J({ params: B, returns: O });
    expect(() => revisionVersion(rawRegistry, "t", candidateJson))
      .toThrow(new Error(REVISION_ERROR));
  });

  it("D2b1 V15", () => {
    const rawRegistry = J({ tools: [{ name: "t", version: "1.2", params: B, returns: O }] });
    const candidateJson = J({ params: B, returns: O });
    expect(() => revisionVersion(rawRegistry, "t", candidateJson))
      .toThrow(new Error(REVISION_ERROR));
  });

});

const S0 = "def run(intake, params):\n    return ({}, None)\n";
const S1 = S0 + "# first\n";
const S2 = S0 + "# corrected\n";

describe("revision producer publication", () => {
  let root: string;
  const now = new Date("2026-10-05T12:00:00.000Z");

  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), "leaf-revision-proposal-"));
    writeFileSync(join(root, "registry.json"), '{"tools":[]}\n', "utf8");
  });

  afterEach(() => {
    rmSync(root, { recursive: true, force: true });
  });

  function candidate(
    params: ToolSourceProposal["params"] = B,
    returns: ToolSourceProposal["returns"] = O,
    source = S1,
  ): ToolSourceProposal {
    return {
      name: "t",
      engine_op: "t",
      description: "Revision fixture.",
      capabilities: ["drawing.read"],
      params,
      returns,
      source,
      session: "revision-test",
    };
  }

  function seed(baseJson = J(B)) {
    const result = submitToolProposal(
      root, candidate(JSON.parse(baseJson), O, S0), now,
    );
    const registered = J({ ...result.tool, version: "1.2.3", params: null })
      .replace('"params":null', '"params":' + baseJson);
    const manifest = J({
      ...result.tool, version: "1.2.3", params: null, entry: "tool.py",
    }).replace('"params":null', '"params":' + baseJson) + "\n";
    writeFileSync(join(root, "registry.json"), '{"tools":[' + registered + ']}\n', "utf8");
    writeFileSync(join(root, result.receipt.manifest), manifest, "utf8");
    return {
      ...result.receipt,
      manifest_sha256: createHash("sha256").update(manifest).digest("hex"),
      manifest_bytes: Buffer.byteLength(manifest, "utf8"),
    };
  }

  function assertAccepted(
    result: ReturnType<typeof submitToolProposal>,
    expected: string,
    expectedSource: string,
  ) {
    expect(result.tool.version).toBe(expected);
    expect(JSON.parse(readFileSync(join(root, result.receipt.manifest), "utf8")).version)
      .toBe(expected);
    expect(readFileSync(join(root, result.receipt.entry), "utf8")).toBe(expectedSource);
  }

  const PINNED = ["registry.json", "tools/t/tool.py", "tools/t/tool.json"];
  const PAST = new Date("2001-01-01T00:00:00.000Z");

  // Any write after this, even of identical bytes, moves a file's mtime off
  // PAST, and a replace-by-rename changes its inode, so snapshot() sees it.
  function pinTimes() {
    for (const path of PINNED) utimesSync(join(root, path), PAST, PAST);
  }

  function snapshot() {
    return {
      stats: PINNED.map(path => {
        const stat = statSync(join(root, path), { bigint: true });
        return `${stat.mtimeNs}:${stat.ino}`;
      }),
      registry: readFileSync(join(root, "registry.json")),
      source: readFileSync(join(root, "tools/t/tool.py")),
      manifest: readFileSync(join(root, "tools/t/tool.json")),
      directories: Buffer.from(J([
        readdirSync(root).sort(),
        readdirSync(join(root, "tools")).sort(),
        readdirSync(join(root, "tools/t")).sort(),
      ])),
    };
  }

  function assertRefused(
    value: ToolSourceProposal,
    previous: ReturnType<typeof seed>,
  ) {
    pinTimes();
    const before = snapshot();
    expect(() => submitToolProposal(root, value, now, previous))
      .toThrow(new Error(REVISION_ERROR));
    expect(snapshot()).toEqual(before);
  }

  it("D2b1 P01", () => {
    const previous = seed();
    assertAccepted(submitToolProposal(root, candidate(A, O, S1), now, previous), "1.3.0", S1);
  });

  it("D2b1 P02", () => {
    const previous = seed();
    assertAccepted(submitToolProposal(root, candidate(B, add(O), S1), now, previous), "1.3.0", S1);
  });

  it("D2b1 P03", () => {
    const previous = seed();
    assertAccepted(submitToolProposal(root, candidate(A, add(O), S1), now, previous), "1.3.0", S1);
  });

  it("D2b1 P04", () => {
    const previous = seed();
    assertAccepted(submitToolProposal(root, candidate(B, O, S1), now, previous), "1.2.4", S1);
  });

  it("D2b1 P05", () => {
    const previous = seed();
    assertRefused(candidate(A, { type: "array" }, S1), previous);
  });

  it("D2b1 P06", () => {
    const previous = seed(numeric("1.0"));
    assertRefused(candidate(JSON.parse(numeric("1")), O, S1), previous);
  });

  it("D2b1 P07", () => {
    const previous = seed(numeric("9007199254740993"));
    assertRefused(candidate(JSON.parse(numeric("9007199254740993")), O, S1), previous);
  });

  it("D2b1 P08", () => {
    const previous = seed();
    const first = submitToolProposal(root, candidate(A, O, S1), now, previous);
    assertAccepted(first, "1.3.0", S1);
    const second = submitToolProposal(root, candidate(A, O, S2), now, first.receipt);
    assertAccepted(second, "1.3.0", S2);
  });

  it("D2b1 P09", () => {
    const previous = seed();
    const first = submitToolProposal(root, candidate(A, O, S1), now, previous);
    assertAccepted(first, "1.3.0", S1);
    const second = submitToolProposal(root, candidate(B, O, S2), now, first.receipt);
    assertAccepted(second, "1.2.4", S2);
  });

  it("D2b1 P11", () => {
    // The refusal rows' detector itself: a same-byte rewrite is seen.
    seed();
    pinTimes();
    const before = snapshot();
    writeFileSync(join(root, "tools/t/tool.py"), readFileSync(join(root, "tools/t/tool.py")));
    expect(snapshot()).not.toEqual(before);
  });

  it("D2b1 P10", () => {
    const previous = seed();
    const first = submitToolProposal(root, candidate(A, O, S1), now, previous);
    assertAccepted(first, "1.3.0", S1);
    assertRefused(candidate({ ...B, properties: {} }, O, S2), first.receipt);
    assertAccepted(first, "1.3.0", S1);
  });
});
