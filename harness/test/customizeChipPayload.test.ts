/**
 * The self-edit approval chip must show EVERY path the server would accept.
 *
 * A display cap below the server's own `MAX_EDITS` ceiling is not cosmetic: it
 * reopens the blind-approval hole the chip exists to close (propose N benign
 * edits, then one for a file nobody asked about; the operator approves what
 * they cannot see, and the args-bound replay executes it). These tests pin the
 * two ceilings together across the language boundary, so raising the server's
 * limit without raising the chip's fails here rather than in production.
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import { describe, expect, it } from "vitest";

import {
  CUSTOMIZE_CHIP_MAX_EDITS,
  customizeChipPayload,
} from "../src/agent/converseLoop.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const LANE_PY = join(HERE, "..", "..", "server", "platform_customize.py");

function serverMaxEdits(): number {
  const src = readFileSync(LANE_PY, "utf8");
  const m = /^MAX_EDITS\s*=\s*(\d+)/m.exec(src);
  if (!m) throw new Error(`MAX_EDITS not found in ${LANE_PY}`);
  return Number(m[1]);
}

describe("customize_platform approval chip", () => {
  it("never displays fewer paths than the server will accept", () => {
    // Read from the lane itself: a drift in either direction is the bug.
    expect(CUSTOMIZE_CHIP_MAX_EDITS).toBe(serverMaxEdits());
  });

  it("lists every path of a maximum-size proposal, including the last", () => {
    const max = serverMaxEdits();
    const edits = Array.from({ length: max }, (_, i) =>
      i === max - 1
        ? { path: "web/src/auth.js", content: "evil" }
        : { path: `console/benign-${i}.jsx`, content: "ok" },
    );
    const payload = customizeChipPayload({ op: "propose", title: "lightmode", edits });

    expect(payload.edit_count).toBe(max);
    const shown = payload.edits as Array<{ path: string }>;
    expect(shown).toHaveLength(max);
    // THE regression this file exists for: the last edit is the dangerous one.
    expect(shown[shown.length - 1]!.path).toBe("web/src/auth.js");
    expect(shown.map((e) => e.path)).toContain("web/src/auth.js");
    // A complete chip carries no incompleteness marker, and never file bytes.
    expect(payload.edits_undisplayed).toBeUndefined();
    expect(JSON.stringify(payload)).not.toContain("evil");
  });

  it("marks the chip incomplete when it cannot show everything", () => {
    // Unreachable through the API (the server refuses past MAX_EDITS), but the
    // marker must exist so the UI can refuse rather than quietly undercount.
    const edits = Array.from({ length: CUSTOMIZE_CHIP_MAX_EDITS + 3 }, (_, i) => ({
      path: `p-${i}.jsx`,
      content: "x",
    }));
    const payload = customizeChipPayload({ op: "propose", title: "t", edits });
    expect(payload.edit_count).toBe(CUSTOMIZE_CHIP_MAX_EDITS + 3);
    expect(payload.edits_undisplayed).toBe(3);
  });

  it("land carries only the change id and the exact commit", () => {
    const payload = customizeChipPayload({
      op: "land",
      change_id: "chg-1",
      commit_sha: "a".repeat(40),
    });
    expect(payload).toEqual({ op: "land", change_id: "chg-1", commit_sha: "a".repeat(40) });
  });
});
