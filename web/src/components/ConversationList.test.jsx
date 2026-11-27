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

vi.mock("../converse.js", async (importOriginal) => ({
  ...(await importOriginal()),
  ensureSession: vi.fn(),
  listSessions: vi.fn(),
}));

import { ensureSession, listSessions } from "../converse.js";
import ConversationList, { resumeHref } from "./ConversationList.jsx";

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
  vi.unstubAllGlobals();
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

  it.each([
    ["legacy drawing", { kind: "drawing", handle: "roof-1" }, undefined, null, "roof-1"],
    ["entity in the first drawing", { kind: "entity", handle: "1A" }, "roof-1", null, "roof-1"],
    ["same entity in a second drawing", { kind: "entity", handle: "1A" }, "roof-2", null, "roof-2"],
    ["drawing-bound project", { kind: "project", handle: "project-1" }, "roof-2", "project-1", "roof-2"],
    ["entity in a project drawing", { kind: "entity", handle: "1A" }, "roof-2", "project-1", "roof-2"],
  ])("reattaches the exact saved identity for %s", async (_, scope, drawingId, projectId, expectedDrawing) => {
    const onResume = vi.fn();
    listSessions.mockResolvedValue({
      sessions: [{
        id: "saved-session",
        title: "Saved thread",
        scope,
        drawing_id: drawingId,
        project_id: projectId,
        last_seq: 9,
      }],
      nextCursor: null,
    });
    ensureSession.mockResolvedValue({ session_id: "saved-session" });

    render(<ConversationList onResume={onResume} />);
    fireEvent.click(await screen.findByRole("button", { name: /Saved thread/ }));

    await waitFor(() => {
      expect(onResume).toHaveBeenCalledWith("saved-session", { scope, afterSeq: 9 });
    });
    expect(ensureSession).toHaveBeenCalledWith(expectedDrawing, projectId);
  });

  it.each([
    ["project default", { kind: "project", handle: "legacy-project" }, "default", "legacy-project"],
    ["project with drawing", { kind: "project", handle: "legacy-project", drawingId: "roof-2" }, "roof-2", "legacy-project"],
    ["entity with drawing", { kind: "entity", handle: "1A", drawingId: "legacy-roof" }, "legacy-roof", null],
  ])("resumes a legacy %s through the session client", async (_, scope, drawingId, projectId) => {
    const { ensureSession: attach, resetSession } = await vi.importActual("../converse.js");
    resetSession(scope);
    const onResume = vi.fn();
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ session_id: "legacy-session" }),
    });
    vi.stubGlobal("fetch", fetch);
    ensureSession.mockImplementationOnce(attach);
    listSessions.mockResolvedValue({
      sessions: [{ id: "legacy-session", title: "Older thread", scope, last_seq: 4 }],
      nextCursor: null,
    });

    render(<ConversationList onResume={onResume} />);
    fireEvent.click(await screen.findByRole("button", { name: /Older thread/ }));

    await waitFor(() => {
      expect(onResume).toHaveBeenCalledWith("legacy-session", { scope, afterSeq: 4 });
    });
    expect(fetch).toHaveBeenCalledTimes(1);
    const [url, request] = fetch.mock.calls[0];
    expect(url).toMatch(/\/api\/sessions$/);
    expect(request.method).toBe("POST");
    expect(JSON.parse(request.body)).toEqual({
      drawing_id: drawingId,
      ...(projectId ? { project_id: projectId } : {}),
      scope: { kind: scope.kind, handle: scope.handle },
    });
  });

  it("keeps a cursor attached to its listed session when the attach returns another ID", async () => {
    const onResume = vi.fn();
    listSessions.mockResolvedValue({
      sessions: [{
        id: "saved-session",
        title: "Saved thread",
        scope: { kind: "drawing", handle: "roof-1" },
        drawing_id: "roof-1",
        last_seq: 9,
      }],
      nextCursor: null,
    });
    ensureSession.mockResolvedValue({ session_id: "different-session" });

    render(<ConversationList onResume={onResume} />);
    fireEvent.click(await screen.findByRole("button", { name: /Saved thread/ }));

    await screen.findByText("Resume failed.");
    expect(onResume).not.toHaveBeenCalled();
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
