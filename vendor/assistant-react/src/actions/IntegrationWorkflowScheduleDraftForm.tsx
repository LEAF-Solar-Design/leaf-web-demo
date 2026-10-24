"use client";

import { useId, useLayoutEffect, useRef, useState, type CSSProperties, type FormEvent } from "react";
import { IntegrationWorkflowScheduleDraftV1 } from "../../../assistant-contract/src/integration";
import { BASE_CSS, color, font, panel, primaryButton, secondaryButton, target, tokenStyle, type ColorMode } from "../tokens";
import {
  acknowledgesScheduleDraft, cloneScheduleDraft, freezeScheduleDraft, initialScheduleDraftInputs,
  IntegrationWorkflowScheduleDraftSaveError, scheduleDraftCopy, scheduleDraftFields, validateScheduleDraftInputs,
  type ScheduleDraftAvailability, type ScheduleDraftField,
} from "./workflow-schedule-draft-form-state";

export interface IntegrationWorkflowScheduleDraftFormProps {
  initialDraft: IntegrationWorkflowScheduleDraftV1;
  /** Opaque organization/project/principal/authorization revision scope. Never displayed. */
  contextKey: string;
  availability: ScheduleDraftAvailability;
  mode?: ColorMode;
  /** Host owns immutable persistence, digest computation and timezone support. */
  onSave: (draft: IntegrationWorkflowScheduleDraftV1, signal: AbortSignal) => Promise<unknown>;
}

const controlStyle: CSSProperties = {
  ...target, width: "100%", maxWidth: "100%", minWidth: 0, padding: "8px 12px", font: "inherit",
  background: color.background, color: color.foreground, border: `1px solid ${color.border}`, borderRadius: "var(--leaf-radius)",
};
const focusCss = `${BASE_CSS}
.leaf-schedule-draft p { margin: 8px 0; }
.leaf-schedule-draft h2 { margin: 12px 0; }
.leaf-schedule-draft input:focus-visible, .leaf-schedule-draft [tabindex]:focus-visible {
  outline: 2px solid var(--leaf-ring); outline-offset: 2px;
}`;

/** Callback-only inert authoring. Parsing and saving never grant execution authority. */
export function IntegrationWorkflowScheduleDraftForm(props: IntegrationWorkflowScheduleDraftFormProps) {
  const parsed = IntegrationWorkflowScheduleDraftV1.safeParse(props.initialDraft);
  if (!parsed.success || typeof props.contextKey !== "string" || props.contextKey.length === 0 ||
      (props.availability !== "ready" && props.availability !== "unavailable" && props.availability !== "forbidden") ||
      (props.mode !== undefined && props.mode !== "light" && props.mode !== "dark") || typeof props.onSave !== "function") {
    return <p role="status" aria-live="polite" aria-atomic="true" tabIndex={-1}>{scheduleDraftCopy.unavailable}</p>;
  }
  // Canonical serialization is stable across equivalent re-created props. Keys never enter the DOM.
  return <ScheduleDraftFormInner key={JSON.stringify([props.contextKey, parsed.data])} {...props} initialDraft={parsed.data} />;
}

type Phase = "editing" | "pending" | "unknown" | "saved" | "conflict" | "invalid_timezone";
type Attempt = { controller: AbortController; timer: ReturnType<typeof setTimeout> };

function ScheduleDraftFormInner({ initialDraft, availability, mode = "light", onSave }: IntegrationWorkflowScheduleDraftFormProps) {
  const id = useId();
  const [inputs, setInputs] = useState(() => initialScheduleDraftInputs(initialDraft));
  const [errors, setErrors] = useState<Partial<Record<ScheduleDraftField, string>>>({});
  const [phase, setPhase] = useState<Phase>("editing");
  const frozen = useRef<IntegrationWorkflowScheduleDraftV1 | undefined>(undefined);
  const uncertain = useRef(false);
  const terminal = useRef(false);
  const latch = useRef(false);
  const active = useRef<Attempt | undefined>(undefined);
  const alive = useRef(true);
  const summary = useRef<HTMLDivElement>(null);
  const controls = useRef<Partial<Record<ScheduleDraftField, HTMLInputElement | null>>>({});
  const ready = availability === "ready";
  const busy = phase === "pending";
  const locked = busy || frozen.current !== undefined;
  const message = [!ready ? scheduleDraftCopy[availability] : "",
    phase === "editing" ? (ready ? scheduleDraftCopy.ready : "") : scheduleDraftCopy[phase]].filter(Boolean).join(" ");

  function invalidate() {
    const attempt = active.current;
    active.current = undefined;
    latch.current = false;
    if (attempt) { clearTimeout(attempt.timer); attempt.controller.abort(); }
  }

  useLayoutEffect(() => {
    alive.current = true;
    return () => { alive.current = false; invalidate(); };
  }, []);

  useLayoutEffect(() => {
    if (ready || !active.current) return;
    invalidate();
    uncertain.current = true;
    setPhase("unknown");
  }, [ready]);

  useLayoutEffect(() => {
    if (phase === "invalid_timezone" && ready) controls.current.timezone?.focus();
    else if (phase !== "editing" || !ready) summary.current?.focus();
  }, [message, phase, ready]);

  async function save(payload: IntegrationWorkflowScheduleDraftV1) {
    if (latch.current || terminal.current || !ready || !alive.current) return;
    latch.current = true;
    setPhase("pending");
    const controller = new AbortController();
    const attempt: Attempt = {
      controller,
      timer: setTimeout(() => {
        if (!alive.current || active.current !== attempt) return;
        invalidate();
        uncertain.current = true;
        setPhase("unknown");
      }, 45000),
    };
    active.current = attempt;
    try {
      const response = await onSave(cloneScheduleDraft(payload), controller.signal);
      if (!alive.current || active.current !== attempt) return;
      if (acknowledgesScheduleDraft(response, payload)) { terminal.current = true; setPhase("saved"); }
      else { uncertain.current = true; setPhase("unknown"); }
    } catch (error) {
      if (!alive.current || active.current !== attempt) return;
      if (error instanceof IntegrationWorkflowScheduleDraftSaveError && error.code === "conflict") {
        terminal.current = true;
        setPhase("conflict");
      }
      else if (error instanceof IntegrationWorkflowScheduleDraftSaveError && error.code === "invalid_timezone" && !uncertain.current) {
        frozen.current = undefined;
        setErrors({ timezone: scheduleDraftCopy.invalid_timezone });
        setPhase("invalid_timezone");
      } else { uncertain.current = true; setPhase("unknown"); }
    } finally {
      if (alive.current && active.current === attempt) {
        clearTimeout(attempt.timer);
        active.current = undefined;
        latch.current = false;
      }
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!ready || latch.current || frozen.current || !alive.current) return;
    const validation = validateScheduleDraftInputs(initialDraft, inputs);
    setErrors(validation.errors);
    const invalid = scheduleDraftFields.find((field) => validation.errors[field]);
    if (invalid) { controls.current[invalid]?.focus(); return; }
    if (!validation.draft) return;
    frozen.current = freezeScheduleDraft(validation.draft);
    void save(frozen.current);
  }

  const labels: Record<ScheduleDraftField, string> = { title: "Draft title", timezone: "Timezone", hour: "Hour (0 to 23)", minute: "Minute (0 to 59)" };
  return <section className="leaf-assistant leaf-schedule-draft" aria-label="Schedule draft"
    style={{ ...tokenStyle(mode), ...panel, fontFamily: font.sans, minWidth: 0, maxWidth: "100%", overflowWrap: "anywhere" }}>
    <style>{focusCss}</style>
    <h2>{initialDraft.title}</h2>
    <p>Draft ID: {initialDraft.id}. Version: {initialDraft.version}.</p>
    <p>Workflow ID: {initialDraft.workflow_ref.id}. Workflow version: {initialDraft.workflow_ref.version}.</p>
    <p>Saving this draft does not schedule or authorize any run.</p>
    <p id={`${id}-policy`}>Daily local time in the exact timezone entered. If daylight saving time skips this minute, use the next valid local minute. If the minute occurs twice, use only the first occurrence. The host must confirm timezone support.</p>
    <form onSubmit={submit} noValidate aria-busy={busy}>
      <fieldset disabled={locked || !ready} style={{ border: 0, padding: 0, margin: 0, minWidth: 0, display: "grid", gap: 16 }}>
        <legend>Daily schedule draft</legend>
        {scheduleDraftFields.map((field) => <div key={field} style={{ minWidth: 0, display: "grid", gap: 6 }}>
          <label htmlFor={`${id}-${field}`}>{labels[field]}</label>
          <input id={`${id}-${field}`} type="text" required style={controlStyle}
            inputMode={field === "hour" || field === "minute" ? "numeric" : "text"}
            ref={(node) => { controls.current[field] = node; }} value={inputs[field]}
            aria-invalid={!!errors[field]} aria-describedby={[field === "timezone" && `${id}-policy`, errors[field] && `${id}-${field}-error`].filter(Boolean).join(" ") || undefined}
            onChange={(event) => {
              const value = event.currentTarget.value;
              setInputs((previous) => ({ ...previous, [field]: value }));
              setErrors((previous) => ({ ...previous, [field]: undefined }));
            }} />
          {errors[field] && <p id={`${id}-${field}-error`}>{errors[field]}</p>}
        </div>)}
        <button type="submit" style={primaryButton}>Save draft</button>
      </fieldset>
    </form>
    <div ref={summary} role="status" aria-live="polite" aria-atomic="true" tabIndex={-1}>{message}</div>
    {phase === "unknown" && <button type="button" style={secondaryButton} disabled={!ready}
      onClick={() => { if (frozen.current) void save(frozen.current); }}>Retry save</button>}
  </section>;
}
