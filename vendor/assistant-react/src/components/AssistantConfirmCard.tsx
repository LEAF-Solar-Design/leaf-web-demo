import { useRef, useState } from "react";
import type { ConfirmCardV1 } from "../../../assistant-contract/src/index";
import {
  CARD_STATE_INK,
  TONE_GLYPH,
  formatCountdown,
  formatSignedMoney,
  newRequestId,
  plainModelText,
  stateSpec,
} from "../states";
import type { StateTone } from "../tokens";
import { color, eyebrow as eyebrowStyle, figure, meta, panel, primaryButton, secondaryButton, target, toneColor } from "../tokens";

/** What Approve hands the host: exactly the ApproveRequestV1 body plus the card to post it to. */
export interface ApproveIntent {
  readonly card_id: string;
  readonly card_digest: string;
  readonly request_id: string;
}

export interface AssistantConfirmCardProps {
  card: ConfirmCardV1;
  /** Epoch ms; drives the expiry countdown. */
  now: number;
  /** Preset label above the StateInk (ProposalCard, ConfigDraftCard, ArtifactCard). */
  eyebrow?: string;
  onApprove?: (intent: ApproveIntent) => void;
  onDecline?: (card: ConfirmCardV1) => void;
}

export function StateInk({ label, tone }: { label: string; tone: StateTone }) {
  return (
    <span
      data-testid="state-ink"
      data-tone={tone}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        fontSize: 11,
        fontWeight: 700,
        letterSpacing: "0.08em",
        color: toneColor(tone),
      }}
    >
      <span aria-hidden="true">{TONE_GLYPH[tone]}</span> {label}
    </span>
  );
}

function SpendLine({ spend }: { spend: ConfirmCardV1["spend"] }) {
  // "Nothing was spent" only when the server says so; unknown is never rounded down to nothing.
  if (spend.state === "none") return <p data-testid="assistant-spend" style={{ margin: 0 }}>Nothing was spent</p>;
  if (spend.state === "unknown" || spend.cents === null) {
    return <p data-testid="assistant-spend" style={{ margin: 0 }}>Spend not reported yet</p>;
  }
  const money = formatSignedMoney(spend.cents);
  return (
    <p data-testid="assistant-spend" aria-label={`Spend ${money.text}`} style={{ margin: 0 }}>
      <span style={{ ...figure, fontSize: 28, fontWeight: 600 }}>
        <span aria-hidden="true">{money.arrow}</span> {money.text}
      </span>
    </p>
  );
}

/**
 * The one consent component (K-D5). Above Approve it renders ONLY fields whose
 * origin is server and placement is above_approve, plus client chrome (StateInk,
 * countdown, spend line). Every other field, model prose above all, goes into
 * the collapsed, labelled "Assistant's note" below the buttons, even when a
 * payload that slipped past the decoder places it higher. Button labels are
 * the server's. A resolved card keeps its receipt line in the transcript.
 */
export function AssistantConfirmCard({ card, now, eyebrow, onApprove, onDecline }: AssistantConfirmCardProps) {
  const requestId = useRef<string | null>(null);
  const [submitted, setSubmitted] = useState<"approve" | "decline" | null>(null);

  const expiresMs = Date.parse(card.expires_at);
  const lapsed = card.state === "pending" && !(now < expiresMs);
  const effective = lapsed ? "expired" : card.state;
  const ink = CARD_STATE_INK[effective];
  const pending = effective === "pending";
  const above = card.fields.filter((field) => field.placement === "above_approve" && field.origin === "server");
  const note = card.fields.filter((field) => !(field.placement === "above_approve" && field.origin === "server"));

  const approve = () => {
    if (!pending || submitted !== null) return;
    const id = requestId.current ?? newRequestId();
    requestId.current = id;
    setSubmitted("approve");
    onApprove?.({ card_id: card.card_id, card_digest: card.card_digest, request_id: id });
  };
  const decline = () => {
    if (!pending || submitted !== null) return;
    setSubmitted("decline");
    onDecline?.(card);
  };

  return (
    <section
      data-testid="assistant-confirm-card"
      data-card-state={effective}
      aria-label={card.title.text}
      style={{ ...panel, display: "flex", flexDirection: "column", gap: 12 }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        {eyebrow ? <span style={eyebrowStyle}>{eyebrow}</span> : null}
        <StateInk label={ink.ink} tone={ink.tone} />
        {pending ? (
          <span data-testid="assistant-countdown" style={{ ...figure, ...meta, marginLeft: "auto" }}>
            {`Expires in ${formatCountdown(expiresMs - now)}`}
          </span>
        ) : null}
      </div>
      <div style={{ fontWeight: 600, fontSize: 17 }}>{card.title.text}</div>
      <dl data-testid="assistant-card-fields" style={{ margin: 0, display: "grid", gap: 6 }}>
        {above.map((field) => (
          <div key={field.key} style={{ display: "flex", gap: 12, justifyContent: "space-between" }}>
            <dt style={{ color: color["muted-foreground"] }}>{field.label}</dt>
            <dd style={{ margin: 0, textAlign: "right", fontWeight: 600 }}>{field.value}</dd>
          </div>
        ))}
      </dl>
      <SpendLine spend={card.spend} />
      {pending && card.requires_face_id ? <p style={{ ...meta, margin: 0 }}>Approving needs Face ID.</p> : null}
      {pending ? (
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button type="button" style={primaryButton} disabled={submitted !== null} onClick={approve}>
            {card.approve.text}
          </button>
          <button type="button" style={secondaryButton} disabled={submitted !== null} onClick={decline}>
            {card.decline.text}
          </button>
        </div>
      ) : null}
      {pending && submitted !== null ? (
        <p role="status" style={{ ...meta, margin: 0 }}>
          {submitted === "approve" ? "Sending your approval" : "Sending your answer"}
        </p>
      ) : null}
      {note.length > 0 ? (
        <details data-testid="assistant-note">
          <summary style={{ ...target, display: "flex", alignItems: "center", cursor: "pointer", color: color["muted-foreground"] }}>
            Assistant's note
          </summary>
          {note.map((field) => (
            <p key={field.key} style={{ margin: "4px 0 0", whiteSpace: "pre-wrap" }}>
              {field.origin === "model" ? plainModelText(field.value) : field.value}
            </p>
          ))}
        </details>
      ) : null}
      {lapsed ? <p style={{ margin: 0 }}>{stateSpec("expired").copy}</p> : null}
      {card.resolution ? (
        <p data-testid="assistant-receipt" style={{ ...meta, margin: 0 }}>
          {card.resolution.receipt.text}
        </p>
      ) : null}
    </section>
  );
}
