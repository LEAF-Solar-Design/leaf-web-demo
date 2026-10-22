import { IntegrationManualWorkflowSnapshotV1 } from "../../../assistant-contract/src/integration";
import { sameRef, sameRun } from "./action-form-state";

export interface ManualWorkflowStatusState {
  contextKey: string;
  runId: string;
  snapshot?: IntegrationManualWorkflowSnapshotV1;
  unverified: boolean;
}

/** One context/run only. Schema validation is evidence validation, not authorization. */
export function updateManualWorkflowStatus(
  previous: ManualWorkflowStatusState | undefined, contextKey: unknown, input: unknown,
): ManualWorkflowStatusState | undefined {
  if (typeof contextKey !== "string" || contextKey.trim().length === 0 || contextKey.length > 512) return undefined;
  const rawRun = input && typeof input === "object" && "run" in input ? input.run : undefined;
  const runId = rawRun && typeof rawRun === "object" && "run_id" in rawRun && typeof rawRun.run_id === "string"
    ? rawRun.run_id : "";
  const prior = previous?.contextKey === contextKey && previous.runId === runId ? previous.snapshot : undefined;
  const rejected = (): ManualWorkflowStatusState => ({ contextKey, runId, snapshot: prior, unverified: true });
  const parsed = IntegrationManualWorkflowSnapshotV1.safeParse(input);
  if (!parsed.success) return rejected();
  const next = parsed.data;
  if (prior) {
    const a = next.run, b = prior.run;
    if (!sameRef(a.workflow_ref, b.workflow_ref) || a.submission_digest !== b.submission_digest ||
        a.authority_binding_digest !== b.authority_binding_digest || a.authority_mode !== b.authority_mode ||
        a.created_at !== b.created_at || a.revision < b.revision ||
        (b.state === "succeeded" && a.state !== "succeeded") ||
        (a.revision === b.revision && JSON.stringify(a) !== JSON.stringify(b)) ||
        JSON.stringify(next.definition) !== JSON.stringify(prior.definition)) return rejected();
    for (const [index, step] of next.steps.entries()) {
      const old = prior.steps[index];
      if (old.run_id !== step.run_id || old.step_id !== step.step_id ||
          (old.state !== "pending" && step.state === "pending")) return rejected();
      if (old.state !== "action") continue;
      if (step.state !== "action") return rejected();
      const child = step.action.run, previousChild = old.action.run;
      if (!sameRun(child, previousChild) || child.created_at !== previousChild.created_at ||
          child.connection_id !== previousChild.connection_id || child.grant_revision !== previousChild.grant_revision ||
          !sameRef(child.connector_ref, previousChild.connector_ref) || !sameRef(child.operation_ref, previousChild.operation_ref) ||
          (previousChild.state === "succeeded" && child.state !== "succeeded")) return rejected();
      const receipts = [...old.action.receipts];
      for (const receipt of step.action.receipts) {
        const duplicate = receipts.find((item) => item.receipt_id.toLowerCase() === receipt.receipt_id.toLowerCase());
        if (duplicate && JSON.stringify(duplicate) !== JSON.stringify(receipt)) return rejected();
        if (!duplicate) receipts.push(receipt);
      }
      receipts.sort((left, right) => left.sequence - right.sequence);
      step.action.receipts = receipts;
    }
  }
  const merged = IntegrationManualWorkflowSnapshotV1.safeParse(next);
  return merged.success ? { contextKey, runId, snapshot: merged.data, unverified: false } : rejected();
}

export function manualWorkflowSummary(snapshot: IntegrationManualWorkflowSnapshotV1): string {
  const { run, steps } = snapshot;
  if (run.state === "accepted") return "Accepted. Awaiting execution results.";
  if (run.state === "succeeded") return "Workflow succeeded.";
  if (run.state === "failed") return "Workflow failed. Inspect the recorded step evidence for individual action outcomes.";
  if (run.state === "uncertain") return "Workflow outcome uncertain. An action outcome is unknown.";
  if (steps.every((step) => step.state === "action" && step.action.run.state === "succeeded")) {
    return "All step actions applied. Workflow completion has not been recorded.";
  }
  if (steps.some((step) => step.state === "action" && step.action.run.state === "uncertain")) {
    return "Workflow in progress. An action outcome is unknown. Workflow settlement is pending.";
  }
  if (steps.some((step) => step.state === "blocked" || (step.state === "action" && step.action.run.state === "failed"))) {
    return "Workflow in progress. Workflow settlement is pending. Inspect the recorded step evidence.";
  }
  return "Workflow in progress.";
}
