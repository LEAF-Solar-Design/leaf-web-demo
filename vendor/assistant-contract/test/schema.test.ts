import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  ApproveRequestV1,
  clientReason,
  emitJsonSchema,
  emitOpenApi,
  ErrorV1,
  EventsPageV1,
  GoldenConversationV1,
  type Json,
  ProposalV1,
  REASON_ACTIONS,
  REASON_CODES,
  REASON_TABLE,
  ReasonV1,
  SCHEMA_DEFS,
  serialize,
  ServerReasonV1,
  TurnRequestV1,
  WorkerEventsBatchV1,
  WorkerEventV1,
} from "../src/index";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
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

function readJson(...parts: string[]): unknown {
  return JSON.parse(readFileSync(join(root, ...parts), "utf8"));
}

function dump(file: string, content: string): string {
  const dir = join(tmpdir(), "assistant-contract-emitted");
  mkdirSync(dir, { recursive: true });
  const path = join(dir, file);
  writeFileSync(path, content);
  return path;
}

/** Deep-equal on parsed JSON, so line endings and key order never fail the diff; content always does. */
function expectCommitted(file: string, emitted: Json): void {
  const path = join(root, "schema", file);
  if (WRITE) writeFileSync(path, serialize(emitted));
  const committed = JSON.parse(readFileSync(path, "utf8"));
  const same = JSON.stringify(sortKeys(committed)) === JSON.stringify(sortKeys(emitted));
  const where = same ? "" : dump(file, serialize(emitted));
  expect(committed, `emitted ${file} differs from the committed file; emitted copy at ${where}`).toEqual(emitted);
}

function sortKeys(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortKeys);
  if (value !== null && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return Object.fromEntries(Object.keys(record).sort().map((key) => [key, sortKeys(record[key])]));
  }
  return value;
}

function collectRefs(value: unknown, out: string[] = []): string[] {
  if (Array.isArray(value)) value.forEach((child) => collectRefs(child, out));
  else if (value !== null && typeof value === "object") {
    for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
      if (key === "$ref" && typeof child === "string") out.push(child);
      else collectRefs(child, out);
    }
  }
  return out;
}

function resolvePointer(doc: unknown, ref: string): unknown {
  expect(ref.startsWith("#/"), `${ref} is a local reference`).toBe(true);
  return ref
    .slice(2)
    .split("/")
    .reduce<unknown>((node, segment) => (node as Record<string, unknown> | undefined)?.[segment], doc);
}

const HEX = "ab".repeat(32);
const common = {
  proposal_id: "prop-1",
  session_id: "sess-1",
  turn_id: "turn-1",
  created_at: "2026-09-23T16:00:00Z",
  expires_at: "2026-09-23T16:05:00Z",
  proposal_digest: HEX,
};

describe("emission", () => {
  it("the emitted JSON Schema equals schema/assistant.schema.json", () => {
    expectCommitted("assistant.schema.json", emitJsonSchema());
  });

  it("the emitted OpenAPI 3.1 document equals schema/openapi.json", () => {
    expectCommitted("openapi.json", emitOpenApi());
  });

  it("emission is deterministic", () => {
    expect(serialize(emitOpenApi())).toBe(serialize(emitOpenApi()));
    expect(serialize(emitJsonSchema())).toBe(serialize(emitJsonSchema()));
  });

  it("every $ref resolves and every definition is emitted", () => {
    const openapi = emitOpenApi() as Record<string, any>;
    const bundle = emitJsonSchema() as Record<string, any>;
    expect(openapi.openapi).toBe("3.1.0");
    for (const name of Object.keys(SCHEMA_DEFS)) {
      expect(openapi.components.schemas[name], name).toBeDefined();
      expect(bundle.$defs[name], name).toBeDefined();
    }
    for (const doc of [openapi, bundle]) {
      const refs = collectRefs(doc);
      expect(refs.length).toBeGreaterThan(0);
      for (const ref of refs) expect(resolvePointer(doc, ref), ref).toBeDefined();
    }
  });
});

describe("round trips", () => {
  it.each(GOLDEN_NAMES)("%s decodes, re-encodes to the same JSON and decodes again", (name) => {
    const raw = readJson("fixtures", `${name}.json`);
    const decoded = GoldenConversationV1.parse(raw);
    const encoded = JSON.parse(JSON.stringify(decoded));
    expect(encoded).toEqual(raw);
    expect(GoldenConversationV1.parse(encoded)).toEqual(decoded);
  });

  it("every standalone request and record round trips", () => {
    const samples: Array<[{ parse: (value: unknown) => unknown }, unknown]> = [
      [ApproveRequestV1, { card_digest: HEX, request_id: "req-1" }],
      [ApproveRequestV1, { card_digest: HEX, request_id: "req-1", presence: { method: "face_id", assertion: "a", asserted_at: "2026-09-24T12:00:00Z" } }],
      [TurnRequestV1, { request_id: "req-2", text: "How did the books do today?" }],
      [
        ErrorV1,
        {
          reason: {
            code: "card_digest_mismatch",
            text: "This request changed. Refresh to see it.",
            next: { label: "Refresh", action: "refresh" },
          },
        },
      ],
      [ProposalV1, { ...common, kind: "control", control: { action: "engine.halt", target_id: "eth-15m" } }],
      [ProposalV1, { ...common, kind: "build", build: { recipe_id: "recipe-7", max_rounds: 24 } }],
      [
        ProposalV1,
        { ...common, kind: "config_draft", config_draft: { config_key: "risk.daily_loss", base_snapshot_digest: HEX, draft_digest: HEX } },
      ],
      [
        ProposalV1,
        { ...common, kind: "slot_promotion", slot_promotion: { slot_id: "slot-1", artifact_id: "art-1", artifact_digest: HEX } },
      ],
      [
        WorkerEventsBatchV1,
        {
          events: [
            { job_id: "job-1", seq: 1, type: "status", status: "thinking" },
            { job_id: "job-1", seq: 2, type: "tool", tool_call_id: "call-1", name: "books.summary", state: "running" },
            { job_id: "job-1", seq: 3, type: "text", text: "Checking." },
            { job_id: "job-1", seq: 4, type: "card_ref", card_id: "card-1" },
            { job_id: "job-1", seq: 5, type: "artifact_ref", artifact_id: "art-1" },
            { job_id: "job-1", seq: 6, type: "error", code: "worker_offline" },
          ],
        },
      ],
      [EventsPageV1, { events: (readJson("fixtures", "card-approved.json") as { events: unknown[] }).events, next_after: 6 }],
    ];
    for (const [schema, sample] of samples) {
      const decoded = schema.parse(sample);
      expect(JSON.parse(JSON.stringify(decoded))).toEqual(sample);
    }
  });
});

describe("reason table", () => {
  it("every code offers exactly one known exit", () => {
    expect(REASON_CODES.length).toBe(Object.keys(REASON_TABLE).length);
    for (const code of REASON_CODES) expect(REASON_ACTIONS).toContain(REASON_TABLE[code].action);
  });

  it("the client owns text only for client.offline and client.timeout", () => {
    const clientCodes = REASON_CODES.filter((code) => REASON_TABLE[code].owner === "client");
    expect(clientCodes.sort()).toEqual(["client.offline", "client.timeout"]);
    for (const code of ["client.offline", "client.timeout"] as const) {
      const reason = clientReason(code);
      expect(ReasonV1.safeParse(reason).success).toBe(true);
      expect(ServerReasonV1.safeParse(reason).success).toBe(false);
    }
    expect(() => clientReason("rate_limited" as "client.offline")).toThrow();
  });

  it("a rate-limited reason carries its reset time and no other code may", () => {
    const base = { code: "rate_limited", text: "You can ask again at 4:12 PM CT.", next: { label: "Wait", action: "wait" } };
    expect(ServerReasonV1.safeParse(base).success).toBe(false);
    expect(ServerReasonV1.safeParse({ ...base, retry_at: "2026-09-23T21:12:00Z" }).success).toBe(true);
    expect(
      ServerReasonV1.safeParse({
        code: "tool_failed",
        text: "Couldn't read that.",
        next: { label: "Try again", action: "retry" },
        retry_at: "2026-09-23T21:12:00Z",
      }).success,
    ).toBe(false);
    expect(ServerReasonV1.safeParse({ ...base, retry_at: "2026-09-23T21:12:00Z", next: { label: "Retry", action: "retry" } }).success).toBe(
      false,
    );
  });

  it("worker events validate one at a time too", () => {
    expect(WorkerEventV1.safeParse({ job_id: "job-1", seq: 1, type: "status", status: "streaming" }).success).toBe(true);
  });
});
