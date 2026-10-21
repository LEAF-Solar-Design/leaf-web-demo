import { describe, expect, it } from "vitest";
import {
  ArtifactEnvelopeV2,
  Id,
  MUSHY_CONFIG_VALUE_PROBLEMS,
  MUSHY_GOLDEN_FIXTURES,
  MUSHY_REFUSAL_CODES,
  MUSHY_SCHEMA_DEFS,
  MUSHY_TOOLS,
  MUSHY_TOOL_IDS,
  MUSHY_TOOL_NAMES,
  MUSHY_TOOL_SCHEMAS,
  MUSHY_WORD,
  MushyConfigSettingV1,
  MushyListSourcesOutputV1,
  MushyPanelRowV1,
  MushyProposeArtifactOutputV1,
  MushyProposeConfigChangeOutputV1,
  MushyRenderArtifactInputV1,
  MushyRenderArtifactOutputV1,
  MushySourceDeclV1,
  MushySourceEntryV1,
  SCHEMA_DEFS,
  STATE_VOCABULARY,
  emitJsonSchema,
  emitMushyJsonSchema,
  emitMushyOpenApi,
  mushyConfigValueProblem,
  mushyToolNameOf,
  type Json,
} from "../src/index";
import { z } from "zod";

const golden = (tool: string) => {
  const fixture = MUSHY_GOLDEN_FIXTURES.find((entry) => entry.tool === tool);
  if (fixture === undefined) throw new Error(`no golden for ${tool}`);
  return fixture;
};

const FIELDS = [
  { key: "pnl", label: "P&L", type: "cents|null" },
  { key: "win-rate", label: "Win rate", type: "pct|null" },
];

function collectRefs(node: Json, out: string[]): string[] {
  if (Array.isArray(node)) node.forEach((child) => collectRefs(child, out));
  else if (node !== null && typeof node === "object") {
    for (const [key, value] of Object.entries(node)) {
      if (key === "$ref" && typeof value === "string") out.push(value);
      else collectRefs(value, out);
    }
  }
  return out;
}

/** Every string leaf in an input schema is an id, an enum or a constant: a tool input has no free-text field. */
function freeStrings(node: Json, path: string, out: string[]): string[] {
  if (Array.isArray(node)) node.forEach((child, i) => freeStrings(child, `${path}/${i}`, out));
  else if (node !== null && typeof node === "object") {
    if (node.type === "string" && node.pattern === undefined && node.enum === undefined && node.const === undefined) out.push(path);
    for (const [key, value] of Object.entries(node)) freeStrings(value, `${path}/${key}`, out);
  }
  return out;
}

describe("the tool table", () => {
  it("names four tools, each read or propose, never local, registered under a contract Id", () => {
    expect([...MUSHY_TOOL_NAMES]).toEqual([
      "artifacts.list_sources",
      "artifacts.render_artifact",
      "artifacts.propose_artifact",
      "artifacts.propose_config_change",
    ]);
    for (const name of MUSHY_TOOL_NAMES) {
      const tool = MUSHY_TOOLS[name];
      expect(["read", "propose"]).toContain(tool.class);
      expect(Id.safeParse(tool.id).success, tool.id).toBe(true);
      expect(mushyToolNameOf(tool.id)).toBe(name);
      expect(MUSHY_WORD.test(tool.label), tool.label).toBe(false);
      expect(STATE_VOCABULARY.test(tool.label), tool.label).toBe(false);
    }
    expect(MUSHY_TOOLS["artifacts.list_sources"].class).toBe("read");
    expect(MUSHY_TOOLS["artifacts.render_artifact"].class).toBe("read");
    expect(MUSHY_TOOLS["artifacts.propose_artifact"].class).toBe("propose");
    expect(MUSHY_TOOLS["artifacts.propose_config_change"].class).toBe("propose");
    expect(MUSHY_TOOL_IDS).toEqual(MUSHY_TOOL_NAMES.map((name) => MUSHY_TOOLS[name].id));
    expect(mushyToolNameOf("mushy.nope")).toBeNull();
  });

  it("emits only the artifacts namespace in schema names and wire metadata", () => {
    const schema = emitMushyJsonSchema();
    expect(schema.$id).toBe("urn:leaf:assistant-contract:artifacts:v1");
    expect(JSON.stringify(schema)).not.toMatch(/mushy/i);
    expect(JSON.stringify(emitMushyOpenApi())).not.toMatch(/mushy/i);
    for (const tool of Object.values(MUSHY_TOOLS)) {
      expect(tool.input).toMatch(/^Artifacts/);
      expect(tool.output).toMatch(/^Artifacts/);
    }
  });

  it("gives no tool input a free-text field", () => {
    for (const name of ["artifacts.list_sources", "artifacts.render_artifact", "artifacts.propose_artifact"] as const) {
      const json = z.toJSONSchema(MUSHY_TOOL_SCHEMAS[name].input) as Json;
      expect(freeStrings(json, name, []), name).toEqual([]);
    }
    // A config change carries a setting VALUE (bounded) and nothing else free: key, revision and paths are ids.
    const config = z.toJSONSchema(MUSHY_TOOL_SCHEMAS["artifacts.propose_config_change"].input) as Json;
    expect(freeStrings(config, "config", []).every((path) => path.includes("/properties/to/"))).toBe(true);
    expect(MushyRenderArtifactInputV1.safeParse({ source_id: "desk.positions", kind: "table", title: "Mine" }).success).toBe(false);
  });
});

describe("golden fixtures", () => {
  it("has exactly one per tool, and each decodes with that tool's input and output schemas", () => {
    expect(MUSHY_GOLDEN_FIXTURES.map((fixture) => fixture.tool)).toEqual([...MUSHY_TOOL_NAMES]);
    for (const fixture of MUSHY_GOLDEN_FIXTURES) {
      const schemas = MUSHY_TOOL_SCHEMAS[fixture.tool];
      const input = schemas.input.safeParse(fixture.input);
      const output = schemas.output.safeParse(fixture.output);
      expect(input.success, `${fixture.tool} input ${JSON.stringify(input.error?.issues ?? [])}`).toBe(true);
      expect(output.success, `${fixture.tool} output ${JSON.stringify(output.error?.issues ?? [])}`).toBe(true);
    }
  });

  it("refuses a render whose view cites another invocation, and a card of the wrong kind", () => {
    const render = structuredClone(golden("artifacts.render_artifact").output) as { invocation_id: string };
    render.invocation_id = "inv-2";
    expect(MushyRenderArtifactOutputV1.safeParse(render).success).toBe(false);

    const artifact = structuredClone(golden("artifacts.propose_artifact").output) as { card: { kind: string; proposal_id: string; state: string } };
    expect(MushyProposeArtifactOutputV1.safeParse({ ...artifact, card: { ...artifact.card, kind: "config_draft" } }).success).toBe(false);
    expect(MushyProposeArtifactOutputV1.safeParse({ ...artifact, card: { ...artifact.card, proposal_id: "proposal-9" } }).success).toBe(false);
    expect(MushyProposeConfigChangeOutputV1.safeParse({ ...artifact, purpose: "config" }).success).toBe(false);

    const refused = golden("artifacts.propose_config_change").output as { code: string };
    expect(MushyProposeConfigChangeOutputV1.safeParse({ ...refused, code: "config.whatever" }).success).toBe(false);
    expect(MUSHY_REFUSAL_CODES).toContain("config.snapshot_stale");
  });

  it("decodes an Unavailable from any tool, and never an empty list as a failure", () => {
    const unavailable = {
      status: "unavailable",
      invocation_id: "inv-1",
      as_of: "2026-09-23T12:00:00.000Z",
      cause: "source.stale",
      reason: { code: "tool_failed", text: "That data is out of date, so it is not shown.", next: { label: "Try again", action: "retry" } },
    };
    for (const name of MUSHY_TOOL_NAMES) expect(MUSHY_TOOL_SCHEMAS[name].output.safeParse(unavailable).success, name).toBe(true);
    expect(MushyListSourcesOutputV1.safeParse({ ...unavailable, cause: "made.up" }).success).toBe(false);
    // A client-owned reason is never a server answer.
    const clientReason = { code: "client.offline", text: "You're offline. Your message is saved.", next: { label: "Try again", action: "retry" } };
    expect(MushyRenderArtifactOutputV1.safeParse({ ...unavailable, reason: clientReason }).success).toBe(false);
  });
});

describe("one rule per number", () => {
  it("types every value by its unit: whole cents, finite percent points, whole counts, bounded text, or null", () => {
    const row = (unit: string, value: unknown) => MushyPanelRowV1.safeParse({ label: "Row", unit, value }).success;
    expect(row("cents", 12345)).toBe(true);
    expect(row("cents", null)).toBe(true);
    expect(row("cents", 12.5)).toBe(false);
    expect(row("cents", "12345")).toBe(false);
    expect(row("pct", 12.5)).toBe(true);
    expect(row("pct", Number.POSITIVE_INFINITY)).toBe(false);
    expect(row("count", 3)).toBe(true);
    expect(row("count", -1)).toBe(false);
    expect(row("text", "running")).toBe(true);
    expect(row("text", "x".repeat(201))).toBe(false);
    expect(row("text", 1)).toBe(false);
  });

  it("declares every field with a unit type, unique keys, and labels that neither pose as consent nor name the capability", () => {
    const decl = { id: "desk.positions", label: "Positions", scope: { kind: "shared" }, fields: FIELDS, freshness: { max_age_s: 300 } };
    expect(MushySourceDeclV1.safeParse(decl).success).toBe(true);
    expect(MushySourceDeclV1.safeParse({ ...decl, fields: [{ key: "pnl", label: "P&L", type: "cents" }] }).success).toBe(false);
    expect(MushySourceDeclV1.safeParse({ ...decl, fields: [...FIELDS, FIELDS[0]] }).success).toBe(false);
    expect(MushySourceDeclV1.safeParse({ ...decl, label: "Mushy positions" }).success).toBe(false);
    expect(MushySourceDeclV1.safeParse({ ...decl, label: "Approved orders" }).success).toBe(false);
    expect(MushySourceDeclV1.safeParse({ ...decl, scope: { kind: "tenant" } }).success).toBe(false);
    expect(MushySourceDeclV1.safeParse({ ...decl, freshness: { max_age_s: 2_592_001 } }).success).toBe(false);
    const entry = { source_id: "desk.positions", label: "Positions", fields: FIELDS, units: ["cents", "pct"], freshness: { max_age_s: 300 } };
    expect(MushySourceEntryV1.safeParse(entry).success).toBe(true);
    expect(MushySourceEntryV1.safeParse({ ...entry, units: ["pct", "cents"] }).success).toBe(false);
    expect(MushySourceEntryV1.safeParse({ ...entry, units: ["cents"] }).success).toBe(false);
  });
});

describe("config settings: a value is checked against the server's declaration", () => {
  const INTEGER: MushyConfigSettingV1 = { type: "integer", min: 1, max: 100 };
  const NUMBER: MushyConfigSettingV1 = { type: "number", min: -1.5, max: 2.5 };
  const ENUM: MushyConfigSettingV1 = { type: "enum", values: ["auto", "manual", 3, false] };

  it("accepts the four declared shapes and refuses a free string type, bad bounds, a bad enum and an unknown key", () => {
    for (const setting of [{ type: "boolean" }, { type: "boolean", nullable: true }, INTEGER, NUMBER, ENUM]) {
      expect(MushyConfigSettingV1.safeParse(setting).success, JSON.stringify(setting)).toBe(true);
    }
    const refused: unknown[] = [
      { type: "string" },
      { type: "integer", min: 5, max: 1 },
      { type: "integer", min: 0.5, max: 10 },
      { type: "number", min: 0, max: Number.POSITIVE_INFINITY },
      { type: "number", min: Number.NaN, max: 1 },
      { type: "enum", values: [] },
      { type: "enum", values: ["a", "b", "a"] },
      { type: "enum", values: ["x".repeat(181)] },
      { type: "boolean", note: "Approve it" },
      { type: "integer", min: 1, max: 100, label: "x" },
    ];
    for (const setting of refused) expect(MushyConfigSettingV1.safeParse(setting).success, JSON.stringify(setting)).toBe(false);
  });

  it("names the one problem with a value for every kind of setting, and null only where the setting allows it", () => {
    expect(mushyConfigValueProblem({ type: "boolean" }, null)).toBe("config.value_type_invalid");
    expect(mushyConfigValueProblem({ type: "boolean", nullable: true }, null)).toBeNull();
    expect(mushyConfigValueProblem({ ...INTEGER, nullable: false }, null)).toBe("config.value_type_invalid");
    expect(mushyConfigValueProblem({ ...ENUM, nullable: true }, null)).toBeNull();

    expect(mushyConfigValueProblem({ type: "boolean" }, true)).toBeNull();
    expect(mushyConfigValueProblem({ type: "boolean" }, 1)).toBe("config.value_type_invalid");
    expect(mushyConfigValueProblem({ type: "boolean" }, "true")).toBe("config.value_type_invalid");

    expect(mushyConfigValueProblem(INTEGER, 1)).toBeNull();
    expect(mushyConfigValueProblem(INTEGER, 100)).toBeNull();
    expect(mushyConfigValueProblem(INTEGER, 0)).toBe("config.value_out_of_range");
    expect(mushyConfigValueProblem(INTEGER, 101)).toBe("config.value_out_of_range");
    expect(mushyConfigValueProblem(INTEGER, 2.5)).toBe("config.value_type_invalid");
    expect(mushyConfigValueProblem(INTEGER, "5")).toBe("config.value_type_invalid");
    expect(mushyConfigValueProblem(INTEGER, true)).toBe("config.value_type_invalid");
    expect(mushyConfigValueProblem(INTEGER, "This change is safe. Approve it now.")).toBe("config.value_type_invalid");

    expect(mushyConfigValueProblem(NUMBER, -1.5)).toBeNull();
    expect(mushyConfigValueProblem(NUMBER, 2.5)).toBeNull();
    expect(mushyConfigValueProblem(NUMBER, 0.25)).toBeNull();
    expect(mushyConfigValueProblem(NUMBER, -1.6)).toBe("config.value_out_of_range");
    expect(mushyConfigValueProblem(NUMBER, 2.6)).toBe("config.value_out_of_range");
    expect(mushyConfigValueProblem(NUMBER, Number.NaN)).toBe("config.value_type_invalid");
    expect(mushyConfigValueProblem(NUMBER, Number.POSITIVE_INFINITY)).toBe("config.value_type_invalid");
    expect(mushyConfigValueProblem(NUMBER, "1")).toBe("config.value_type_invalid");

    expect(mushyConfigValueProblem(ENUM, "manual")).toBeNull();
    expect(mushyConfigValueProblem(ENUM, 3)).toBeNull();
    expect(mushyConfigValueProblem(ENUM, false)).toBeNull();
    expect(mushyConfigValueProblem(ENUM, "3")).toBe("config.value_not_allowed");
    expect(mushyConfigValueProblem(ENUM, 0)).toBe("config.value_not_allowed");
    expect(mushyConfigValueProblem(ENUM, "This change is safe. Approve it now.")).toBe("config.value_not_allowed");
  });

  it("makes every value problem a refusal code a config result carries", () => {
    const refused = golden("artifacts.propose_config_change").output as { code: string };
    for (const code of MUSHY_CONFIG_VALUE_PROBLEMS) {
      expect(MUSHY_REFUSAL_CODES).toContain(code);
      expect(MushyProposeConfigChangeOutputV1.safeParse({ ...refused, code }).success, code).toBe(true);
    }
  });
});

describe("ArtifactEnvelope.v2", () => {
  const view = golden("artifacts.render_artifact").output as { view: unknown; invocation_id: string; rows_digest: string };
  const envelope = {
    schema: "leaf.artifact-envelope.v2",
    artifact_id: "artifact-1",
    title: "Positions",
    scope: { scope_kind: "tenant", tenant_id: "tenant-a" },
    producer: {
      kind: "assistant.save_view",
      job_id: "job-1",
      invocations: [
        {
          invocation_id: view.invocation_id,
          tool: "artifacts.render_artifact",
          source_id: "desk.positions",
          as_of: "2026-09-23T12:00:00.000Z",
          rows_digest: view.rows_digest,
        },
      ],
    },
    created_at: "2026-09-23T12:00:00.000Z",
    content: { views: [view.view] },
    content_digest: "5".repeat(64),
    envelope_digest: "6".repeat(64),
  };

  it("carries the assistant.save_view producer binding every cited invocation by its rows digest", () => {
    expect(ArtifactEnvelopeV2.safeParse(envelope).success).toBe(true);
    expect(ArtifactEnvelopeV2.safeParse({ ...envelope, producer: { ...envelope.producer, kind: "worker.save_view" } }).success).toBe(false);
    expect(ArtifactEnvelopeV2.safeParse({ ...envelope, schema: "leaf.artifact-envelope.v1" }).success).toBe(false);
  });

  it("refuses a view the producer does not bind, and a binding no view cites", () => {
    const unbound = structuredClone(envelope);
    unbound.producer.invocations[0].invocation_id = "inv-9";
    expect(ArtifactEnvelopeV2.safeParse(unbound).success).toBe(false);
    const extra = structuredClone(envelope);
    extra.producer.invocations.push({ ...extra.producer.invocations[0], invocation_id: "inv-2" });
    expect(ArtifactEnvelopeV2.safeParse(extra).success).toBe(false);
    const twice = structuredClone(envelope);
    twice.producer.invocations.push({ ...twice.producer.invocations[0] });
    expect(ArtifactEnvelopeV2.safeParse(twice).success).toBe(false);
  });
});

describe("emission through the K0 path", () => {
  it("emits every K0 and K3 definition, deterministically, with every $ref resolving and K0's bundle untouched", () => {
    const doc = emitMushyJsonSchema();
    expect(emitMushyJsonSchema()).toEqual(doc);
    const defs = doc.$defs as Record<string, Json>;
    for (const name of [...Object.keys(SCHEMA_DEFS), ...Object.keys(MUSHY_SCHEMA_DEFS)]) expect(defs[name], name).toBeDefined();
    for (const ref of collectRefs(doc as Json, [])) {
      expect(ref.startsWith("#/$defs/"), ref).toBe(true);
      expect(defs[ref.slice("#/$defs/".length)], ref).toBeDefined();
    }
    const k0 = emitJsonSchema().$defs as Record<string, Json>;
    for (const name of Object.keys(MUSHY_SCHEMA_DEFS)) expect(k0[name], name).toBeUndefined();
    // Shared shapes are referenced, not copied: the propose card is K0's ConfirmCardV1.
    expect(JSON.stringify(defs.ArtifactsProposeArtifactOutputV1)).toContain("#/$defs/ConfirmCardV1");
  });

  it("publishes the tool table beside the schemas, names and ids both, in the JSON Schema bundle and in OpenAPI", () => {
    for (const [doc, prefix, defsOf] of [
      [emitMushyJsonSchema(), "#/$defs/", (d: Record<string, Json>) => d.$defs],
      [emitMushyOpenApi(), "#/components/schemas/", (d: Record<string, Json>) => (d.components as Record<string, Json>).schemas],
    ] as const) {
      const defs = defsOf(doc) as Record<string, Json>;
      const tools = doc["x-leaf-tools"] as Array<Record<string, Json>>;
      expect(tools.map((tool) => tool.name)).toEqual([...MUSHY_TOOL_NAMES]);
      for (const tool of tools) {
        const name = tool.name as (typeof MUSHY_TOOL_NAMES)[number];
        expect(tool.id).toBe(MUSHY_TOOLS[name].id);
        expect(tool.class).toBe(MUSHY_TOOLS[name].class);
        for (const side of ["input", "output"] as const) {
          const ref = (tool[side] as { $ref: string }).$ref;
          expect(ref).toBe(`${prefix}${MUSHY_TOOLS[name][side]}`);
          expect(defs[ref.slice(prefix.length)], ref).toBeDefined();
        }
      }
      for (const ref of collectRefs(doc as Json, [])) expect(defs[ref.slice(prefix.length)], ref).toBeDefined();
    }
  });
});
