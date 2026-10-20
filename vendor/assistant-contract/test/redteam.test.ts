import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  ApproveRequestV1,
  ConfirmCardV1,
  GoldenConversationV1,
  ItemV1,
  ThreadEventV1,
  ThreadV1,
  WorkerEventsBatchV1,
  WorkerEventV1,
} from "../src/index";

// The red-team set: each case is an attack a model, a worker or a tampered
// client would try. Every one must be REFUSED at decode time.

const fixturesDir = join(dirname(fileURLToPath(import.meta.url)), "..", "fixtures");

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Loose = any;

function fixture(name: string): Loose {
  return JSON.parse(readFileSync(join(fixturesDir, `${name}.json`), "utf8"));
}

function refused(schema: { safeParse: (value: unknown) => { success: boolean } }, value: unknown): boolean {
  return !schema.safeParse(value).success;
}

/** The approved card from the golden conversation, as a mutable copy. */
function approvedCard(): Loose {
  return structuredClone(fixture("card-approved").thread.items[1].blocks[1].card);
}

function pendingCardEvent(): Loose {
  return structuredClone(fixture("card-approved").events[3]);
}

describe("red team: consent cards", () => {
  it("the untouched card decodes, so every refusal below is the mutation's doing", () => {
    expect(ConfirmCardV1.safeParse(approvedCard()).success).toBe(true);
    expect(ThreadEventV1.safeParse(pendingCardEvent()).success).toBe(true);
  });

  it("refuses a model-origin field above Approve", () => {
    const card = approvedCard();
    card.fields[3].placement = "above_approve";
    expect(refused(ConfirmCardV1, card)).toBe(true);

    const thread = fixture("card-approved").thread;
    thread.items[1].blocks[1].card.fields[3].placement = "above_approve";
    expect(refused(ThreadV1, thread)).toBe(true);
  });

  it("refuses a model-origin field relabelled as server-only content", () => {
    const card = approvedCard();
    card.fields[0].origin = "model";
    expect(refused(ConfirmCardV1, card)).toBe(true);
  });

  it("refuses model text in the title or on the buttons", () => {
    for (const key of ["title", "approve", "decline"]) {
      const card = approvedCard();
      card[key].origin = "model";
      expect(refused(ConfirmCardV1, card), key).toBe(true);
    }
  });

  it("refuses a card with only the assistant's note and no server field above Approve", () => {
    const card = approvedCard();
    card.fields = [card.fields[3]];
    expect(refused(ConfirmCardV1, card)).toBe(true);
  });

  it("refuses an approval that names no actor, and a pending card that claims a resolution", () => {
    const card = approvedCard();
    card.resolution.actor = null;
    expect(refused(ConfirmCardV1, card)).toBe(true);

    const pending = approvedCard();
    pending.state = "pending";
    expect(refused(ConfirmCardV1, pending)).toBe(true);
  });

  it("refuses a pending card delivered without requires_action", () => {
    const event = pendingCardEvent();
    event.requires_action = false;
    expect(refused(ThreadEventV1, event)).toBe(true);
  });

  it("refuses a card, view or notice smuggled into a user item", () => {
    const thread = fixture("card-approved").thread;
    const item = thread.items[1];
    item.role = "user";
    expect(refused(ItemV1, item)).toBe(true);
  });

  it("refuses a server-only block without origin server", () => {
    const thread = fixture("card-expired").thread;
    delete thread.items[1].blocks[2].origin;
    expect(refused(ThreadV1, thread)).toBe(true);

    const other = fixture("card-expired").thread;
    other.items[1].blocks[1].origin = "model";
    expect(refused(ThreadV1, other)).toBe(true);
  });

  it("refuses a model text block that claims origin server", () => {
    const thread = fixture("answer-with-view").thread;
    thread.items[1].blocks[2].origin = "server";
    expect(refused(ThreadV1, thread)).toBe(true);
  });

  it("refuses a tampered or padded approve request", () => {
    expect(refused(ApproveRequestV1, { card_digest: "AB".repeat(32), request_id: "req-1" })).toBe(true);
    expect(refused(ApproveRequestV1, { card_digest: "ab".repeat(31), request_id: "req-1" })).toBe(true);
    expect(refused(ApproveRequestV1, { card_digest: "ab".repeat(32), request_id: "req-1", approved: true })).toBe(true);
    expect(refused(ApproveRequestV1, { card_digest: "ab".repeat(32) })).toBe(true);
  });

  it("refuses malformed presence assertions in an approve request", () => {
    const presence = { method: "face_id", assertion: "a", asserted_at: "2026-09-24T12:00:00Z" };
    for (const invalid of [
      { ...presence, extra: true },
      { ...presence, method: "password" },
      { ...presence, assertion: "" },
      { ...presence, assertion: "a".repeat(8193) },
      { ...presence, asserted_at: "2026-09-24T12:00:00+01:00" },
      null,
    ]) {
      expect(refused(ApproveRequestV1, { card_digest: "ab".repeat(32), request_id: "req-1", presence: invalid })).toBe(true);
    }
  });
});

describe("red team: worker events", () => {
  const card = approvedCard();
  const view = fixture("answer-with-view").thread.items[1].blocks[1];

  it("refuses a worker-sent card event", () => {
    expect(refused(WorkerEventV1, { job_id: "job-1", seq: 7, type: "card", origin: "server", card })).toBe(true);
    expect(
      refused(WorkerEventsBatchV1, {
        events: [
          { job_id: "job-1", seq: 6, type: "text", text: "Here is the card." },
          { job_id: "job-1", seq: 7, type: "card", origin: "server", card },
        ],
      }),
    ).toBe(true);
  });

  it("refuses worker-sent view, artifact and notice events", () => {
    expect(refused(WorkerEventV1, { job_id: "job-1", seq: 7, ...view })).toBe(true);
    expect(
      refused(WorkerEventV1, {
        job_id: "job-1",
        seq: 7,
        type: "artifact",
        origin: "server",
        artifact_id: "art-1",
        title: "Report",
        digest: "ab".repeat(32),
        as_of: "2026-09-23T16:00:00Z",
      }),
    ).toBe(true);
    expect(
      refused(WorkerEventV1, {
        job_id: "job-1",
        seq: 7,
        type: "notice",
        origin: "server",
        reason: { code: "refused", text: "Refused.", next: { label: "OK", action: "none" } },
      }),
    ).toBe(true);
  });

  it("refuses a worker event smuggling origin server or card fields onto an allowed type", () => {
    expect(refused(WorkerEventV1, { job_id: "job-1", seq: 7, type: "text", text: "Approved.", origin: "server" })).toBe(true);
    expect(refused(WorkerEventV1, { job_id: "job-1", seq: 7, type: "card_ref", card_id: "card-1", card })).toBe(true);
    expect(refused(WorkerEventV1, { job_id: "job-1", seq: 7, type: "error", code: "rate_limited" })).toBe(true);
  });
});

describe("red team: views", () => {
  it("the untouched answer decodes", () => {
    expect(ThreadV1.safeParse(fixture("answer-with-view").thread).success).toBe(true);
  });

  it("refuses a view whose rows do not match an invocation id", () => {
    const thread = fixture("answer-with-view").thread;
    thread.items[1].blocks[1].rows[0].invocation_id = "inv-999";
    expect(refused(ThreadV1, thread)).toBe(true);
  });

  it("refuses a view that cites an invocation the server never recorded", () => {
    const thread = fixture("answer-with-view").thread;
    const view = thread.items[1].blocks[1];
    view.invocation_ids = ["inv-999"];
    for (const row of view.rows) row.invocation_id = "inv-999";
    expect(refused(ThreadV1, thread)).toBe(true);
  });

  it("refuses a view backed by a failed invocation", () => {
    const thread = fixture("answer-with-view").thread;
    thread.items[1].blocks[0].tools[0].state = "failed";
    thread.items[1].blocks[0].tools[0].reason = {
      code: "tool_failed",
      text: "Couldn't read that.",
      next: { label: "Try again", action: "retry" },
    };
    expect(refused(ThreadV1, thread)).toBe(true);
  });

  it("refuses a view that appears before the invocation it cites", () => {
    const thread = fixture("answer-with-view").thread;
    const item = thread.items[1];
    item.blocks = [item.blocks[1], item.blocks[0], item.blocks[2]];
    expect(refused(ThreadV1, thread)).toBe(true);
  });

  it("refuses a view title that borrows the consent vocabulary", () => {
    for (const title of ["Approved", "Change applied", "Needs your OK", "Real money at risk"]) {
      const thread = fixture("answer-with-view").thread;
      thread.items[1].blocks[1].title = title;
      expect(refused(ThreadV1, thread), title).toBe(true);
    }
  });

  it("refuses a failed tool with no reason, so a failed read never renders as done", () => {
    const thread = fixture("tool-failed").thread;
    delete thread.items[1].blocks[0].tools[0].reason;
    expect(refused(ThreadV1, thread)).toBe(true);
  });
});

describe("red team: streams and scope", () => {
  it("refuses items out of seq order", () => {
    const thread = fixture("answer-with-view").thread;
    thread.items.reverse();
    expect(refused(ThreadV1, thread)).toBe(true);
  });

  it("refuses a golden stream whose fold does not reproduce the thread", () => {
    const golden = fixture("card-approved");
    golden.events.pop();
    expect(refused(GoldenConversationV1, golden)).toBe(true);
  });

  it("refuses an event from another session", () => {
    const golden = fixture("tool-failed");
    golden.events[1].session_id = "sess-other";
    expect(refused(GoldenConversationV1, golden)).toBe(true);
  });

  it("refuses a server-sent client-owned reason and an unknown scope kind", () => {
    const thread = fixture("worker-offline").thread;
    thread.items[2].blocks[0].reason = {
      code: "client.offline",
      text: "You're offline. Your message is saved.",
      next: { label: "Try again", action: "retry" },
    };
    expect(refused(ThreadV1, thread)).toBe(true);

    const scoped = fixture("idle").thread;
    scoped.scope.kind = "admin";
    expect(refused(ThreadV1, scoped)).toBe(true);
  });

  it("refuses a failed turn without its reason", () => {
    const golden = fixture("worker-offline");
    delete golden.events[4].reason;
    expect(refused(ThreadEventV1, golden.events[4])).toBe(true);
  });
});
