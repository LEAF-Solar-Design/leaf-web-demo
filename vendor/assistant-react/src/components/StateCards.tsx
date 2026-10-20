import type { ReactNode } from "react";
import type { ReasonV1 } from "../../../assistant-contract/src/index";
import { TONE_GLYPH, reasonCopy, stateSpec } from "../states";
import type { AssistantStateId, ExitAction, StateContext, StateExit } from "../states";
import { color, primaryButton, radius, secondaryButton, toneColor } from "../tokens";
import { StateInk } from "./AssistantConfirmCard";

export interface StateCardProps {
  state: AssistantStateId;
  context?: StateContext;
  /** A server reason for this state; its text and exit win over the standard copy. */
  reason?: ReasonV1 | null;
  onAction?: (action: ExitAction) => void;
  /** Extra content between the copy and the exit (idle starters). */
  children?: ReactNode;
}

/** Any VI.3 state: its StateInk or glyph, its copy, and exactly one exit. */
export function StateCard({ state, context, reason, onAction, children }: StateCardProps) {
  const spec = stateSpec(state, context);
  const fromReason = reason ? reasonCopy(reason) : null;
  const copy = fromReason ? fromReason.text : spec.copy;
  const exit: StateExit = fromReason ? { label: fromReason.label, action: fromReason.action } : spec.exit;
  return (
    <section
      data-testid="assistant-state"
      data-state={state}
      aria-label={spec.ink ?? copy}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 12,
        flexWrap: "wrap",
        padding: "8px 12px",
        border: `1px solid ${color.border}`,
        borderRadius: radius,
        background: color.card,
      }}
    >
      {spec.ink ? (
        <StateInk label={spec.ink} tone={spec.tone} />
      ) : (
        <span
          aria-hidden="true"
          className={spec.tone === "progress" ? "leaf-assistant-pulse" : undefined}
          style={{ color: toneColor(spec.tone), fontWeight: 700 }}
        >
          {TONE_GLYPH[spec.tone]}
        </span>
      )}
      <p data-testid="assistant-state-copy" style={{ margin: 0, flex: 1, minWidth: 160 }}>
        {copy}
      </p>
      {children}
      <button
        type="button"
        style={exit.action === "approve" ? primaryButton : secondaryButton}
        onClick={() => onAction?.(exit.action)}
      >
        {exit.label}
      </button>
    </section>
  );
}

export type PresetStateCardProps = Omit<StateCardProps, "state">;

/** card_expired: nothing was changed; ask again. */
export function ExpiredCard(props: PresetStateCardProps) {
  return <StateCard {...props} state="expired" />;
}

/** card_consumed: a single-use approval was already used. */
export function ConsumedCard(props: PresetStateCardProps) {
  return <StateCard {...props} state="consumed" />;
}

/** worker_offline mid-turn: what arrived is kept; try again. */
export function WorkerOfflineCard(props: PresetStateCardProps) {
  return <StateCard {...props} state="worker_offline" />;
}

/** This build cannot decode the conversation. */
export function UpdateRequiredCard(props: PresetStateCardProps) {
  return <StateCard {...props} state="update_required" />;
}
