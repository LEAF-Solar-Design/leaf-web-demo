import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { GoldenConversationV1, ID_PATTERN } from "../../assistant-contract/src/index";
import type { BlockV1, ConfirmCardV1, ItemV1, ReasonV1, ToolCallV1 } from "../../assistant-contract/src/index";
import {
  AssistantComposer,
  AssistantConfirmCard,
  AssistantStream,
  AssistantThread,
  AssistantTurnBubble,
  AssistantViewPanel,
  COLOR_TOKENS,
  ConfigDraftCard,
  ConsentCard,
  LEAF_TOKENS,
  ProposalCard,
  ReasonBanner,
  foldEvents,
  mergeToolCalls,
  tokenStyle,
} from "../src/index";

// Every component is rendered from the K0 golden conversations, decoded at
// their pin with the contract's own GoldenConversationV1.

const here = dirname(fileURLToPath(import.meta.url));
const FIXTURES = join(here, "..", "..", "assistant-contract", "fixtures");
const SRC = join(here, "..", "src");
const GOLDEN_NAMES = [
  "idle",
  "answer-with-view",
  "tool-failed",
  "card-approved",
  "card-expired",
  "rate-limited",
  "worker-offline",
] as const;
type GoldenName = (typeof GOLDEN_NAMES)[number];

/** A day after every fixture, so every time renders as a CT clock, never relative. */
const LATER = Date.parse("2026-09-24T18:00:00Z");

function golden(name: GoldenName): GoldenConversationV1 {
  return GoldenConversationV1.parse(JSON.parse(readFileSync(join(FIXTURES, `${name}.json`), "utf8")));
}

function renderGolden(name: GoldenName, extra: { onAction?: (action: string) => void } = {}) {
  const conversation = golden(name);
  const { live } = foldEvents(conversation.events);
  return render(<AssistantThread thread={conversation.thread} live={live} now={LATER} onAction={extra.onAction} />);
}

/** The card as the server first delivered it (pending), from the golden event stream. */
function pendingCard(name: "card-approved" | "card-expired"): ConfirmCardV1 {
  for (const event of golden(name).events) {
    if (event.type !== "item") continue;
    for (const block of event.item.blocks) {
      if (block.type === "card" && block.card.state === "pending") return block.card;
    }
  }
  throw new Error(`${name} has no pending card`);
}

const SHOW_TEXT = 4;
const DOCUMENT_POSITION_PRECEDING = 2;

/** Every piece of text that precedes `anchor` in document order. */
function textBefore(root: HTMLElement, anchor: Node): string {
  const walker = root.ownerDocument.createTreeWalker(root, SHOW_TEXT);
  let out = "";
  for (let node = walker.nextNode(); node !== null; node = walker.nextNode()) {
    if (anchor.compareDocumentPosition(node) & DOCUMENT_POSITION_PRECEDING) out += `${node.textContent ?? ""}\n`;
  }
  return out;
}

describe("golden conversations", () => {
  it.each(GOLDEN_NAMES)("%s decodes, folds to its thread and renders in seq order with no markup", (name) => {
    const conversation = golden(name);
    const folded = foldEvents(conversation.events);
    expect(folded.items).toEqual(conversation.thread.items);
    const { container } = render(<AssistantThread thread={conversation.thread} live={folded.live} now={LATER} />);
    const seqs = [...container.querySelectorAll<HTMLElement>("[data-seq]")].map((el) => Number(el.dataset.seq));
    expect(seqs).toEqual(conversation.thread.items.map((item) => item.seq));
    expect(container.querySelectorAll("a, h1, h2, h3, h4, h5, h6")).toHaveLength(0);
    expect(container.textContent ?? "").not.toMatch(/mushy/i);
  });

  it("idle: the copy, at most four server starters with no digits, and the quota", () => {
    const conversation = golden("idle");
    render(
      <AssistantThread
        thread={conversation.thread}
        now={LATER}
        starters={[
          "Show 3 fills",
          "What changed today?",
          "How are my engines?",
          "Any alerts?",
          "Summarize my week",
          "Show open positions",
        ]}
      />,
    );
    expect(screen.getByText("Ask about your account. Answers come from your own data.")).toBeInTheDocument();
    const list = screen.getByRole("list", { name: "Suggested questions" });
    const starters = within(list)
      .getAllByRole("button")
      .map((button) => button.textContent ?? "");
    expect(starters).toEqual(["What changed today?", "How are my engines?", "Any alerts?", "Summarize my week"]);
    expect(starters.join(" ")).not.toMatch(/[0-9]/);
    expect(screen.getByTestId("assistant.empty")).toBeInTheDocument();
    expect(screen.getByTestId("assistant.prompt")).toBeInTheDocument();
    expect(screen.getByText("0 of 30 questions used this hour")).toBeInTheDocument();
    expect(screen.getByText("Resets at 12:00 PM CT")).toBeInTheDocument();
    expect(screen.getByTestId("assistant-provider")).toHaveTextContent("Claude");
    expect(screen.getByText("Your account")).toBeInTheDocument();
  });

  it("answer-with-view: the ANSWER frame, no StateInk, mono figures, Not reported, provenance and done time", () => {
    renderGolden("answer-with-view");
    const answer = screen.getByTestId("assistant-answer");
    expect(within(answer).getByText("ANSWER")).toBeInTheDocument();
    expect(within(answer).queryByTestId("state-ink")).toBeNull();
    expect(within(answer).getByText("$12.50")).toHaveAttribute("data-figure", "mono");
    expect(within(answer).getByText("4")).toHaveAttribute("data-figure", "mono");
    expect(within(answer).getByLabelText("Not reported")).toHaveTextContent("—");
    expect(screen.getByTestId("assistant-provenance")).toHaveTextContent(
      "From Read today's books · as of 11:00 AM CT",
    );
    expect(within(screen.getByTestId("assistant-tool-row")).getByText("Read today's books")).toBeInTheDocument();
    expect(screen.getByText("Answered in 6.0 s")).toBeInTheDocument();
    expect(screen.getByTestId("assistant-announcer").textContent).toBe("Answer ready. Answered in 6.0 s");
    const log = screen.getByRole("log");
    expect(log).toHaveAttribute("aria-live", "polite");
    expect(log).toHaveAttribute("aria-busy", "false");
  });

  it("tool-failed: the failed read says Unavailable, with its reason and one exit", () => {
    const onAction = vi.fn();
    renderGolden("tool-failed", { onAction });
    const row = screen.getByTestId("assistant-tool-row");
    expect(within(row).getByText("Read open positions")).toBeInTheDocument();
    expect(within(row).getByText("Unavailable")).toBeInTheDocument();
    expect(within(row).queryByText("Done")).toBeNull();
    const reason = screen.getByTestId("assistant-reason");
    expect(within(reason).getByText("Couldn't read your open positions.")).toBeInTheDocument();
    const exits = within(reason).getAllByRole("button");
    expect(exits).toHaveLength(1);
    fireEvent.click(exits[0]);
    expect(onAction).toHaveBeenCalledWith("retry");
  });

  it("card-approved: the receipt line stays in the transcript and no decision buttons remain", () => {
    renderGolden("card-approved");
    expect(screen.getByTestId("state-ink")).toHaveTextContent("APPROVED");
    expect(screen.getByTestId("assistant-receipt")).toHaveTextContent("Approved on Owner iPhone at 11:20 AM CT");
    expect(screen.queryByRole("button", { name: "Halt engine" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Keep it running" })).toBeNull();
  });

  it("card-expired: EXPIRED ink, the receipt, the server notice with its exit, and the quota", () => {
    renderGolden("card-expired");
    expect(screen.getByTestId("state-ink")).toHaveTextContent("EXPIRED");
    expect(screen.getByTestId("assistant-receipt")).toHaveTextContent("Expired at 11:35 AM CT. Nothing was changed.");
    const reason = screen.getByTestId("assistant-reason");
    expect(reason).toHaveTextContent("This request expired. Nothing was changed.");
    expect(within(reason).getByRole("button", { name: "Ask again" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Request change" })).toBeNull();
    expect(screen.getByText("4 of 30 questions used this hour")).toBeInTheDocument();
    expect(screen.getByText("CONFIG CHANGE")).toBeInTheDocument();
  });

  it("rate-limited: the reset time in CT, one banner, and an empty quota", () => {
    renderGolden("rate-limited");
    const reasons = screen.getAllByTestId("assistant-reason");
    expect(reasons).toHaveLength(1);
    expect(reasons[0]).toHaveTextContent("You can ask again at 4:12 PM CT.");
    expect(within(reasons[0]).getByRole("button", { name: "Wait" })).toBeInTheDocument();
    expect(screen.getByText("No questions left this hour")).toBeInTheDocument();
    expect(screen.getByText("Resets at 4:12 PM CT")).toBeInTheDocument();
  });

  it("worker-offline: the partial answer is kept and the failure shows once with Try again", () => {
    renderGolden("worker-offline");
    expect(screen.getByText("Pulling today's fills now.")).toBeInTheDocument();
    const reasons = screen.getAllByTestId("assistant-reason");
    expect(reasons).toHaveLength(1);
    expect(reasons[0]).toHaveTextContent("The assistant's computer went offline mid-answer.");
    expect(within(reasons[0]).getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});

describe("AssistantConfirmCard", () => {
  it("a pending card: NEEDS YOUR OK, countdown, server labels, and model prose only below Approve", () => {
    const card = pendingCard("card-approved");
    const onApprove = vi.fn();
    const { container } = render(
      <ProposalCard card={card} now={Date.parse(card.created_at) + 28_000} onApprove={onApprove} />,
    );
    expect(screen.getByTestId("state-ink")).toHaveTextContent("NEEDS YOUR OK");
    expect(screen.getByTestId("assistant-countdown")).toHaveTextContent("Expires in 4:32");
    expect(screen.getByText("CONTROL REQUEST")).toBeInTheDocument();
    expect(screen.getByText("Nothing was spent")).toBeInTheDocument();
    const approve = screen.getByRole("button", { name: "Halt engine" });
    expect(screen.getByRole("button", { name: "Keep it running" })).toBeInTheDocument();
    const note = screen.getByTestId("assistant-note") as HTMLDetailsElement;
    expect(note.open).toBe(false);
    expect(within(note).getByText("Assistant's note")).toBeInTheDocument();
    expect(textBefore(container, approve)).not.toContain("You asked to stop trading ETH");
    fireEvent.click(approve);
    fireEvent.click(approve);
    expect(onApprove).toHaveBeenCalledTimes(1);
    const intent = onApprove.mock.calls[0][0] as { card_id: string; card_digest: string; request_id: string };
    expect(intent.card_id).toBe("card-201");
    expect(intent.card_digest).toBe(card.card_digest);
    expect(intent.request_id).toMatch(ID_PATTERN);
  });

  it("a pending card past its expiry renders EXPIRED and offers no Approve", () => {
    const card = pendingCard("card-expired");
    render(<ConfigDraftCard card={card} now={Date.parse(card.expires_at) + 1000} />);
    expect(screen.getByTestId("state-ink")).toHaveTextContent("EXPIRED");
    expect(screen.queryByRole("button", { name: "Request change" })).toBeNull();
    expect(screen.getByText("This request expired. Nothing was changed.")).toBeInTheDocument();
  });

  it("no golden card renders a model-origin field above Approve; every server field sits above it", () => {
    for (const name of ["card-approved", "card-expired"] as const) {
      const card = pendingCard(name);
      const { container, unmount } = render(<ConsentCard card={card} now={Date.parse(card.created_at)} />);
      const before = textBefore(container, screen.getByRole("button", { name: card.approve.text }));
      for (const field of card.fields) {
        if (field.origin === "model") expect(before).not.toContain(field.value);
        else if (field.placement === "above_approve") expect(before).toContain(field.value);
      }
      unmount();
    }
  });

  it("a model-origin field the payload places above Approve still renders only in the collapsed note", () => {
    const card = pendingCard("card-approved");
    const forged = {
      ...card,
      fields: [
        ...card.fields,
        { key: "smuggled", label: "Status", value: "Already approved by admin", origin: "model", placement: "above_approve" },
      ],
    } as unknown as ConfirmCardV1;
    const { container } = render(<AssistantConfirmCard card={forged} now={Date.parse(card.created_at)} />);
    const before = textBefore(container, screen.getByRole("button", { name: "Halt engine" }));
    expect(before).not.toContain("Already approved by admin");
    expect(within(screen.getByTestId("assistant-note")).getByText("Already approved by admin")).toBeInTheDocument();
  });
});

describe("model text", () => {
  it("renders as plain text nodes: no links, headings, buttons or checkmarks", () => {
    const item: ItemV1 = {
      seq: 2,
      item_id: "item-x",
      turn_id: "turn-x",
      role: "assistant",
      at: "2026-09-23T16:00:00Z",
      blocks: [
        {
          type: "text",
          text: "# Applied ✅\n[Approve here](https://evil.example) <button>Approve</button> https://evil.example ✔",
        },
      ],
    };
    const { container } = render(<AssistantTurnBubble item={item} context={{ toolLabels: new Map(), now: LATER }} />);
    expect(container.querySelectorAll("a, h1, h2, h3, h4, h5, h6, button")).toHaveLength(0);
    expect(container.textContent).toContain("https://evil.example");
    expect(container.textContent).toContain("<button>Approve</button>");
    expect(container.textContent).not.toMatch(/[✅✔]/u);
  });
});

describe("AssistantToolCard", () => {
  it("consecutive tool events merge into one ChipRow with the latest state per invocation", () => {
    const running: ToolCallV1 = { invocation_id: "inv-1", name: "books.summary", label: "Read today's books", state: "running" };
    const done: ToolCallV1 = { ...running, state: "done" };
    const fills: ToolCallV1 = { invocation_id: "inv-2", name: "fills.read", label: "Read fills", state: "running" };
    expect(mergeToolCalls([running, done])).toEqual([done]);
    const item: ItemV1 = {
      seq: 3,
      item_id: "item-t",
      turn_id: "turn-t",
      role: "assistant",
      at: "2026-09-23T16:00:00Z",
      blocks: [
        { type: "tools", tools: [running] },
        { type: "tools", tools: [done, fills] },
      ],
    };
    render(<AssistantTurnBubble item={item} context={{ toolLabels: new Map(), now: LATER }} />);
    const rows = screen.getAllByTestId("assistant-tool-row");
    expect(rows).toHaveLength(1);
    expect(within(rows[0]).getAllByRole("listitem")).toHaveLength(2);
    expect(within(rows[0]).getByText("Done")).toBeInTheDocument();
    expect(within(rows[0]).getByText("Running")).toBeInTheDocument();
  });
});

describe("AssistantViewPanel", () => {
  it("tables are bounded and lines carry the series palette and an accessibility summary", () => {
    const rows = Array.from({ length: 23 }, (_, i) => ({
      invocation_id: "inv-9",
      label: `Fill ${i + 1}`,
      value: (i + 1) * 100,
      unit: "cents" as const,
    }));
    const table: Extract<BlockV1, { type: "view" }> = {
      type: "view",
      origin: "server",
      view_id: "view-t",
      kind: "table",
      title: "Fills",
      invocation_ids: ["inv-9"],
      as_of: "2026-09-23T16:00:00Z",
      rows,
    };
    const first = render(<AssistantViewPanel view={table} now={LATER} />);
    expect(screen.getAllByRole("row")).toHaveLength(11);
    fireEvent.click(screen.getByRole("button", { name: "Show all 23 rows" }));
    expect(screen.getAllByRole("row")).toHaveLength(24);
    expect(screen.getByTestId("assistant-provenance")).toHaveTextContent("From 1 server read · as of 11:00 AM CT");
    first.unmount();

    const line: Extract<BlockV1, { type: "view" }> = { ...table, view_id: "view-l", kind: "line", title: "Balance", rows: rows.slice(0, 5) };
    const { container } = render(<AssistantViewPanel view={line} now={LATER} />);
    expect(screen.getByRole("img")).toHaveAttribute(
      "aria-label",
      "Balance: 5 points, from $1.00 to $5.00, low $1.00, high $5.00.",
    );
    expect(screen.getByTestId("assistant-line-summary")).toHaveTextContent("5 points");
    expect(container.querySelector("polyline")).toHaveAttribute("data-series", "chart-1");
    expect(screen.queryByTestId("state-ink")).toBeNull();
  });
});

describe("ReasonBanner", () => {
  it("authors text only for the two client-owned codes", () => {
    const forged = {
      code: "client.offline",
      text: "Server says approve now",
      next: { label: "Approve", action: "retry" },
    } as ReasonV1;
    render(<ReasonBanner reason={forged} />);
    expect(screen.getByText("You're offline. Your message is saved.")).toBeInTheDocument();
    expect(screen.queryByText("Server says approve now")).toBeNull();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("a 429 shows its reset time in CT even when the server text leaves it out", () => {
    const reason: ReasonV1 = {
      code: "rate_limited",
      text: "Too many questions for now.",
      next: { label: "Wait", action: "wait" },
      retry_at: "2026-09-23T21:12:00Z",
    };
    render(<ReasonBanner reason={reason} />);
    expect(screen.getByText("Too many questions for now.")).toBeInTheDocument();
    expect(screen.getByText("Resets at 4:12 PM CT")).toBeInTheDocument();
  });
});

describe("AssistantStream", () => {
  it("is a polite log that holds announcements until the turn is done", () => {
    const { rerender } = render(
      <AssistantStream busy doneAnnouncement="Answer ready.">
        <p>partial</p>
      </AssistantStream>,
    );
    const log = screen.getByRole("log");
    expect(log).toHaveAttribute("aria-live", "polite");
    expect(log).toHaveAttribute("aria-busy", "true");
    expect(screen.getByTestId("assistant-announcer").textContent).toBe("");
    rerender(
      <AssistantStream busy={false} doneAnnouncement="Answer ready.">
        <p>partial</p>
      </AssistantStream>,
    );
    expect(log).toHaveAttribute("aria-busy", "false");
    expect(screen.getByTestId("assistant-announcer").textContent).toBe("Answer ready.");
  });
});

describe("AssistantComposer", () => {
  function box(): HTMLTextAreaElement {
    return screen.getByTestId("assistant.prompt") as HTMLTextAreaElement;
  }

  it("counts down from 7,000 and refuses to send over the limit", () => {
    render(<AssistantComposer onSend={vi.fn()} isOnline={() => true} />);
    expect(screen.getByTestId("assistant-counter")).toHaveTextContent("7,000 left");
    fireEvent.change(box(), { target: { value: "hello" } });
    expect(screen.getByTestId("assistant-counter")).toHaveTextContent("6,995 left");
    fireEvent.change(box(), { target: { value: "x".repeat(7001) } });
    expect(screen.getByTestId("assistant-counter")).toHaveTextContent("Over by 1");
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
  });

  it("sends optimistically: Sending, then Sent, with the draft cleared", async () => {
    let settle: (value: { ok: true }) => void = () => {};
    const onSend = vi.fn(() => new Promise<{ ok: true }>((resolve) => (settle = resolve)));
    render(<AssistantComposer onSend={onSend} isOnline={() => true} />);
    fireEvent.change(box(), { target: { value: "How did the books do today?" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText("Sending")).toBeInTheDocument();
    expect(box().value).toBe("");
    expect(onSend).toHaveBeenCalledTimes(1);
    const request = (onSend.mock.calls[0] as unknown as [{ request_id: string; text: string }])[0];
    expect(request.text).toBe("How did the books do today?");
    expect(request.request_id).toMatch(ID_PATTERN);
    await act(async () => {
      settle({ ok: true });
    });
    expect(await screen.findByText("Sent")).toBeInTheDocument();
  });

  it("a refused send says Not sent with the server's reason and puts the draft back", async () => {
    const reason: ReasonV1 = {
      code: "busy",
      text: "The assistant is answering a question from your phone.",
      next: { label: "Wait", action: "wait" },
    };
    render(<AssistantComposer onSend={async () => ({ ok: false, reason })} isOnline={() => true} />);
    fireEvent.change(box(), { target: { value: "Try this" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText("Not sent")).toBeInTheDocument();
    expect(screen.getByText("The assistant is answering a question from your phone.")).toBeInTheDocument();
    expect(screen.getByText("Your draft is back in the box.")).toBeInTheDocument();
    expect(box().value).toBe("Try this");
  });

  it("a send that never answers times out with the client's own copy and keeps its request id on retry", async () => {
    const onSend = vi.fn(() => new Promise<void>(() => {}));
    render(<AssistantComposer onSend={onSend} timeoutMs={20} isOnline={() => true} />);
    fireEvent.change(box(), { target: { value: "Still there?" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText("The assistant didn't answer in time.")).toBeInTheDocument();
    expect(box().value).toBe("Still there?");
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Sending")).toBeInTheDocument();
    const calls = onSend.mock.calls as unknown as [{ request_id: string }][];
    expect(calls).toHaveLength(2);
    expect(calls[1][0].request_id).toBe(calls[0][0].request_id);
  });

  it("offline keeps the draft and never calls onSend", () => {
    const onSend = vi.fn();
    render(<AssistantComposer onSend={onSend} isOnline={() => false} />);
    fireEvent.change(box(), { target: { value: "Hello" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(screen.getByText("You're offline. Your message is saved.")).toBeInTheDocument();
    expect(onSend).not.toHaveBeenCalled();
    expect(box().value).toBe("Hello");
  });
});

describe("tokens and targets", () => {
  it("every colour token has a light and a dark value from contract/leaf-tokens.v1.json", () => {
    expect(COLOR_TOKENS.length).toBeGreaterThan(20);
    const light = tokenStyle("light") as Record<string, string>;
    const dark = tokenStyle("dark") as Record<string, string>;
    for (const token of COLOR_TOKENS) {
      expect(light[`--leaf-${token}`]).toMatch(/^#[0-9a-f]{6}$/);
      expect(dark[`--leaf-${token}`]).toMatch(/^#[0-9a-f]{6}$/);
    }
    expect(dark["--leaf-background"]).toBe(LEAF_TOKENS.color.dark.background);
    expect(light["--leaf-font-mono"]).toContain("Geist Mono");
  });

  it("no component hard-codes a colour", () => {
    const files = [
      join(SRC, "Thread.tsx"),
      join(SRC, "states.ts"),
      ...readdirSync(join(SRC, "components")).map((file) => join(SRC, "components", file)),
    ];
    for (const file of files) {
      expect(readFileSync(file, "utf8"), file).not.toMatch(/#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(/);
    }
  });

  it("every button is at least 44 px", () => {
    const card = pendingCard("card-approved");
    const conversation = golden("tool-failed");
    render(
      <>
        <ConsentCard card={card} now={Date.parse(card.created_at)} />
        <AssistantThread thread={conversation.thread} live={foldEvents(conversation.events).live} now={LATER} />
      </>,
    );
    const buttons = screen.getAllByRole("button");
    expect(buttons.length).toBeGreaterThan(3);
    for (const button of buttons) expect(button.style.minHeight).toBe("44px");
  });
});
