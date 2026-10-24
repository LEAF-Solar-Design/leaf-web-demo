"use client";

import { useLayoutEffect, useRef, useState } from "react";
import type { IntegrationRefV1 } from "../../../assistant-contract/src/integration";
import { BASE_CSS, font, panel, primaryButton, secondaryButton, tokenStyle, type ColorMode } from "../tokens";
import {
  cloneScheduleDraftReference, parseScheduleDraftReferencePage, scheduleDraftPageMessage, scheduleDraftPickerCopy,
  type ScheduleDraftPageQuery, type ScheduleDraftPickerAvailability, type ScheduleDraftReferencePage,
} from "./workflow-schedule-draft-picker-state";

export interface IntegrationWorkflowScheduleDraftPickerProps {
  /** Opaque organization/project/principal/authorization revision binding; never displayed. */
  contextKey: string;
  availability: ScheduleDraftPickerAvailability;
  mode?: ColorMode;
  onLoadPage: (query: ScheduleDraftPageQuery, signal: AbortSignal) => Promise<unknown>;
  /** Selection is inert. The host retains all authority and digest verification. */
  onSelect: (reference: IntegrationRefV1) => void;
}

const focusCss = `${BASE_CSS}
.leaf-schedule-draft-picker [tabindex]:focus-visible { outline: 2px solid var(--leaf-ring); outline-offset: 2px; }
.leaf-schedule-draft-picker button { max-width: 100%; white-space: normal; overflow-wrap: anywhere; }
`;

export function IntegrationWorkflowScheduleDraftPicker(props: IntegrationWorkflowScheduleDraftPickerProps) {
  if (typeof props.contextKey !== "string" || props.contextKey.length === 0 ||
      !["ready", "unavailable", "forbidden"].includes(props.availability) ||
      (props.mode !== undefined && props.mode !== "light" && props.mode !== "dark") ||
      typeof props.onLoadPage !== "function" || typeof props.onSelect !== "function") {
    return <p role="status" aria-live="polite" aria-atomic="true" tabIndex={-1}>{scheduleDraftPickerCopy.unavailable}</p>;
  }
  // A scope or readiness transition unmounts all private page and request state.
  return <ScheduleDraftPickerInner key={JSON.stringify([props.contextKey, props.availability])} {...props} />;
}

type Phase = "idle" | "pending" | "success" | "failure";
type Attempt = { controller: AbortController; timer: ReturnType<typeof setTimeout> };

function ScheduleDraftPickerInner({ availability, mode = "light", onLoadPage, onSelect }: IntegrationWorkflowScheduleDraftPickerProps) {
  const [page, setPage] = useState<ScheduleDraftReferencePage | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [selected, setSelected] = useState<number | null>(null);
  const [selectionFailed, setSelectionFailed] = useState(false);
  const active = useRef<Attempt | undefined>(undefined);
  const failedQuery = useRef<ScheduleDraftPageQuery>({ limit: 25 });
  const alive = useRef(true);
  const selecting = useRef(false);
  const summary = useRef<HTMLDivElement>(null);
  const ready = availability === "ready";
  const busy = phase === "pending";
  const message = !ready ? scheduleDraftPickerCopy[availability] :
    phase === "success" ? (selectionFailed ? scheduleDraftPickerCopy.selectionFailure :
      selected !== null && page ? `${scheduleDraftPickerCopy.selected} Draft ID: ${page.references[selected].id}. Version: ${page.references[selected].version}.` :
        page ? scheduleDraftPageMessage(page) : scheduleDraftPickerCopy.idle) : scheduleDraftPickerCopy[phase];

  function invalidate() {
    const attempt = active.current;
    active.current = undefined;
    if (attempt) { clearTimeout(attempt.timer); attempt.controller.abort(); }
  }

  useLayoutEffect(() => {
    alive.current = true;
    return () => { alive.current = false; invalidate(); };
  }, []);

  useLayoutEffect(() => {
    if (phase !== "idle" || !ready) summary.current?.focus();
  }, [phase, message, ready]);

  async function load(query: ScheduleDraftPageQuery) {
    if (!ready || !alive.current || active.current || selecting.current) return;
    // Keep our query independent of any callback mutation, including on retry.
    const request: ScheduleDraftPageQuery = query.after === undefined ? { limit: 25 } : { limit: 25, after: query.after };
    failedQuery.current = request;
    setPhase("pending"); setSelected(null); setSelectionFailed(false);
    const controller = new AbortController();
    const attempt: Attempt = { controller, timer: setTimeout(() => {
      if (!alive.current || active.current !== attempt) return;
      invalidate(); setPhase("failure");
    }, 45000) };
    active.current = attempt;
    try {
      const response = await onLoadPage({ ...request }, controller.signal);
      if (!alive.current || active.current !== attempt) return;
      const verified = parseScheduleDraftReferencePage(response, request.after);
      if (verified) { setPage(verified); setPhase("success"); }
      else setPhase("failure");
    } catch {
      if (alive.current && active.current === attempt) setPhase("failure");
    } finally {
      if (active.current === attempt) { clearTimeout(attempt.timer); active.current = undefined; }
    }
  }

  function select(index: number) {
    if (!ready || !alive.current || active.current || selecting.current || phase !== "success" || !page) return;
    selecting.current = true;
    try {
      onSelect(cloneScheduleDraftReference(page.references[index]));
      if (alive.current) { setSelected(index); setSelectionFailed(false); }
    } catch {
      if (alive.current) { setSelected(null); setSelectionFailed(true); }
    } finally { selecting.current = false; }
  }

  return <section className="leaf-assistant leaf-schedule-draft-picker" aria-label="Schedule draft picker"
    style={{ ...tokenStyle(mode), ...panel, fontFamily: font.sans, minWidth: 0, maxWidth: "100%", overflowWrap: "anywhere" }}>
    <style>{focusCss}</style>
    <h2>Choose a saved schedule draft</h2>
    <p>Selecting a draft reference does not schedule or authorize any run.</p>
    <div ref={summary} role="status" aria-live="polite" aria-atomic="true" tabIndex={-1}>{message}</div>
    {ready && <>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 16 }}>
        <button type="button" style={primaryButton} disabled={busy} onClick={() => { void load({ limit: 25 }); }}>
          {page ? "Refresh" : "Load drafts"}
        </button>
        {page && page.nextCursor !== null && <button type="button" style={secondaryButton}
          disabled={busy || phase !== "success"} onClick={() => { if (page.nextCursor !== null) void load({ limit: 25, after: page.nextCursor }); }}>Next</button>}
        {phase === "failure" && <button type="button" style={secondaryButton} onClick={() => { void load(failedQuery.current); }}>Retry</button>}
      </div>
      {page && <ul aria-label="Saved schedule draft references" aria-busy={busy} style={{ listStyle: "none", padding: 0, margin: "16px 0 0", display: "grid", gap: 8, minWidth: 0 }}>
        {page.references.map((reference, index) => <li key={index} style={{ minWidth: 0 }}>
          <button type="button" style={{ ...secondaryButton, width: "100%", textAlign: "left", padding: "8px 16px" }}
            disabled={busy || phase !== "success"} aria-pressed={selected === index}
            aria-label={`Select draft ${reference.id}, version ${reference.version}`} onClick={() => select(index)}>
            Draft ID: {reference.id}. Version: {reference.version}.
          </button>
        </li>)}
      </ul>}
    </>}
  </section>;
}
