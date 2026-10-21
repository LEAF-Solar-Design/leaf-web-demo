import type { ReasonAction, ReasonV1, ToolCallV1 } from "../../../assistant-contract/src/index";
import { TONE_GLYPH, UNAVAILABLE } from "../states";
import { color, radius, toneColor } from "../tokens";
import { ReasonBanner } from "./ReasonBanner";

/** Consecutive tool events merge: one chip per invocation, first-seen order, latest state wins. O(n). */
export function mergeToolCalls(tools: readonly ToolCallV1[]): ToolCallV1[] {
  const order: string[] = [];
  const latest = new Map<string, ToolCallV1>();
  for (const tool of tools) {
    if (!latest.has(tool.invocation_id)) order.push(tool.invocation_id);
    latest.set(tool.invocation_id, tool);
  }
  const merged: ToolCallV1[] = [];
  for (const id of order) {
    const tool = latest.get(id);
    if (tool) merged.push(tool);
  }
  return merged;
}

const STATE_WORD: Readonly<Record<ToolCallV1["state"], string>> = {
  running: "Running",
  done: "Done",
  failed: UNAVAILABLE,
};

const STATE_TONE = { running: "progress", done: "healthy", failed: "critical" } as const;

export interface AssistantToolCardProps {
  tools: readonly ToolCallV1[];
  onAction?: (action: ReasonAction, reason: ReasonV1) => void;
}

/**
 * The ChipRow: every tool of a run of consecutive tool events on ONE line,
 * with the server's label. A failed read says Unavailable and carries its
 * reason, never an empty success.
 */
export function AssistantToolCard({ tools, onAction }: AssistantToolCardProps) {
  const merged = mergeToolCalls(tools);
  const failed = merged.filter((tool) => tool.state === "failed" && tool.reason !== undefined);
  return (
    <div data-testid="assistant-tool-card" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <ul
        aria-label="Tools used"
        data-testid="assistant-tool-row"
        style={{
          display: "flex",
          flexWrap: "nowrap",
          gap: 8,
          overflowX: "auto",
          whiteSpace: "nowrap",
          listStyle: "none",
          margin: 0,
          padding: 0,
        }}
      >
        {merged.map((tool) => {
          const tone = STATE_TONE[tool.state];
          return (
            <li
              key={tool.invocation_id}
              data-state={tool.state}
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                padding: "2px 10px",
                border: `1px solid ${color.border}`,
                borderRadius: radius,
                background: color.secondary,
                color: color["secondary-foreground"],
                fontSize: 13,
              }}
            >
              <span
                aria-hidden="true"
                className={tool.state === "running" ? "leaf-assistant-pulse" : undefined}
                style={{ color: toneColor(tone) }}
              >
                {TONE_GLYPH[tone]}
              </span>
              <span>{tool.label}</span>
              <span style={{ color: color["muted-foreground"] }}>{STATE_WORD[tool.state]}</span>
            </li>
          );
        })}
      </ul>
      {failed.map((tool) =>
        tool.reason ? <ReasonBanner key={`reason-${tool.invocation_id}`} reason={tool.reason} onAction={onAction} /> : null,
      )}
    </div>
  );
}
