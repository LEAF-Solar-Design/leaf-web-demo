import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { GoldenConversationV1 } from "../../assistant-contract/src/index";
import {
  ASSISTANT_STATE_IDS,
  AssistantThread,
  ConsumedCard,
  ExpiredCard,
  StateCard,
  UpdateRequiredCard,
  WorkerOfflineCard,
  decodeGolden,
  decodeThread,
  foldEvents,
  formatClockCt,
  formatCountdown,
  formatMoneyCents,
  formatWhen,
  liveStateFor,
  plainModelText,
  safeStarters,
  stateForReason,
  stateSpec,
} from "../src/index";
import type { LiveTurn } from "../src/index";

const here = dirname(fileURLToPath(import.meta.url));
const FIXTURES = join(here, "..", "..", "assistant-contract", "fixtures");

function golden(name: string): GoldenConversationV1 {
  return GoldenConversationV1.parse(JSON.parse(readFileSync(join(FIXTURES, `${name}.json`), "utf8")));
}

// VI.3, state by state. Each must exist, carry copy, and offer exactly one exit.
const VI3_STATES = [
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

describe("VI.3 states", () => {
  it.each(VI3_STATES)("%s has copy and exactly one 44 px exit", (id) => {
    expect(ASSISTANT_STATE_IDS).toContain(id);
    const spec = stateSpec(id);
    expect(spec.copy.length).toBeGreaterThan(0);
    expect(spec.exit.label.length).toBeGreaterThan(0);
    render(<StateCard state={id} />);
    const card = screen.getByTestId("assistant-state");
    expect(within(card).getByTestId("assistant-state-copy")).toHaveTextContent(spec.copy);
    const exits = within(card).getAllByRole("button");
    expect(exits).toHaveLength(1);
    expect(exits[0]).toHaveTextContent(spec.exit.label);
    expect(exits[0].style.minHeight).toBe("44px");
  });

  it("thinking escalates at 5 s and 45 s and always offers Stop", () => {
    expect(stateSpec("thinking", { elapsedMs: 1_000 }).copy).toBe("Thinking");
    expect(stateSpec("thinking", { elapsedMs: 5_000 }).copy).toBe("Still thinking");
    expect(stateSpec("thinking", { elapsedMs: 45_000 }).copy).toBe("Taking longer than usual");
    expect(stateSpec("thinking", { elapsedMs: 45_000 }).exit).toEqual({ label: "Stop", action: "stop" });
  });

  it("queued names the wait and the server's queue position", () => {
    expect(stateSpec("queued", { elapsedMs: 3_400 }).copy).toBe("Waiting for the assistant to start · 3 s");
    expect(stateSpec("queued", { elapsedMs: 3_400, queuePosition: 2 }).copy).toBe(
      "Waiting for the assistant to start · 3 s · number 2 in line",
    );
  });

  it("done, rate-limited, busy, not sent and the consent states carry the standard's copy", () => {
    expect(stateSpec("done", { answeredMs: 6_200 }).copy).toBe("Answered in 6.2 s");
    expect(stateSpec("rate_limited", { retryAt: "2026-09-23T21:12:00Z" }).copy).toBe("You can ask again at 4:12 PM CT.");
    expect(stateSpec("busy", { busyDevice: "phone" }).copy).toBe("The assistant is answering a question from your phone.");
    expect(stateSpec("busy", { busyDevice: "web" }).copy).toBe("The assistant is answering a question from your web.");
    expect(stateSpec("not_sent", { reasonText: "No signal." }).copy).toBe(
      "Not sent. No signal. Your draft is back in the box.",
    );
    expect(stateSpec("needs_confirmation", { approveLabel: "Halt engine" }).exit).toEqual({
      label: "Halt engine",
      action: "approve",
    });
    expect(stateSpec("needs_confirmation").ink).toBe("NEEDS YOUR OK");
    expect(stateSpec("requested").ink).toBe("REQUESTED");
    expect(stateSpec("applied").ink).toBe("APPLIED");
    expect(stateSpec("offline").copy).toBe("You're offline. Your message is saved.");
  });

  it("the preset state cards render their state", () => {
    render(
      <>
        <ExpiredCard />
        <ConsumedCard />
        <WorkerOfflineCard />
        <UpdateRequiredCard />
      </>,
    );
    const states = screen.getAllByTestId("assistant-state").map((el) => el.getAttribute("data-state"));
    expect(states).toEqual(["expired", "consumed", "worker_offline", "update_required"]);
    expect(screen.getByText("This version can't read the conversation. Update to continue.")).toBeInTheDocument();
  });

  it("a server reason's text and exit win over the standard copy", () => {
    render(
      <WorkerOfflineCard
        reason={{
          code: "worker_offline",
          text: "The desk computer dropped off.",
          next: { label: "Retry", action: "retry" },
        }}
      />,
    );
    expect(screen.getByText("The desk computer dropped off.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });
});

describe("availability", () => {
  it("unconfigured keeps assistant.empty and assistant.prompt on screen", () => {
    render(<AssistantThread thread={golden("idle").thread} availability="unconfigured" now={0} />);
    expect(screen.getByTestId("assistant.empty")).toBeInTheDocument();
    expect(screen.getByTestId("assistant.prompt")).toBeInTheDocument();
    expect(screen.getByText("The assistant isn't set up here yet.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open settings" })).toBeInTheDocument();
  });

  it("offline and lost connection show above the transcript", () => {
    const conversation = golden("answer-with-view");
    const { unmount } = render(<AssistantThread thread={conversation.thread} connection="offline" now={0} />);
    expect(screen.getByText("You're offline. Your message is saved.")).toBeInTheDocument();
    unmount();
    render(<AssistantThread thread={conversation.thread} connection="lost" now={0} />);
    expect(screen.getByText("Lost connection. Your conversation is saved.")).toBeInTheDocument();
  });

  it("a payload this build cannot decode is update_required", () => {
    expect(decodeThread({ session_id: "s" })).toMatchObject({ ok: false, state: "update_required" });
    expect(decodeGolden({})).toMatchObject({ ok: false, state: "update_required" });
    const decoded = decodeThread(golden("card-approved").thread);
    expect(decoded.ok).toBe(true);
  });
});

describe("the live turn", () => {
  it("folds each golden's events to its thread and live state", () => {
    const expected: Record<string, string | null> = {
      idle: null,
      "answer-with-view": "done",
      "tool-failed": "done",
      "card-approved": "done",
      "card-expired": "done",
      "rate-limited": "rate_limited",
      "worker-offline": "worker_offline",
    };
    for (const [name, state] of Object.entries(expected)) {
      const conversation = golden(name);
      const folded = foldEvents(conversation.events);
      expect(folded.items).toEqual(conversation.thread.items);
      expect(folded.live === null ? null : liveStateFor(folded.live)).toBe(state);
    }
    const offline = foldEvents(golden("worker-offline").events).live;
    expect(offline?.partial).toBe(true);
    expect(offline?.reason?.code).toBe("worker_offline");
  });

  it("a mid-card replay shows the card waiting while the turn runs", () => {
    const events = golden("card-approved").events;
    const folded = foldEvents(events.slice(0, 4));
    expect(folded.live?.state).toBe("running");
    expect(liveStateFor(folded.live as LiveTurn)).toBe("thinking");
    const card = folded.items[1].blocks.find((block) => block.type === "card");
    expect(card?.type === "card" ? card.card.state : null).toBe("pending");
  });

  it("an unmapped failure after partial text is partial_then_error", () => {
    const live: LiveTurn = {
      turn_id: "t",
      state: "failed",
      partial: true,
      reason: { code: "internal", text: "Something broke.", next: { label: "Try again", action: "retry" } },
    };
    expect(liveStateFor(live)).toBe("partial_then_error");
    expect(liveStateFor({ ...live, state: "stopped", reason: undefined })).toBe("stopped");
    expect(liveStateFor({ ...live, state: "expired" })).toBe("turn_expired");
  });

  it("reason codes land in their VI.3 states", () => {
    expect(stateForReason("card_expired")).toBe("expired");
    expect(stateForReason("card_consumed")).toBe("consumed");
    expect(stateForReason("card_superseded")).toBe("consent_superseded");
    expect(stateForReason("access_revoked")).toBe("access_revoked");
    expect(stateForReason("update_required")).toBe("update_required");
    expect(stateForReason("internal")).toBeNull();
  });
});

describe("formatting", () => {
  it("clock times are CT with a plain space, and malformed input fails closed", () => {
    expect(formatClockCt("2026-09-23T21:12:00Z")).toBe("4:12 PM CT");
    expect(formatClockCt("2026-01-15T21:12:00Z")).toBe("3:12 PM CT");
    expect(formatClockCt("not a time")).toBe("time not reported");
  });

  it("under an hour reads relative", () => {
    const now = Date.parse("2026-09-23T16:05:00Z");
    expect(formatWhen("2026-09-23T16:00:00Z", now)).toBe("5 min ago");
    expect(formatWhen("2026-09-23T16:04:30Z", now)).toBe("just now");
    expect(formatWhen("2026-09-23T14:00:00Z", now)).toBe("9:00 AM CT");
  });

  it("money, countdowns and model text", () => {
    expect(formatMoneyCents(123450)).toBe("$1,234.50");
    expect(formatMoneyCents(-300)).toBe("-$3.00");
    expect(formatCountdown(272_000)).toBe("4:32");
    expect(formatCountdown(-5)).toBe("0:00");
    expect(plainModelText("Done ✅ ‮evil")).toBe("Done  evil");
  });

  it("starters: at most four, none with digits", () => {
    expect(safeStarters(["A?", "B 2?", "C?", "D?", "E?", "F?"])).toEqual(["A?", "C?", "D?", "E?"]);
    expect(safeStarters(undefined)).toEqual([]);
    expect(safeStarters([42, "", "x".repeat(121), "Ok?"])).toEqual(["Ok?"]);
  });
});
