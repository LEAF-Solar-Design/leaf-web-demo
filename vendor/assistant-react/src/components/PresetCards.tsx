import type { ProposalKind } from "../../../assistant-contract/src/index";
import { AssistantConfirmCard } from "./AssistantConfirmCard";
import type { AssistantConfirmCardProps } from "./AssistantConfirmCard";

// ArtifactCard, ProposalCard and ConfigDraftCard are presets over the one
// consent component: they set the eyebrow and nothing else, so every kind
// shares the same provenance rule above Approve.

export type PresetCardProps = Omit<AssistantConfirmCardProps, "eyebrow">;

export const PRESET_EYEBROW: Readonly<Record<ProposalKind, string>> = {
  control: "CONTROL REQUEST",
  build: "BUILD REQUEST",
  config_draft: "CONFIG CHANGE",
  slot_promotion: "PROMOTION",
  "walk.resume": "WALK RESUME",
  "walk.side_quest": "SIDE QUEST",
};

/** Control proposals and build requests. */
export function ProposalCard(props: PresetCardProps) {
  const kind = props.card.kind === "build" ? "build" : "control";
  return <AssistantConfirmCard {...props} eyebrow={PRESET_EYEBROW[kind]} />;
}

/** Config drafts checked against the server snapshot. */
export function ConfigDraftCard(props: PresetCardProps) {
  return <AssistantConfirmCard {...props} eyebrow={PRESET_EYEBROW.config_draft} />;
}

/** Artifact slot promotions (global promotion). */
export function ArtifactCard(props: PresetCardProps) {
  return <AssistantConfirmCard {...props} eyebrow={PRESET_EYEBROW.slot_promotion} />;
}

/** Picks the preset for the card's kind. */
export function ConsentCard(props: PresetCardProps) {
  switch (props.card.kind) {
    case "config_draft":
      return <ConfigDraftCard {...props} />;
    case "slot_promotion":
      return <ArtifactCard {...props} />;
    case "walk.resume":
    case "walk.side_quest":
      return <AssistantConfirmCard {...props} eyebrow={PRESET_EYEBROW[props.card.kind]} />;
    default:
      return <ProposalCard {...props} />;
  }
}
