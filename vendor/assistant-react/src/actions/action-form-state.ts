import type {
  IntegrationActionElementV1, IntegrationActionFieldV1, IntegrationActionRunV1,
  IntegrationActionSubmissionV1, IntegrationRefV1,
} from "../../../assistant-contract/src/integration";

export type InputMode = "absent" | "null" | "value";
export type FieldInput = { mode: InputMode; value: string };
export type ActionInputs = Record<string, FieldInput>;
export type ActionAvailability = "ready" | "unavailable" | "forbidden" | "stale";

export const availabilityCopy = {
  ready: "", unavailable: "This action is unavailable.",
  forbidden: "You do not have permission to perform this action.",
  stale: "This action has changed. Refresh it before continuing.",
} as const;
export const unknownCopy = "Outcome unknown. The action may have executed. Local cancellation does not undo an action.";
export const conflictCopy = "This submission conflicts with an existing request. Contact your workspace administrator.";

export function initialInputs(element: IntegrationActionElementV1): ActionInputs {
  return Object.fromEntries(element.form.fields.map((field) => [field.id, {
    mode: field.required ? "value" : "absent", value: "",
  }]));
}

function parseField(field: IntegrationActionFieldV1, input: FieldInput): {
  value?: string | number | boolean | null; error?: string;
} {
  if (input.mode === "absent") return field.required ? { error: "Provide a value." } : {};
  if (input.mode === "null") return field.nullable ? { value: null } : { error: "Provide a value." };
  const value = input.value;
  if (field.kind === "text") {
    if (value.length < (field.min_length ?? 0) || value.length > (field.max_length ?? 4096)) {
      return { error: "Enter text within the allowed length." };
    }
    return { value };
  }
  if (field.kind === "number") {
    // Decimal syntax only: no whitespace, hex, empty-to-zero coercion or partial parsing.
    if (!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?(?![\s\S])/.test(value)) {
      return { error: "Enter a finite number." };
    }
    const number = Number(value);
    if (!Number.isFinite(number)) return { error: "Enter a finite number." };
    if (field.integer && !Number.isInteger(number)) return { error: "Enter a whole number." };
    if ((field.minimum !== undefined && number < field.minimum) ||
        (field.maximum !== undefined && number > field.maximum)) return { error: "Enter a number within the allowed range." };
    return { value: number };
  }
  if (field.kind === "boolean") return value === "true" || value === "false"
    ? { value: value === "true" } : { error: "Choose a value." };
  return field.choices.some((choice) => choice.value === value)
    ? { value } : { error: "Choose a value." };
}

export function validateInputs(element: IntegrationActionElementV1, inputs: ActionInputs) {
  const args: IntegrationActionSubmissionV1["arguments"] = {};
  const errors: Record<string, string> = Object.create(null);
  for (const field of element.form.fields) {
    const parsed = parseField(field, inputs[field.id]);
    if (parsed.error) errors[field.id] = parsed.error;
    else if (parsed.value !== undefined) args[field.id] = parsed.value;
  }
  return { arguments: args, errors };
}

export function sameRef(left: IntegrationRefV1, right: IntegrationRefV1): boolean {
  return left.id === right.id && left.version === right.version && left.digest === right.digest;
}

export function matchesSubmission(run: IntegrationActionRunV1, submission: IntegrationActionSubmissionV1): boolean {
  return run.connection_id === submission.connection_id &&
    sameRef(run.connector_ref, submission.connector_ref) && sameRef(run.operation_ref, submission.operation_ref) &&
    run.grant_revision === submission.expected_grant_revision;
}

export function sameRun(next: IntegrationActionRunV1, previous: IntegrationActionRunV1): boolean {
  return next.run_id === previous.run_id && next.submission_digest === previous.submission_digest &&
    next.authority_binding_digest === previous.authority_binding_digest && next.revision >= previous.revision &&
    (next.revision !== previous.revision || JSON.stringify(next) === JSON.stringify(previous));
}

export function isTerminal(run: IntegrationActionRunV1): boolean {
  return (run.state === "succeeded" || run.state === "failed") && run.effect_outcome !== "unknown";
}
