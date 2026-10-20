import { createHash } from "node:crypto";
import { mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { GoldenConversationV1, ThreadV1 } from "../src/index";

// The seven golden conversations every consumer (TS and Swift) decodes at its
// pin (K-D8). FIXTURES.sha256 is sha256sum-compatible and hashes the
// LF-normalized bytes, so a CRLF checkout on Windows recomputes the same pin.

const fixturesDir = join(dirname(fileURLToPath(import.meta.url)), "..", "fixtures");
const pinPath = join(fixturesDir, "FIXTURES.sha256");
const WRITE = process.env.ASSISTANT_CONTRACT_WRITE === "1";

const GOLDEN_NAMES = [
  "idle",
  "answer-with-view",
  "tool-failed",
  "card-approved",
  "card-expired",
  "rate-limited",
  "worker-offline",
] as const;

function readLf(path: string): string {
  return readFileSync(path, "utf8").replace(/\r\n/g, "\n");
}

function recomputePin(): string {
  const files = GOLDEN_NAMES.map((name) => `${name}.json`).sort();
  const lines = files.map((file) => {
    const hex = createHash("sha256").update(readLf(join(fixturesDir, file)), "utf8").digest("hex");
    return `${hex}  ${file}`;
  });
  return `${lines.join("\n")}\n`;
}

/** On a mismatch, leave the recomputed bytes where a reviewer can diff them; never touch the tree. */
function dump(file: string, content: string): string {
  const dir = join(tmpdir(), "assistant-contract-emitted");
  mkdirSync(dir, { recursive: true });
  const path = join(dir, file);
  writeFileSync(path, content);
  return path;
}

describe("golden conversations", () => {
  it("the fixture directory holds exactly the seven pinned conversations", () => {
    const present = readdirSync(fixturesDir)
      .filter((file) => file.endsWith(".json"))
      .sort();
    expect(present).toEqual(GOLDEN_NAMES.map((name) => `${name}.json`).sort());
  });

  it("FIXTURES.sha256 recomputes", () => {
    const expected = recomputePin();
    if (WRITE) writeFileSync(pinPath, expected);
    const committed = readLf(pinPath);
    const where = committed === expected ? "" : dump("FIXTURES.sha256", expected);
    expect(committed, `recomputed pin written to ${where}`).toBe(expected);
  });

  it.each(GOLDEN_NAMES)("%s decodes as GoldenConversationV1", (name) => {
    const raw = JSON.parse(readLf(join(fixturesDir, `${name}.json`)));
    const result = GoldenConversationV1.safeParse(raw);
    expect(result.success, JSON.stringify(result.error?.issues, null, 2)).toBe(true);
    expect(result.data?.name).toBe(name);
    expect(ThreadV1.safeParse(raw.thread).success).toBe(true);
  });
});
