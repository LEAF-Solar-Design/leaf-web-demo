import React from "react";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  IntegrationActionElementV1, IntegrationActionRunV1, IntegrationActionSnapshotV1,
  IntegrationActionSubmissionV1, type IntegrationActionReceiptV1,
} from "../../assistant-contract/src/integration";
import { IntegrationActionForm, IntegrationActionRunStatus, type IntegrationActionFormProps } from "../src/actions";
import { formatDatasetValue, resolveDisplayPreferences } from "../src/datasets/formatting";

const ref = { id: "sales", version: 1, digest: "ab".repeat(32) };
const uuid = "7b92f224-18f2-4939-b368-c199cc1cc290";
const instant = "2026-10-20T12:00:00Z";
const element = IntegrationActionElementV1.parse({
  schema_version: 1, id: "create-sale", title: "Create sale", kind: "integration.action",
  connection_id: "sales-account", connector_ref: ref, operation_ref: ref,
  form: { submit_label: "Create", fields: [
    { id: "name", label: "Name", kind: "text", required: true, nullable: false, max_length: 4 },
    { id: "amount", label: "Amount", kind: "number", required: true, nullable: false, integer: true, minimum: 0, maximum: 10 },
    { id: "enabled", label: "Enabled", kind: "boolean", required: true, nullable: false },
    { id: "stage", label: "Stage", kind: "choice", required: true, nullable: false, choices: [{ label: "Open", value: "open" }] },
    { id: "note", label: "Note", kind: "text", required: false, nullable: true },
  ] },
});
const run = IntegrationActionRunV1.parse({
  schema_version: 1, run_id: uuid, revision: 1, connection_id: element.connection_id,
  connector_ref: ref, operation_ref: ref, submission_digest: ref.digest,
  authority_binding_digest: ref.digest, grant_revision: 1, created_at: instant,
  updated_at: instant, state: "accepted", effect_outcome: "not_dispatched",
});
function receipt(outcome: IntegrationActionReceiptV1["effect_outcome"], sequence = 1): IntegrationActionReceiptV1 {
  return { schema_version: 1, receipt_id: sequence === 1 ? uuid : "7b92f224-18f2-4939-b368-c199cc1cc291",
    run_id: uuid, sequence, submission_digest: ref.digest, operation_ref: ref, observed_at: instant, effect_outcome: outcome };
}
function admitted(value = run) { return { status: "admitted", run: value, replayed: false }; }
function deferred() {
  let resolve!: (value: unknown) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<unknown>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function props(overrides: Partial<IntegrationActionFormProps> = {}): IntegrationActionFormProps {
  return { element, contextKey: "org/project/principal/auth-1", grantRevision: 1, availability: "ready",
    onSubmit: vi.fn().mockResolvedValue(admitted()), onCheckStatus: vi.fn(), ...overrides };
}
function field(label: string) { return screen.getByLabelText(`${label} (required)`); }
function fill() {
  fireEvent.change(field("Amount"), { target: { value: "0" } });
  fireEvent.change(field("Enabled"), { target: { value: "false" } });
  fireEvent.change(field("Stage"), { target: { value: "open" } });
}
function submit() { fireEvent.submit(screen.getByRole("button", { name: "Create" }).closest("form")!); }
async function settle() { await act(async () => { await Promise.resolve(); }); }

beforeEach(() => { vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValue(uuid); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers(); });

describe("native integration action form", () => {
  it("keeps empty, zero, false and absent distinct, freezing the canonical request", async () => {
    const host = deferred();
    const input = props({ onSubmit: vi.fn(() => host.promise) });
    render(<IntegrationActionForm {...input} />);
    expect((field("Enabled") as HTMLSelectElement).value).toBe("");
    expect((field("Stage") as HTMLSelectElement).value).toBe("");
    expect((screen.getByLabelText("Note value mode") as HTMLSelectElement).value).toBe("absent");
    fill(); act(() => { submit(); submit(); });
    expect(input.onSubmit).toHaveBeenCalledTimes(1);
    const request = vi.mocked(input.onSubmit).mock.calls[0][0];
    expect(IntegrationActionSubmissionV1.parse(request).arguments).toEqual({ name: "", amount: 0, enabled: false, stage: "open" });
    expect(Object.isFrozen(request)).toBe(true);
    expect(Object.isFrozen(request.arguments)).toBe(true);
    expect(field("Amount")).toBeDisabled();
    expect(field("Amount")).toHaveValue("0");
    expect(screen.getByRole("status")).toHaveAttribute("aria-live", "polite");
    expect(screen.getByRole("status")).toHaveFocus();
    expect(document.body.textContent).not.toContain(input.contextKey);
    await act(async () => host.resolve(admitted()));
    expect(screen.getByText("Accepted. Awaiting execution.")).toBeTruthy();
    expect(screen.queryByText("Start another action")).toBeNull();
  });

  it.each([["null", null], ["value", ""]] as const)("submits explicit optional %s", async (mode, value) => {
    const input = props(); render(<IntegrationActionForm {...input} />); fill();
    fireEvent.change(screen.getByLabelText("Note value mode"), { target: { value: mode } });
    submit(); await settle();
    expect(vi.mocked(input.onSubmit).mock.calls[0][0].arguments.note).toBe(value);
  });

  it("accepts canonical field names that overlap Object prototype methods", async () => {
    const input = props({ element: { ...element, form: { submit_label: "Create", fields: [
      { id: "toString", label: "Value", kind: "text", required: true, nullable: false },
    ] } } });
    render(<IntegrationActionForm {...input} />); submit(); await settle();
    expect(vi.mocked(input.onSubmit).mock.calls[0][0].arguments).toEqual({ toString: "" });
  });

  it("links validation errors, focuses the first invalid field and retains all inputs", () => {
    const input = props(); render(<IntegrationActionForm {...input} />);
    fireEvent.change(field("Name"), { target: { value: "longer" } });
    submit();
    expect(field("Name")).toHaveFocus();
    expect(field("Name")).toHaveAttribute("aria-invalid", "true");
    expect(document.getElementById(field("Name").getAttribute("aria-describedby")!)?.textContent).toBe("Enter text within the allowed length.");
    expect(field("Name")).toHaveValue("longer");
    expect(input.onSubmit).not.toHaveBeenCalled();
    fireEvent.change(field("Name"), { target: { value: "" } });
    submit(); expect(field("Amount")).toHaveFocus();
  });

  it.each(["", " ", "0x10", "1junk", "Infinity", "1.5", "-1", "11", "1 "])("rejects invalid numeric input %j without coercion", (value) => {
    const input = props(); render(<IntegrationActionForm {...input} />); fill();
    fireEvent.change(field("Amount"), { target: { value } }); submit();
    expect(input.onSubmit).not.toHaveBeenCalled(); expect(field("Amount")).toHaveFocus();
  });

  it("leaves required booleans and choices unselected and validates them in order", () => {
    const input = props(); render(<IntegrationActionForm {...input} />);
    fireEvent.change(field("Amount"), { target: { value: "0" } }); submit();
    expect(field("Enabled")).toHaveFocus();
    fireEvent.change(field("Enabled"), { target: { value: "false" } }); submit();
    expect(field("Stage")).toHaveFocus(); expect(input.onSubmit).not.toHaveBeenCalled();
  });

  it("recovers only on explicit click with the identical frozen request and key", async () => {
    const input = props({ onSubmit: vi.fn().mockRejectedValue(new Error("private provider exception")) });
    render(<IntegrationActionForm {...input} />); fill(); submit(); await settle();
    expect(screen.getByText(/Outcome unknown\./)).toBeTruthy();
    expect(document.body.textContent).not.toContain("private provider exception");
    expect(screen.getByText(/It can execute the original action/)).toBeTruthy();
    expect(field("Name")).toBeDisabled();
    expect(input.onSubmit).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Recover submission" })); await settle();
    const calls = vi.mocked(input.onSubmit).mock.calls;
    expect(calls).toHaveLength(2); expect(calls[1][0]).toBe(calls[0][0]);
    expect(globalThis.crypto.randomUUID).toHaveBeenCalledTimes(1);
  });

  it.each([{}, admitted({ ...run, grant_revision: 2 }), admitted({ ...run, connection_id: "other" }),
    admitted({ ...run, connector_ref: { ...ref, digest: "cd".repeat(32) } }),
    admitted({ ...run, operation_ref: { ...ref, version: 2 } })])("refuses malformed or wrongly bound admission", async (response) => {
    render(<IntegrationActionForm {...props({ onSubmit: vi.fn().mockResolvedValue(response) })} />);
    fill(); submit(); await settle(); expect(screen.getByText(/Outcome unknown\./)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Start another action" })).toBeNull();
  });

  it("shows fixed conflict copy without a replacement key or recovery", async () => {
    render(<IntegrationActionForm {...props({ onSubmit: vi.fn().mockResolvedValue({ status: "refused", code: "idempotency_conflict" }) })} />);
    fill(); submit(); await settle();
    expect(screen.getByText(/conflicts with an existing request/)).toBeTruthy();
    expect(screen.queryByText("Recover submission")).toBeNull();
    expect(screen.queryByText("Start another action")).toBeNull();
    expect(globalThis.crypto.randomUUID).toHaveBeenCalledTimes(1);
  });

  it.each(["not_applied", "not_dispatched"] as const)("permits an explicit new action after terminal %s", async (effect_outcome) => {
    const input = props({ onSubmit: vi.fn().mockResolvedValue(admitted({ ...run, state: "failed", effect_outcome })) });
    render(<IntegrationActionForm {...input} />); fill(); submit(); await settle();
    expect(screen.getByRole("button", { name: "Start another action" })).toBeTruthy();
    expect(field("Amount")).toBeDisabled(); expect(input.onSubmit).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("list", { name: "Action history" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Start another action" }));
    expect(field("Amount")).not.toBeDisabled(); expect(field("Amount")).toHaveValue("0");
    expect(globalThis.crypto.randomUUID).toHaveBeenCalledTimes(1);
  });

  it("retains unknown and the active run when a status read rejects", async () => {
    const input = props({ onCheckStatus: vi.fn().mockRejectedValue(new Error("private status failure")) });
    render(<IntegrationActionForm {...input} />); fill(); submit(); await settle();
    fireEvent.click(screen.getByRole("button", { name: "Check status" })); await settle();
    expect(screen.getByText(/Outcome unknown\./)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Check status" })).toBeTruthy();
    expect(screen.queryByText("Recover submission")).toBeNull();
    expect(document.body.textContent).not.toContain("private status failure");
    expect(input.onSubmit).toHaveBeenCalledTimes(1);
  });

  it("checks status without writing, preserves unknown evidence, and resets terminal state explicitly", async () => {
    const uncertain = { ...run, state: "uncertain" as const, effect_outcome: "unknown" as const, revision: 3 };
    const succeeded = { ...run, state: "succeeded" as const, effect_outcome: "applied" as const, revision: 4 };
    const input = props({ onCheckStatus: vi.fn()
      .mockResolvedValueOnce(IntegrationActionSnapshotV1.parse({ run: { ...run, state: "running", effect_outcome: "unknown", revision: 2 }, receipts: [] }))
      .mockResolvedValueOnce(IntegrationActionSnapshotV1.parse({ run: uncertain, receipts: [receipt("unknown")] }))
      .mockResolvedValueOnce(IntegrationActionSnapshotV1.parse({ run: succeeded, receipts: [receipt("applied", 2)] })) });
    render(<IntegrationActionForm {...input} />); fill(); submit(); await settle();
    fireEvent.click(screen.getByRole("button", { name: "Check status" })); await settle();
    expect(screen.getByText("Running. Outcome pending.")).toBeTruthy();
    expect(screen.queryByText("Start another action")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Check status" })); await settle();
    expect(screen.getByText("Uncertain. Outcome unknown.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Check status" })); await settle();
    expect(within(screen.getByRole("list", { name: "Action history" })).getAllByRole("listitem")).toHaveLength(2);
    expect(input.onSubmit).toHaveBeenCalledTimes(1); expect(input.onCheckStatus).toHaveBeenCalledTimes(3);
    expect(vi.mocked(input.onCheckStatus).mock.calls[0][0]).toBe(uuid);
    fireEvent.click(screen.getByRole("button", { name: "Start another action" }));
    expect(field("Amount")).toHaveValue("0"); expect(field("Amount")).not.toBeDisabled();
    expect(screen.queryByRole("region", { name: "Action run status" })).toBeNull();
    expect(globalThis.crypto.randomUUID).toHaveBeenCalledTimes(1);
    submit(); await settle(); expect(globalThis.crypto.randomUUID).toHaveBeenCalledTimes(2);
  });

  it.each([{}, { run: { ...run, revision: 1, connection_id: "other" }, receipts: [] },
    { run: { ...run, revision: 1, run_id: "7b92f224-18f2-4939-b368-c199cc1cc291" }, receipts: [] },
    { run: { ...run, revision: 1 }, receipts: [] }])("retains unknown on malformed, wrong-run, wrong-binding or stale status", async (response) => {
    const input = props({ onSubmit: vi.fn().mockResolvedValue(admitted({ ...run, revision: 2 })), onCheckStatus: vi.fn().mockResolvedValue(response) });
    render(<IntegrationActionForm {...input} />); fill(); submit(); await settle();
    fireEvent.click(screen.getByRole("button", { name: "Check status" })); await settle();
    expect(screen.getByText(/Outcome unknown\./)).toBeTruthy(); expect(input.onSubmit).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("Recover submission")).toBeNull();
  });

  it.each(["unavailable", "forbidden", "stale"] as const)("blocks new writes and recovery when %s", async (availability) => {
    const input = props({ onSubmit: vi.fn().mockRejectedValue("hidden") });
    const view = render(<IntegrationActionForm {...input} availability={availability} />);
    submit(); expect(input.onSubmit).not.toHaveBeenCalled();
    view.rerender(<IntegrationActionForm {...input} />); fill(); submit(); await settle();
    view.rerender(<IntegrationActionForm {...input} availability={availability} />);
    expect(screen.getByRole("button", { name: "Recover submission" })).toBeDisabled();
  });

  it("refuses invalid props and unavailable secure UUID generation safely", () => {
    const input = props();
    const view = render(<IntegrationActionForm {...input} grantRevision={0} />);
    expect(screen.getByText("This action is unavailable.")).toBeTruthy();
    view.rerender(<IntegrationActionForm {...input} element={{ ...element, form: { ...element.form, fields: [element.form.fields[0], element.form.fields[0]] } }} />);
    expect(screen.queryByRole("button")).toBeNull();
    view.rerender(<IntegrationActionForm {...input} />); fill();
    vi.mocked(globalThis.crypto.randomUUID).mockImplementation(() => { throw new Error("secret"); });
    submit(); expect(screen.getByText(/Secure submission is unavailable/)).toBeTruthy();
    expect(input.onSubmit).not.toHaveBeenCalled(); expect(field("Amount")).not.toBeDisabled();
  });

  it.each(["context", "grant", "form", "binding"])("drops old inputs/results on %s change and aborts late completion", async (change) => {
    const host = deferred(); const input = props({ onSubmit: vi.fn(() => host.promise) });
    const view = render(<IntegrationActionForm {...input} />); fill(); submit();
    const signal = vi.mocked(input.onSubmit).mock.calls[0][1];
    const changed = change === "context" ? { contextKey: "new identity" } : change === "grant" ? { grantRevision: 2 } :
      { element: { ...element, ...(change === "binding" ? { connection_id: "other" } : { form: { ...element.form, submit_label: "Send" } }) } };
    view.rerender(<IntegrationActionForm {...input} {...changed} />);
    expect(signal.aborted).toBe(true); expect(field("Amount")).toHaveValue("");
    await act(async () => host.resolve(admitted()));
    expect(screen.queryByText("Accepted — awaiting execution")).toBeNull();
    expect(field("Amount")).not.toBeDisabled();
  });

  it("aborts on unmount and ignores the eventual host response", async () => {
    const host = deferred(); const input = props({ onSubmit: vi.fn(() => host.promise) });
    const view = render(<IntegrationActionForm {...input} />); fill(); submit(); view.unmount();
    expect(vi.mocked(input.onSubmit).mock.calls[0][1].aborted).toBe(true);
    await act(async () => host.resolve(admitted())); expect(view.container).toBeEmptyDOMElement();
  });

  it.each(["submit", "status"] as const)("bounds %s waits, aborts locally, and ignores late results", async (kind) => {
    vi.useFakeTimers(); const host = deferred();
    const input = props(kind === "submit" ? { onSubmit: vi.fn(() => host.promise) } : { onCheckStatus: vi.fn(() => host.promise) });
    render(<IntegrationActionForm {...input} />); fill(); submit(); await settle();
    if (kind === "status") fireEvent.click(screen.getByRole("button", { name: "Check status" }));
    const callback = kind === "submit" ? vi.mocked(input.onSubmit) : vi.mocked(input.onCheckStatus);
    const signal = callback.mock.calls[0][1];
    await act(async () => { vi.advanceTimersByTime(kind === "submit" ? 45000 : 15000); });
    expect(signal.aborted).toBe(true); expect(screen.getByText(/Outcome unknown\./)).toBeTruthy();
    await act(async () => host.resolve(kind === "submit" ? admitted() : { run, receipts: [] }));
    expect(screen.getByText(/Outcome unknown\./)).toBeTruthy();
    expect(field("Amount")).toBeDisabled();
  });

  it("uses native form and keyboard-capable controls with explicit labels", async () => {
    const input = props(); render(<IntegrationActionForm {...input} />); fill();
    field("Amount").focus(); fireEvent.keyDown(field("Amount"), { key: "Enter" });
    // jsdom does not implement Enter's native form default; dispatch that native submit event.
    submit(); await settle();
    expect(input.onSubmit).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Check status" }).getAttribute("type")).toBe("button");
  });
});

describe("safe action status", () => {
  it("formats canonical timestamps with shared timezone preferences and hides internal values", () => {
    const preferences = resolveDisplayPreferences({ timezone: "America/Chicago", hourFormat: "12h" });
    const failed = { ...run, state: "failed" as const, effect_outcome: "not_applied" as const };
    const view = render(<IntegrationActionRunStatus run={failed} receipts={[{ ...receipt("not_applied"), failure_code: "forbidden" }]} displayPreferences={preferences} mode="dark" />);
    expect(screen.getByText("Permission was denied.")).toBeTruthy();
    const expected = formatDatasetValue({ id: "timestamp", label: "Timestamp", type: { kind: "instant" }, nullable: false, sensitivity: "public" }, instant, preferences);
    expect(view.container.querySelector("time")?.textContent).toBe(expected);
    for (const hidden of [uuid, ref.digest, run.connection_id]) expect(view.container.textContent).not.toContain(hidden);
    expect(view.container.querySelector("section")?.getAttribute("style")).toContain("--leaf-background");
  });

  it("refuses invalid receipts and preferences, without exposing arbitrary fields", () => {
    const view = render(<IntegrationActionRunStatus run={run} receipts={[receipt("unknown")]} />);
    expect(screen.getByText("Action status is unavailable.")).toBeTruthy();
    view.rerender(<IntegrationActionRunStatus run={run} displayPreferences={{ locale: "en-US", timezone: "invalid", hourFormat: "24h" }} />);
    expect(screen.getByText("Action status is unavailable.")).toBeTruthy();
  });

  it("retains earlier unknown receipts when final reconciliation omits them", () => {
    const view = render(<IntegrationActionRunStatus run={{ ...run, state: "uncertain", effect_outcome: "unknown" }} receipts={[receipt("unknown")]} />);
    view.rerender(<IntegrationActionRunStatus run={{ ...run, revision: 2, state: "succeeded", effect_outcome: "applied" }} receipts={[receipt("applied", 2)]} />);
    expect(screen.getByText("Outcome unknown")).toBeTruthy();
    expect(screen.getByText("Action applied")).toBeTruthy();
  });
});
