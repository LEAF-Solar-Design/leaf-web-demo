import { createHash } from "node:crypto";
import { z } from "zod";

export const PLAN_CONFIRMATION_TTL_S = 1800;
const MAX_PLAN_BYTES = 16_384;

// Zod's object projection deliberately drops descriptive fields supplied by the model.
export const PlanBuildRequest = z.object({
  plan_confirmation_id: z.string({ error: "propose a plan first" }).refine(value => value.trim().length > 0, "propose a plan first"),
  mode: z.enum(["build", "one_off"]).default("build"),
  retry_of_change_set_id: z.string().uuid().optional(),
});
export type PlanBuildRequest = z.infer<typeof PlanBuildRequest>;

export function planModeEnabled(packet: Record<string, unknown>): boolean {
  const inline = packet.inline_authoring;
  return typeof inline === "object" && inline !== null && !Array.isArray(inline) &&
    "plan_mode" in inline && inline.plan_mode === true;
}

// Python's len(str) and JSON Schema count Unicode code points, including astral characters.
const bounded = (min: number, max: number) => z.string().refine(value => {
  const length = Array.from(value).length;
  return length >= min && length <= max;
}).meta({ minLength: min, maxLength: max });
const planText = (min: number, max: number, multiline = false) => bounded(min, max)
  .refine(value => !/[\x00-\x09\x0b-\x1f\x7f\p{Cs}]/u.test(value) &&
    !/^\p{White_Space}|\p{White_Space}(?![\s\S])/u.test(value) && (multiline || !value.includes("\n")))
  .meta({ minLength: min, maxLength: max });
const toolName = planText(1, 64).regex(/^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/).meta({ minLength: 1, maxLength: 64 });
const item = planText(1, 240);
const planAnswer = z.union([
  z.strictObject({ labels: z.array(planText(1, 60)).min(1).max(4) }),
  z.strictObject({ other: planText(1, 500) }),
]);
const answerKey = /^[a-z0-9_-]{1,32}(?![\s\S])/;
// A record parser rebuilds its result and drops "__proto__", a legal plan answer key.
// Validate each entry in place to keep every key covered by the approval digest.
const planAnswers = z.unknown().refine((value): value is Record<string, z.infer<typeof planAnswer>> => {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const entries = Object.entries(value);
  return entries.length <= 4 && entries.every(([key, answer]) => answerKey.test(key) && planAnswer.safeParse(answer).success);
}).meta({ type: "object", maxProperties: 4, propertyNames: { pattern: answerKey.source },
  additionalProperties: z.toJSONSchema(planAnswer, { io: "input" }) });
const inputField = z.strictObject({
  name: z.string().regex(/^[a-z][a-z0-9_]{0,39}(?![\s\S])/),
  type: z.enum(["string", "integer", "boolean", "number"]),
  default: z.union([planText(0, 200), z.number(), z.boolean(), z.null()]),
  description: planText(0, 200),
}).refine(field => field.default === null || (field.type === "integer"
  ? Number.isInteger(field.default) : typeof field.default === field.type), "default must match input type");

// Serialize objects directly: JSON.stringify reorders numeric-looking keys even if
// they were inserted in lexical order. Python retains the sorted order for those keys.
function canonical(value: unknown): string {
  if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
  if (typeof value === "object" && value !== null) {
    const entries = Object.entries(value).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0);
    return "{" + entries.map(([key, child]) => JSON.stringify(key) + ":" + canonical(child)).join(",") + "}";
  }
  if (typeof value === "number") {
    // The gate reads the JSON number on the wire. Integer tokens stay integers.
    // Python renders small float tokens in scientific notation with a two-digit exponent.
    const token = JSON.stringify(value);
    if (token.includes("e") || (token.includes(".") && Math.abs(value) < 0.0001)) {
      return value.toExponential().replace(/e([+-])(\d)$/, (_match, sign, digit) => "e" + sign + "0" + digit);
    }
    return token;
  }
  return JSON.stringify(value);
}

export const BuildPlan = z.strictObject({
  schema: z.literal("leaf.build-plan.v1"),
  target: z.enum(["branch", "studio"]),
  change_kind: z.enum(["create", "revise"]),
  tool_name: toolName,
  target_tool_name: toolName.optional(),
  title: planText(1, 80),
  summary: planText(1, 500, true),
  behavior: z.array(item).min(1).max(12),
  inputs: z.array(inputField).max(12),
  output: planText(1, 300),
  capabilities: z.array(z.enum(["drawing.read", "drawing.write"])).min(1).max(2),
  undo: planText(0, 200),
  assumptions: z.array(item).max(8),
  acceptance: z.array(item).max(6),
  answers: planAnswers,
}).refine(plan => (plan.change_kind === "revise") === (plan.target_tool_name !== undefined),
  "target_tool_name is required only for revisions")
  .refine(plan => new Set(plan.inputs.map(field => field.name)).size === plan.inputs.length,
    "input names must be unique")
  .refine(plan => new Set(plan.capabilities).size === plan.capabilities.length, "capabilities must be unique")
  .refine(plan => Buffer.byteLength(canonical(plan), "utf8") <= MAX_PLAN_BYTES, "plan_too_large");
export type BuildPlan = z.infer<typeof BuildPlan>;
export const planDigest = (plan: BuildPlan) => createHash("sha256").update(canonical(plan)).digest("hex");

export const Questions = z.array(z.strictObject({
  header: bounded(1, 12),
  question: bounded(1, 300),
  multiSelect: z.boolean(),
  options: z.array(z.strictObject({
    label: bounded(1, 60).refine(value => value.trim() === value).meta({ minLength: 1, maxLength: 60 }),
    description: bounded(0, 200).optional(),
  })).min(2).max(4),
})).min(1).max(4);

const turnAnswer = z.union([
  z.strictObject({ labels: z.array(bounded(1, 60)).min(1).max(4) }),
  z.strictObject({ other: bounded(1, 500) }),
]).optional();
export const TurnAnswers = z.strictObject({
  question_id: z.string().min(1),
  answers: z.strictObject({ q1: turnAnswer, q2: turnAnswer, q3: turnAnswer, q4: turnAnswer })
    .refine(answers => Object.keys(answers).length > 0),
});
export type TurnAnswers = z.infer<typeof TurnAnswers>;
