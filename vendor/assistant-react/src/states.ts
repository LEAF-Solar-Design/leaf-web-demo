import { useEffect, useState } from "react";
import { REASON_TABLE } from "../../assistant-contract/src/index";
import type {
  CARD_STATES,
  ItemV1,
  ReasonAction,
  ReasonCode,
  ReasonV1,
  ThreadEventV1,
  TURN_STATES,
} from "../../assistant-contract/src/index";
import type { StateTone } from "./tokens";

export type { StateTone } from "./tokens";

// VI.3 of the assistant UX standard as data: every state a thread can be in,
// with its copy and exactly ONE exit. Components render from this table, so a
// state without copy, or with two exits, cannot ship. Everything here is pure
// and bounded except the clock hook at the bottom.

export const CT_TIME_ZONE = "America/Chicago";
export const NOT_REPORTED = "Not reported";
export const UNAVAILABLE = "Unavailable";
/** The null glyph, drawn from its code point; it always renders with the accessibility label "Not reported". */
export const NULL_GLYPH = "—";
export const STARTER_LIMIT = 4;
export const STARTER_MAX_CHARS = 120;
export const THINKING_SLOW_MS = 5_000;
export const THINKING_LONG_MS = 45_000;
/** Times under an hour old read relative ("12 min ago"). */
export const RELATIVE_WINDOW_MS = 3_600_000;

// ---------------------------------------------------------------------------
// Formatting. Every formatter fails closed to fixed copy on malformed input.

const ctClock = new Intl.DateTimeFormat("en-US", {
  timeZone: CT_TIME_ZONE,
  hour: "numeric",
  minute: "2-digit",
  hour12: true,
});

/** "4:12 PM CT". Built from parts so ICU's narrow no-break space never reaches copy. */
export function formatClockCt(iso: string): string {
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return "time not reported";
  let hour = "";
  let minute = "";
  let period = "";
  for (const part of ctClock.formatToParts(ms)) {
    if (part.type === "hour") hour = part.value;
    else if (part.type === "minute") minute = part.value;
    else if (part.type === "dayPeriod") period = part.value.toUpperCase();
  }
  return `${hour}:${minute} ${period} CT`;
}

/** Under an hour ago reads relative; anything older, and any future time, reads as a CT clock. */
export function formatWhen(iso: string, now: number): string {
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return "time not reported";
  const age = now - ms;
  if (age >= 0 && age < RELATIVE_WINDOW_MS) {
    const minutes = Math.floor(age / 60_000);
    return minutes < 1 ? "just now" : `${minutes} min ago`;
  }
  return formatClockCt(iso);
}

/** "6.2 s". */
export function formatSeconds(ms: number): string {
  const safe = Number.isFinite(ms) && ms > 0 ? ms : 0;
  return `${(safe / 1000).toFixed(1)} s`;
}

/** "4:32", or "1:04:32" past an hour. Never negative. */
export function formatCountdown(ms: number): string {
  const total = Number.isFinite(ms) ? Math.max(0, Math.ceil(ms / 1000)) : 0;
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = String(total % 60).padStart(2, "0");
  return hours > 0 ? `${hours}:${String(minutes).padStart(2, "0")}:${seconds}` : `${minutes}:${seconds}`;
}

/** "$1,234.50", "-$3.00". Cents are rounded to whole cents first. */
export function formatMoneyCents(cents: number): string {
  const whole = Math.round(Math.abs(cents));
  const dollars = Math.floor(whole / 100).toLocaleString("en-US");
  const rest = String(whole % 100).padStart(2, "0");
  return `${cents < 0 && whole !== 0 ? "-" : ""}$${dollars}.${rest}`;
}

/** Money with sign and arrow, for the large spend figure on a card. */
export function formatSignedMoney(cents: number): { arrow: string; text: string } {
  const whole = Math.round(cents);
  if (whole === 0 || !Number.isFinite(whole)) return { arrow: "", text: "$0.00" };
  return {
    arrow: whole > 0 ? "↑" : "↓",
    text: `${whole > 0 ? "+" : "-"}${formatMoneyCents(Math.abs(whole))}`,
  };
}

export type ViewUnit = "cents" | "pct" | "count" | "text" | null;

/** One view cell. A null (or non-finite) value is the dash glyph labelled "Not reported". */
export function formatViewValue(value: number | string | null, unit: ViewUnit): { text: string; label?: string } {
  if (value === null) return { text: NULL_GLYPH, label: NOT_REPORTED };
  if (typeof value === "string") return { text: value };
  if (!Number.isFinite(value)) return { text: NULL_GLYPH, label: NOT_REPORTED };
  switch (unit) {
    case "cents":
      return { text: formatMoneyCents(value) };
    case "pct":
      return { text: `${value.toLocaleString("en-US", { maximumFractionDigits: 2 })}%` };
    case "count":
      return { text: value.toLocaleString("en-US", { maximumFractionDigits: 0 }) };
    default:
      return { text: value.toLocaleString("en-US", { maximumFractionDigits: 2 }) };
  }
}

// ---------------------------------------------------------------------------
// Model text. The caller renders the result as ONE text node: no markup, no
// links, no headings. Consent glyphs and bidi overrides are removed so model
// prose cannot draw a checkmark or reorder what the user reads.

const CONSENT_GLYPHS = /[☑✓✔✅\u{1F5F8}]/gu;
const BIDI_CONTROLS = /[‎‏‪-‮⁦-⁩]/g;

export function plainModelText(text: string): string {
  return text.replace(BIDI_CONTROLS, "").replace(CONSENT_GLYPHS, "");
}

/** Server-authored starters: at most 4, each short, none containing a digit. */
export function safeStarters(starters: readonly unknown[] | undefined): string[] {
  const out: string[] = [];
  if (!starters) return out;
  for (const starter of starters) {
    if (out.length >= STARTER_LIMIT) break;
    if (typeof starter !== "string") continue;
    const text = starter.trim();
    if (text.length === 0 || text.length > STARTER_MAX_CHARS || /[0-9]/.test(text)) continue;
    out.push(text);
  }
  return out;
}

/** A request id that satisfies the contract's Id pattern. */
export function newRequestId(): string {
  const cryptoApi = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto;
  if (cryptoApi && typeof cryptoApi.randomUUID === "function") return cryptoApi.randomUUID();
  return `req-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

// ---------------------------------------------------------------------------
// Reasons. The client authors text only for client.offline and client.timeout;
// for every server code the server's own text and exit label are shown.

type ReasonEntryView = {
  readonly owner: "server" | "client";
  readonly action: ReasonAction;
  readonly client_text?: string;
  readonly client_label?: string;
};

export function reasonCopy(reason: ReasonV1): { text: string; label: string; action: ReasonAction } {
  const entry: ReasonEntryView = REASON_TABLE[reason.code];
  if (entry.owner === "client") {
    return {
      text: entry.client_text ?? reason.text,
      label: entry.client_label ?? reason.next.label,
      action: entry.action,
    };
  }
  return { text: reason.text, label: reason.next.label, action: reason.next.action };
}

// ---------------------------------------------------------------------------
// The state table.

export const ASSISTANT_STATE_IDS = [
  "idle",
  "offline",
  "unconfigured",
  "unpaired",
  "composing",
  "sending",
  "sent",
  "not_sent",
  "queued",
  "thinking",
  "tool_running",
  "tool_done",
  "tool_failed",
  "view",
  "done",
  "needs_confirmation",
  "requested",
  "applied",
  "refused",
  "declined",
  "expired",
  "consumed",
  "consent_superseded",
  "cancelled",
  "access_revoked",
  "assistant_disabled",
  "partial_then_error",
  "rate_limited",
  "busy",
  "stopped",
  "worker_offline",
  "lost_connection",
  "turn_expired",
  "update_required",
] as const;
export type AssistantStateId = (typeof ASSISTANT_STATE_IDS)[number];

/** Every exit a state can offer: the reason table's actions plus the thread's own. */
export type ExitAction = ReasonAction | "stop" | "compose" | "send" | "approve";

export interface StateExit {
  readonly label: string;
  readonly action: ExitAction;
}

export interface StateContext {
  elapsedMs?: number;
  queuePosition?: number | null;
  busyDevice?: "phone" | "web" | null;
  retryAt?: string;
  answeredMs?: number;
  toolLabel?: string;
  reasonText?: string;
  asOf?: string;
  approveLabel?: string;
}

export interface StateSpec {
  readonly id: AssistantStateId;
  readonly tone: StateTone;
  /** The StateInk label for consent states; null elsewhere. */
  readonly ink: string | null;
  readonly copy: string;
  readonly exit: StateExit;
}

export const TONE_GLYPH: Readonly<Record<StateTone, string>> = {
  neutral: "•",
  progress: "◌",
  attention: "!",
  critical: "✕",
  healthy: "✓",
  stale: "○",
};

type CardStateName = (typeof CARD_STATES)[number];

/** StateInk per card state. Only a pending card says NEEDS YOUR OK. */
export const CARD_STATE_INK: { readonly [K in CardStateName]: { readonly ink: string; readonly tone: StateTone } } = {
  pending: { ink: "NEEDS YOUR OK", tone: "attention" },
  approved: { ink: "APPROVED", tone: "healthy" },
  declined: { ink: "DECLINED", tone: "stale" },
  expired: { ink: "EXPIRED", tone: "stale" },
  superseded: { ink: "REPLACED", tone: "stale" },
};

type Copy = string | ((ctx: StateContext) => string);
type Exit = StateExit | ((ctx: StateContext) => StateExit);
interface Entry {
  readonly tone: StateTone;
  readonly ink?: string;
  readonly copy: Copy;
  readonly exit: Exit;
}

const STOP: StateExit = { label: "Stop", action: "stop" };
const FOLLOW_UP: StateExit = { label: "Ask a follow-up", action: "compose" };
const ASK_AGAIN: StateExit = { label: "Ask again", action: "compose" };
const TRY_AGAIN: StateExit = { label: "Try again", action: "retry" };
const OFFLINE = REASON_TABLE["client.offline"];

function queuedCopy(ctx: StateContext): string {
  const seconds = Math.floor(Math.max(0, ctx.elapsedMs ?? 0) / 1000);
  const position = ctx.queuePosition;
  const line = typeof position === "number" && Number.isInteger(position) && position > 0 ? ` · number ${position} in line` : "";
  return `Waiting for the assistant to start · ${seconds} s${line}`;
}

function thinkingCopy(ctx: StateContext): string {
  const elapsed = ctx.elapsedMs ?? 0;
  if (elapsed >= THINKING_LONG_MS) return "Taking longer than usual";
  if (elapsed >= THINKING_SLOW_MS) return "Still thinking";
  return "Thinking";
}

const toolLabel = (ctx: StateContext): string => ctx.toolLabel ?? "A tool";

const TABLE: { readonly [K in AssistantStateId]: Entry } = {
  idle: {
    tone: "neutral",
    copy: "Ask about your account. Answers come from your own data.",
    exit: { label: "Ask a question", action: "compose" },
  },
  offline: {
    tone: "stale",
    copy: OFFLINE.client_text,
    exit: { label: OFFLINE.client_label, action: OFFLINE.action },
  },
  unconfigured: {
    tone: "neutral",
    copy: "The assistant isn't set up here yet.",
    exit: { label: "Open settings", action: "open_settings" },
  },
  unpaired: {
    tone: "neutral",
    copy: "Pair this device to use the assistant.",
    exit: { label: "Pair device", action: "pair" },
  },
  composing: {
    tone: "neutral",
    copy: "Your draft stays on this device until you send it.",
    exit: { label: "Send", action: "send" },
  },
  sending: { tone: "progress", copy: "Sending", exit: STOP },
  sent: { tone: "progress", copy: "Sent", exit: STOP },
  not_sent: {
    tone: "critical",
    copy: (ctx) =>
      `Not sent. ${ctx.reasonText ?? "Your message didn't reach the assistant."} Your draft is back in the box.`,
    exit: TRY_AGAIN,
  },
  queued: { tone: "progress", copy: queuedCopy, exit: STOP },
  thinking: { tone: "progress", copy: thinkingCopy, exit: STOP },
  tool_running: { tone: "progress", copy: (ctx) => `${toolLabel(ctx)} is running`, exit: STOP },
  tool_done: { tone: "healthy", copy: (ctx) => `${toolLabel(ctx)} is done`, exit: STOP },
  tool_failed: { tone: "critical", copy: (ctx) => `${toolLabel(ctx)}: ${UNAVAILABLE}`, exit: TRY_AGAIN },
  view: {
    tone: "neutral",
    copy: (ctx) => (ctx.asOf ? `From your account · as of ${formatClockCt(ctx.asOf)}` : "From your account"),
    exit: FOLLOW_UP,
  },
  done: {
    tone: "healthy",
    copy: (ctx) => (ctx.answeredMs !== undefined ? `Answered in ${formatSeconds(ctx.answeredMs)}` : "Answered"),
    exit: FOLLOW_UP,
  },
  needs_confirmation: {
    tone: "attention",
    ink: CARD_STATE_INK.pending.ink,
    copy: "Review the details, then decide. Nothing happens until you approve.",
    exit: (ctx) => ({ label: ctx.approveLabel ?? "Review", action: "approve" }),
  },
  requested: {
    tone: "progress",
    ink: "REQUESTED",
    copy: "Requested. The server applies it after its own checks.",
    exit: { label: "Refresh", action: "refresh" },
  },
  applied: {
    tone: "healthy",
    ink: "APPLIED",
    copy: "Applied. The receipt stays in this conversation.",
    exit: FOLLOW_UP,
  },
  refused: {
    tone: "critical",
    ink: "REFUSED",
    copy: "The server refused this request. Nothing was changed.",
    exit: { label: "OK", action: "none" },
  },
  declined: { tone: "stale", ink: "DECLINED", copy: "You declined. Nothing was changed.", exit: ASK_AGAIN },
  expired: {
    tone: "stale",
    ink: "EXPIRED",
    copy: "This request expired. Nothing was changed.",
    exit: { label: "Ask again", action: "retry" },
  },
  consumed: {
    tone: "stale",
    ink: "ALREADY USED",
    copy: "This request was already used. Nothing else was changed.",
    exit: { label: "Refresh", action: "refresh" },
  },
  consent_superseded: {
    tone: "stale",
    ink: "REPLACED",
    copy: "A newer request replaced this one.",
    exit: { label: "Show latest", action: "refresh" },
  },
  cancelled: { tone: "stale", ink: "CANCELLED", copy: "Cancelled. Nothing was changed.", exit: ASK_AGAIN },
  access_revoked: {
    tone: "critical",
    copy: "Your access to the assistant was removed.",
    exit: { label: "Pair again", action: "pair" },
  },
  assistant_disabled: {
    tone: "stale",
    copy: "The assistant is turned off for this account.",
    exit: { label: "OK", action: "none" },
  },
  partial_then_error: {
    tone: "critical",
    copy: "The answer stopped partway. What arrived is kept above.",
    exit: TRY_AGAIN,
  },
  rate_limited: {
    tone: "attention",
    copy: (ctx) =>
      ctx.retryAt ? `You can ask again at ${formatClockCt(ctx.retryAt)}.` : "You've reached the question limit for now.",
    exit: { label: "Wait", action: "wait" },
  },
  busy: {
    tone: "attention",
    copy: (ctx) => `The assistant is answering a question from your ${ctx.busyDevice ?? "other device"}.`,
    exit: { label: "Wait", action: "wait" },
  },
  stopped: { tone: "stale", copy: "Stopped. What arrived is kept.", exit: ASK_AGAIN },
  worker_offline: {
    tone: "critical",
    copy: "The assistant's computer went offline mid-answer.",
    exit: TRY_AGAIN,
  },
  lost_connection: {
    tone: "stale",
    copy: "Lost connection. Your conversation is saved.",
    exit: { label: "Reconnect", action: "retry" },
  },
  turn_expired: { tone: "critical", copy: "This answer took too long and was stopped.", exit: TRY_AGAIN },
  update_required: {
    tone: "attention",
    copy: "This version can't read the conversation. Update to continue.",
    exit: { label: "Update", action: "open_settings" },
  },
};

export function stateSpec(id: AssistantStateId, ctx: StateContext = {}): StateSpec {
  const entry = TABLE[id];
  return {
    id,
    tone: entry.tone,
    ink: entry.ink ?? null,
    copy: typeof entry.copy === "function" ? entry.copy(ctx) : entry.copy,
    exit: typeof entry.exit === "function" ? entry.exit(ctx) : entry.exit,
  };
}

const REASON_STATE: Partial<Record<ReasonCode, AssistantStateId>> = {
  rate_limited: "rate_limited",
  busy: "busy",
  worker_offline: "worker_offline",
  tool_failed: "tool_failed",
  turn_expired: "turn_expired",
  card_expired: "expired",
  card_superseded: "consent_superseded",
  card_consumed: "consumed",
  card_digest_mismatch: "consent_superseded",
  self_approval_refused: "refused",
  access_revoked: "access_revoked",
  unpaired: "unpaired",
  unconfigured: "unconfigured",
  assistant_disabled: "assistant_disabled",
  update_required: "update_required",
  refused: "refused",
  "client.offline": "offline",
  "client.timeout": "not_sent",
};

/** The VI.3 state a reason code lands in, or null for codes shown only as a banner (internal, not_found). */
export function stateForReason(code: ReasonCode): AssistantStateId | null {
  return REASON_STATE[code] ?? null;
}

// ---------------------------------------------------------------------------
// Folding the event stream. One linear pass; the latest version of each item
// wins and keeps its seq, and the last turn event names the live turn.

export type TurnState = (typeof TURN_STATES)[number];

export interface LiveTurn {
  readonly turn_id: string;
  readonly state: TurnState;
  readonly reason?: ReasonV1;
  /** When the user's message for this turn was recorded. */
  readonly sentAt?: string;
  readonly queuedAt?: string;
  readonly runningAt?: string;
  readonly endedAt?: string;
  /** True when assistant text for this turn arrived before it ended. */
  readonly partial: boolean;
}

interface TurnDraft {
  turn_id: string;
  state: TurnState;
  reason?: ReasonV1;
  queuedAt?: string;
  runningAt?: string;
  endedAt?: string;
}

export function foldEvents(events: readonly ThreadEventV1[]): { items: ItemV1[]; live: LiveTurn | null } {
  const latest = new Map<string, ItemV1>();
  const turns = new Map<string, TurnDraft>();
  let current: TurnDraft | null = null;
  for (const event of events) {
    if (event.type === "item") {
      latest.set(event.item.item_id, event.item);
      continue;
    }
    let turn = turns.get(event.turn_id);
    if (turn === undefined) {
      turn = { turn_id: event.turn_id, state: event.state };
      turns.set(event.turn_id, turn);
    }
    turn.state = event.state;
    if (event.state === "queued") {
      turn.queuedAt = turn.queuedAt ?? event.at;
    } else if (event.state === "running") {
      turn.runningAt = turn.runningAt ?? event.at;
    } else {
      turn.endedAt = event.at;
      turn.reason = event.reason;
    }
    current = turn;
  }
  const items = [...latest.values()].sort((a, b) => a.seq - b.seq);
  if (current === null) return { items, live: null };
  const live: TurnDraft = current;
  let sentAt: string | undefined;
  let partial = false;
  for (const item of items) {
    if (item.turn_id !== live.turn_id) continue;
    if (item.role === "user" && sentAt === undefined) sentAt = item.at;
    if (item.role === "assistant" && item.blocks.some((block) => block.type === "text")) partial = true;
  }
  return { items, live: { ...live, sentAt, partial } };
}

/** The VI.3 state for the live turn. */
export function liveStateFor(live: LiveTurn): AssistantStateId {
  switch (live.state) {
    case "queued":
      return "queued";
    case "running":
      return "thinking";
    case "done":
      return "done";
    case "stopped":
      return "stopped";
    case "expired":
      return "turn_expired";
    case "failed": {
      const mapped = live.reason ? stateForReason(live.reason.code) : null;
      if (mapped === "worker_offline" || mapped === "rate_limited" || mapped === "busy") return mapped;
      if (live.partial) return "partial_then_error";
      return mapped ?? "partial_then_error";
    }
  }
}

export function liveContext(
  live: LiveTurn,
  now: number,
  extra: { queuePosition?: number | null; busyDevice?: "phone" | "web" | null } = {},
): StateContext {
  const since = live.state === "queued" ? live.queuedAt : live.state === "running" ? live.runningAt : undefined;
  const sinceMs = since ? Date.parse(since) : Number.NaN;
  const sent = live.sentAt ?? live.queuedAt;
  const answeredMs =
    live.state === "done" && sent && live.endedAt ? Date.parse(live.endedAt) - Date.parse(sent) : undefined;
  return {
    elapsedMs: Number.isFinite(sinceMs) ? Math.max(0, now - sinceMs) : 0,
    queuePosition: extra.queuePosition ?? null,
    busyDevice: extra.busyDevice ?? null,
    retryAt: live.reason?.retry_at,
    answeredMs: answeredMs !== undefined && Number.isFinite(answeredMs) ? answeredMs : undefined,
  };
}

/** A 1 s clock that runs only while `active`; a fixed `now` (tests, replays) turns it off. */
export function useClock(fixedNow: number | undefined, active: boolean): number {
  const [tick, setTick] = useState(() => Date.now());
  useEffect(() => {
    if (fixedNow !== undefined || !active) return undefined;
    setTick(Date.now());
    const id = setInterval(() => setTick(Date.now()), 1000);
    return () => clearInterval(id);
  }, [fixedNow, active]);
  return fixedNow ?? tick;
}
