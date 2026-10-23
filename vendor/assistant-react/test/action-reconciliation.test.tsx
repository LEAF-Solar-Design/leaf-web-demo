import React from "react";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  IntegrationActionElementV1, IntegrationActionRunV1, IntegrationActionSnapshotV1,
  type IntegrationActionReceiptV1,
} from "../../assistant-contract/src/integration";
import { IntegrationActionForm, type IntegrationActionFormProps } from "../src/actions";

const uuid = "7b92f224-18f2-4939-b368-c199cc1cc290";
const otherUuid = "7b92f224-18f2-4939-b368-c199cc1cc291";
const ref = { id: "sales", version: 1, digest: "ab".repeat(32) };
const instant = "2026-10-20T12:00:00Z";
const element = IntegrationActionElementV1.parse({
  schema_version: 1, id: "create-sale", title: "Create sale", kind: "integration.action",
  connection_id: "sales-account", connector_ref: ref, operation_ref: ref,
  form: { submit_label: "Create", fields: [
    { id: "name", label: "Name", kind: "text", required: true, nullable: false },
  ] },
});
const accepted = IntegrationActionRunV1.parse({
  schema_version: 1, run_id: uuid, revision: 1, connection_id: element.connection_id,
  connector_ref: ref, operation_ref: ref, submission_digest: ref.digest,
  authority_binding_digest: ref.digest, grant_revision: 1, created_at: instant,
  updated_at: instant, state: "accepted", effect_outcome: "not_dispatched",
});
const uncertain = IntegrationActionRunV1.parse({ ...accepted, revision: 2, state: "uncertain", effect_outcome: "unknown" });
const succeeded = IntegrationActionRunV1.parse({ ...accepted, revision: 3, state: "succeeded", effect_outcome: "applied" });
function receipt(effect_outcome: IntegrationActionReceiptV1["effect_outcome"], sequence = 1): IntegrationActionReceiptV1 {
  return { schema_version: 1, receipt_id: sequence === 1 ? uuid : otherUuid, run_id: uuid,
    sequence, submission_digest: ref.digest, operation_ref: ref, observed_at: instant, effect_outcome };
}
const unknownSnapshot = IntegrationActionSnapshotV1.parse({ run: uncertain, receipts: [receipt("unknown")] });
const resolvedSnapshot = IntegrationActionSnapshotV1.parse({ run: succeeded, receipts: [receipt("applied", 2)] });
function admitted(run = accepted) { return { status: "admitted", run, replayed: false }; }
function deferred() {
  let resolve!: (value: unknown) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<unknown>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function props(overrides: Partial<IntegrationActionFormProps> = {}): IntegrationActionFormProps {
  return { element, contextKey: "org/project/principal/auth-1", grantRevision: 1, availability: "ready",
    onSubmit: vi.fn().mockResolvedValue(admitted()),
    onCheckStatus: vi.fn().mockResolvedValue(unknownSnapshot),
    onReconcile: vi.fn().mockResolvedValue(resolvedSnapshot), ...overrides };
}
function button(name: string) { return screen.getByRole("button", { name }); }
function nameInput() { return screen.getByLabelText("Name (required)"); }
function submit() { fireEvent.submit(button("Create").closest("form")!); }
async function settle() { await act(async () => { await Promise.resolve(); }); }
async function observeUnknown(input: IntegrationActionFormProps) {
  const view = render(<IntegrationActionForm {...input} />);
  fireEvent.change(nameInput(), { target: { value: "Sale" } });
  submit(); await settle();
  fireEvent.click(button("Check status")); await settle();
  return view;
}
function expectUnknownEvidence() {
  expect(screen.getByText("Uncertain. Outcome unknown.")).toBeTruthy();
  expect(within(screen.getByRole("list", { name: "Action history" })).getAllByRole("listitem")).toHaveLength(1);
  expect(nameInput()).toHaveValue("Sale");
  expect(nameInput()).toBeDisabled();
  expect(screen.queryByRole("button", { name: "Start another action" })).toBeNull();
}

beforeEach(() => { vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValue(uuid); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers(); });

describe("explicit direct action reconciliation", () => {
  it("preserves status-only behavior when the optional callback is absent", async () => {
    const input = props({ onReconcile: undefined });
    await observeUnknown(input);
    expect(screen.queryByRole("button", { name: "Reconcile outcome" })).toBeNull();
    fireEvent.click(button("Check status")); await settle();
    expect(input.onCheckStatus).toHaveBeenCalledTimes(2);
    expectUnknownEvidence();
  });

  it.each([null, false, {}, "callback"])("fails closed for malformed callback %j", (callback) => {
    const input = props({ onReconcile: callback as unknown as IntegrationActionFormProps["onReconcile"] });
    render(<IntegrationActionForm {...input} />);
    expect(screen.getByText("This action is unavailable.")).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
    expect(input.onSubmit).not.toHaveBeenCalled();
    expect(input.onCheckStatus).not.toHaveBeenCalled();
  });

  it.each([
    accepted,
    { ...accepted, state: "running", effect_outcome: "unknown" },
    succeeded,
    { ...accepted, state: "failed", effect_outcome: "not_applied" },
  ])("does not offer reconciliation for $state", async (run) => {
    const input = props({ onSubmit: vi.fn().mockResolvedValue(admitted(IntegrationActionRunV1.parse(run))) });
    render(<IntegrationActionForm {...input} />);
    expect(screen.queryByRole("button", { name: "Reconcile outcome" })).toBeNull();
    submit(); await settle();
    expect(screen.queryByRole("button", { name: "Reconcile outcome" })).toBeNull();
    expect(input.onReconcile).not.toHaveBeenCalled();
  });

  it.each([{}, { status: "refused", code: "idempotency_conflict" }])("requires a known run without conflict", async (response) => {
    const input = props({ onSubmit: vi.fn().mockResolvedValue(response) });
    render(<IntegrationActionForm {...input} />); submit(); await settle();
    expect(screen.queryByRole("button", { name: "Reconcile outcome" })).toBeNull();
    expect(input.onReconcile).not.toHaveBeenCalled();
  });

  it("dispatches only on explicit click, suppresses bursts, and retains focused unknown evidence while pending", async () => {
    const host = deferred();
    const input = props({ onReconcile: vi.fn(() => host.promise) });
    const view = await observeUnknown(input);
    view.rerender(<IntegrationActionForm {...input} />); await settle();
    expect(input.onReconcile).not.toHaveBeenCalled();
    expect(screen.getByText(/Reconciliation checks provider evidence without repeating the action/)).toBeTruthy();
    const reconcile = button("Reconcile outcome");
    act(() => { fireEvent.click(reconcile); fireEvent.click(reconcile); fireEvent.click(reconcile); });
    expect(input.onReconcile).toHaveBeenCalledTimes(1);
    expect(vi.mocked(input.onReconcile!).mock.calls[0][0]).toBe(uuid);
    expect(vi.mocked(input.onReconcile!).mock.calls[0][1]).toBeInstanceOf(AbortSignal);
    const pending = screen.getByText("Reconciliation pending. The action outcome is still unknown.");
    expect(pending).toHaveAttribute("role", "status");
    expect(pending).toHaveAttribute("aria-live", "polite");
    expect(pending).toHaveFocus();
    expect(reconcile).toBeDisabled();
    expectUnknownEvidence();
    submit();
    expect(input.onSubmit).toHaveBeenCalledTimes(1);
    expect(input.onCheckStatus).toHaveBeenCalledTimes(1);
    expect(globalThis.crypto.randomUUID).toHaveBeenCalledTimes(1);
    expect(vi.mocked(input.onSubmit).mock.calls[0][0].arguments).toEqual({ name: "Sale" });
    await act(async () => host.resolve(resolvedSnapshot));
    expect(screen.getByText("The action succeeded and was applied.")).toBeTruthy();
    const history = within(screen.getByRole("list", { name: "Action history" }));
    expect(history.getAllByRole("listitem")).toHaveLength(2);
    expect(history.getByText("Outcome unknown")).toBeTruthy();
    expect(history.getByText("Action applied")).toBeTruthy();
    expect(button("Start another action")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Reconcile outcome" })).toBeNull();
    expect(input.onSubmit).toHaveBeenCalledTimes(1);
    expect(input.onCheckStatus).toHaveBeenCalledTimes(1);
  });

  it("keeps a valid unresolved snapshot uncertain without automatic retry", async () => {
    const input = props({ onReconcile: vi.fn().mockResolvedValue({ ...unknownSnapshot, run: { ...uncertain, revision: 3 } }) });
    await observeUnknown(input);
    fireEvent.click(button("Reconcile outcome")); await settle(); await settle();
    expectUnknownEvidence();
    expect(screen.getByText(/Outcome unknown\. The action may have executed/)).toBeTruthy();
    expect(button("Reconcile outcome")).not.toBeDisabled();
    expect(input.onReconcile).toHaveBeenCalledTimes(1);
  });

  it("settles a confirmed not-applied result on the original run", async () => {
    const input = props({ onReconcile: vi.fn().mockResolvedValue({
      run: { ...succeeded, state: "failed", effect_outcome: "not_applied" }, receipts: [receipt("not_applied", 2)],
    }) });
    await observeUnknown(input); fireEvent.click(button("Reconcile outcome")); await settle();
    expect(screen.getByText("The action failed and was not applied.")).toBeTruthy();
    expect(within(screen.getByRole("list", { name: "Action history" })).getAllByRole("listitem")).toHaveLength(2);
    expect(button("Start another action")).toBeTruthy();
    expect(input.onSubmit).toHaveBeenCalledTimes(1);
  });

  const rejectedSnapshots = [
    ["malformed", {}],
    ["cross-run", { run: { ...succeeded, run_id: otherUuid }, receipts: [{ ...receipt("applied", 2), run_id: otherUuid }] }],
    ["stale", { run: { ...succeeded, revision: 1 }, receipts: [receipt("applied", 2)] }],
    ["same revision changed", { run: { ...succeeded, revision: 2 }, receipts: [receipt("applied", 2)] }],
    ["connection rebound", { run: { ...succeeded, connection_id: "other" }, receipts: [receipt("applied", 2)] }],
    ["grant rebound", { run: { ...succeeded, grant_revision: 2 }, receipts: [receipt("applied", 2)] }],
    ["connector rebound", { run: { ...succeeded, connector_ref: { ...ref, version: 2 } }, receipts: [receipt("applied", 2)] }],
    ["operation rebound", { run: { ...succeeded, operation_ref: { ...ref, version: 2 } }, receipts: [{ ...receipt("applied", 2), operation_ref: { ...ref, version: 2 } }] }],
    ["submission rebound", { run: { ...succeeded, submission_digest: "cd".repeat(32) }, receipts: [{ ...receipt("applied", 2), submission_digest: "cd".repeat(32) }] }],
    ["authority rebound", { run: { ...succeeded, authority_binding_digest: "cd".repeat(32) }, receipts: [receipt("applied", 2)] }],
    ["changed observed receipt", { run: succeeded, receipts: [{ ...receipt("unknown"), failure_code: "timeout" }, receipt("applied", 2)] }],
    ["replaced observed receipt", { run: succeeded, receipts: [receipt("applied")] }],
    ["conflicting sequence", { run: succeeded, receipts: [{ ...receipt("applied", 2), sequence: 1 }] }],
    ["wrong receipt binding", { run: succeeded, receipts: [{ ...receipt("applied", 2), run_id: otherUuid }] }],
  ] as const;
  it.each(rejectedSnapshots)("preserves observed evidence and unknown outcome for %s", async (_name, response) => {
    const input = props({ onReconcile: vi.fn().mockResolvedValue(response) });
    await observeUnknown(input); fireEvent.click(button("Reconcile outcome")); await settle();
    expectUnknownEvidence();
    expect(screen.getByText(/Outcome unknown\. The action may have executed/)).toBeTruthy();
    expect(screen.queryByText("Action applied")).toBeNull();
    expect(input.onSubmit).toHaveBeenCalledTimes(1);
    expect(input.onCheckStatus).toHaveBeenCalledTimes(1);
  });

  it.each(["throw", "reject", "abort"])("sanitizes %s failures and never retries automatically", async (failure) => {
    const input = props({ onReconcile: vi.fn(() => {
      if (failure === "throw") throw new Error("private provider credential");
      return Promise.reject(failure === "abort" ? new DOMException("private cancellation", "AbortError") : new Error("private provider credential"));
    }) });
    await observeUnknown(input); fireEvent.click(button("Reconcile outcome")); await settle(); await settle();
    expectUnknownEvidence();
    expect(screen.getByText(/Local cancellation does not undo an action/)).toBeTruthy();
    expect(document.body.textContent).not.toContain("private");
    expect(input.onReconcile).toHaveBeenCalledTimes(1);
    expect(button("Reconcile outcome")).not.toBeDisabled();
  });

  it("bounds reconciliation at 45 seconds and ignores a late terminal response even after another attempt starts", async () => {
    vi.useFakeTimers();
    const first = deferred(); const second = deferred();
    const input = props({ onReconcile: vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise) });
    await observeUnknown(input); fireEvent.click(button("Reconcile outcome"));
    const signal = vi.mocked(input.onReconcile!).mock.calls[0][1];
    await act(async () => { vi.advanceTimersByTime(44999); });
    expect(signal.aborted).toBe(false);
    expect(screen.getByText(/Reconciliation pending/)).toBeTruthy();
    await act(async () => { vi.advanceTimersByTime(1); });
    expect(signal.aborted).toBe(true); expectUnknownEvidence();
    expect(screen.getByText(/Local cancellation does not undo an action/)).toBeTruthy();
    expect(input.onReconcile).toHaveBeenCalledTimes(1);
    fireEvent.click(button("Reconcile outcome"));
    await act(async () => first.resolve(resolvedSnapshot));
    expectUnknownEvidence();
    expect(screen.getByText(/Reconciliation pending/)).toBeTruthy();
    await act(async () => second.resolve(unknownSnapshot));
    expectUnknownEvidence();
  });

  it.each(["context", "grant", "element", "unmount"])("aborts and ignores pending reconciliation on %s invalidation", async (change) => {
    const host = deferred(); const input = props({ onReconcile: vi.fn(() => host.promise) });
    const view = await observeUnknown(input); fireEvent.click(button("Reconcile outcome"));
    const signal = vi.mocked(input.onReconcile!).mock.calls[0][1];
    if (change === "unmount") view.unmount();
    else view.rerender(<IntegrationActionForm {...input} {...(change === "context" ? { contextKey: "new identity" } :
      change === "grant" ? { grantRevision: 2 } : { element: { ...element, title: "Changed action" } })} />);
    expect(signal.aborted).toBe(true);
    await act(async () => host.resolve(resolvedSnapshot));
    expect(screen.queryByText("The action succeeded and was applied.")).toBeNull();
    expect(screen.queryByRole("list", { name: "Action history" })).toBeNull();
    if (change !== "unmount") { expect(nameInput()).toHaveValue(""); expect(nameInput()).not.toBeDisabled(); }
    else expect(view.container).toBeEmptyDOMElement();
    expect(input.onReconcile).toHaveBeenCalledTimes(1);
  });

  it.each(["unavailable", "forbidden", "stale", "callback removed"] as const)("retains evidence when %s cancels reconciliation and does not retry on restoration", async (change) => {
    const host = deferred(); const input = props({ onReconcile: vi.fn(() => host.promise) });
    const view = await observeUnknown(input); fireEvent.click(button("Reconcile outcome"));
    const signal = vi.mocked(input.onReconcile!).mock.calls[0][1];
    view.rerender(<IntegrationActionForm {...input} {...(change === "callback removed" ? { onReconcile: undefined } : { availability: change })} />);
    expect(signal.aborted).toBe(true); expectUnknownEvidence();
    expect(screen.getByText(/Local cancellation does not undo an action/)).toBeTruthy();
    const control = screen.queryByRole("button", { name: "Reconcile outcome" });
    if (control) expect(control).toBeDisabled();
    await act(async () => host.resolve(resolvedSnapshot));
    expectUnknownEvidence();
    view.rerender(<IntegrationActionForm {...input} />); await settle();
    expectUnknownEvidence();
    expect(button("Reconcile outcome")).not.toBeDisabled();
    expect(input.onReconcile).toHaveBeenCalledTimes(1);
    expect(input.onSubmit).toHaveBeenCalledTimes(1);
    expect(input.onCheckStatus).toHaveBeenCalledTimes(1);
  });

  it.each(["unavailable", "forbidden", "stale"] as const)("blocks reconciliation while %s before a click", async (availability) => {
    const input = props(); const view = await observeUnknown(input);
    view.rerender(<IntegrationActionForm {...input} availability={availability} />);
    expect(button("Reconcile outcome")).toBeDisabled(); fireEvent.click(button("Reconcile outcome"));
    expect(input.onReconcile).not.toHaveBeenCalled(); expectUnknownEvidence();
  });
});
