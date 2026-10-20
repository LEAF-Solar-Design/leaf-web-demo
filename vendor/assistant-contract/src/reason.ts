import { z } from "zod";

// Shared scalars. Every string is bounded and every pattern is anchored, so a
// malformed value fails closed at decode time instead of reaching a renderer.
// Patterns use [0-9] rather than \d so the JSON Schema dialect in Swift and TS
// reads them the same way.
export const ISO_UTC_PATTERN = /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{3})?Z$/;
export const ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
export const SHA256_PATTERN = /^[a-f0-9]{64}$/;

export const IsoUtc = z.string().regex(ISO_UTC_PATTERN);
export const Id = z.string().regex(ID_PATTERN);
export const Sha256Hex = z.string().regex(SHA256_PATTERN);
export const Seq = z.number().int().min(0).max(2147483647);
export const Label = z.string().min(1).max(60);

export const REASON_ACTIONS = ["retry", "wait", "refresh", "open_settings", "pair", "none"] as const;
export const ReasonAction = z.enum(REASON_ACTIONS);
export type ReasonAction = z.infer<typeof ReasonAction>;

type ReasonEntry = {
  /** The one exit every reason offers; `next.action` must equal it. */
  readonly action: ReasonAction;
  /** Whether `retry_at` must, may or must not be present. */
  readonly retry_at: "required" | "optional" | "forbidden";
  /** Who authors `text`. The client owns text only for client.offline and client.timeout. */
  readonly owner: "server" | "client";
  /** Fixed copy for client-owned codes; server codes carry server-rendered text. */
  readonly client_text?: string;
  readonly client_label?: string;
};

// The reason table (VI.3). Adding a code is a contract change: the emitted
// OpenAPI and JSON Schema move with it and the diff test says so.
export const REASON_TABLE = {
  rate_limited: { action: "wait", retry_at: "required", owner: "server" },
  busy: { action: "wait", retry_at: "optional", owner: "server" },
  worker_offline: { action: "retry", retry_at: "forbidden", owner: "server" },
  tool_failed: { action: "retry", retry_at: "forbidden", owner: "server" },
  turn_expired: { action: "retry", retry_at: "forbidden", owner: "server" },
  card_expired: { action: "retry", retry_at: "forbidden", owner: "server" },
  card_superseded: { action: "refresh", retry_at: "forbidden", owner: "server" },
  card_consumed: { action: "refresh", retry_at: "forbidden", owner: "server" },
  card_digest_mismatch: { action: "refresh", retry_at: "forbidden", owner: "server" },
  self_approval_refused: { action: "none", retry_at: "forbidden", owner: "server" },
  access_revoked: { action: "pair", retry_at: "forbidden", owner: "server" },
  unpaired: { action: "pair", retry_at: "forbidden", owner: "server" },
  unconfigured: { action: "open_settings", retry_at: "forbidden", owner: "server" },
  assistant_disabled: { action: "none", retry_at: "forbidden", owner: "server" },
  update_required: { action: "open_settings", retry_at: "forbidden", owner: "server" },
  not_found: { action: "refresh", retry_at: "forbidden", owner: "server" },
  refused: { action: "none", retry_at: "forbidden", owner: "server" },
  internal: { action: "retry", retry_at: "forbidden", owner: "server" },
  "client.offline": {
    action: "retry",
    retry_at: "forbidden",
    owner: "client",
    client_text: "You're offline. Your message is saved.",
    client_label: "Try again",
  },
  "client.timeout": {
    action: "retry",
    retry_at: "forbidden",
    owner: "client",
    client_text: "The assistant didn't answer in time.",
    client_label: "Try again",
  },
} as const satisfies Record<string, ReasonEntry>;

export type ReasonCode = keyof typeof REASON_TABLE;
export const REASON_CODES = Object.keys(REASON_TABLE) as [ReasonCode, ...ReasonCode[]];
export const ReasonCode = z.enum(REASON_CODES);

const ReasonShape = z.strictObject({
  code: ReasonCode,
  text: z.string().min(1).max(280),
  next: z.strictObject({
    label: z.string().min(1).max(24),
    action: ReasonAction,
  }),
  retry_at: IsoUtc.optional(),
});

/** Any reason, including the two the client authors itself. Fails closed on a table mismatch. */
export const ReasonV1 = ReasonShape.superRefine((reason, ctx) => {
  const entry: ReasonEntry = REASON_TABLE[reason.code];
  if (reason.next.action !== entry.action) {
    ctx.addIssue({
      code: "custom",
      path: ["next", "action"],
      message: `reason ${reason.code} must offer action ${entry.action}`,
    });
  }
  if (entry.retry_at === "required" && reason.retry_at === undefined) {
    ctx.addIssue({ code: "custom", path: ["retry_at"], message: `reason ${reason.code} requires retry_at` });
  }
  if (entry.retry_at === "forbidden" && reason.retry_at !== undefined) {
    ctx.addIssue({ code: "custom", path: ["retry_at"], message: `reason ${reason.code} carries no retry_at` });
  }
});
export type ReasonV1 = z.infer<typeof ReasonV1>;

/** A reason the SERVER sent: client-owned codes are refused, the server never speaks for the client. */
export const ServerReasonV1 = ReasonV1.superRefine((reason, ctx) => {
  if (REASON_TABLE[reason.code].owner !== "server") {
    ctx.addIssue({
      code: "custom",
      path: ["code"],
      message: `${reason.code} is client-owned; the server may not send it`,
    });
  }
});

/** The client's own copy for the two client-owned codes. Throws for any server code. */
export function clientReason(code: "client.offline" | "client.timeout"): ReasonV1 {
  const entry: ReasonEntry = REASON_TABLE[code];
  if (entry.owner !== "client" || !entry.client_text || !entry.client_label) {
    throw new Error(`${code} is not client-owned`);
  }
  return ReasonV1.parse({ code, text: entry.client_text, next: { label: entry.client_label, action: entry.action } });
}

/** The error body of every refused request (400, 404, 409, 410, 429). */
export const ErrorV1 = z.strictObject({
  reason: ServerReasonV1,
});
export type ErrorV1 = z.infer<typeof ErrorV1>;
