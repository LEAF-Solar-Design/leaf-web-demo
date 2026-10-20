import { z } from "zod";
import { ConfirmCardV1 } from "./card";
import { Id, IsoUtc, Label, Seq, Sha256Hex, ServerReasonV1 } from "./reason";

// ThreadV1 is the one view of a conversation both platforms render (VI.3).
// Items arrive in seq order; blocks interleave where they happened. view, card,
// artifact and notice blocks are SERVER-ONLY: each requires `origin: "server"`,
// and the worker event union below has no variant that can carry one.

/** View titles never borrow the consent vocabulary, so an answer cannot pose as a card. */
export const STATE_VOCABULARY = /(approve|applied|requested|needs your ok|real money)/i;

export const ToolCallV1 = z
  .strictObject({
    invocation_id: Id,
    name: Id,
    label: Label,
    state: z.enum(["running", "done", "failed"]),
    reason: ServerReasonV1.optional(),
  })
  .superRefine((tool, ctx) => {
    // A failed read renders as failed, with its reason, never as an empty success.
    if ((tool.state === "failed") !== (tool.reason !== undefined)) {
      ctx.addIssue({ code: "custom", path: ["reason"], message: "a tool carries a reason exactly when it failed" });
    }
  });
export type ToolCallV1 = z.infer<typeof ToolCallV1>;

const TextBlock = z.strictObject({
  type: z.literal("text"),
  text: z.string().min(1).max(16000),
});

const ToolsBlock = z.strictObject({
  type: z.literal("tools"),
  tools: z.array(ToolCallV1).min(1).max(16),
});

const ViewRow = z.strictObject({
  invocation_id: Id,
  label: Label,
  value: z.union([z.number(), z.string().max(200), z.null()]),
  unit: z.enum(["cents", "pct", "count", "text"]).nullable(),
});

const ViewBlock = z
  .strictObject({
    type: z.literal("view"),
    origin: z.literal("server"),
    view_id: Id,
    kind: z.enum(["metric", "table", "line"]),
    title: Label,
    invocation_ids: z.array(Id).min(1).max(16),
    as_of: IsoUtc,
    rows: z.array(ViewRow).min(1).max(50),
  })
  .superRefine((view, ctx) => {
    if (STATE_VOCABULARY.test(view.title)) {
      ctx.addIssue({ code: "custom", path: ["title"], message: "a view title may not use the consent vocabulary" });
    }
    const declared = new Set(view.invocation_ids);
    view.rows.forEach((row, index) => {
      if (!declared.has(row.invocation_id)) {
        ctx.addIssue({
          code: "custom",
          path: ["rows", index, "invocation_id"],
          message: `row cites ${row.invocation_id}, which this view does not declare`,
        });
      }
    });
  });

const CardBlock = z.strictObject({
  type: z.literal("card"),
  origin: z.literal("server"),
  card: ConfirmCardV1,
});

const ArtifactBlock = z.strictObject({
  type: z.literal("artifact"),
  origin: z.literal("server"),
  artifact_id: Id,
  title: Label,
  digest: Sha256Hex,
  as_of: IsoUtc,
});

const NoticeBlock = z.strictObject({
  type: z.literal("notice"),
  origin: z.literal("server"),
  reason: ServerReasonV1,
});

export const BlockV1 = z.union([TextBlock, ToolsBlock, ViewBlock, CardBlock, ArtifactBlock, NoticeBlock]);
export type BlockV1 = z.infer<typeof BlockV1>;

const SERVER_ONLY_BLOCKS = new Set(["view", "card", "artifact", "notice"]);

export const ItemV1 = z
  .strictObject({
    seq: Seq,
    item_id: Id,
    turn_id: Id,
    role: z.enum(["user", "assistant", "server"]),
    at: IsoUtc,
    blocks: z.array(BlockV1).min(1).max(32),
  })
  .superRefine((item, ctx) => {
    item.blocks.forEach((block, index) => {
      if (item.role === "user" && block.type !== "text") {
        ctx.addIssue({ code: "custom", path: ["blocks", index], message: "a user item carries text only" });
      }
      if (item.role === "server" && !SERVER_ONLY_BLOCKS.has(block.type)) {
        ctx.addIssue({ code: "custom", path: ["blocks", index], message: "a server item carries server blocks only" });
      }
    });
  });
export type ItemV1 = z.infer<typeof ItemV1>;

/** True when the item holds a card still waiting for the user's decision. */
export function itemRequiresAction(item: ItemV1): boolean {
  return item.blocks.some((block) => block.type === "card" && block.card.state === "pending");
}

export const ThreadV1 = z
  .strictObject({
    session_id: Id,
    scope: z.strictObject({
      kind: z.enum(["owner", "tenant"]),
      label: Label,
    }),
    provider: z.strictObject({
      kind: z.enum(["claude-code-cli", "anthropic-api", "bedrock", "codex-cli"]),
      label: Label,
      model: Id,
    }),
    quota: z
      .strictObject({
        used: z.number().int().min(0).max(1000000),
        limit: z.number().int().min(1).max(1000000),
        window: z.enum(["hour", "day"]),
        resets_at: IsoUtc,
      })
      .nullable(),
    items: z.array(ItemV1).max(500),
  })
  .superRefine((thread, ctx) => {
    if (thread.quota !== null && thread.quota.used > thread.quota.limit) {
      ctx.addIssue({ code: "custom", path: ["quota", "used"], message: "quota used cannot exceed its limit" });
    }
    // One linear pass in render order: seq order, unique ids, and every view
    // resolves to a completed tool invocation recorded before it (K-D4).
    let previousSeq = -1;
    const itemIds = new Set<string>();
    const invocations = new Map<string, ToolCallV1["state"]>();
    thread.items.forEach((item, i) => {
      if (item.seq <= previousSeq) {
        ctx.addIssue({ code: "custom", path: ["items", i, "seq"], message: "items are in strictly increasing seq order" });
      }
      previousSeq = item.seq;
      if (itemIds.has(item.item_id)) {
        ctx.addIssue({ code: "custom", path: ["items", i, "item_id"], message: `duplicate item ${item.item_id}` });
      }
      itemIds.add(item.item_id);
      item.blocks.forEach((block, b) => {
        if (block.type === "tools") {
          block.tools.forEach((tool, t) => {
            if (invocations.has(tool.invocation_id)) {
              ctx.addIssue({
                code: "custom",
                path: ["items", i, "blocks", b, "tools", t, "invocation_id"],
                message: `duplicate invocation ${tool.invocation_id}`,
              });
            }
            invocations.set(tool.invocation_id, tool.state);
          });
        } else if (block.type === "view") {
          block.invocation_ids.forEach((id, k) => {
            if (invocations.get(id) !== "done") {
              ctx.addIssue({
                code: "custom",
                path: ["items", i, "blocks", b, "invocation_ids", k],
                message: `view cites ${id}, which is not a completed tool invocation recorded before it`,
              });
            }
          });
        }
      });
    });
  });
export type ThreadV1 = z.infer<typeof ThreadV1>;

// Server-to-client events. The type travels in the envelope (K-D7: unnamed SSE
// frames), and `requires_action` tells a client to surface the turn without
// having to inspect the item.
export const TURN_STATES = ["queued", "running", "done", "failed", "stopped", "expired"] as const;

const ItemEvent = z.strictObject({
  type: z.literal("item"),
  session_id: Id,
  seq: Seq,
  at: IsoUtc,
  requires_action: z.boolean(),
  item: ItemV1,
});

const TurnEvent = z.strictObject({
  type: z.literal("turn"),
  session_id: Id,
  seq: Seq,
  at: IsoUtc,
  requires_action: z.boolean(),
  turn_id: Id,
  state: z.enum(TURN_STATES),
  reason: ServerReasonV1.optional(),
});

export const ThreadEventV1 = z.union([ItemEvent, TurnEvent]).superRefine((event, ctx) => {
  if (event.type === "item") {
    const expected = itemRequiresAction(event.item);
    if (event.requires_action !== expected) {
      ctx.addIssue({
        code: "custom",
        path: ["requires_action"],
        message: `requires_action is ${expected} exactly when the item holds a pending card`,
      });
    }
    return;
  }
  if (event.requires_action) {
    ctx.addIssue({ code: "custom", path: ["requires_action"], message: "a turn event never requires action" });
  }
  const needsReason = event.state === "failed" || event.state === "expired";
  if (needsReason !== (event.reason !== undefined)) {
    ctx.addIssue({
      code: "custom",
      path: ["reason"],
      message: needsReason ? `a ${event.state} turn carries its reason` : `a ${event.state} turn carries no reason`,
    });
  }
});
export type ThreadEventV1 = z.infer<typeof ThreadEventV1>;

export const EventsPageV1 = z.strictObject({
  events: z.array(ThreadEventV1).max(200),
  next_after: Seq,
});

/** POST /sessions/{session_id}/turns. 7,000 characters is where the composer's counter starts. */
export const TurnRequestV1 = z.strictObject({
  request_id: Id,
  text: z.string().min(1).max(7000),
});

// Worker-to-server events (K-D3). The worker owns seq and may send ONLY these
// six types. There is no variant for view, card, artifact or notice, and every
// variant is strict, so a worker cannot smuggle `origin: "server"` either: the
// events endpoint answers 400 for anything this union refuses.
const workerCommon = { job_id: Id, seq: Seq };

export const WorkerEventV1 = z.union([
  z.strictObject({ ...workerCommon, type: z.literal("status"), status: z.enum(["thinking", "streaming"]) }),
  z.strictObject({ ...workerCommon, type: z.literal("text"), text: z.string().min(1).max(16000) }),
  z.strictObject({
    ...workerCommon,
    type: z.literal("tool"),
    tool_call_id: Id,
    name: Id,
    state: z.enum(["running", "done", "failed"]),
  }),
  z.strictObject({
    ...workerCommon,
    type: z.literal("error"),
    code: z.enum(["worker_offline", "tool_failed", "turn_expired", "internal"]),
  }),
  z.strictObject({ ...workerCommon, type: z.literal("card_ref"), card_id: Id }),
  z.strictObject({ ...workerCommon, type: z.literal("artifact_ref"), artifact_id: Id }),
]);
export type WorkerEventV1 = z.infer<typeof WorkerEventV1>;

export const WorkerEventsBatchV1 = z.strictObject({
  events: z.array(WorkerEventV1).min(1).max(100),
});

// Worker-to-server usage (TCM-03): transparency of what a job cost, never
// billing, quota or a limit. A report is CUMULATIVE for the job attempt, so a
// replayed or reordered report cannot double count. It takes its own seq in the
// worker's one seq space but is NOT a WorkerEventV1 variant: it never reaches a
// thread, and the six-type worker event union (and the committed schema bundle
// the worker validates against) stays unchanged. Token counts are optional and
// absent when the provider does not report them; `usd` is a decimal string.
export const USAGE_USD_PATTERN = /^\d{1,9}(\.\d{1,12})?$/;
const TokenCount = z.number().int().min(0).max(1_000_000_000_000);

export const JobUsageV1 = z.strictObject({
  input_tokens: TokenCount.optional(),
  output_tokens: TokenCount.optional(),
  cache_read_tokens: TokenCount.optional(),
  cache_write_tokens: TokenCount.optional(),
  usd: z.string().regex(USAGE_USD_PATTERN),
  provider: z.enum(["claude-code-cli", "anthropic-api", "bedrock", "codex-cli"]),
  model: Id,
});
export type JobUsageV1 = z.infer<typeof JobUsageV1>;

/** POST /worker/jobs/{job_id}/usage. */
export const WorkerUsageReportV1 = z.strictObject({
  job_id: Id,
  seq: Seq,
  usage: JobUsageV1,
});
export type WorkerUsageReportV1 = z.infer<typeof WorkerUsageReportV1>;

// A golden conversation: the thread snapshot plus the event stream that built
// it. Folding the item events (latest version per item_id, in item seq order)
// must reproduce thread.items exactly, and an item keeps the seq of the event
// that first delivered it, so a card stays where it happened.
export const GoldenConversationV1 = z
  .strictObject({
    name: Id,
    title: z.string().min(1).max(120),
    thread: ThreadV1,
    events: z.array(ThreadEventV1).max(200),
  })
  .superRefine((golden, ctx) => {
    let previousSeq = -1;
    const firstSeq = new Map<string, number>();
    const latest = new Map<string, ItemV1>();
    golden.events.forEach((event, index) => {
      if (event.seq <= previousSeq) {
        ctx.addIssue({ code: "custom", path: ["events", index, "seq"], message: "events are in strictly increasing seq order" });
      }
      previousSeq = event.seq;
      if (event.session_id !== golden.thread.session_id) {
        ctx.addIssue({ code: "custom", path: ["events", index, "session_id"], message: "event belongs to another session" });
      }
      if (event.type !== "item") return;
      const id = event.item.item_id;
      const first = firstSeq.get(id);
      if (first === undefined) {
        firstSeq.set(id, event.seq);
        if (event.item.seq !== event.seq) {
          ctx.addIssue({
            code: "custom",
            path: ["events", index, "item", "seq"],
            message: "an item's seq is the seq of the event that first delivered it",
          });
        }
      } else if (event.item.seq !== first) {
        ctx.addIssue({ code: "custom", path: ["events", index, "item", "seq"], message: "an updated item keeps its seq" });
      }
      latest.set(id, event.item);
    });
    const folded = [...latest.values()].sort((a, b) => a.seq - b.seq);
    if (JSON.stringify(folded) !== JSON.stringify(golden.thread.items)) {
      ctx.addIssue({ code: "custom", path: ["events"], message: "folding the item events must reproduce thread.items" });
    }
  });
export type GoldenConversationV1 = z.infer<typeof GoldenConversationV1>;
