import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { ProposalKind, ProposalV1, PROPOSAL_KINDS, WALK_KINDS, WALK_FIELD, WALK_TEXT_MAX_CODE_POINTS } from "../src/proposal";

const fixture = JSON.parse(readFileSync(new URL("../fixtures/proposals/walk.json", import.meta.url), "utf8"));
const resume = fixture.proposals.find((row: { proposal: { kind: string } }) => row.proposal.kind === "walk.resume").proposal;
const sideQuest = fixture.proposals.find((row: { proposal: { kind: string } }) => row.proposal.kind === "walk.side_quest").proposal;
const withWalk = (proposal: typeof resume, changes: Record<string, unknown>) => ({
  ...proposal, walk: { ...proposal.walk, ...changes },
});

describe("concierge walk proposals", () => {
  it("publishes the six kinds and mirror constants", () => {
    expect(PROPOSAL_KINDS).toEqual(["control", "build", "config_draft", "slot_promotion", "walk.resume", "walk.side_quest"]);
    expect(WALK_KINDS).toEqual(["walk.resume", "walk.side_quest"]);
    expect(WALK_FIELD).toEqual({ "walk.resume": "instruction", "walk.side_quest": "side_quest_brief" });
    expect(WALK_TEXT_MAX_CODE_POINTS).toBe(3000);
    for (const kind of WALK_KINDS) expect(ProposalKind.safeParse(kind).success).toBe(true);
  });

  it("round trips both golden proposals without changing the wire shape", () => {
    expect(fixture.proposals.map((row: { proposal: { kind: string } }) => row.proposal.kind)).toEqual(WALK_KINDS);
    for (const { proposal } of fixture.proposals) {
      expect(JSON.parse(JSON.stringify(ProposalV1.parse(proposal)))).toEqual(proposal);
    }
  });

  const { child_walk_id: _child, ...withoutChild } = sideQuest.walk;
  const { walk: resumeWalk, ...withoutWalk } = resume;
  it.each([
    ["resume with side quest field", withWalk(resume, { field: "side_quest_brief" })],
    ["resume with child", withWalk(resume, { child_walk_id: "child" })],
    ["side quest without child", { ...sideQuest, walk: withoutChild }],
    ["side quest with empty child", withWalk(sideQuest, { child_walk_id: "" })],
    ["empty walk id", withWalk(resume, { walk_id: "" })],
    ["long walk id", withWalk(resume, { walk_id: "a".repeat(257) })],
    ["empty text", withWalk(resume, { text: "" })],
    ["blank text", withWalk(resume, { text: " \n\t" })],
    ["too many code points", withWalk(resume, { text: "a".repeat(3001) })],
    ["unknown walk key", withWalk(resume, { extra: true })],
    ["kind used as payload key", { ...withoutWalk, "walk.resume": resumeWalk }],
    ["equal expiry", { ...resume, expires_at: resume.created_at }],
  ])("refuses %s", (_label, proposal) => {
    expect(ProposalV1.safeParse(proposal).success).toBe(false);
  });

  it.each([
    ["empty device and item", { device: "", item_id: "" }],
    ["3000 supplementary code points", { text: "\u{1F680}".repeat(3000) }],
    ["3000 ASCII characters", { text: "a".repeat(3000) }],
  ])("accepts %s", (_label, changes) => {
    expect(ProposalV1.safeParse(withWalk(resume, changes as Record<string, unknown>)).success).toBe(true);
  });
});
