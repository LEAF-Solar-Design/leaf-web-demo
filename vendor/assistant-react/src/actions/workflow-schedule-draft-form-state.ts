import { IntegrationRefV1, IntegrationWorkflowScheduleDraftV1 } from "../../../assistant-contract/src/integration";

export type ScheduleDraftAvailability = "ready" | "unavailable" | "forbidden";
export type ScheduleDraftInputs = { title: string; timezone: string; hour: string; minute: string };
export type ScheduleDraftField = keyof ScheduleDraftInputs;
export const scheduleDraftFields: ScheduleDraftField[] = ["title", "timezone", "hour", "minute"];
export const scheduleDraftCopy = {
  ready: "Ready to save an inert draft.",
  unavailable: "Schedule draft saving is unavailable.",
  forbidden: "You do not have permission to save this schedule draft.",
  pending: "Saving draft. The save outcome is pending.",
  unknown: "Save outcome unknown. Values are locked. Retry saves the same draft.",
  saved: "Draft saved. Nothing is scheduled or authorized to run. This version is locked.",
  invalid_timezone: "The host does not support this timezone. Enter another timezone.",
  conflict: "This version conflicts with stored data and is locked. Ask the host for a new version.",
} as const;
const fieldErrors: Record<ScheduleDraftField, string> = {
  title: "Enter a title of 1 to 120 characters.",
  timezone: "Enter a timezone key using letters, numbers, slash, underscore, dot, plus or minus.",
  hour: "Enter a whole decimal hour from 0 to 23.",
  minute: "Enter a whole decimal minute from 0 to 59.",
};

/** Trusted host signal only; this class grants no persistence or execution authority. */
export class IntegrationWorkflowScheduleDraftSaveError extends Error {
  readonly code: "invalid_timezone" | "conflict";
  constructor(code: "invalid_timezone" | "conflict") {
    super("Schedule draft save was refused.");
    this.name = "IntegrationWorkflowScheduleDraftSaveError";
    if (code !== "invalid_timezone" && code !== "conflict") throw new TypeError("Invalid schedule draft save error code.");
    this.code = code;
  }
}

export function initialScheduleDraftInputs(draft: IntegrationWorkflowScheduleDraftV1): ScheduleDraftInputs {
  return { title: draft.title, timezone: draft.schedule.timezone, hour: String(draft.schedule.hour), minute: String(draft.schedule.minute) };
}

function decimal(value: string): number {
  return /^[0-9]+(?![\s\S])/.test(value) ? Number(value) : NaN;
}

export function validateScheduleDraftInputs(initial: IntegrationWorkflowScheduleDraftV1, inputs: ScheduleDraftInputs): {
  draft?: IntegrationWorkflowScheduleDraftV1; errors: Partial<Record<ScheduleDraftField, string>>;
} {
  const parsed = IntegrationWorkflowScheduleDraftV1.safeParse({ ...initial, title: inputs.title,
    schedule: { ...initial.schedule, timezone: inputs.timezone, hour: decimal(inputs.hour), minute: decimal(inputs.minute) } });
  const errors: Partial<Record<ScheduleDraftField, string>> = {};
  if (parsed.success) return { draft: parsed.data, errors };
  for (const issue of parsed.error.issues) {
    const field = issue.path[0] === "schedule" ? issue.path[1] : issue.path[0];
    if (scheduleDraftFields.includes(field as ScheduleDraftField)) errors[field as ScheduleDraftField] = fieldErrors[field as ScheduleDraftField];
  }
  return { errors };
}

/** Each callback receives a mutable, independent copy of the private frozen payload. */
export function cloneScheduleDraft(draft: IntegrationWorkflowScheduleDraftV1): IntegrationWorkflowScheduleDraftV1 {
  return { ...draft, workflow_ref: { ...draft.workflow_ref }, schedule: { ...draft.schedule } };
}

export function freezeScheduleDraft(draft: IntegrationWorkflowScheduleDraftV1): IntegrationWorkflowScheduleDraftV1 {
  const copy = cloneScheduleDraft(draft);
  Object.freeze(copy.workflow_ref);
  Object.freeze(copy.schedule);
  return Object.freeze(copy);
}

export function acknowledgesScheduleDraft(response: unknown, draft: IntegrationWorkflowScheduleDraftV1): boolean {
  const parsed = IntegrationRefV1.safeParse(response);
  return parsed.success && parsed.data.id === draft.id && parsed.data.version === draft.version;
}
