import React from "react";
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import {
  IntegrationManualWorkflowSnapshotV1, type IntegrationActionReceiptV1,
  type IntegrationActionSnapshotV1,
} from "../../assistant-contract/src/integration";
import { IntegrationManualWorkflowRunStatus, type IntegrationManualWorkflowRunStatusProps } from "../src/actions";
import { formatDatasetValue, resolveDisplayPreferences } from "../src/datasets/formatting";
import { tokenStyle } from "../src/tokens";
import { updateManualWorkflowStatus } from "../src/actions/manual-workflow-status-state";

const instant = "2026-10-20T12:00:00Z";
const later = "2026-10-20T13:00:00Z";
const ref = { id: "workflow", version: 1, digest: "ab".repeat(32) };
const contextKey = "private-caller/project/auth";
const id = (value: number) => `7b92f224-18f2-4939-b368-${String(value).padStart(12, "0")}`;
type Snapshot = IntegrationManualWorkflowSnapshotV1;
type ChildState = IntegrationActionSnapshotV1["run"]["state"];

function child(index: number, state: ChildState, revision = 1): IntegrationActionSnapshotV1 {
  const effect_outcome = state === "succeeded" ? "applied" : state === "failed" ? "not_applied"
    : state === "accepted" ? "not_dispatched" : "unknown";
  return {
    run: { schema_version: 1, run_id: id(index + 10), revision, connection_id: "private-connection",
      connector_ref: ref, operation_ref: ref, submission_digest: "cd".repeat(32),
      authority_binding_digest: "ef".repeat(32), grant_revision: 1,
      created_at: instant, updated_at: later, state, effect_outcome },
    receipts: state === "accepted" || state === "running" ? [] : [{ schema_version: 1,
      receipt_id: id(index + 20), run_id: id(index + 10), sequence: 1,
      submission_digest: "cd".repeat(32), operation_ref: ref, observed_at: instant, effect_outcome }],
  };
}

function workflow(states: (ChildState | "pending" | "blocked")[] = ["pending", "pending"], state: Snapshot["run"]["state"] = "accepted"): Snapshot {
  return IntegrationManualWorkflowSnapshotV1.parse({
    definition: { schema_version: 1, id: ref.id, version: 1, title: "Create records", trigger: "manual", authority_mode: "caller_bound",
      steps: states.map((_, index) => ({ id: `step-${index + 1}`, kind: "action", connection_id: "private-connection",
        connector_ref: ref, operation_ref: ref, arguments: { hidden: "private-argument" } })) },
    run: { schema_version: 1, run_id: id(1), revision: 1, workflow_ref: ref, submission_digest: ref.digest,
      authority_binding_digest: ref.digest, authority_mode: "caller_bound", created_at: instant, updated_at: later, state },
    steps: states.map((step, index) => ({ run_id: id(1), step_id: `step-${index + 1}`,
      ...(step === "pending" ? { state: "pending" } : step === "blocked"
        ? { state: "blocked", effect_outcome: "not_dispatched", failure_code: "forbidden" }
        : { state: "action", action: child(index, step) }) })),
  });
}

function props(snapshot = workflow()): IntegrationManualWorkflowRunStatusProps { return { snapshot, contextKey }; }
function withAction(snapshot: Snapshot, change: (action: IntegrationActionSnapshotV1) => void): Snapshot {
  const next = structuredClone(snapshot);
  const step = next.steps[0];
  if (step.state !== "action") throw new Error("Expected action fixture");
  change(step.action);
  return next;
}
function reconcile(snapshot: Snapshot): Snapshot {
  return withAction(snapshot, (action) => {
    action.run = { ...action.run, revision: 2, state: "succeeded", effect_outcome: "applied" };
    const applied: IntegrationActionReceiptV1 = { ...action.receipts[0], receipt_id: id(99), sequence: 2,
      observed_at: later, effect_outcome: "applied" };
    action.receipts = [applied];
  });
}

afterEach(cleanup);

describe("manual workflow status", () => {
  it("renders accepted pending steps in order without controls or asserting no dispatch", () => {
    const snapshot = workflow(Array(8).fill("pending"));
    const { container } = render(<IntegrationManualWorkflowRunStatus {...props(snapshot)} />);
    expect(screen.getByRole("status")).toHaveTextContent("Accepted. Awaiting execution results.");
    const list = screen.getByRole("list", { name: "Workflow steps" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(8);
    expect(within(list).getAllByRole("heading").map((heading) => heading.textContent)).toEqual(
      Array.from({ length: 8 }, (_, index) => `Step ${index + 1}`));
    expect(screen.getAllByText("Pending. No execution result recorded.")).toHaveLength(8);
    expect(container.textContent).not.toMatch(/not dispatched|not sent|unsent/i);
    expect(container.querySelector("button, input, select, textarea, a, form")).toBeNull();
    expect(document.activeElement).toBe(document.body);
  });

  it.each(["uncertain", "failed", "blocked"] as const)("preserves an applied prefix with a %s frontier", (frontier) => {
    const snapshot = workflow(["succeeded", frontier, "pending"], frontier === "uncertain" ? "uncertain" : "failed");
    const { container } = render(<IntegrationManualWorkflowRunStatus {...props(snapshot)} />);
    expect(within(screen.getByRole("region", { name: "Step 1" })).getByRole("list", { name: "Action history" })).toHaveTextContent("Action applied");
    expect(screen.getByText("1 of 3 step actions recorded as applied.")).toBeTruthy();
    expect(container.textContent).not.toMatch(/Workflow succeeded|rolled back|all actions failed/i);
    if (frontier === "blocked") expect(screen.getByText("The step was not dispatched. Permission was denied.")).toBeTruthy();
    if (frontier === "uncertain") expect(screen.getByRole("status")).toHaveTextContent("An action outcome is unknown.");
  });

  it.each(["uncertain", "failed", "blocked", "succeeded"] as const)("keeps a running parent with %s children pending settlement", (frontier) => {
    const snapshot = workflow(["succeeded", frontier], "running");
    const view = render(<IntegrationManualWorkflowRunStatus {...props(snapshot)} />);
    const summary = screen.getByRole("status");
    if (frontier === "succeeded") {
      expect(summary).toHaveTextContent("All step actions applied. Workflow completion has not been recorded.");
      view.rerender(<IntegrationManualWorkflowRunStatus {...props({ ...snapshot, run: { ...snapshot.run, revision: 2, state: "succeeded" } })} />);
      expect(screen.getByRole("status")).toHaveTextContent("Workflow succeeded.");
    } else {
      expect(summary).toHaveTextContent("Workflow settlement is pending.");
      expect(summary).not.toHaveTextContent("Workflow succeeded");
      if (frontier === "uncertain") expect(summary).toHaveTextContent("An action outcome is unknown.");
    }
  });

  it("retains verified success when a newer parent regresses, and accepts later successful evidence", () => {
    const succeeded = workflow(["succeeded", "succeeded"], "succeeded");
    const initial = updateManualWorkflowStatus(undefined, contextKey, succeeded)!;
    const regressed = { ...succeeded, run: { ...succeeded.run, revision: 2, state: "running" as const } };
    expect(IntegrationManualWorkflowSnapshotV1.safeParse(regressed).success).toBe(true);
    const rejected = updateManualWorkflowStatus(initial, contextKey, regressed)!;
    expect(rejected.unverified).toBe(true);
    expect(rejected.snapshot).toEqual(succeeded);
    const view = render(<IntegrationManualWorkflowRunStatus {...props(succeeded)} />);
    view.rerender(<IntegrationManualWorkflowRunStatus {...props(regressed)} />);
    expect(screen.getByRole("status")).toHaveTextContent("Workflow update could not be verified. Showing last verified evidence.");
    expect(screen.getAllByText("Succeeded. Action applied.")).toHaveLength(2);
    expect(screen.queryByText("All step actions applied. Workflow completion has not been recorded.")).toBeNull();
    const laterSuccess = { ...succeeded, run: { ...succeeded.run, revision: 3 } };
    const accepted = updateManualWorkflowStatus(rejected, contextKey, laterSuccess)!;
    expect(accepted.unverified).toBe(false);
    expect(accepted.snapshot?.run.revision).toBe(3);
    expect(accepted.snapshot?.run.state).toBe("succeeded");
    view.rerender(<IntegrationManualWorkflowRunStatus {...props(laterSuccess)} />);
    expect(screen.getByRole("status")).toHaveTextContent("Workflow succeeded.");
  });

  it("renders ordinary running child evidence and later pending steps", () => {
    render(<IntegrationManualWorkflowRunStatus {...props(workflow(["succeeded", "running", "pending"], "running"))} />);
    expect(screen.getByRole("status")).toHaveTextContent("Workflow in progress.");
    expect(within(screen.getByRole("region", { name: "Step 2" })).getByText("Running. Outcome pending.")).toBeTruthy();
    expect(within(screen.getByRole("region", { name: "Step 3" })).getByText("Pending. No execution result recorded.")).toBeTruthy();
  });

  it("merges unknown-to-applied observations at equal parent revision even when earlier receipts are omitted", () => {
    const initial = workflow(["uncertain"], "running");
    const view = render(<IntegrationManualWorkflowRunStatus {...props(initial)} />);
    const next = reconcile(initial);
    expect(next.run.revision).toBe(initial.run.revision);
    view.rerender(<IntegrationManualWorkflowRunStatus {...props(next)} />);
    const history = screen.getByRole("list", { name: "Action history" });
    expect(within(history).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      expect.stringContaining("Outcome unknown"), expect.stringContaining("Action applied"),
    ]);
    expect(screen.getByRole("status")).toHaveTextContent("Workflow completion has not been recorded.");
    view.rerender(<IntegrationManualWorkflowRunStatus {...props(next)} />);
    expect(within(screen.getByRole("list", { name: "Action history" })).getAllByRole("listitem")).toHaveLength(2);
  });

  it.each(["parent revision", "parent fields", "parent binding", "workflow reference", "parent creation", "definition", "child revision", "child fields", "child identity", "child binding", "child creation", "duplicate receipt", "pending regression", "success regression"])(
    "rejects changed %s while retaining explicitly labeled evidence", (mutation) => {
      const initial = workflow(["uncertain"], "running");
      const verified = reconcile(initial);
      verified.run.revision = 2;
      const view = render(<IntegrationManualWorkflowRunStatus {...props(initial)} />);
      view.rerender(<IntegrationManualWorkflowRunStatus {...props(verified)} />);
      let bad = structuredClone(verified);
      if (mutation === "parent revision") bad.run.revision = 1;
      if (mutation === "parent fields") bad.run.state = "succeeded";
      if (mutation === "parent binding") { bad.run.revision++; bad.run.authority_binding_digest = "11".repeat(32); }
      if (mutation === "workflow reference") { bad.run.revision++; bad.run.workflow_ref.digest = "11".repeat(32); }
      if (mutation === "parent creation") { bad.run.revision++; bad.run.created_at = "2026-10-19T12:00:00Z"; }
      if (mutation === "definition") bad.definition.title = "substituted title";
      if (mutation === "pending regression") bad.steps = workflow(["pending"], "running").steps;
      if (mutation === "success regression") bad = withAction(bad, (action) => {
        action.run = { ...action.run, revision: 3, state: "uncertain", effect_outcome: "unknown" };
        action.receipts = [{ ...child(0, "uncertain").receipts[0], receipt_id: id(88), sequence: 3 }];
      });
      if (mutation.startsWith("child") || mutation === "duplicate receipt") bad = withAction(bad, (action) => {
        if (mutation === "child revision") action.run.revision = 1;
        if (mutation === "child fields") action.run.updated_at = "2026-10-20T14:00:00Z";
        if (mutation === "child identity") {
          action.run.run_id = id(77);
          action.receipts.forEach((receipt) => { receipt.run_id = id(77); });
        }
        if (mutation === "child binding") { action.run.revision++; action.run.authority_binding_digest = "11".repeat(32); }
        if (mutation === "child creation") { action.run.revision++; action.run.created_at = "2026-10-19T12:00:00Z"; }
        if (mutation === "duplicate receipt") action.receipts[0].observed_at = instant;
      });
      expect(IntegrationManualWorkflowSnapshotV1.safeParse(bad).success).toBe(true);
      view.rerender(<IntegrationManualWorkflowRunStatus {...props(bad)} />);
      expect(screen.getByRole("status")).toHaveTextContent("Workflow update could not be verified. Showing last verified evidence.");
      expect(screen.getByText("Last verified evidence")).toBeTruthy();
      expect(screen.getByRole("status")).not.toHaveTextContent("Workflow succeeded");
      expect(screen.getByText("1 of 1 step actions recorded as applied.")).toBeTruthy();
      expect(within(screen.getByRole("list", { name: "Action history" })).getAllByRole("listitem")).toHaveLength(2);
    },
  );

  it("rejects malformed same-run input without exposing it, then accepts a valid update", () => {
    const initial = workflow(["uncertain"], "running");
    const view = render(<IntegrationManualWorkflowRunStatus {...props(initial)} />);
    const bad = { ...initial, private_error: "secret provider exception" } as Snapshot;
    view.rerender(<IntegrationManualWorkflowRunStatus {...props(bad)} />);
    expect(screen.getByText("Last verified evidence")).toBeTruthy();
    expect(document.body.textContent).not.toContain("secret provider exception");
    view.rerender(<IntegrationManualWorkflowRunStatus {...props(reconcile(initial))} />);
    expect(screen.queryByText("Last verified evidence")).toBeNull();
    expect(screen.getByText("1 of 1 step actions recorded as applied.")).toBeTruthy();
  });

  it.each(["context", "run", "missing run", "invalid context"])("clears old evidence immediately on %s changes, including invalid replacements", (change) => {
    const initial = workflow(["succeeded"], "succeeded");
    const view = render(<IntegrationManualWorkflowRunStatus {...props(initial)} />);
    const replacement = { ...props(initial), snapshot: { ...initial, private_error: "do not display" } as Snapshot };
    if (change === "context") replacement.contextKey = "another-caller";
    if (change === "invalid context") replacement.contextKey = " ";
    if (change === "run") replacement.snapshot.run = { ...initial.run, run_id: id(2) };
    if (change === "missing run") replacement.snapshot = {} as Snapshot;
    view.rerender(<IntegrationManualWorkflowRunStatus {...replacement} />);
    expect(document.body.textContent).toBe("Workflow status is unavailable.");
    // Switching back is a new observation, not resurrection of old retained evidence.
    view.rerender(<IntegrationManualWorkflowRunStatus {...props({ ...initial, extra: true } as Snapshot)} />);
    expect(document.body.textContent).toBe("Workflow status is unavailable.");
  });

  it("accepts a valid new context and run without retaining previous receipts", () => {
    const view = render(<IntegrationManualWorkflowRunStatus {...props(workflow(["uncertain"], "running"))} />);
    const next = workflow(); next.run.run_id = id(2); next.steps.forEach((step) => { step.run_id = id(2); });
    view.rerender(<IntegrationManualWorkflowRunStatus {...props(next)} contextKey="new-context" />);
    expect(screen.getByRole("status")).toHaveTextContent("Accepted. Awaiting execution results.");
    expect(screen.queryByRole("list", { name: "Action history" })).toBeNull();
  });

  it.each([
    { mode: "private-mode" }, { displayPreferences: { timezone: "private-zone" } },
    { displayPreferences: { locale: "private_locale" } }, { displayPreferences: { extra: "private-option" } },
    { contextKey: "" }, { contextKey: "x".repeat(513) }, { snapshot: null },
  ])("uses fixed unavailable copy for invalid runtime props %j", (invalid) => {
    render(<IntegrationManualWorkflowRunStatus {...({ ...props(), ...invalid } as IntegrationManualWorkflowRunStatusProps)} />);
    expect(document.body.textContent).toBe("Workflow status is unavailable.");
  });

  it.each(["light", "dark"] as const)("uses named regions, shared timestamps, wrapping and %s tokens without HTML injection", (mode) => {
    const snapshot = workflow(["succeeded"], "succeeded");
    snapshot.definition.title = "<img src=x onerror=alert(1)>" + "W".repeat(90);
    const preferences = resolveDisplayPreferences({ locale: "en-US", timezone: "America/Chicago", hourFormat: "12h" });
    const { container } = render(<IntegrationManualWorkflowRunStatus {...props(snapshot)} mode={mode} displayPreferences={preferences} />);
    const region = screen.getByRole("region", { name: "Manual workflow run status" });
    expect(region).toHaveStyle({ minWidth: 0, overflowWrap: "anywhere" });
    expect(screen.getByRole("heading", { name: snapshot.definition.title })).toHaveStyle({ minWidth: 0, overflowWrap: "anywhere" });
    expect(screen.getByRole("region", { name: "Step 1" })).toHaveStyle({ minWidth: 0, overflowWrap: "anywhere" });
    expect(screen.getByRole("list", { name: "Workflow steps" })).toHaveStyle({ minWidth: 0 });
    for (const [name, value] of Object.entries(tokenStyle(mode))) {
      if (name.startsWith("--")) expect(region.style.getPropertyValue(name)).toBe(value);
    }
    expect(screen.getByRole("status")).toHaveAttribute("aria-live", "polite");
    const field = { id: "timestamp", label: "Timestamp", type: { kind: "instant" as const }, nullable: false, sensitivity: "public" as const };
    expect(container.querySelector(`time[datetime="${instant}"]`)).toHaveTextContent(formatDatasetValue(field, instant, preferences));
    expect(container.querySelector(`time[datetime="${later}"]`)).toHaveTextContent(formatDatasetValue(field, later, preferences));
    expect(screen.getByRole("list", { name: "Action history" }).closest('[aria-live]')).toBeNull();
    expect(container.querySelector("img, script, button, input, form, [tabindex]")).toBeNull();
    for (const hidden of [contextKey, "private-connection", "private-argument", ref.digest, "cd".repeat(32)]) expect(container.textContent).not.toContain(hidden);
    expect(document.activeElement).toBe(document.body);
  });

  it("keeps the pure guard bounded to one run and does not mutate caller snapshots", () => {
    const initial = workflow(["uncertain"], "running");
    const before = JSON.stringify(initial);
    const first = updateManualWorkflowStatus(undefined, contextKey, initial)!;
    const next = reconcile(initial), nextBefore = JSON.stringify(next);
    const state = updateManualWorkflowStatus(first, contextKey, next)!;
    expect(JSON.stringify(initial)).toBe(before);
    expect(JSON.stringify(next)).toBe(nextBefore);
    expect(state.snapshot?.steps[0].state === "action" && state.snapshot.steps[0].action.receipts).toHaveLength(2);
    expect(updateManualWorkflowStatus(state, "", next)).toBeUndefined();
    expect(updateManualWorkflowStatus(state, "other", {})?.snapshot).toBeUndefined();
  });

  it("rejects a merged receipt history beyond the canonical bound without dropping old observations", () => {
    const initial = withAction(workflow(["uncertain"], "running"), (action) => {
      const observation = action.receipts[0];
      action.receipts = Array.from({ length: 100 }, (_, index) => ({ ...observation, receipt_id: id(200 + index), sequence: index + 1 }));
    });
    expect(IntegrationManualWorkflowSnapshotV1.safeParse(initial).success).toBe(true);
    const first = updateManualWorkflowStatus(undefined, contextKey, initial)!;
    const next = withAction(initial, (action) => {
      action.run = { ...action.run, revision: 2, state: "succeeded", effect_outcome: "applied" };
      action.receipts = [{ ...action.receipts[0], receipt_id: id(999), sequence: 101, effect_outcome: "applied" }];
    });
    expect(IntegrationManualWorkflowSnapshotV1.safeParse(next).success).toBe(true);
    const rejected = updateManualWorkflowStatus(first, contextKey, next)!;
    expect(rejected.unverified).toBe(true);
    expect(rejected.snapshot).toBe(first.snapshot);
    const retained = rejected.snapshot!.steps[0];
    expect(retained.state === "action" && retained.action.receipts).toHaveLength(100);
  });
});
