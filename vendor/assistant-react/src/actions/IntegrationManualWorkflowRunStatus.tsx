"use client";

import { useRef } from "react";
import type { DatasetFieldV1, IntegrationManualWorkflowSnapshotV1 } from "../../../assistant-contract/src/integration";
import { formatDatasetValue, resolveDisplayPreferences, type DisplayPreferences } from "../datasets/formatting";
import { color, font, panel, tokenStyle, type ColorMode } from "../tokens";
import { IntegrationActionRunStatus } from "./IntegrationActionRunStatus";
import { manualWorkflowSummary, updateManualWorkflowStatus, type ManualWorkflowStatusState } from "./manual-workflow-status-state";

export interface IntegrationManualWorkflowRunStatusProps {
  snapshot: IntegrationManualWorkflowSnapshotV1;
  contextKey: string;
  displayPreferences?: DisplayPreferences;
  mode?: ColorMode;
}

const failureLabels = {
  invalid_input: "The input was rejected.", forbidden: "Permission was denied.",
  revoked: "Authorization was revoked.", conflict: "The action conflicted with existing data.",
  quota: "The action limit was reached.", timeout: "The execution wait expired.",
  unavailable: "The service was unavailable.", invalid_response: "The service response could not be verified.",
};
const timestampField: DatasetFieldV1 = {
  id: "timestamp", label: "Timestamp", type: { kind: "instant" }, nullable: false, sensitivity: "public",
};

/** Inert host evidence display; no execution, reconciliation or authorization controls. */
export function IntegrationManualWorkflowRunStatus({ snapshot: input, contextKey, displayPreferences, mode = "light" }: IntegrationManualWorkflowRunStatusProps) {
  const history = useRef<ManualWorkflowStatusState | undefined>(undefined);
  history.current = updateManualWorkflowStatus(history.current, contextKey, input);
  let preferences: DisplayPreferences;
  try { preferences = resolveDisplayPreferences(displayPreferences); }
  catch { return <p role="status">Workflow status is unavailable.</p>; }
  const snapshot = history.current?.snapshot;
  if ((mode !== "light" && mode !== "dark") || !snapshot) return <p role="status">Workflow status is unavailable.</p>;
  const unverified = history.current?.unverified;
  const time = (value: string) => <time dateTime={value}>{formatDatasetValue(timestampField, value, preferences)}</time>;
  const applied = snapshot.steps.filter((step) => step.state === "action" && step.action.run.state === "succeeded").length;
  return <section aria-label="Manual workflow run status" style={{ ...tokenStyle(mode), ...panel, fontFamily: font.sans, color: color.foreground, minWidth: 0, overflowWrap: "anywhere" }}>
    <h2 style={{ minWidth: 0, overflowWrap: "anywhere" }}>{snapshot.definition.title}</h2>
    <p role="status" aria-live="polite">{unverified ? "Workflow update could not be verified. Showing last verified evidence." : manualWorkflowSummary(snapshot)}</p>
    {unverified && <p>Last verified evidence</p>}
    <p>{applied} of {snapshot.steps.length} step actions recorded as applied.</p>
    <dl><dt>Created</dt><dd>{time(snapshot.run.created_at)}</dd><dt>Updated</dt><dd>{time(snapshot.run.updated_at)}</dd></dl>
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
