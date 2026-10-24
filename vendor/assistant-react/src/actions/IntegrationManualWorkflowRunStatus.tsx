"use client";

import { useEffect, useRef, useState } from "react";
import {
  IntegrationManualWorkflowCancellationResultV1, type DatasetFieldV1,
  type IntegrationManualWorkflowSnapshotV1, type IntegrationManualWorkflowSnapshotV2,
  type IntegrationManualWorkflowCancellationRequestV1,
} from "../../../assistant-contract/src/integration";
import { formatDatasetValue, resolveDisplayPreferences, type DisplayPreferences } from "../datasets/formatting";
import { BASE_CSS, color, font, panel, secondaryButton, tokenStyle, type ColorMode } from "../tokens";
import { IntegrationActionRunStatus } from "./IntegrationActionRunStatus";
import { manualWorkflowSummary, updateManualWorkflowStatus, type ManualWorkflowStatusState } from "./manual-workflow-status-state";

type CancelWorkflow = (request: IntegrationManualWorkflowCancellationRequestV1, signal: AbortSignal) => Promise<IntegrationManualWorkflowCancellationResultV1>;

export type IntegrationManualWorkflowRunStatusProps = {
  contextKey: string;
  displayPreferences?: DisplayPreferences;
  mode?: ColorMode;
} & ({ snapshot: IntegrationManualWorkflowSnapshotV1; onCancel?: never } |
  { snapshot: IntegrationManualWorkflowSnapshotV2; onCancel?: CancelWorkflow });

const failureLabels = {
  invalid_input: "The input was rejected.", forbidden: "Permission was denied.",
  revoked: "Authorization was revoked.", conflict: "The action conflicted with existing data.",
  quota: "The action limit was reached.", timeout: "The execution wait expired.",
  unavailable: "The service was unavailable.", invalid_response: "The service response could not be verified.",
};
const timestampField: DatasetFieldV1 = {
  id: "timestamp", label: "Timestamp", type: { kind: "instant" }, nullable: false, sensitivity: "public",
};

/** Evidence display with opt-in host cancellation; validation grants no authority. */
export function IntegrationManualWorkflowRunStatus({ snapshot: input, contextKey, displayPreferences, mode = "light", onCancel }: IntegrationManualWorkflowRunStatusProps) {
  const history = useRef<ManualWorkflowStatusState | undefined>(undefined);
  const observation = useRef<{ contextKey: string; input: unknown; serialized?: string } | undefined>(undefined);
  // Internal pending/result renders must not reapply unchanged, older host props.
  if (!observation.current || observation.current.contextKey !== contextKey || observation.current.input !== input) {
    const observed = updateManualWorkflowStatus(undefined, contextKey, input);
    const serialized = observed?.snapshot ? JSON.stringify(observed.snapshot) : undefined;
    if (!observation.current || observation.current.contextKey !== contextKey || serialized === undefined ||
        observation.current.serialized !== serialized) {
      history.current = updateManualWorkflowStatus(history.current, contextKey, input);
    }
    observation.current = { contextKey, input, serialized };
  }
  const runId = history.current?.runId;
  const scopeRef = useRef({ contextKey, runId, onCancel });
  const active = useRef<{ scope: typeof scopeRef.current; controller: AbortController } | undefined>(undefined);
  if (scopeRef.current.contextKey !== contextKey || scopeRef.current.runId !== runId || scopeRef.current.onCancel !== onCancel) {
    active.current?.controller.abort();
    active.current = undefined;
    scopeRef.current = { contextKey, runId, onCancel };
  }
  const scope = scopeRef.current;
  const [feedback, setFeedback] = useState<{ scope: typeof scope; state: "pending" | "error" } | undefined>(undefined);
  const [, refresh] = useState(0);
  useEffect(() => () => {
    if (active.current?.scope === scope) {
      active.current.controller.abort();
      active.current = undefined;
    }
  }, [scope]);
  let preferences: DisplayPreferences;
  try { preferences = resolveDisplayPreferences(displayPreferences); }
  catch { return <p role="status">Workflow status is unavailable.</p>; }
  const snapshot = history.current?.snapshot;
  if ((mode !== "light" && mode !== "dark") || !snapshot) return <p role="status">Workflow status is unavailable.</p>;
  const unverified = history.current?.unverified;
  const pending = feedback?.scope === scope && feedback.state === "pending";
  const cancelError = feedback?.scope === scope && feedback.state === "error";
  const canCancel = typeof onCancel === "function" && input?.run?.schema_version === 2 &&
    snapshot.run.schema_version === 2 && !unverified &&
    (snapshot.run.state === "accepted" || snapshot.run.state === "running" || snapshot.run.state === "uncertain");
  const requestCancellation = async () => {
    if (!canCancel || active.current || !onCancel) return;
    const baseline = history.current!;
    const controller = new AbortController();
    const attempt = { scope, controller };
    active.current = attempt;
    setFeedback({ scope, state: "pending" });
    const current = () => active.current === attempt && scopeRef.current === scope && !controller.signal.aborted;
    try {
      const result = IntegrationManualWorkflowCancellationResultV1.safeParse(await onCancel({
        schema_version: 1, run_id: snapshot.run.run_id, expected_revision: snapshot.run.revision,
      }, controller.signal));
      if (!current()) return;
      if (!result.success || result.data.snapshot.run.run_id !== snapshot.run.run_id) throw new Error();
      const bound = updateManualWorkflowStatus(baseline, contextKey, result.data.snapshot);
      if (!bound?.snapshot || bound.unverified) throw new Error();
      const next = updateManualWorkflowStatus(history.current, contextKey, bound.snapshot);
      if (!next?.snapshot || next.unverified) throw new Error();
      history.current = next;
      setFeedback(undefined);
      refresh((value) => value + 1);
    } catch {
      if (current()) setFeedback({ scope, state: "error" });
    } finally {
      if (active.current === attempt) active.current = undefined;
    }
  };
  const time = (value: string) => <time dateTime={value}>{formatDatasetValue(timestampField, value, preferences)}</time>;
  const applied = snapshot.steps.filter((step) => step.state === "action" && step.action.run.state === "succeeded").length;
  return <section aria-label="Manual workflow run status" style={{ ...tokenStyle(mode), ...panel, fontFamily: font.sans, color: color.foreground, minWidth: 0, overflowWrap: "anywhere" }}>
    <h2 style={{ minWidth: 0, overflowWrap: "anywhere" }}>{snapshot.definition.title}</h2>
    <p role="status" aria-live="polite">{unverified ? "Workflow update could not be verified. Showing last verified evidence." : manualWorkflowSummary(snapshot)}
      {pending && " Requesting cancellation. Waiting for verified host evidence."}
      {cancelError && " Cancellation response could not be verified. Showing last verified evidence. Review the workflow status before manually requesting cancellation again."}
    </p>
    {unverified && <p>Last verified evidence</p>}
    {(canCancel || pending || cancelError || snapshot.run.state === "cancel_requested" || snapshot.run.state === "cancelled") && <div className="leaf-assistant">
      {canCancel && <style>{BASE_CSS}</style>}
      <p>A cancellation request stops future admissions. Already claimed actions can still finish. Cancellation does not undo applied actions.</p>
      {canCancel && <button type="button" disabled={pending} onClick={requestCancellation}
        style={{ ...secondaryButton, maxWidth: "100%", padding: "8px 16px", whiteSpace: "normal", overflowWrap: "anywhere",
          cursor: pending ? "wait" : "pointer", opacity: pending ? 0.65 : 1 }}>Request cancellation</button>}
    </div>}
    <p>{applied} of {snapshot.steps.length} step actions recorded as applied.</p>
    <dl><dt>Created</dt><dd>{time(snapshot.run.created_at)}</dd><dt>Updated</dt><dd>{time(snapshot.run.updated_at)}</dd></dl>
    {snapshot.run.schema_version === 2 && snapshot.run.requested_at !== undefined &&
      <dl><dt>Cancellation requested</dt><dd>{time(snapshot.run.requested_at)}</dd></dl>}
    <ol aria-label="Workflow steps" style={{ minWidth: 0, paddingInlineStart: "1.5rem" }}>
      {snapshot.steps.map((step, index) => <li key={step.step_id} style={{ minWidth: 0 }}>
        <section aria-label={`Step ${index + 1}`} style={{ minWidth: 0, overflowWrap: "anywhere" }}>
          <h3>Step {index + 1}</h3>
          {step.state === "pending" && <p>Pending. No execution result recorded.</p>}
          {step.state === "blocked" && <p>The step was not dispatched. {failureLabels[step.failure_code]}</p>}
          {step.state === "action" && <IntegrationActionRunStatus
            key={JSON.stringify([contextKey, snapshot.run.run_id, step.step_id])}
            run={step.action.run} receipts={step.action.receipts} displayPreferences={preferences} mode={mode} />}
        </section>
      </li>)}
    </ol>
  </section>;
}
