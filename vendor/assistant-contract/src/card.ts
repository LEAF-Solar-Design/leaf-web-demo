import { z } from "zod";
import { ProposalKind } from "./proposal";
import { Id, IsoUtc, Label, Sha256Hex } from "./reason";

// ConfirmCardV1 is rendered by the SERVER from a stored proposal (K-D5). Every
// displayed string carries its origin. Only server-origin fields may render
// above Approve; model prose may appear only in the collapsed, labelled
// "assistant's note" below it. The decoder refuses anything else, so a client
// that decodes a card can render it without re-checking provenance.

export const Origin = z.enum(["server", "model"]);
export type Origin = z.infer<typeof Origin>;

/** A server-authored string: titles, button labels and receipt lines are never model text. */
export const ServerText = z.strictObject({
  text: z.string().min(1).max(120),
  origin: z.literal("server"),
});

export const CardField = z.strictObject({
  key: Id,
  label: Label,
  value: z.string().min(1).max(400),
  origin: Origin,
  placement: z.enum(["above_approve", "assistants_note"]),
});
export type CardField = z.infer<typeof CardField>;

export const CARD_STATES = ["pending", "approved", "declined", "expired", "superseded"] as const;
export const CardState = z.enum(CARD_STATES);

export const Actor = z.strictObject({
  kind: z.enum(["owner", "tenant_device", "admin_session"]),
  label: Label,
  principal_id: Id,
});

export const ConfirmCardV1 = z
  .strictObject({
    card_id: Id,
    proposal_id: Id,
    proposal_digest: Sha256Hex,
    kind: ProposalKind,
    state: CardState,
    title: ServerText,
    fields: z.array(CardField).min(1).max(24),
    approve: ServerText,
    decline: ServerText,
    spend: z.strictObject({
      state: z.enum(["none", "unknown", "reported"]),
      cents: z.number().int().min(-999999999999).max(999999999999).nullable(),
    }),
    requires_face_id: z.boolean(),
    created_at: IsoUtc,
    expires_at: IsoUtc,
    card_digest: Sha256Hex,
    resolution: z
      .strictObject({
        at: IsoUtc,
        actor: Actor.nullable(),
        receipt: ServerText,
      })
      .nullable(),
  })
  .superRefine((card, ctx) => {
    const keys = new Set<string>();
    let aboveApprove = 0;
    card.fields.forEach((field, index) => {
      if (keys.has(field.key)) {
        ctx.addIssue({ code: "custom", path: ["fields", index, "key"], message: `duplicate card field ${field.key}` });
      }
      keys.add(field.key);
      if (field.placement === "above_approve") {
        aboveApprove += 1;
        if (field.origin !== "server") {
          ctx.addIssue({
            code: "custom",
            path: ["fields", index, "origin"],
            message: "only server-origin fields may render above Approve",
          });
        }
      }
    });
    if (aboveApprove === 0) {
      ctx.addIssue({ code: "custom", path: ["fields"], message: "a card shows at least one server field above Approve" });
    }
    if (!(Date.parse(card.expires_at) > Date.parse(card.created_at))) {
      ctx.addIssue({ code: "custom", path: ["expires_at"], message: "a card must expire after it is created" });
    }
    if ((card.spend.state === "reported") !== (card.spend.cents !== null)) {
      ctx.addIssue({ code: "custom", path: ["spend", "cents"], message: "cents is present exactly when spend is reported" });
    }
    if (card.state === "pending") {
      if (card.resolution !== null) {
        ctx.addIssue({ code: "custom", path: ["resolution"], message: "a pending card has no resolution" });
      }
      return;
    }
    if (card.resolution === null) {
      ctx.addIssue({ code: "custom", path: ["resolution"], message: `a ${card.state} card carries its resolution` });
      return;
    }
    // Approvals and declines persist the ACTOR; expiry and supersession have none.
    const needsActor = card.state === "approved" || card.state === "declined";
    if (needsActor !== (card.resolution.actor !== null)) {
      ctx.addIssue({
        code: "custom",
        path: ["resolution", "actor"],
        message: needsActor ? `a ${card.state} card names its actor` : `a ${card.state} card has no actor`,
      });
    }
  });
export type ConfirmCardV1 = z.infer<typeof ConfirmCardV1>;

export const PRESENCE_METHODS = ["face_id", "touch_id", "passkey"] as const;
export const MAX_PRESENCE_ASSERTION_CHARS = 8_192;
export const PresenceAssertionV1 = z.strictObject({
  method: z.enum(PRESENCE_METHODS),
  assertion: z.string().min(1).max(MAX_PRESENCE_ASSERTION_CHARS),
  asserted_at: IsoUtc,
});
export type PresenceAssertionV1 = z.infer<typeof PresenceAssertionV1>;

/** POST /cards/{card_id}/approve. The server rechecks: digest mismatch 409, expired 410, reuse 409. */
export const ApproveRequestV1 = z.strictObject({
  card_digest: Sha256Hex,
  request_id: Id,
  presence: PresenceAssertionV1.optional(),
});
export type ApproveRequestV1 = z.infer<typeof ApproveRequestV1>;

export const APPROVE_STATUS = { digest_mismatch: 409, expired: 410, reused: 409 } as const;
