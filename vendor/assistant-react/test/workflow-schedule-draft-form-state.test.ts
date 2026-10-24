import { describe, expect, it } from "vitest";
import { IntegrationWorkflowScheduleDraftV1 } from "../../assistant-contract/src/integration";
import {
  acknowledgesScheduleDraft, cloneScheduleDraft, freezeScheduleDraft, initialScheduleDraftInputs,
  IntegrationWorkflowScheduleDraftSaveError, validateScheduleDraftInputs,
} from "../src/actions/workflow-schedule-draft-form-state";

const draft = IntegrationWorkflowScheduleDraftV1.parse({
  schema_version: 1, id: "daily-review", version: 2, title: "Daily review",
  workflow_ref: { id: "review", version: 3, digest: "ab".repeat(32) },
  schedule: { kind: "daily", timezone: "UTC", hour: 9, minute: 5,
    gap_policy: "next_valid_minute", fold_policy: "first_occurrence" }, state: "draft",
});

describe("schedule draft canonical state", () => {
  it.each(["UTC", "America/New_York", "Etc/GMT+5"])("preserves exact %s and immutable policy/reference", (timezone) => {
    const result = validateScheduleDraftInputs(draft, { title: "Edited", timezone, hour: "00", minute: "59" });
    expect(result.errors).toEqual({});
    expect(result.draft).toEqual({ ...draft, title: "Edited", schedule: { ...draft.schedule, timezone, hour: 0, minute: 59 } });
    expect(IntegrationWorkflowScheduleDraftV1.parse(result.draft)).toEqual(result.draft);
    expect(initialScheduleDraftInputs(draft)).toEqual({ title: "Daily review", timezone: "UTC", hour: "9", minute: "5" });
  });

  it.each(["", " ", "1.5", "1.0", "0x10", "1e1", "+1", "-0", "1\n", "Infinity", "NaN", "24", "99999999999999999999999"])("rejects invalid hour %j without coercion", (hour) => {
    const result = validateScheduleDraftInputs(draft, { ...initialScheduleDraftInputs(draft), hour });
    expect(result.draft).toBeUndefined();
    expect(result.errors.hour).toBe("Enter a whole decimal hour from 0 to 23.");
  });

  it.each(["", "1.2", "0x10", "60", "-1", "5 "])("rejects invalid minute %j", (minute) => {
    expect(validateScheduleDraftInputs(draft, { ...initialScheduleDraftInputs(draft), minute }).errors.minute).toBeTruthy();
  });

  it("uses canonical title, timezone and reference validation without leaking validation data", () => {
    for (const title of ["", "a".repeat(121)]) {
      expect(validateScheduleDraftInputs(draft, { ...initialScheduleDraftInputs(draft), title }).errors.title).toBe("Enter a title of 1 to 120 characters.");
    }
    for (const timezone of ["UTC\n", "Bad Zone", "", "a".repeat(129)]) {
      const result = validateScheduleDraftInputs(draft, { ...initialScheduleDraftInputs(draft), timezone });
      expect(result.draft).toBeUndefined();
      expect(result.errors.timezone).not.toContain(timezone || "sentinel");
    }
    const badRef = { ...draft, workflow_ref: { ...draft.workflow_ref, digest: "sentinel" } };
    expect(validateScheduleDraftInputs(badRef, initialScheduleDraftInputs(draft)).draft).toBeUndefined();
  });

  it("keeps a privately frozen snapshot independent of every callback copy", () => {
    const snapshot = freezeScheduleDraft(draft);
    const first = cloneScheduleDraft(snapshot);
    first.title = "mutated";
    first.workflow_ref.id = "other";
    first.schedule.timezone = "Etc/GMT+5";
    expect(cloneScheduleDraft(snapshot)).toEqual(draft);
    expect(Object.isFrozen(snapshot)).toBe(true);
    expect(Object.isFrozen(snapshot.schedule)).toBe(true);
    expect(Object.isFrozen(snapshot.workflow_ref)).toBe(true);
    expect(snapshot).not.toBe(draft);
  });

  it("requires a canonical acknowledgement bound to submitted identity/version", () => {
    const ref = { id: draft.id, version: draft.version, digest: "cd".repeat(32) };
    expect(acknowledgesScheduleDraft(ref, draft)).toBe(true);
    for (const response of [null, {}, { ...ref, id: "other" }, { ...ref, version: 3 },
      { ...ref, digest: "invalid" }, { ...ref, extra: true }, { ref }]) {
      expect(acknowledgesScheduleDraft(response, draft)).toBe(false);
    }
  });

  it("limits trusted host errors to finite codes and fixed text", () => {
    expect(new IntegrationWorkflowScheduleDraftSaveError("invalid_timezone").code).toBe("invalid_timezone");
    expect(new IntegrationWorkflowScheduleDraftSaveError("conflict").message).toBe("Schedule draft save was refused.");
    expect(() => new IntegrationWorkflowScheduleDraftSaveError("sentinel" as "conflict")).toThrow("Invalid schedule draft save error code.");
  });
});
