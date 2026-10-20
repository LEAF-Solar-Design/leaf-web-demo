import { useMemo, useRef, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import { GoldenConversationV1, ThreadV1, clientReason } from "../../assistant-contract/src/index";
import type { ConfirmCardV1, ItemV1 } from "../../assistant-contract/src/index";
import { AssistantComposer } from "./components/AssistantComposer";
import type { ComposerSend } from "./components/AssistantComposer";
import type { ApproveIntent } from "./components/AssistantConfirmCard";
import { AssistantStream } from "./components/AssistantStream";
import { AssistantTurnBubble } from "./components/AssistantTurnBubble";
import type { TurnContext } from "./components/AssistantTurnBubble";
import { ProviderChip } from "./components/ProviderChip";
import { QuotaStrip } from "./components/QuotaStrip";
import { StateCard } from "./components/StateCards";
import { liveContext, liveStateFor, safeStarters, stateSpec, useClock } from "./states";
import type { ExitAction, LiveTurn } from "./states";
import { BASE_CSS, color, font, secondaryButton, tokenStyle } from "./tokens";
import type { ColorMode } from "./tokens";

export type Availability =
  | "ready"
  | "unconfigured"
  | "unpaired"
  | "access_revoked"
  | "assistant_disabled"
  | "update_required";
export type Connection = "online" | "offline" | "lost";

// ---------------------------------------------------------------------------
// Decoding. A payload this build cannot decode is the update_required state,
// never a partial render.

export type DecodeResult<T> =
  | { readonly ok: true; readonly value: T }
  | { readonly ok: false; readonly state: "update_required"; readonly issues: number };

export function decodeThread(input: unknown): DecodeResult<ThreadV1> {
  const parsed = ThreadV1.safeParse(input);
  return parsed.success
    ? { ok: true, value: parsed.data }
    : { ok: false, state: "update_required", issues: parsed.error.issues.length };
}

export function decodeGolden(input: unknown): DecodeResult<GoldenConversationV1> {
  const parsed = GoldenConversationV1.safeParse(input);
  return parsed.success
    ? { ok: true, value: parsed.data }
    : { ok: false, state: "update_required", issues: parsed.error.issues.length };
}

/** Items in seq order. The decoder already guarantees it; a copy is sorted only if the input is not. */
export function itemsInSeqOrder(items: readonly ItemV1[]): readonly ItemV1[] {
  for (let i = 1; i < items.length; i += 1) {
    if (items[i].seq <= items[i - 1].seq) return [...items].sort((a, b) => a.seq - b.seq);
  }
  return items;
}

/** invocation_id to server label for every tool in the thread, for view provenance. */
export function toolLabelsOf(items: readonly ItemV1[]): Map<string, string> {
  const labels = new Map<string, string>();
  for (const item of items) {
    for (const block of item.blocks) {
      if (block.type !== "tools") continue;
      for (const tool of block.tools) labels.set(tool.invocation_id, tool.label);
    }
  }
  return labels;
}

/** Sets the Leaf tokens for one colour mode and injects the base stylesheet. */
export function AssistantRoot({
  mode = "light",
  style,
  children,
}: {
  mode?: ColorMode;
  style?: CSSProperties;
  children: ReactNode;
}) {
  return (
    <div
      className="leaf-assistant"
      data-mode={mode}
      style={{ ...tokenStyle(mode), fontFamily: font.sans, color: color.foreground, background: color.background, ...style }}
    >
      <style>{BASE_CSS}</style>
      {children}
    </div>
  );
}

export interface AssistantThreadProps {
  thread: ThreadV1;
  /** The live turn folded from the event stream (foldEvents). */
  live?: LiveTurn | null;
  /** Epoch ms. Fixed for tests and replays; omitted, a 1 s clock runs while a turn is live. */
  now?: number;
  mode?: ColorMode;
  availability?: Availability;
  connection?: Connection;
  /** Server-authored starters; at most 4 without digits are shown. */
  starters?: readonly string[];
  /** Queue position from the server, when it sends one. */
  queuePosition?: number | null;
  busyDevice?: "phone" | "web" | null;
  onSend?: ComposerSend;
  onApprove?: (intent: ApproveIntent) => void;
  onDecline?: (card: ConfirmCardV1) => void;
  /** Every exit that is not compose (retry, wait, refresh, stop, pair, open_settings, none). */
  onAction?: (action: ExitAction) => void;
  sendTimeoutMs?: number;
  isOnline?: () => boolean;
}

// With no onSend the composer stays visible but disabled; it never claims "Sent" for a message that went nowhere.
const noSend: ComposerSend = async () => ({ ok: false, reason: clientReason("client.offline") });

/** A server-recorded notice for the live turn already says why it failed; the live card would say it twice. */
function failureAlreadyShown(live: LiveTurn, items: readonly ItemV1[]): boolean {
  const code = live.reason?.code;
  if (code === undefined) return false;
  return items.some(
    (item) =>
      item.turn_id === live.turn_id && item.blocks.some((block) => block.type === "notice" && block.reason.code === code),
  );
}

/**
 * ThreadV1, rendered: provider and quota, items in seq order with blocks
 * where they happened, the live turn's state with its one exit, and the
 * composer. Availability states replace the transcript; unconfigured keeps
 * `assistant.empty` and `assistant.prompt` on screen.
 */
export function AssistantThread(props: AssistantThreadProps) {
  const {
    thread,
    live = null,
    mode = "light",
    availability = "ready",
    connection = "online",
    queuePosition = null,
    busyDevice = null,
    onSend,
    onApprove,
    onDecline,
    onAction,
    sendTimeoutMs,
    isOnline,
  } = props;
  const inFlight = live !== null && (live.state === "queued" || live.state === "running");
  const now = useClock(props.now, inFlight);
  const items = useMemo(() => itemsInSeqOrder(thread.items), [thread.items]);
  const toolLabels = useMemo(() => toolLabelsOf(items), [items]);
  const starters = useMemo(() => safeStarters(props.starters), [props.starters]);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const seedCount = useRef(0);
  const [seed, setSeed] = useState<{ text: string; nonce: number } | null>(null);

  const handleAction = (action: ExitAction) => {
    if (action === "compose") {
      composerRef.current?.focus();
      return;
    }
    onAction?.(action);
  };

  const context: TurnContext = { toolLabels, now, onAction: handleAction, onApprove, onDecline };
  const ready = availability === "ready";

  let liveCard: ReactNode = null;
  let doneAnnouncement: string | null = null;
  if (ready && live !== null && !failureAlreadyShown(live, items)) {
    const state = liveStateFor(live);
    const ctx = liveContext(live, now, { queuePosition, busyDevice });
    liveCard = <StateCard state={state} context={ctx} reason={live.reason ?? null} onAction={handleAction} />;
    if (state === "done") doneAnnouncement = `Answer ready. ${stateSpec("done", ctx).copy}`;
  }

  let body: ReactNode;
  if (!ready) {
    const card = <StateCard state={availability} onAction={handleAction} />;
    body = availability === "unconfigured" ? <div data-testid="assistant.empty">{card}</div> : card;
  } else if (items.length === 0) {
    body = (
      <div data-testid="assistant.empty">
        <StateCard state="idle" onAction={handleAction}>
          {starters.length > 0 ? (
            <ul
              aria-label="Suggested questions"
              style={{ display: "flex", flexWrap: "wrap", gap: 8, listStyle: "none", margin: 0, padding: 0, width: "100%" }}
            >
              {starters.map((starter) => (
                <li key={starter}>
                  <button
                    type="button"
                    style={secondaryButton}
                    onClick={() => {
                      seedCount.current += 1;
                      setSeed({ text: starter, nonce: seedCount.current });
                    }}
                  >
                    {starter}
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
        </StateCard>
      </div>
    );
  } else {
    const last = items[items.length - 1];
    body = (
      <AssistantStream busy={inFlight} doneAnnouncement={doneAnnouncement} scrollKey={`${items.length}:${last.seq}:${live?.state ?? ""}`}>
        {items.map((item) => (
          <AssistantTurnBubble key={item.item_id} item={item} context={context} />
        ))}
        {liveCard}
      </AssistantStream>
    );
  }

  return (
    <AssistantRoot mode={mode} style={{ display: "flex", flexDirection: "column", gap: 16, padding: 16 }}>
      <header style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
        <ProviderChip provider={thread.provider} scope={thread.scope} />
        <QuotaStrip quota={thread.quota} />
      </header>
      {ready && connection !== "online" ? (
        <StateCard state={connection === "offline" ? "offline" : "lost_connection"} onAction={handleAction} />
      ) : null}
      {body}
      <AssistantComposer
        onSend={onSend ?? noSend}
        disabled={!ready || onSend === undefined}
        timeoutMs={sendTimeoutMs}
        isOnline={isOnline}
        textareaRef={composerRef}
        seed={seed}
      />
    </AssistantRoot>
  );
}
