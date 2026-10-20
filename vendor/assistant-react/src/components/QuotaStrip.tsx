import type { ThreadV1 } from "../../../assistant-contract/src/index";
import { formatClockCt } from "../states";
import { color, figure, meta } from "../tokens";

export interface QuotaStripProps {
  quota: ThreadV1["quota"];
}

/** Questions used against the server's quota, and when it resets (CT). Renders nothing when the server sends no quota. */
export function QuotaStrip({ quota }: QuotaStripProps) {
  if (!quota) return null;
  const windowWord = quota.window === "hour" ? "this hour" : "today";
  const left = Math.max(0, quota.limit - quota.used);
  const percent = Math.min(100, Math.max(0, Math.round((quota.used / quota.limit) * 100)));
  const headline = left > 0 ? `${quota.used} of ${quota.limit} questions used ${windowWord}` : `No questions left ${windowWord}`;
  return (
    <div data-testid="assistant-quota" style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
      <div
        role="meter"
        aria-label="Questions used"
        aria-valuemin={0}
        aria-valuemax={quota.limit}
        aria-valuenow={quota.used}
        style={{ width: 96, height: 6, borderRadius: 3, background: color.muted, overflow: "hidden" }}
      >
        <div
          style={{
            width: `${percent}%`,
            height: "100%",
            background: left === 0 ? color["state-aging"] : color.foreground,
          }}
        />
      </div>
      <span style={{ ...figure, fontSize: 13 }}>{headline}</span>
      <span style={meta}>{`Resets at ${formatClockCt(quota.resets_at)}`}</span>
    </div>
  );
}
