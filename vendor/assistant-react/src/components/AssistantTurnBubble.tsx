import type { BlockV1, ConfirmCardV1, ItemV1, ToolCallV1 } from "../../../assistant-contract/src/index";
import { formatWhen, plainModelText } from "../states";
import type { ExitAction } from "../states";
import { color, eyebrow, figure, meta, radius } from "../tokens";
import type { ApproveIntent } from "./AssistantConfirmCard";
import { AssistantToolCard } from "./AssistantToolCard";
import { AssistantViewPanel } from "./AssistantViewPanel";
import { ConsentCard } from "./PresetCards";
import { ReasonBanner } from "./ReasonBanner";

export interface TurnContext {
  /** invocation_id to the server's tool label, across the whole thread. */
  toolLabels: ReadonlyMap<string, string>;
  now: number;
  onAction?: (action: ExitAction) => void;
  onApprove?: (intent: ApproveIntent) => void;
  onDecline?: (card: ConfirmCardV1) => void;
}

type NonToolBlock = Exclude<BlockV1, { type: "tools" }>;
export type BlockGroup =
  | { readonly kind: "tools"; readonly key: string; readonly tools: ToolCallV1[] }
  | { readonly kind: "block"; readonly key: string; readonly block: NonToolBlock };

/** Consecutive tools blocks become one ChipRow; every other block keeps its place. O(n). */
export function groupBlocks(blocks: readonly BlockV1[]): BlockGroup[] {
  const groups: BlockGroup[] = [];
  blocks.forEach((block, index) => {
    if (block.type === "tools") {
      const last = groups[groups.length - 1];
      if (last !== undefined && last.kind === "tools") {
        last.tools.push(...block.tools);
        return;
      }
      groups.push({ kind: "tools", key: `tools-${index}`, tools: [...block.tools] });
      return;
    }
    groups.push({ kind: "block", key: `${block.type}-${index}`, block });
  });
  return groups;
}

function BlockView({ block, context, fromUser }: { block: NonToolBlock; context: TurnContext; fromUser: boolean }) {
  const forward = (action: ExitAction) => context.onAction?.(action);
  switch (block.type) {
    case "text":
      // Model text is ONE text node: no markup, links, headings, buttons or consent glyphs.
      return (
        <p style={{ margin: 0, whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
          {fromUser ? block.text : plainModelText(block.text)}
        </p>
      );
    case "view":
      return <AssistantViewPanel view={block} toolLabels={context.toolLabels} now={context.now} />;
    case "card":
      return (
        <ConsentCard card={block.card} now={context.now} onApprove={context.onApprove} onDecline={context.onDecline} />
      );
    case "artifact":
      return (
        <div data-testid="assistant-artifact" style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <span style={eyebrow}>Artifact</span>
          <span style={{ fontWeight: 600 }}>{block.title}</span>
          <span style={{ ...meta, ...figure }}>{`${block.digest.slice(0, 12)} · as of ${formatWhen(block.as_of, context.now)}`}</span>
        </div>
      );
    case "notice":
      return <ReasonBanner reason={block.reason} onAction={forward} />;
  }
}

export interface AssistantTurnBubbleProps {
  item: ItemV1;
  context: TurnContext;
}

/** One ThreadV1 item. User text sits in a bubble; assistant and server blocks run full width in the order they happened. */
export function AssistantTurnBubble({ item, context }: AssistantTurnBubbleProps) {
  const fromUser = item.role === "user";
  const forward = (action: ExitAction) => context.onAction?.(action);
  return (
    <article
      data-seq={item.seq}
      data-role={item.role}
      data-item-id={item.item_id}
      aria-label={fromUser ? "You" : item.role === "assistant" ? "Assistant" : "Notice"}
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 8,
        alignSelf: fromUser ? "flex-end" : "stretch",
        maxWidth: fromUser ? "85%" : "100%",
        padding: fromUser ? "8px 12px" : 0,
        borderRadius: fromUser ? radius : 0,
        background: fromUser ? color.secondary : "transparent",
        color: fromUser ? color["secondary-foreground"] : color.foreground,
      }}
    >
      {groupBlocks(item.blocks).map((group) =>
        group.kind === "tools" ? (
          <AssistantToolCard key={group.key} tools={group.tools} onAction={forward} />
        ) : (
          <BlockView key={group.key} block={group.block} context={context} fromUser={fromUser} />
        ),
      )}
    </article>
  );
}
