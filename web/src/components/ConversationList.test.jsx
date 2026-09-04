// @vitest-environment jsdom
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../converse.js", () => ({
  ensureSession: vi.fn(),
  listSessions: vi.fn(),
  normalizeScope(scope) {
    if (!scope || !["project", "drawing", "entity"].includes(scope.kind))
      return null;
    const handle = typeof scope.handle === "string" ? scope.handle.trim() : "";
    return handle ? { kind: scope.kind, handle } : null;
  },
}));

import { ensureSession, listSessions } from "../converse.js";
import ConversationList, { resumeHref } from "./ConversationList.jsx";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ConversationList resume", () => {
  it("builds only a same-origin drawing URL and stays put for the current drawing", () => {
    expect(
      resumeHref(
        { kind: "drawing", handle: "roof 2" },
        "roof-1",
        "https://studio.example/app?surface=cad#viewer",
      ),
    ).toBe("https://studio.example/app?surface=cad&drawing=roof+2#viewer");
    expect(
      resumeHref(
        { kind: "drawing", handle: "roof-1" },
        "roof-1",
        "https://studio.example/app?surface=cad",
      ),
    ).toBeNull();
    expect(
      resumeHref({ kind: "project", handle: "p1" }, "roof-1", "not a URL"),
    ).toBeNull();
  });

  it("gives the disabled row a visible reason while resume is in flight", async () => {
    let finish;
    const pending = new Promise((resolve) => {
      finish = resolve;
    });
    const onResume = vi.fn();
    listSessions.mockResolvedValue({
      sessions: [
        {
          id: "session-1",
          scope: { kind: "drawing", handle: "roof-1" },
          title: "Count panels",
          updated_at: Date.now(),
          turn_count: 2,
          last_seq: 7,
        },
      ],
      nextCursor: null,
    });
    ensureSession.mockReturnValue(pending);

    render(<ConversationList onResume={onResume} />);
    const row = await screen.findByRole("button", { name: /Count panels/ });
    fireEvent.click(row);

    await waitFor(() => {
      expect(row.disabled).toBe(true);
      expect(row.textContent).toContain("Opening");
    });

    await act(async () => {
      finish({ session_id: "session-1" });
      await pending;
    });
    expect(onResume).toHaveBeenCalledWith("session-1", {
      scope: { kind: "drawing", handle: "roof-1" },
      afterSeq: 7,
    });
  });
});
