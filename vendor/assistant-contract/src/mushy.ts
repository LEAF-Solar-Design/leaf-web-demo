import { z } from "zod";
import { ConfirmCardV1 } from "./card";
import { CONTRACT_VERSION, SCHEMA_DEFS, type Json } from "./emit";
import { Id, IsoUtc, Label, Sha256Hex, ServerReasonV1 } from "./reason";
import { BlockV1, STATE_VOCABULARY } from "./thread";

// Mushy as a capability of the assistant server (K3, VI.3, K-D4, K-D5, K-D10).
// Mushy reaches the assistant only as four registered tools, each `read` or
// `propose`, never `local`. Every string a tool result carries is either an id
// or server-authored text (a declared source's labels, the server's reason
// table, a K2 card); no tool input has a free-text field. The one unpatterned
// input string, a config change's value, is checked against the server-declared
// setting before any card renders, and a string reaches a card only as a
// member of a declared enum, so no tool result can carry model prose. Tool
// names are internal: the word "Mushy" never appears in
// user-visible copy, so a declared label that says it is refused at decode.

// ---------------------------------------------------------------------------
// Tools
// ---------------------------------------------------------------------------

export const MUSHY_TOOL_NAMES = [
  "artifacts.list_sources",
  "artifacts.render_artifact",
  "artifacts.propose_artifact",
  "artifacts.propose_config_change",
] as const;
export const MushyToolName = z.enum(MUSHY_TOOL_NAMES);
export type MushyToolName = z.infer<typeof MushyToolName>;

/**
 * Tool metadata the server registers with and the emitters publish. Labels are
 * user visible and never say "Mushy". `id` is the name the tool registers and
 * travels under (worker tool events, ToolCallV1, tool_invocations): the
 * contract's Id has no underscore, so the registered id hyphenates the name.
 */
export const MUSHY_TOOLS = {
  "artifacts.list_sources": {
    id: "artifacts.list-sources",
    class: "read",
    label: "Data sources",
    input: "ArtifactsListSourcesInputV1",
    output: "ArtifactsListSourcesOutputV1",
  },
  "artifacts.render_artifact": {
    id: "artifacts.render-artifact",
    class: "read",
    label: "Data view",
    input: "ArtifactsRenderArtifactInputV1",
    output: "ArtifactsRenderArtifactOutputV1",
  },
  "artifacts.propose_artifact": {
    id: "artifacts.propose-artifact",
    class: "propose",
    label: "Save a view",
    input: "ArtifactsProposeArtifactInputV1",
    output: "ArtifactsProposeArtifactOutputV1",
  },
  "artifacts.propose_config_change": {
    id: "artifacts.propose-config-change",
    class: "propose",
    label: "Draft a setting change",
    input: "ArtifactsProposeConfigChangeInputV1",
    output: "ArtifactsProposeConfigChangeOutputV1",
  },
} as const satisfies Record<
  MushyToolName,
  { id: string; class: "read" | "propose"; label: string; input: string; output: string }
>;

/** The registered ids, in MUSHY_TOOL_NAMES order: what a role policy grants. */
export const MUSHY_TOOL_IDS: readonly string[] = Object.freeze(MUSHY_TOOL_NAMES.map((name) => MUSHY_TOOLS[name].id));

/** The tool name for a registered id, or null. */
export function mushyToolNameOf(id: string): MushyToolName | null {
  for (const name of MUSHY_TOOL_NAMES) if (MUSHY_TOOLS[name].id === id) return name;
  return null;
}

/** K-D10: the internal name never reaches user-visible copy. */
export const MUSHY_WORD = /mushy/i;

// ---------------------------------------------------------------------------
// Units: one rule per number, enforced by type
// ---------------------------------------------------------------------------

export const MUSHY_UNITS = ["cents", "pct", "count", "text"] as const;
export const MushyUnit = z.enum(MUSHY_UNITS);
export type MushyUnit = z.infer<typeof MushyUnit>;

/**
 * A field's type names its unit, and every value is that unit or null. A
 * missing number is null, never 0. `cents` is a whole number of cents, `pct`
 * is percent points (12.5 means 12.5%), `count` is a whole number >= 0 and
 * `text` is a string of at most 200 characters.
 */
export const MUSHY_FIELD_TYPES = ["cents|null", "pct|null", "count|null", "text|null"] as const;
export const MushyFieldType = z.enum(MUSHY_FIELD_TYPES);
export type MushyFieldType = z.infer<typeof MushyFieldType>;

export const MUSHY_FIELD_UNIT: Readonly<Record<MushyFieldType, MushyUnit>> = Object.freeze({
  "cents|null": "cents",
  "pct|null": "pct",
  "count|null": "count",
  "text|null": "text",
});

export const MAX_MUSHY_TEXT_VALUE = 200;
export const MAX_MUSHY_PCT = 1_000_000;

/** Null when `value` obeys the unit's one rule; pure, never throws. */
export function mushyValueProblem(unit: MushyUnit, value: unknown): string | null {
  if (value === null) return null;
  switch (unit) {
    case "cents":
      return typeof value === "number" && Number.isSafeInteger(value) ? null : "a cents value is a whole number of cents or null";
    case "pct":
      return typeof value === "number" && Number.isFinite(value) && Math.abs(value) <= MAX_MUSHY_PCT
        ? null
        : "a pct value is a finite number of percent points or null";
    case "count":
      return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? null : "a count value is a whole number >= 0 or null";
    case "text":
      return typeof value === "string" && value.length <= MAX_MUSHY_TEXT_VALUE
        ? null
        : `a text value is a string of at most ${MAX_MUSHY_TEXT_VALUE} characters or null`;
  }
}

/** The slice of a zod refinement context the shared checks below use. */
type IssueSink = { addIssue(issue: { code: "custom"; path: PropertyKey[]; message: string }): void };

function labelProblem(label: string): string | null {
  if (STATE_VOCABULARY.test(label)) return "a label may not use the consent vocabulary";
  if (MUSHY_WORD.test(label)) return "a label may not name the internal capability";
  return null;
}

// ---------------------------------------------------------------------------
// Source declarations (K-D4)
// ---------------------------------------------------------------------------

/** owner: the owner scope only. tenant: that one tenant only. shared: every scope, each reading only its own rows. */
export const MushySourceScopeV1 = z.union([
  z.strictObject({ kind: z.literal("owner") }),
  z.strictObject({ kind: z.literal("tenant"), tenant_id: Id }),
  z.strictObject({ kind: z.literal("shared") }),
]);
export type MushySourceScopeV1 = z.infer<typeof MushySourceScopeV1>;

export const MushySourceFieldV1 = z
  .strictObject({
    key: Id,
    label: Label,
    type: MushyFieldType,
  })
  .superRefine((field, ctx) => {
    const problem = labelProblem(field.label);
    if (problem !== null) ctx.addIssue({ code: "custom", path: ["label"], message: problem });
  });
export type MushySourceFieldV1 = z.infer<typeof MushySourceFieldV1>;

/** Thirty days: the longest freshness contract a source may declare. */
export const MAX_MUSHY_MAX_AGE_S = 2_592_000;
export const MAX_MUSHY_SOURCE_FIELDS = 32;

export const MushyFreshnessV1 = z.strictObject({
  max_age_s: z.number().int().min(1).max(MAX_MUSHY_MAX_AGE_S),
});

function fieldsProblem(fields: readonly MushySourceFieldV1[], ctx: IssueSink, path: string): void {
  const keys = new Set<string>();
  fields.forEach((field, index) => {
    if (keys.has(field.key)) ctx.addIssue({ code: "custom", path: [path, index, "key"], message: `duplicate field ${field.key}` });
    keys.add(field.key);
  });
}

/** What `defineSource` declares, minus the reader: id, label, scope, typed fields and the freshness contract. */
export const MushySourceDeclV1 = z
  .strictObject({
    id: Id,
    label: Label,
    scope: MushySourceScopeV1,
    fields: z.array(MushySourceFieldV1).min(1).max(MAX_MUSHY_SOURCE_FIELDS),
    freshness: MushyFreshnessV1,
  })
  .superRefine((decl, ctx) => {
    const problem = labelProblem(decl.label);
    if (problem !== null) ctx.addIssue({ code: "custom", path: ["label"], message: problem });
    fieldsProblem(decl.fields, ctx, "fields");
  });
export type MushySourceDeclV1 = z.infer<typeof MushySourceDeclV1>;

/** The distinct units of a field list, in MUSHY_UNITS order. */
export function mushyUnitsOf(fields: readonly { readonly type: MushyFieldType }[]): MushyUnit[] {
  const present = new Set(fields.map((field) => MUSHY_FIELD_UNIT[field.type]));
  return MUSHY_UNITS.filter((unit) => present.has(unit));
}

/** One catalog entry: what a caller may ask about, with freshness and units. */
export const MushySourceEntryV1 = z
  .strictObject({
    source_id: Id,
    label: Label,
    fields: z.array(MushySourceFieldV1).min(1).max(MAX_MUSHY_SOURCE_FIELDS),
    units: z.array(MushyUnit).min(1).max(MUSHY_UNITS.length),
    freshness: MushyFreshnessV1,
  })
  .superRefine((entry, ctx) => {
    fieldsProblem(entry.fields, ctx, "fields");
    if (JSON.stringify(entry.units) !== JSON.stringify(mushyUnitsOf(entry.fields))) {
      ctx.addIssue({ code: "custom", path: ["units"], message: "units are exactly the distinct units of the fields, in unit order" });
    }
  });
export type MushySourceEntryV1 = z.infer<typeof MushySourceEntryV1>;

export const MAX_MUSHY_SOURCES = 256;
export const ScopeKindV1 = z.enum(["owner", "tenant"]);

// ---------------------------------------------------------------------------
// Unavailable: every tool's typed failure
// ---------------------------------------------------------------------------

/** Why a call came back Unavailable. It renders as Unavailable, never as an empty success (K-D4). */
export const MUSHY_UNAVAILABLE_CAUSES = [
  "source.not_found",
  "source.field_unknown",
  "source.unavailable",
  "source.stale",
  "source.empty",
  "source.result_invalid",
  "tool.threw",
  "tool.timeout",
  "tool.result_invalid",
] as const;
export const MushyUnavailableCause = z.enum(MUSHY_UNAVAILABLE_CAUSES);
export type MushyUnavailableCause = z.infer<typeof MushyUnavailableCause>;

/** A recorded call that could not answer. Any tool may return it; the reason is server text. */
export const MushyUnavailableV1 = z.strictObject({
  status: z.literal("unavailable"),
  invocation_id: Id,
  as_of: IsoUtc,
  cause: MushyUnavailableCause,
  reason: ServerReasonV1,
});
export type MushyUnavailableV1 = z.infer<typeof MushyUnavailableV1>;

// ---------------------------------------------------------------------------
// artifacts.list_sources (read)
// ---------------------------------------------------------------------------

/** No arguments: the caller's scope comes from the job claim, never from the model. */
export const MushyListSourcesInputV1 = z.strictObject({});
export type MushyListSourcesInputV1 = z.infer<typeof MushyListSourcesInputV1>;

/** The catalog a scope may ask about, in declaration order. */
export const MushySourceListV1 = z
  .strictObject({
    status: z.literal("ok"),
    scope_kind: ScopeKindV1,
    sources: z.array(MushySourceEntryV1).max(MAX_MUSHY_SOURCES),
  })
  .superRefine((output, ctx) => {
    const ids = new Set<string>();
    output.sources.forEach((source, index) => {
      if (ids.has(source.source_id)) {
        ctx.addIssue({ code: "custom", path: ["sources", index, "source_id"], message: `duplicate source ${source.source_id}` });
      }
      ids.add(source.source_id);
    });
  });
export type MushySourceListV1 = z.infer<typeof MushySourceListV1>;

export const MushyListSourcesOutputV1 = z.union([MushySourceListV1, MushyUnavailableV1]);
export type MushyListSourcesOutputV1 = z.infer<typeof MushyListSourcesOutputV1>;

// ---------------------------------------------------------------------------
// artifacts.render_artifact (read; replaces show_view)
// ---------------------------------------------------------------------------

export const MUSHY_VIEW_KINDS = ["metric", "table", "line"] as const;
export const MAX_MUSHY_VIEW_FIELDS = 8;
/** The K0 view panel holds at most 50 rows. */
export const MAX_MUSHY_VIEW_ROWS = 50;

/** No title and no prose: the server titles the view with the source's declared label. */
export const MushyRenderArtifactInputV1 = z
  .strictObject({
    source_id: Id,
    kind: z.enum(MUSHY_VIEW_KINDS),
    fields: z.array(Id).min(1).max(MAX_MUSHY_VIEW_FIELDS).optional(),
    limit: z.number().int().min(1).max(MAX_MUSHY_VIEW_ROWS).optional(),
  })
  .superRefine((input, ctx) => {
    if (input.fields !== undefined && new Set(input.fields).size !== input.fields.length) {
      ctx.addIssue({ code: "custom", path: ["fields"], message: "each field is named once" });
    }
  });
export type MushyRenderArtifactInputV1 = z.infer<typeof MushyRenderArtifactInputV1>;

/** One projected row: a K0 view row before the server stamps its invocation id. The unit is never null. */
export const MushyPanelRowV1 = z
  .strictObject({
    label: Label,
    value: z.union([z.number(), z.string().max(MAX_MUSHY_TEXT_VALUE), z.null()]),
    unit: MushyUnit,
  })
  .superRefine((row, ctx) => {
    const problem = mushyValueProblem(row.unit, row.value);
    if (problem !== null) ctx.addIssue({ code: "custom", path: ["value"], message: problem });
  });
export type MushyPanelRowV1 = z.infer<typeof MushyPanelRowV1>;

/**
 * What artifacts.render_artifact returns to the registry, and so what its rows
 * digest covers: the server's projection of one declared source into the K0
 * view panel shape. The view is this projection stamped with the recorded
 * invocation id.
 */
export const MushyProjectionV1 = z
  .strictObject({
    source_id: Id,
    kind: z.enum(MUSHY_VIEW_KINDS),
    title: Label,
    as_of: IsoUtc,
    max_age_s: z.number().int().min(1).max(MAX_MUSHY_MAX_AGE_S),
    rows: z.array(MushyPanelRowV1).min(1).max(MAX_MUSHY_VIEW_ROWS),
  })
  .superRefine((projection, ctx) => {
    const problem = labelProblem(projection.title);
    if (problem !== null) ctx.addIssue({ code: "custom", path: ["title"], message: problem });
  });
export type MushyProjectionV1 = z.infer<typeof MushyProjectionV1>;

/** A K0 view block, and only a view block. */
export const MushyViewV1 = BlockV1.superRefine((block, ctx) => {
  if (block.type !== "view") ctx.addIssue({ code: "custom", path: ["type"], message: "a view block is expected" });
});
export type MushyViewV1 = Extract<z.infer<typeof BlockV1>, { type: "view" }>;

export const MushyRenderArtifactOutputV1 = z.union([
  z
    .strictObject({
      status: z.literal("ok"),
      invocation_id: Id,
      as_of: IsoUtc,
      rows_digest: Sha256Hex,
      view: MushyViewV1,
    })
    .superRefine((output, ctx) => {
      const view = output.view;
      if (view.type !== "view") return;
      if (view.invocation_ids.length !== 1 || view.invocation_ids[0] !== output.invocation_id) {
        ctx.addIssue({ code: "custom", path: ["view", "invocation_ids"], message: "the view cites exactly this invocation" });
      }
    }),
  MushyUnavailableV1,
]);
export type MushyRenderArtifactOutputV1 = z.infer<typeof MushyRenderArtifactOutputV1>;

// ---------------------------------------------------------------------------
// Refusals inside a propose tool's result
// ---------------------------------------------------------------------------

export const MUSHY_REFUSAL_CODES = [
  "artifact.invocation_unknown",
  "artifact.invocation_unavailable",
  "artifact.projection_missing",
  "artifact.limit_reached",
  "config.unknown",
  "config.snapshot_stale",
  "config.path_unknown",
  "config.no_change",
  "config.value_type_invalid",
  "config.value_out_of_range",
  "config.value_not_allowed",
  "consent.refused",
] as const;
export const MushyRefusalCode = z.enum(MUSHY_REFUSAL_CODES);
export type MushyRefusalCode = z.infer<typeof MushyRefusalCode>;

export const MushyRefusedV1 = z.strictObject({
  status: z.literal("refused"),
  code: MushyRefusalCode,
  reason: ServerReasonV1,
});
export type MushyRefusedV1 = z.infer<typeof MushyRefusedV1>;

// ---------------------------------------------------------------------------
// ArtifactEnvelope.v2
// ---------------------------------------------------------------------------

export const ARTIFACT_ENVELOPE_SCHEMA = "leaf.artifact-envelope.v2";
/** Who produced an envelope. K3 adds the assistant's saved view. */
export const ARTIFACT_PRODUCERS = ["assistant.save_view"] as const;
export const ArtifactProducerKindV2 = z.enum(ARTIFACT_PRODUCERS);
export const MAX_ARTIFACT_VIEWS = 16;

/** One recorded render the artifact was built from, bound by its rows digest. */
export const MushyInvocationBindingV1 = z.strictObject({
  invocation_id: Id,
  tool: z.literal("artifacts.render_artifact"),
  source_id: Id,
  as_of: IsoUtc,
  rows_digest: Sha256Hex,
});
export type MushyInvocationBindingV1 = z.infer<typeof MushyInvocationBindingV1>;

export const ArtifactProducerV2 = z.strictObject({
  kind: ArtifactProducerKindV2,
  job_id: Id,
  invocations: z.array(MushyInvocationBindingV1).min(1).max(MAX_ARTIFACT_VIEWS),
});

/**
 * An immutable, digest-bound artifact. `content_digest` is sha256 of the
 * canonical content and `envelope_digest` is sha256 of the canonical envelope
 * without `envelope_digest`; the server recomputes both, and the slot publish
 * card binds `envelope_digest` as its artifact digest.
 */
export const ArtifactEnvelopeV2 = z
  .strictObject({
    schema: z.literal(ARTIFACT_ENVELOPE_SCHEMA),
    artifact_id: Id,
    title: Label,
    scope: z.strictObject({ scope_kind: ScopeKindV1, tenant_id: Id }),
    producer: ArtifactProducerV2,
    created_at: IsoUtc,
    content: z.strictObject({
      views: z.array(MushyViewV1).min(1).max(MAX_ARTIFACT_VIEWS),
    }),
    content_digest: Sha256Hex,
    envelope_digest: Sha256Hex,
  })
  .superRefine((envelope, ctx) => {
    const problem = labelProblem(envelope.title);
    if (problem !== null) ctx.addIssue({ code: "custom", path: ["title"], message: problem });
    const bound = new Set<string>();
    envelope.producer.invocations.forEach((binding, index) => {
      if (bound.has(binding.invocation_id)) {
        ctx.addIssue({
          code: "custom",
          path: ["producer", "invocations", index, "invocation_id"],
          message: `invocation ${binding.invocation_id} is bound twice`,
        });
      }
      bound.add(binding.invocation_id);
    });
    const cited = new Set<string>();
    envelope.content.views.forEach((view, v) => {
      if (view.type !== "view") return;
      view.invocation_ids.forEach((id, k) => {
        cited.add(id);
        if (!bound.has(id)) {
          ctx.addIssue({
            code: "custom",
            path: ["content", "views", v, "invocation_ids", k],
            message: `view cites ${id}, which the producer does not bind`,
          });
        }
      });
    });
    envelope.producer.invocations.forEach((binding, index) => {
      if (!cited.has(binding.invocation_id)) {
        ctx.addIssue({
          code: "custom",
          path: ["producer", "invocations", index],
          message: `invocation ${binding.invocation_id} is bound but no view cites it`,
        });
      }
    });
  });
export type ArtifactEnvelopeV2 = z.infer<typeof ArtifactEnvelopeV2>;

// ---------------------------------------------------------------------------
// artifacts.propose_artifact (propose)
// ---------------------------------------------------------------------------

/** The server's name for this card; it renders as the K0 card kind `slot_promotion`. */
export const MUSHY_CARD_KIND = { slot_publish: "slot_promotion", config: "config_draft" } as const;

export const MushyProposeArtifactInputV1 = z
  .strictObject({
    slot_id: Id,
    invocation_ids: z.array(Id).min(1).max(MAX_ARTIFACT_VIEWS),
  })
  .superRefine((input, ctx) => {
    if (new Set(input.invocation_ids).size !== input.invocation_ids.length) {
      ctx.addIssue({ code: "custom", path: ["invocation_ids"], message: "each invocation is named once" });
    }
  });
export type MushyProposeArtifactInputV1 = z.infer<typeof MushyProposeArtifactInputV1>;

export const MushyProposeArtifactOutputV1 = z.union([
  z
    .strictObject({
      status: z.literal("ok"),
      purpose: z.literal("slot_publish"),
      artifact: z.strictObject({
        artifact_id: Id,
        title: Label,
        digest: Sha256Hex,
        as_of: IsoUtc,
      }),
      proposal_id: Id,
      card: ConfirmCardV1,
    })
    .superRefine((output, ctx) => {
      if (output.card.kind !== MUSHY_CARD_KIND.slot_publish) {
        ctx.addIssue({ code: "custom", path: ["card", "kind"], message: "a slot publish card is a slot_promotion card" });
      }
      if (output.card.proposal_id !== output.proposal_id) {
        ctx.addIssue({ code: "custom", path: ["card", "proposal_id"], message: "the card is rendered from this proposal" });
      }
      if (output.card.state !== "pending") {
        ctx.addIssue({ code: "custom", path: ["card", "state"], message: "a proposed card is pending" });
      }
    }),
  MushyRefusedV1,
  MushyUnavailableV1,
]);
export type MushyProposeArtifactOutputV1 = z.infer<typeof MushyProposeArtifactOutputV1>;

// ---------------------------------------------------------------------------
// artifacts.propose_config_change (propose)
// ---------------------------------------------------------------------------

export const MAX_MUSHY_CONFIG_CHANGES = 16;
export const MAX_MUSHY_CONFIG_STRING = 180;
export const MAX_MUSHY_CONFIG_ENUM = 64;

export const MushyConfigScalarV1 = z.union([
  z.string().max(MAX_MUSHY_CONFIG_STRING),
  z.number(),
  z.boolean(),
  z.null(),
]);
export type MushyConfigScalarV1 = z.infer<typeof MushyConfigScalarV1>;

/**
 * The server's declaration of one setting a draft may change. There is no free
 * `string` type: a string value can be proposed only as a member of a declared
 * enum, so no model-chosen text reaches a card. Bounds are inclusive.
 */
export const MushyConfigSettingV1 = z
  .discriminatedUnion("type", [
    z.strictObject({ type: z.literal("boolean"), nullable: z.boolean().optional() }),
    z.strictObject({
      type: z.literal("integer"),
      min: z.number().int(),
      max: z.number().int(),
      nullable: z.boolean().optional(),
    }),
    z.strictObject({ type: z.literal("number"), min: z.number(), max: z.number(), nullable: z.boolean().optional() }),
    z.strictObject({
      type: z.literal("enum"),
      values: z
        .array(z.union([z.string().max(MAX_MUSHY_CONFIG_STRING), z.number(), z.boolean()]))
        .min(1)
        .max(MAX_MUSHY_CONFIG_ENUM),
      nullable: z.boolean().optional(),
    }),
  ])
  .superRefine((setting, ctx) => {
    if (setting.type === "integer" || setting.type === "number") {
      if (!Number.isFinite(setting.min) || !Number.isFinite(setting.max)) {
        ctx.addIssue({ code: "custom", path: ["min"], message: "bounds are finite" });
      } else if (setting.min > setting.max) {
        ctx.addIssue({ code: "custom", path: ["min"], message: "min is at most max" });
      }
      if (setting.type === "integer" && (!Number.isSafeInteger(setting.min) || !Number.isSafeInteger(setting.max))) {
        ctx.addIssue({ code: "custom", path: ["min"], message: "integer bounds are safe integers" });
      }
    }
    if (setting.type === "enum") {
      setting.values.forEach((value, index) => {
        if (setting.values.indexOf(value) !== index) {
          ctx.addIssue({ code: "custom", path: ["values", index], message: `duplicate value ${String(value)}` });
        }
      });
    }
  });
export type MushyConfigSettingV1 = z.infer<typeof MushyConfigSettingV1>;

export const MUSHY_CONFIG_VALUE_PROBLEMS = ["config.value_type_invalid", "config.value_out_of_range", "config.value_not_allowed"] as const;
export type MushyConfigValueProblem = (typeof MUSHY_CONFIG_VALUE_PROBLEMS)[number];

/** Null when `value` is one the declared setting allows; pure, never throws. Enum membership is strict equality. */
export function mushyConfigValueProblem(setting: MushyConfigSettingV1, value: MushyConfigScalarV1): MushyConfigValueProblem | null {
  if (value === null) return setting.nullable === true ? null : "config.value_type_invalid";
  switch (setting.type) {
    case "boolean":
      return typeof value === "boolean" ? null : "config.value_type_invalid";
    case "integer":
      if (typeof value !== "number" || !Number.isSafeInteger(value)) return "config.value_type_invalid";
      return value < setting.min || value > setting.max ? "config.value_out_of_range" : null;
    case "number":
      if (typeof value !== "number" || !Number.isFinite(value)) return "config.value_type_invalid";
      return value < setting.min || value > setting.max ? "config.value_out_of_range" : null;
    case "enum":
      return setting.values.some((allowed) => allowed === value) ? null : "config.value_not_allowed";
    default:
      return "config.value_type_invalid";
  }
}

function uniquePaths(changes: readonly { readonly path: string }[], ctx: IssueSink): void {
  const paths = new Set<string>();
  changes.forEach((change, index) => {
    if (paths.has(change.path)) ctx.addIssue({ code: "custom", path: ["changes", index, "path"], message: `duplicate path ${change.path}` });
    paths.add(change.path);
  });
}

/** `base_revision` is the snapshot revision the draft was made against; the server refuses it on drift. */
export const MushyProposeConfigChangeInputV1 = z
  .strictObject({
    config_key: Id,
    base_revision: Id,
    changes: z
      .array(z.strictObject({ path: Id, to: MushyConfigScalarV1 }))
      .min(1)
      .max(MAX_MUSHY_CONFIG_CHANGES),
  })
  .superRefine((input, ctx) => uniquePaths(input.changes, ctx));
export type MushyProposeConfigChangeInputV1 = z.infer<typeof MushyProposeConfigChangeInputV1>;

export const MushyConfigDraftV1 = z
  .strictObject({
    config_key: Id,
    base_revision: Id,
    base_snapshot_digest: Sha256Hex,
    draft_digest: Sha256Hex,
    /** `from` is read from the server snapshot, never from the model. */
    changes: z
      .array(z.strictObject({ path: Id, from: MushyConfigScalarV1, to: MushyConfigScalarV1 }))
      .min(1)
      .max(MAX_MUSHY_CONFIG_CHANGES),
  })
  .superRefine((draft, ctx) => uniquePaths(draft.changes, ctx));
export type MushyConfigDraftV1 = z.infer<typeof MushyConfigDraftV1>;

export const MushyProposeConfigChangeOutputV1 = z.union([
  z
    .strictObject({
      status: z.literal("ok"),
      purpose: z.literal("config"),
      proposal_id: Id,
      draft: MushyConfigDraftV1,
      card: ConfirmCardV1,
    })
    .superRefine((output, ctx) => {
      if (output.card.kind !== MUSHY_CARD_KIND.config) {
        ctx.addIssue({ code: "custom", path: ["card", "kind"], message: "a config card is a config_draft card" });
      }
      if (output.card.proposal_id !== output.proposal_id) {
        ctx.addIssue({ code: "custom", path: ["card", "proposal_id"], message: "the card is rendered from this proposal" });
      }
      if (output.card.state !== "pending") {
        ctx.addIssue({ code: "custom", path: ["card", "state"], message: "a proposed card is pending" });
      }
    }),
  MushyRefusedV1,
  MushyUnavailableV1,
]);
export type MushyProposeConfigChangeOutputV1 = z.infer<typeof MushyProposeConfigChangeOutputV1>;

// ---------------------------------------------------------------------------
// Emission: the K0 path, extended
// ---------------------------------------------------------------------------

/** Each tool's input and output schema, by tool name. */
export const MUSHY_TOOL_SCHEMAS = {
  "artifacts.list_sources": { input: MushyListSourcesInputV1, output: MushyListSourcesOutputV1 },
  "artifacts.render_artifact": { input: MushyRenderArtifactInputV1, output: MushyRenderArtifactOutputV1 },
  "artifacts.propose_artifact": { input: MushyProposeArtifactInputV1, output: MushyProposeArtifactOutputV1 },
  "artifacts.propose_config_change": { input: MushyProposeConfigChangeInputV1, output: MushyProposeConfigChangeOutputV1 },
} as const satisfies Record<MushyToolName, { input: z.ZodType; output: z.ZodType }>;

export interface MushyToolInputs {
  readonly "artifacts.list_sources": MushyListSourcesInputV1;
  readonly "artifacts.render_artifact": MushyRenderArtifactInputV1;
  readonly "artifacts.propose_artifact": MushyProposeArtifactInputV1;
  readonly "artifacts.propose_config_change": MushyProposeConfigChangeInputV1;
}

export interface MushyToolOutputs {
  readonly "artifacts.list_sources": MushyListSourcesOutputV1;
  readonly "artifacts.render_artifact": MushyRenderArtifactOutputV1;
  readonly "artifacts.propose_artifact": MushyProposeArtifactOutputV1;
  readonly "artifacts.propose_config_change": MushyProposeConfigChangeOutputV1;
}

/** The K3 named definitions, emitted after K0's. */
export const MUSHY_SCHEMA_DEFS = {
  ArtifactsSourceDeclV1: MushySourceDeclV1,
  ArtifactsSourceEntryV1: MushySourceEntryV1,
  ArtifactsUnavailableV1: MushyUnavailableV1,
  ArtifactsRefusedV1: MushyRefusedV1,
  ArtifactsListSourcesInputV1: MushyListSourcesInputV1,
  ArtifactsSourceListV1: MushySourceListV1,
  ArtifactsListSourcesOutputV1: MushyListSourcesOutputV1,
  ArtifactsRenderArtifactInputV1: MushyRenderArtifactInputV1,
  ArtifactsProjectionV1: MushyProjectionV1,
  ArtifactsRenderArtifactOutputV1: MushyRenderArtifactOutputV1,
  ArtifactEnvelopeV2,
  ArtifactsProposeArtifactInputV1: MushyProposeArtifactInputV1,
  ArtifactsProposeArtifactOutputV1: MushyProposeArtifactOutputV1,
  ArtifactsConfigDraftV1: MushyConfigDraftV1,
  ArtifactsConfigSettingV1: MushyConfigSettingV1,
  ArtifactsProposeConfigChangeInputV1: MushyProposeConfigChangeInputV1,
  ArtifactsProposeConfigChangeOutputV1: MushyProposeConfigChangeOutputV1,
} as const;
export type MushySchemaName = keyof typeof MUSHY_SCHEMA_DEFS;

type JsonObject = { [key: string]: Json };

function emitMushyRaw(schema: z.ZodType): JsonObject {
  const json = z.toJSONSchema(schema) as JsonObject;
  delete json.$schema;
  return json;
}

function canonicalMushy(value: Json): string {
  if (Array.isArray(value)) return `[${value.map(canonicalMushy).join(",")}]`;
  if (value !== null && typeof value === "object") {
    const keys = Object.keys(value).sort();
    return `{${keys.map((key) => `${JSON.stringify(key)}:${canonicalMushy(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function dedupeMushyRefs(node: Json, isRoot: boolean, index: Map<string, string>, refPrefix: string): Json {
  if (Array.isArray(node)) return node.map((child) => dedupeMushyRefs(child, false, index, refPrefix));
  if (node === null || typeof node !== "object") return node;
  if (!isRoot) {
    const name = index.get(canonicalMushy(node));
    if (name !== undefined) return { $ref: `${refPrefix}${name}` };
  }
  const out: JsonObject = {};
  for (const key of Object.keys(node)) out[key] = dedupeMushyRefs(node[key], false, index, refPrefix);
  return out;
}

/**
 * K0's definitions followed by K3's, through the same deterministic emission
 * as emitDefinitions: every shared subtree becomes a `$ref`, so a mushy schema
 * that embeds ConfirmCardV1 or a view block refers to K0's definition. K0's
 * own documents (schema/*.json) are untouched.
 */
export function emitMushyDefinitions(refPrefix: string): Record<string, Json> {
  const defs: Record<string, z.ZodType> = { ...SCHEMA_DEFS, ...MUSHY_SCHEMA_DEFS };
  const names = Object.keys(defs);
  const raw = names.map((name) => [name, emitMushyRaw(defs[name])] as const);
  const index = new Map<string, string>();
  for (const [name, json] of raw) {
    const key = canonicalMushy(json);
    const clash = index.get(key);
    if (clash !== undefined) throw new Error(`definitions ${clash} and ${name} emit identical schemas`);
    index.set(key, name);
  }
  const out: Record<string, Json> = {};
  for (const [name, json] of raw) out[name] = dedupeMushyRefs(json, true, index, refPrefix);
  return out;
}

function mushyToolsExtension(refPrefix: string): Json {
  return MUSHY_TOOL_NAMES.map((name) => ({
    name,
    id: MUSHY_TOOLS[name].id,
    class: MUSHY_TOOLS[name].class,
    label: MUSHY_TOOLS[name].label,
    input: { $ref: `${refPrefix}${MUSHY_TOOLS[name].input}` },
    output: { $ref: `${refPrefix}${MUSHY_TOOLS[name].output}` },
  }));
}

/** JSON Schema 2020-12 bundle: K0's definitions plus K3's, with the tool table under `x-leaf-tools`. */
export function emitMushyJsonSchema(): JsonObject {
  return {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "urn:leaf:assistant-contract:artifacts:v1",
    title: "Leaf Automation assistant contract, data capability tools",
    version: CONTRACT_VERSION,
    "x-leaf-tools": mushyToolsExtension("#/$defs/"),
    $defs: emitMushyDefinitions("#/$defs/"),
  };
}

/**
 * OpenAPI 3.1 components. The tools run through the worker tool route
 * (POST /worker/jobs/{job_id}/tools {tool_call_id, name, args}), so the tool
 * table travels as `x-leaf-tools` beside the schemas rather than as paths.
 */
export function emitMushyOpenApi(): JsonObject {
  return {
    openapi: "3.1.0",
    info: {
      title: "Leaf Automation assistant contract, data capability tools",
      version: CONTRACT_VERSION,
    },
    jsonSchemaDialect: "https://json-schema.org/draft/2020-12/schema",
    servers: [{ url: "/assistant/v1" }],
    paths: {},
    "x-leaf-tools": mushyToolsExtension("#/components/schemas/"),
    components: {
      schemas: emitMushyDefinitions("#/components/schemas/"),
    },
  };
}

// ---------------------------------------------------------------------------
// Golden fixtures: one per tool
// ---------------------------------------------------------------------------

const G_AS_OF = "2026-09-23T11:58:00.000Z";
const G_NOW = "2026-09-23T12:00:00.000Z";
const G_EXPIRES = "2026-09-23T12:15:00.000Z";

const G_FIELDS = [
  { key: "pnl", label: "P&L", type: "cents|null" },
  { key: "win-rate", label: "Win rate", type: "pct|null" },
  { key: "trades", label: "Trades", type: "count|null" },
] as const;

export interface MushyGoldenV1 {
  readonly tool: MushyToolName;
  readonly title: string;
  readonly input: unknown;
  readonly output: unknown;
}

/** One golden input and output per tool. Every consumer decodes these at its pin. */
export const MUSHY_GOLDEN_FIXTURES: readonly MushyGoldenV1[] = [
  {
    tool: "artifacts.list_sources",
    title: "A tenant lists the sources it may ask about",
    input: {},
    output: {
      status: "ok",
      scope_kind: "tenant",
      sources: [
        {
          source_id: "desk.positions",
          label: "Positions",
          fields: G_FIELDS,
          units: ["cents", "pct", "count"],
          freshness: { max_age_s: 300 },
        },
      ],
    },
  },
  {
    tool: "artifacts.render_artifact",
    title: "The server projects a declared source into a table view",
    input: { source_id: "desk.positions", kind: "table", fields: ["pnl", "win-rate"], limit: 1 },
    output: {
      status: "ok",
      invocation_id: "inv-1",
      as_of: G_NOW,
      rows_digest: "1".repeat(64),
      view: {
        type: "view",
        origin: "server",
        view_id: "view-2c3b",
        kind: "table",
        title: "Positions",
        invocation_ids: ["inv-1"],
        as_of: G_AS_OF,
        rows: [
          { invocation_id: "inv-1", label: "Book A / P&L", value: 12345, unit: "cents" },
          { invocation_id: "inv-1", label: "Book A / Win rate", value: null, unit: "pct" },
        ],
      },
    },
  },
  {
    tool: "artifacts.propose_artifact",
    title: "A saved view becomes a digest-bound artifact and a slot publish card",
    input: { slot_id: "slot-home", invocation_ids: ["inv-1"] },
    output: {
      status: "ok",
      purpose: "slot_publish",
      artifact: { artifact_id: "artifact-1", title: "Positions", digest: "2".repeat(64), as_of: G_AS_OF },
      proposal_id: "proposal-1",
      card: {
        card_id: "card-1",
        proposal_id: "proposal-1",
        proposal_digest: "3".repeat(64),
        kind: "slot_promotion",
        state: "pending",
        title: { text: "Promote to a slot", origin: "server" },
        fields: [
          { key: "slot", label: "Slot", value: "slot-home", origin: "server", placement: "above_approve" },
          { key: "artifact", label: "Artifact", value: "artifact-1", origin: "server", placement: "above_approve" },
          { key: "artifact-digest", label: "Artifact digest", value: "2".repeat(64), origin: "server", placement: "above_approve" },
        ],
        approve: { text: "Approve", origin: "server" },
        decline: { text: "Decline", origin: "server" },
        spend: { state: "none", cents: null },
        requires_face_id: false,
        created_at: G_NOW,
        expires_at: G_EXPIRES,
        card_digest: "4".repeat(64),
        resolution: null,
      },
    },
  },
  {
    tool: "artifacts.propose_config_change",
    title: "A draft made against a stale snapshot is refused",
    input: { config_key: "orders", base_revision: "rev-7", changes: [{ path: "orders.max-size", to: 5 }] },
    output: {
      status: "refused",
      code: "config.snapshot_stale",
      reason: {
        code: "refused",
        text: "That setting changed after this draft was made. Read it again first.",
        next: { label: "OK", action: "none" },
      },
    },
  },
];
