import type { ReasonAction, ReasonV1 } from "../../../assistant-contract/src/index";
import { TONE_GLYPH, formatClockCt, reasonCopy } from "../states";
import type { StateTone } from "../tokens";
import { color, meta, radius, secondaryButton, toneColor } from "../tokens";

export interface ReasonBannerProps {
  reason: ReasonV1;
  onAction?: (action: ReasonAction, reason: ReasonV1) => void;
}

/**
 * One reason, its text and its one exit. The client authors text only for
 * client.offline and client.timeout (reasonCopy); every server code shows the
 * server's own words. A 429 always shows its reset time in CT.
 */
export function ReasonBanner({ reason, onAction }: ReasonBannerProps) {
  const copy = reasonCopy(reason);
  const tone: StateTone = reason.code === "rate_limited" || reason.code === "busy" ? "attention" : "critical";
  const reset = reason.code === "rate_limited" && reason.retry_at ? formatClockCt(reason.retry_at) : null;
  const showReset = reset !== null && !copy.text.includes(reset);
  return (
    <div
      role="status"
      data-testid="assistant-reason"
      data-code={reason.code}
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
      <span aria-hidden="true" style={{ color: toneColor(tone), fontWeight: 700 }}>
        {TONE_GLYPH[tone]}
      </span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <p style={{ margin: 0 }}>{copy.text}</p>
        {showReset ? <p style={{ ...meta, margin: 0 }}>{`Resets at ${reset}`}</p> : null}
      </div>
      <button type="button" style={secondaryButton} onClick={() => onAction?.(copy.action, reason)}>
        {copy.label}
      </button>
    </div>
  );
}
