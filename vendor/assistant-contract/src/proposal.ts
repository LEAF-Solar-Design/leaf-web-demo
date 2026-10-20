import { z } from "zod";
import { Id, IsoUtc, Sha256Hex } from "./reason";

// ProposalV1 is what the assistant may ask for. It never applies anything: the
// server stores it immutably, renders a ConfirmCardV1 from the stored row and
// applies it only on a digest-bound approve (K-D5).
export const PROPOSAL_KINDS = ["control", "build", "config_draft", "slot_promotion", "walk.resume", "walk.side_quest"] as const;
// Walk kinds are minted only by the claudewalk concierge (plan VI.4 row 6,
// AD6) and mirror walk_proposals.py field for field.
export const WALK_KINDS = ["walk.resume", "walk.side_quest"] as const;
export const WALK_FIELD = { "walk.resume": "instruction", "walk.side_quest": "side_quest_brief" } as const;
export const WALK_TEXT_MAX_CODE_POINTS = 3000;
export const ProposalKind = z.enum(PROPOSAL_KINDS);
export type ProposalKind = z.infer<typeof ProposalKind>;

export const CONTROL_ACTIONS = ["engine.halt", "engine.enable", "engine.disable", "engine.probe", "risk.breaker"] as const;

const common = {
  proposal_id: Id,
  session_id: Id,
  turn_id: Id,
  created_at: IsoUtc,
  expires_at: IsoUtc,
  proposal_digest: Sha256Hex,
};

const ControlProposal = z.strictObject({
  ...common,
  kind: z.literal("control"),
  control: z.strictObject({
    action: z.enum(CONTROL_ACTIONS),
    target_id: Id,
  }),
});

const BuildProposal = z.strictObject({
  ...common,
  kind: z.literal("build"),
  build: z.strictObject({
    recipe_id: Id,
    max_rounds: z.number().int().min(1).max(100),
  }),
});

const ConfigDraftProposal = z.strictObject({
  ...common,
  kind: z.literal("config_draft"),
  config_draft: z.strictObject({
    config_key: Id,
    base_snapshot_digest: Sha256Hex,
    draft_digest: Sha256Hex,
  }),
});

const SlotPromotionProposal = z.strictObject({
  ...common,
  kind: z.literal("slot_promotion"),
  slot_promotion: z.strictObject({
    slot_id: Id,
    artifact_id: Id,
    artifact_digest: Sha256Hex,
  }),
});

const WalkText = z.string().min(1).max(12000)
  .refine((text) => /\S/u.test(text), "walk text must not be blank")
  .refine((text) => [...text].length <= WALK_TEXT_MAX_CODE_POINTS, "walk text exceeds 3000 code points");

const WalkResumeProposal = z.strictObject({
  ...common,
  kind: z.literal("walk.resume"),
  walk: z.strictObject({
    walk_id: z.string().min(1).max(256),
    device: z.string().max(256),
    item_id: z.string().max(256),
    field: z.literal("instruction"),
    text: WalkText,
  }),
});

const WalkSideQuestProposal = z.strictObject({
  ...common,
  kind: z.literal("walk.side_quest"),
  walk: z.strictObject({
    walk_id: z.string().min(1).max(256),
    device: z.string().max(256),
    item_id: z.string().max(256),
    field: z.literal("side_quest_brief"),
    text: WalkText,
    child_walk_id: z.string().min(1).max(256),
  }),
});

export const ProposalV1 = z
  .union([ControlProposal, BuildProposal, ConfigDraftProposal, SlotPromotionProposal, WalkResumeProposal, WalkSideQuestProposal])
  .superRefine((proposal, ctx) => {
    if (!(Date.parse(proposal.expires_at) > Date.parse(proposal.created_at))) {
      ctx.addIssue({ code: "custom", path: ["expires_at"], message: "a proposal must expire after it is created" });
    }
  });
export type ProposalV1 = z.infer<typeof ProposalV1>;
