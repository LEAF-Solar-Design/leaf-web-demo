import { describe, expect, it } from "vitest";
import {
  IntegrationDailyWorkflowScheduleV1, IntegrationWorkflowScheduleDraftV1,
  IntegrationManualWorkflowDefinitionV1, SCHEMA_DEFS, emitJsonSchema, emitOpenApi,
} from "../src/index";

const ref = { id: "orders", version: 1, digest: "ab".repeat(32) };
const schedule: IntegrationDailyWorkflowScheduleV1 = {
  kind: "daily", timezone: "America/New_York", hour: 9, minute: 30,
  gap_policy: "next_valid_minute", fold_policy: "first_occurrence",
};
const draft: IntegrationWorkflowScheduleDraftV1 = {
  schema_version: 1, id: "daily-orders", version: 1, title: "Daily orders",
  workflow_ref: ref, schedule, state: "draft",
};

describe("inert daily workflow schedule drafts", () => {
  it("round trips exact calendar keys and boundary values without mutating input", () => {
    for (const timezone of ["UTC", "America/New_York", "Etc/GMT+5", "a", "a".repeat(128)]) {
      for (const hour of [0, 23]) for (const minute of [0, 59]) {
        const input = {
          ...draft, id: "a".repeat(128), version: Number.MAX_SAFE_INTEGER, title: "a".repeat(120),
          workflow_ref: { ...ref, id: "a".repeat(128), version: Number.MAX_SAFE_INTEGER },
          schedule: { ...schedule, timezone, hour, minute },
        };
        const before = JSON.stringify(input);
        Object.freeze(input.workflow_ref);
        Object.freeze(input.schedule);
        Object.freeze(input);
        const parsed = IntegrationWorkflowScheduleDraftV1.parse(input);
        expect(JSON.stringify(parsed)).toBe(before);
        expect(IntegrationWorkflowScheduleDraftV1.parse(JSON.parse(before))).toEqual(input);
        expect(IntegrationDailyWorkflowScheduleV1.parse(input.schedule)).toEqual(input.schedule);
        expect(JSON.stringify(input)).toBe(before);
      }
    }
  });

  it("accepts unknown syntactically valid keys without claiming host timezone support", () => {
    // Recognition and calendar support belong to the host; this is no DST or firing fixture.
    const lexical = { ...schedule, timezone: "Unknown/Calendar_Key.v1+2-3" };
    expect(IntegrationDailyWorkflowScheduleV1.parse(lexical)).toEqual(lexical);
    expect(IntegrationWorkflowScheduleDraftV1.parse({ ...draft, schedule: lexical }).schedule).toEqual(lexical);
  });

  it("requires every outer, calendar and reference field without supplying defaults", () => {
    for (const key of Object.keys(draft)) {
      const missing: Record<string, unknown> = { ...draft };
      delete missing[key];
      expect(IntegrationWorkflowScheduleDraftV1.safeParse(missing).success, key).toBe(false);
      expect(IntegrationWorkflowScheduleDraftV1.safeParse({ ...draft, [key]: undefined }).success, key).toBe(false);
    }
    for (const key of Object.keys(schedule)) {
      const missing: Record<string, unknown> = { ...schedule };
      delete missing[key];
      for (const value of [missing, { ...schedule, [key]: undefined }]) {
        expect(IntegrationDailyWorkflowScheduleV1.safeParse(value).success, key).toBe(false);
        expect(IntegrationWorkflowScheduleDraftV1.safeParse({ ...draft, schedule: value }).success, key).toBe(false);
      }
    }
    for (const key of Object.keys(ref)) {
      const missing: Record<string, unknown> = { ...ref };
      delete missing[key];
      expect(IntegrationWorkflowScheduleDraftV1.safeParse({ ...draft, workflow_ref: missing }).success, key).toBe(false);
    }
  });

  it("rejects malformed timezone keys including trailing newlines and empty segments", () => {
    for (const timezone of [
      "", "a".repeat(129), " UTC", "UTC ", "America/New York", "UTC\n", "UTC\r\n",
      "UTC\t", "UTC\u0000", "UTC\u007f", "/UTC", "UTC/", "America//New_York",
      "America\\New_York", "América/New_York", "UTC:00", "UTC@host", "UTC\u2028",
    ]) {
      const value = { ...schedule, timezone };
      expect(IntegrationDailyWorkflowScheduleV1.safeParse(value).success, timezone).toBe(false);
      expect(IntegrationWorkflowScheduleDraftV1.safeParse({ ...draft, schedule: value }).success, timezone).toBe(false);
    }
  });

  it("rejects fractional, out-of-range and coerced calendar values and wrong policies", () => {
    for (const patch of [
      { hour: -1 }, { hour: 24 }, { hour: 0.5 }, { hour: "9" }, { hour: null },
      { hour: NaN }, { hour: Infinity }, { minute: -1 }, { minute: 60 }, { minute: 0.5 },
      { minute: "30" }, { minute: false }, { minute: Infinity }, { timezone: 0 },
      { kind: "cron" }, { kind: "interval" }, { kind: "startup" },
      { gap_policy: "skip" }, { fold_policy: "second_occurrence" },
    ]) {
      const value = { ...schedule, ...patch };
      expect(IntegrationDailyWorkflowScheduleV1.safeParse(value).success).toBe(false);
      expect(IntegrationWorkflowScheduleDraftV1.safeParse({ ...draft, schedule: value }).success).toBe(false);
    }
  });

  it("reuses existing identity and reference bounds and requires the draft literals", () => {
    for (const patch of [
      { schema_version: 2 }, { schema_version: "1" }, { state: "active" }, { state: "enabled" },
      { id: "" }, { id: "Bad Slug" }, { id: "a".repeat(129) }, { id: "orders\n" },
      { title: "" }, { title: "a".repeat(121) },
      ...[0, -1, 1.5, Number.MAX_SAFE_INTEGER + 1, Infinity, NaN, "1", null].map((version) => ({ version })),
      ...[
        null, {}, { ...ref, id: "" }, { ...ref, id: "a".repeat(129) }, { ...ref, id: "orders/child" },
        { ...ref, version: 0 }, { ...ref, version: 1.5 }, { ...ref, version: Number.MAX_SAFE_INTEGER + 1 },
        { ...ref, digest: "ab".repeat(31) }, { ...ref, digest: "AB".repeat(32) },
        { ...ref, digest: `${ref.digest}\n` }, { ...ref, extra: true },
      ].map((workflow_ref) => ({ workflow_ref })),
    ]) expect(IntegrationWorkflowScheduleDraftV1.safeParse({ ...draft, ...patch }).success).toBe(false);
    expect(IntegrationWorkflowScheduleDraftV1.parse({ ...draft, id: "a", title: "a" }).state).toBe("draft");
  });

  it("rejects authority and execution metadata at both public object boundaries", () => {
    for (const [key, value] of Object.entries({
      extra: true, scope: "tenant", actor: "caller", authority: {}, authority_mode: "unattended",
      grant: {}, grant_revision: 1, enabled: true, active: true, credentials: {}, credential_refs: [],
      provider_instructions: "execute", child_id: "child", run_id: "run", occurrence_cursor: "cursor",
      next_fire_at: "2026-10-23T12:00:00Z", cron: "0 9 * * *", retry: {},
    })) {
      expect(IntegrationWorkflowScheduleDraftV1.safeParse({ ...draft, [key]: value }).success, key).toBe(false);
      const nested = { ...schedule, [key]: value };
      expect(IntegrationDailyWorkflowScheduleV1.safeParse(nested).success, key).toBe(false);
      expect(IntegrationWorkflowScheduleDraftV1.safeParse({ ...draft, schedule: nested }).success, key).toBe(false);
    }
  });

  it("leaves manual definitions manual and caller-bound even when a draft references them", () => {
    const definition: IntegrationManualWorkflowDefinitionV1 = {
      schema_version: 1, id: ref.id, version: ref.version, title: "Orders",
      trigger: "manual", authority_mode: "caller_bound", steps: [{
        id: "create", kind: "action", connection_id: "orders-account",
        connector_ref: ref, operation_ref: ref, arguments: {},
      }],
    };
    expect(IntegrationManualWorkflowDefinitionV1.parse(definition)).toEqual(definition);
    expect(IntegrationWorkflowScheduleDraftV1.parse(draft)).toEqual(draft);
    for (const trigger of ["scheduled", "schedule", "daily", schedule]) {
      expect(IntegrationManualWorkflowDefinitionV1.safeParse({ ...definition, trigger }).success).toBe(false);
    }
    for (const authority_mode of ["unattended", "standing", "scheduled"]) {
      expect(IntegrationManualWorkflowDefinitionV1.safeParse({ ...definition, authority_mode }).success).toBe(false);
    }
    expect(IntegrationManualWorkflowDefinitionV1.safeParse({ ...definition, schedule }).success).toBe(false);
  });

  it("registers the two named definitions in order with resolvable references and no new paths", () => {
    const names = ["IntegrationDailyWorkflowScheduleV1", "IntegrationWorkflowScheduleDraftV1"];
    const registered = Object.keys(SCHEMA_DEFS);
    const start = registered.indexOf(names[0]);
    expect(start).toBeGreaterThanOrEqual(0);
    expect(registered.slice(start, start + names.length)).toEqual(names);
    const bundle = emitJsonSchema() as Record<string, any>;
    const openapi = emitOpenApi() as Record<string, any>;
    for (const [definitions, prefix] of [
      [bundle.$defs, "#/$defs/"], [openapi.components.schemas, "#/components/schemas/"],
    ] as const) {
      expect(Object.keys(definitions)).toEqual(Object.keys(SCHEMA_DEFS));
      expect(definitions.IntegrationWorkflowScheduleDraftV1.properties.workflow_ref).toEqual({ $ref: `${prefix}IntegrationRefV1` });
      expect(definitions.IntegrationWorkflowScheduleDraftV1.properties.schedule).toEqual({ $ref: `${prefix}IntegrationDailyWorkflowScheduleV1` });
      const visit = (node: unknown): void => {
        if (node === null || typeof node !== "object") return;
        for (const [key, value] of Object.entries(node)) {
          if (key === "$ref") {
            expect(typeof value).toBe("string");
            expect((value as string).startsWith(prefix)).toBe(true);
            expect(definitions[(value as string).slice(prefix.length)]).toBeDefined();
          } else visit(value);
        }
      };
      for (const name of names) visit(definitions[name]);
    }
    expect(Object.keys(openapi.paths)).toEqual([
      "/sessions/{session_id}/thread", "/sessions/{session_id}/turns", "/sessions/{session_id}/events",
      "/cards/{card_id}/approve", "/proposals/{proposal_id}", "/worker/jobs/{job_id}/events",
    ]);
  });
});
