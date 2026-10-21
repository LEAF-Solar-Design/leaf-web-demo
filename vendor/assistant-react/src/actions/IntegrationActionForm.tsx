"use client";

import { useEffect, useId, useRef, useState, type CSSProperties, type FormEvent } from "react";
import {
  IntegrationActionElementV1, IntegrationActionSnapshotV1, IntegrationActionSubmissionResultV1,
  IntegrationActionSubmissionV1, type IntegrationActionRunV1, type IntegrationActionReceiptV1,
} from "../../../assistant-contract/src/integration";
import { resolveDisplayPreferences, type DisplayPreferences } from "../datasets/formatting";
import { BASE_CSS, color, font, panel, primaryButton, secondaryButton, target, tokenStyle, type ColorMode } from "../tokens";
import {
  availabilityCopy, conflictCopy, initialInputs, IntegrationActionPreflightError, isTerminal, matchesSubmission, preflightCopy, sameRun,
  unknownCopy, validateInputs, type ActionAvailability, type ActionInputs, type InputMode,
} from "./action-form-state";
import { IntegrationActionRunStatus } from "./IntegrationActionRunStatus";

export interface IntegrationActionFormProps {
  element: IntegrationActionElementV1;
  /** Opaque host identity including organization, project, principal and authorization revision. Never displayed. */
  contextKey: string;
  grantRevision: number;
  availability: ActionAvailability;
  displayPreferences?: DisplayPreferences;
  mode?: ColorMode;
  /** May throw IntegrationActionPreflightError only before invoking the action submission client. */
  onSubmit: (submission: IntegrationActionSubmissionV1, signal: AbortSignal) => Promise<unknown>;
  onCheckStatus: (runId: string, signal: AbortSignal) => Promise<unknown>;
}

const controlStyle: CSSProperties = {
  ...target, width: "100%", maxWidth: "100%", minWidth: 44, padding: "8px 12px", font: "inherit",
  background: color.background, color: color.foreground, border: `1px solid ${color.border}`,
  borderRadius: "var(--leaf-radius)",
};
const focusCss = `${BASE_CSS}
.leaf-action input:focus-visible, .leaf-action select:focus-visible, .leaf-action [tabindex]:focus-visible {
  outline: 2px solid var(--leaf-ring); outline-offset: 2px;
}`;

function resultCopy(run: IntegrationActionRunV1): string {
  switch (run.state) {
    case "accepted": return "Submission accepted. Execution is pending.";
    case "running": return "The action is running. Its outcome is pending.";
    case "succeeded": return "The action succeeded and was applied.";
    case "failed": return run.effect_outcome === "not_dispatched"
      ? "The action failed before dispatch." : "The action failed and was not applied.";
    case "uncertain": return unknownCopy;
  }
}

/** No execution authority or I/O: callbacks are supplied by the trusted host. */
export function IntegrationActionForm(props: IntegrationActionFormProps) {
  const parsed = IntegrationActionElementV1.safeParse(props.element);
  let preferences: DisplayPreferences;
  try { preferences = resolveDisplayPreferences(props.displayPreferences); }
  catch { return <p role="status">{availabilityCopy.unavailable}</p>; }
  if (!parsed.success || typeof props.contextKey !== "string" || props.contextKey.length === 0 ||
      !Number.isSafeInteger(props.grantRevision) || props.grantRevision <= 0 ||
      !Object.hasOwn(availabilityCopy, props.availability) || typeof props.onSubmit !== "function" ||
      typeof props.onCheckStatus !== "function" || (props.mode !== undefined && props.mode !== "light" && props.mode !== "dark")) {
    return <p role="status">{availabilityCopy.unavailable}</p>;
  }
  // React's key is never a DOM attribute. Exact presentation/binding changes discard scoped state synchronously.
  const scope = JSON.stringify([props.contextKey, parsed.data, props.grantRevision]);
  return <ActionFormInner key={scope} {...props} element={parsed.data} displayPreferences={preferences} />;
}

function ActionFormInner({ element, grantRevision, availability, displayPreferences, mode = "light", onSubmit, onCheckStatus }: IntegrationActionFormProps) {
  const id = useId();
  const [inputs, setInputs] = useState<ActionInputs>(() => initialInputs(element));
  const [errors, setErrors] = useState<Record<string, string>>(() => Object.create(null));
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [run, setRun] = useState<IntegrationActionRunV1>();
  const [receipts, setReceipts] = useState<IntegrationActionReceiptV1[]>();
  const [conflict, setConflict] = useState(false);
  const frozen = useRef<IntegrationActionSubmissionV1 | undefined>(undefined);
  const mayHaveSubmitted = useRef(false);
  const latch = useRef(false);
  const active = useRef<{ controller: AbortController; timer: ReturnType<typeof setTimeout> } | undefined>(undefined);
  const alive = useRef(true);
  const summary = useRef<HTMLDivElement>(null);
  const controls = useRef<Record<string, HTMLInputElement | HTMLSelectElement | null>>({});
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      const attempt = active.current;
      active.current = undefined;
      if (attempt) { clearTimeout(attempt.timer); attempt.controller.abort(); }
    };
  }, []);
  useEffect(() => { if (message) summary.current?.focus(); }, [message, busy, run]);

  const ready = availability === "ready";
  const locked = busy || frozen.current !== undefined;
  const terminal = run !== undefined && isTerminal(run) && message !== unknownCopy;

  function update(fieldId: string, change: Partial<ActionInputs[string]>) {
    setInputs((previous) => ({ ...previous, [fieldId]: { ...previous[fieldId], ...change } }));
  }

  async function dispatch(kind: "submit" | "status", submission: IntegrationActionSubmissionV1) {
    // A synchronous latch covers click/Enter bursts before React commits disabled controls.
    if (latch.current || !ready) return;
    latch.current = true;
    setBusy(true);
    setMessage(kind === "status" ? "Checking action status." : "Submitting action. Outcome pending.");
    const controller = new AbortController();
    const attempt = {
      controller,
      timer: setTimeout(() => {
        if (!alive.current || active.current !== attempt) return;
        active.current = undefined;
        latch.current = false;
        controller.abort();
        setBusy(false);
        setMessage(unknownCopy);
      }, kind === "status" ? 15000 : 45000),
    };
    active.current = attempt;
    const previouslyMayHaveSubmitted = mayHaveSubmitted.current;
    try {
      // Remain conservative on timeout, cancellation, malformed responses and ordinary errors.
      if (kind === "submit") mayHaveSubmitted.current = true;
      const response = kind === "status"
        ? await onCheckStatus(run!.run_id, controller.signal)
        : await onSubmit(submission, controller.signal);
      if (!alive.current || active.current !== attempt) return;
      if (kind === "submit") {
        const parsed = IntegrationActionSubmissionResultV1.safeParse(response);
        if (!parsed.success) throw new Error();
        if (parsed.data.status === "refused") {
          setConflict(true);
          setMessage(conflictCopy);
        } else {
          if (!matchesSubmission(parsed.data.run, submission)) throw new Error();
          setRun(parsed.data.run);
          setReceipts(undefined);
          setMessage(resultCopy(parsed.data.run));
        }
      } else {
        const parsed = IntegrationActionSnapshotV1.safeParse(response);
        if (!parsed.success || !run || !matchesSubmission(parsed.data.run, submission) || !sameRun(parsed.data.run, run)) throw new Error();
        // Preserve append-only evidence, and reject changes to a receipt already observed.
        const merged = [...(receipts ?? [])];
        for (const receipt of parsed.data.receipts) {
          const prior = merged.find((item) => item.receipt_id === receipt.receipt_id);
          if (prior && JSON.stringify(prior) !== JSON.stringify(receipt)) throw new Error();
          if (!prior) merged.push(receipt);
        }
        merged.sort((left, right) => left.sequence - right.sequence);
        if (!IntegrationActionSnapshotV1.safeParse({ run: parsed.data.run, receipts: merged }).success) throw new Error();
        setRun(parsed.data.run);
        setReceipts(merged);
        setMessage(resultCopy(parsed.data.run));
      }
    } catch (error) {
      if (alive.current && active.current === attempt) {
        if (kind === "submit" && !previouslyMayHaveSubmitted && error instanceof IntegrationActionPreflightError) {
          mayHaveSubmitted.current = false;
          setMessage(preflightCopy);
        } else setMessage(unknownCopy);
      }
    } finally {
      if (alive.current && active.current === attempt) {
        clearTimeout(attempt.timer);
        active.current = undefined;
        latch.current = false;
        setBusy(false);
      }
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!ready || latch.current || frozen.current) return;
    const validation = validateInputs(element, inputs);
    setErrors(validation.errors);
    const invalid = element.form.fields.find((field) => validation.errors[field.id]);
    if (invalid) { controls.current[invalid.id]?.focus(); return; }
    try {
      if (typeof globalThis.crypto?.randomUUID !== "function") throw new Error();
      const submission = IntegrationActionSubmissionV1.parse({
        schema_version: 1, connection_id: element.connection_id, connector_ref: element.connector_ref,
        operation_ref: element.operation_ref, expected_grant_revision: grantRevision,
        arguments: validation.arguments, idempotency_key: globalThis.crypto.randomUUID(),
      });
      Object.freeze(submission.arguments);
      Object.freeze(submission.connector_ref);
      Object.freeze(submission.operation_ref);
      frozen.current = Object.freeze(submission);
      void dispatch("submit", submission);
    } catch {
      setMessage("Secure submission is unavailable. Try again when secure key generation is available.");
    }
  }

  return <section className="leaf-assistant leaf-action" aria-label={element.title}
    style={{ ...tokenStyle(mode), ...panel, fontFamily: font.sans, minWidth: 0, maxWidth: "100%", overflowWrap: "anywhere" }}>
    <style>{focusCss}</style>
    <h2>{element.title}</h2>
    {element.form.description && <p>{element.form.description}</p>}
    {!ready && <p role="status">{availabilityCopy[availability]}</p>}
    <form onSubmit={submit} noValidate>
      <fieldset disabled={locked || !ready} style={{ border: 0, padding: 0, margin: 0, minWidth: 0, display: "grid", gap: 16 }}>
        <legend>Action inputs</legend>
        {element.form.fields.map((field) => {
          const input = inputs[field.id];
          const inputId = `${id}-${field.id}`;
          const errorId = `${inputId}-error`;
          const descriptionId = `${inputId}-description`;
          const describedBy = [field.description && descriptionId, errors[field.id] && errorId].filter(Boolean).join(" ") || undefined;
          const props = {
            id: inputId, style: controlStyle, "aria-describedby": describedBy,
            "aria-invalid": !!errors[field.id], "aria-required": field.required,
            ref: (node: HTMLInputElement | HTMLSelectElement | null) => { controls.current[field.id] = node; },
          };
          return <div key={field.id} style={{ minWidth: 0, display: "grid", gap: 6 }}>
            <label htmlFor={inputId}>{field.label}{field.required ? " (required)" : " (optional)"}</label>
            {field.description && <p id={descriptionId}>{field.description}</p>}
            {(!field.required || field.nullable) && <>
              <label htmlFor={`${inputId}-mode`}>{field.label} value mode</label>
              <select id={`${inputId}-mode`} style={controlStyle} value={input.mode}
                onChange={(event) => update(field.id, { mode: event.target.value as InputMode })}>
                {!field.required && <option value="absent">Not provided</option>}
                {field.nullable && <option value="null">No value</option>}
                <option value="value">Value</option>
              </select>
            </>}
            {field.kind === "text" || field.kind === "number" ? <input {...props} type="text"
              inputMode={field.kind === "number" ? "decimal" : "text"} value={input.value}
              disabled={locked || !ready || input.mode !== "value"}
              onChange={(event) => update(field.id, { value: event.target.value })} /> :
              <select {...props} value={input.value} disabled={locked || !ready || input.mode !== "value"}
                onChange={(event) => update(field.id, { value: event.target.value })}>
                <option value="">Choose a value</option>
                {field.kind === "boolean" ? <><option value="true">True</option><option value="false">False</option></> :
                  field.choices.map((choice) => <option key={choice.value} value={choice.value}>{choice.label}</option>)}
              </select>}
            {errors[field.id] && <p id={errorId}>{errors[field.id]}</p>}
          </div>;
        })}
        <button type="submit" style={primaryButton}>{element.form.submit_label}</button>
      </fieldset>
    </form>
    <div ref={summary} role="status" aria-live="polite" aria-atomic="true" tabIndex={-1} aria-busy={busy}>{message}</div>
    {run && <IntegrationActionRunStatus run={run} receipts={receipts} displayPreferences={displayPreferences} mode={mode} />}
    {frozen.current && !busy && !terminal && !conflict && (run ?
      <button type="button" style={secondaryButton} disabled={!ready} onClick={() => { if (frozen.current) void dispatch("status", frozen.current); }}>Check status</button> :
      <div>{message !== preflightCopy && <p>Recovery sends the same request and key. It can execute the original action if the server never received it.</p>}
        <button type="button" style={secondaryButton} disabled={!ready} onClick={() => { if (frozen.current) void dispatch("submit", frozen.current); }}>{message === preflightCopy ? "Retry submission" : "Recover submission"}</button></div>)}
    {terminal && !busy && <button type="button" style={secondaryButton} onClick={() => {
      frozen.current = undefined;
      mayHaveSubmitted.current = false;
      setRun(undefined); setReceipts(undefined); setMessage(""); setConflict(false);
    }}>Start another action</button>}
  </section>;
}
