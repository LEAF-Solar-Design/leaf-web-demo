"use client";

import { useRef } from "react";
import {
  IntegrationActionRunV1, IntegrationActionSnapshotV1, type DatasetFieldV1,
  type IntegrationActionReceiptV1,
} from "../../../assistant-contract/src/integration";
import { formatDatasetValue, resolveDisplayPreferences, type DisplayPreferences } from "../datasets/formatting";
import { color, font, panel, tokenStyle, type ColorMode } from "../tokens";
import { sameRef, sameRun } from "./action-form-state";

export interface IntegrationActionRunStatusProps {
  run: IntegrationActionRunV1;
  receipts?: IntegrationActionReceiptV1[];
  displayPreferences?: DisplayPreferences;
  mode?: ColorMode;
}

const stateLabels = {
  accepted: "Accepted. Awaiting execution.", running: "Running. Outcome pending.",
  succeeded: "Succeeded. Action applied.", failed: "Failed. Action not applied.",
  uncertain: "Uncertain. Outcome unknown.",
};
const outcomeLabels = {
  unknown: "Outcome unknown", applied: "Action applied", not_applied: "Action not applied",
  not_dispatched: "Action not dispatched",
};
const failureLabels = {
  invalid_input: "The input was rejected.", forbidden: "Permission was denied.",
  revoked: "Authorization was revoked.", conflict: "The action conflicted with existing data.",
  quota: "The action limit was reached.", timeout: "The execution wait expired.",
  unavailable: "The service was unavailable.", invalid_response: "The service response could not be verified.",
  outcome_unknown: "The action outcome is unknown.",
};
const timestampField: DatasetFieldV1 = {
  id: "timestamp", label: "Timestamp", type: { kind: "instant" }, nullable: false, sensitivity: "public",
};

/** Renders canonical evidence only, retaining earlier observations during reconciliation. */
export function IntegrationActionRunStatus({ run: rawRun, receipts, displayPreferences, mode = "light" }: IntegrationActionRunStatusProps) {
  const history = useRef<{ run: IntegrationActionRunV1; receipts: IntegrationActionReceiptV1[] } | undefined>(undefined);
  const parsed = IntegrationActionRunV1.safeParse(rawRun);
  const snapshot = receipts === undefined ? undefined : IntegrationActionSnapshotV1.safeParse({ run: rawRun, receipts });
  let preferences: DisplayPreferences;
  try { preferences = resolveDisplayPreferences(displayPreferences); }
  catch { return <p role="status">Action status is unavailable.</p>; }
  if ((mode !== "light" && mode !== "dark") || !parsed.success || (snapshot !== undefined && !snapshot.success)) {
    return <p role="status">Action status is unavailable.</p>;
  }
  const run = parsed.data;
  const prior = history.current;
  let observations = snapshot?.success ? snapshot.data.receipts : undefined;
  if (prior && prior.run.run_id === run.run_id) {
    if (!sameRun(run, prior.run) || run.connection_id !== prior.run.connection_id ||
        run.grant_revision !== prior.run.grant_revision || !sameRef(run.connector_ref, prior.run.connector_ref) ||
        !sameRef(run.operation_ref, prior.run.operation_ref)) return <p role="status">Action status is unavailable.</p>;
    const merged = [...prior.receipts];
    for (const receipt of observations ?? []) {
      const existing = merged.find((item) => item.receipt_id === receipt.receipt_id);
      if (existing && JSON.stringify(existing) !== JSON.stringify(receipt)) return <p role="status">Action status is unavailable.</p>;
      if (!existing) merged.push(receipt);
    }
    merged.sort((left, right) => left.sequence - right.sequence);
    if (merged.length > 0) {
      if (!IntegrationActionSnapshotV1.safeParse({ run, receipts: merged }).success) return <p role="status">Action status is unavailable.</p>;
      observations = merged;
    }
  }
  history.current = { run, receipts: observations ?? [] };
  const time = (value: string) => <time dateTime={value}>{formatDatasetValue(timestampField, value, preferences)}</time>;
  return <section aria-label="Action run status" style={{ ...tokenStyle(mode), ...panel, fontFamily: font.sans, minWidth: 0, overflowWrap: "anywhere" }}>
    <p>{stateLabels[run.state]}</p>
    <dl><dt>Created</dt><dd>{time(run.created_at)}</dd><dt>Updated</dt><dd>{time(run.updated_at)}</dd></dl>
    {observations !== undefined && observations.length > 0 && <ol aria-label="Action history" style={{ color: color.foreground }}>
      {observations.map((receipt) => <li key={receipt.receipt_id}>
        <span>{outcomeLabels[receipt.effect_outcome]}</span>{". "}{time(receipt.observed_at)}
        {receipt.failure_code && <p>{failureLabels[receipt.failure_code]}</p>}
      </li>)}
    </ol>}
  </section>;
}
