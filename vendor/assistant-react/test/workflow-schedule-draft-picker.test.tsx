import React from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  IntegrationWorkflowScheduleDraftPicker, type IntegrationWorkflowScheduleDraftPickerProps,
} from "../src/actions";
import { scheduleDraftPickerCopy } from "../src/actions/workflow-schedule-draft-picker-state";
import { tokenStyle } from "../src/tokens";

const first = { id: "daily-review", version: 2, digest: "ab".repeat(32) };
const second = { id: "another-draft", version: 7, digest: "cd".repeat(32) };
const cursor = "PRIVATE_CURSOR/+?= exact";
const page = { references: [first, second], nextCursor: cursor };
function props(overrides: Partial<IntegrationWorkflowScheduleDraftPickerProps> = {}): IntegrationWorkflowScheduleDraftPickerProps {
  return { contextKey: "PRIVATE_SCOPE/org/project/principal/auth-1", availability: "ready",
    onLoadPage: vi.fn().mockResolvedValue(page), onSelect: vi.fn(), ...overrides };
}
function deferred() {
  let resolve!: (value: unknown) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<unknown>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function click(name: string) { fireEvent.click(screen.getByRole("button", { name })); }
async function settle() { await act(async () => { await Promise.resolve(); }); }
function row(id = first.id, version = first.version) { return screen.getByRole("button", { name: `Select draft ${id}, version ${version}` }); }
function watchReadDeadline() {
  const scheduled = vi.spyOn(globalThis, "setTimeout");
  const cleared = vi.spyOn(globalThis, "clearTimeout");
  return () => {
    // Focus also schedules jsdom's selectionchange timer; track only the read deadline.
    const indices = scheduled.mock.calls.flatMap((call, index) => call[1] === 45000 ? [index] : []);
    expect(indices).toHaveLength(1);
    expect(cleared).toHaveBeenCalledWith(scheduled.mock.results[indices[0]].value);
  };
}

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers(); });

describe("inert schedule draft picker", () => {
  it("loads only explicitly, preserves host order, forwards exact cursors and refreshes the first page", async () => {
    const input = props(); render(<IntegrationWorkflowScheduleDraftPicker {...input} />);
    expect(input.onLoadPage).not.toHaveBeenCalled();
    expect(input.onSelect).not.toHaveBeenCalled();
    click("Load drafts"); await settle();
    expect(input.onLoadPage).toHaveBeenCalledTimes(1);
    expect(vi.mocked(input.onLoadPage).mock.calls[0][0]).toEqual({ limit: 25 });
    const rows = screen.getAllByRole("button", { name: /^Select draft/ });
    expect(rows.map((node) => node.textContent)).toEqual([
      "Draft ID: daily-review. Version: 2.", "Draft ID: another-draft. Version: 7.",
    ]);
    expect(screen.getByRole("status")).toHaveFocus();
    expect(screen.getByRole("status")).toHaveTextContent("2 schedule draft references loaded.");
    vi.mocked(input.onLoadPage).mockResolvedValueOnce({ references: [second], nextCursor: null });
    click("Next"); await settle();
    expect(vi.mocked(input.onLoadPage).mock.calls[1][0]).toEqual({ limit: 25, after: cursor });
    expect(screen.queryByRole("button", { name: `Select draft ${first.id}, version ${first.version}` })).toBeNull();
    expect(screen.queryByRole("button", { name: "Next" })).toBeNull();
    click("Refresh"); await settle();
    expect(vi.mocked(input.onLoadPage).mock.calls[2][0]).toEqual({ limit: 25 });
    expect(input.onLoadPage).toHaveBeenCalledTimes(3);
    for (const hidden of [cursor, input.contextKey, first.digest]) expect(document.body.textContent).not.toContain(hidden);
    expect(screen.getByText("Selecting a draft reference does not schedule or authorize any run.")).toBeTruthy();
  });

  it("selects exact independent copies through native button activation and permits deliberate reselection", async () => {
    const onSelect = vi.fn((reference) => { reference.id = "host-mutated"; reference.digest = "host-mutated"; });
    const input = props({ onSelect }); render(<IntegrationWorkflowScheduleDraftPicker {...input} />);
    click("Load drafts"); await settle();
    const button = row(); button.focus(); expect(button).toHaveFocus();
    // Browsers dispatch a detail=0 click when a native button is activated by keyboard.
    fireEvent.click(button, { detail: 0 });
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect.mock.calls[0][0]).not.toBe(first);
    expect(button).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("status")).toHaveTextContent(scheduleDraftPickerCopy.selected);
    expect(row()).toHaveTextContent("Draft ID: daily-review. Version: 2.");
    expect(input.onLoadPage).toHaveBeenCalledTimes(1);
    fireEvent.click(row(second.id, second.version));
    expect(onSelect).toHaveBeenCalledTimes(2);
    expect(row()).toHaveAttribute("aria-pressed", "false");
    expect(row(second.id, second.version)).toHaveAttribute("aria-pressed", "true");
  });

  it("passes exactly id, version and digest without letting later host mutation alter stored references", async () => {
    const input = props(); render(<IntegrationWorkflowScheduleDraftPicker {...input} />);
    click("Load drafts"); await settle(); fireEvent.click(row());
    const sent = vi.mocked(input.onSelect).mock.calls[0][0];
    expect(sent).toEqual(first); expect(Object.keys(sent)).toEqual(["id", "version", "digest"]);
    sent.version = 99;
    fireEvent.click(row());
    expect(vi.mocked(input.onSelect).mock.calls[1][0]).toEqual(first);
    expect(vi.mocked(input.onSelect).mock.calls[1][0]).not.toBe(sent);
  });

  it("distinguishes empty terminal pages from empty continuation without filling or prefetching", async () => {
    const onLoadPage = vi.fn().mockResolvedValueOnce({ references: [], nextCursor: cursor })
      .mockResolvedValueOnce({ references: [], nextCursor: null });
    render(<IntegrationWorkflowScheduleDraftPicker {...props({ onLoadPage })} />);
    click("Load drafts"); await settle();
    expect(screen.getByRole("status")).toHaveTextContent(scheduleDraftPickerCopy.emptyContinuation);
    expect(screen.getByRole("button", { name: "Next" })).toBeEnabled();
    expect(onLoadPage).toHaveBeenCalledTimes(1);
    click("Next"); await settle();
    expect(screen.getByRole("status")).toHaveTextContent(scheduleDraftPickerCopy.empty);
    expect(screen.queryByRole("button", { name: "Next" })).toBeNull();
    expect(onLoadPage).toHaveBeenCalledTimes(2);
  });

  it.each([
    { references: [first], nextCursor: cursor }, // fails to advance the requested cursor
    { references: [first], nextCursor: null, extra: "PRIVATE_ERROR" },
    { references: Array.from({ length: 26 }, () => first), nextCursor: null },
    { references: [{ ...second, digest: "PRIVATE_ERROR" }], nextCursor: null },
  ])("retains the verified page but disables selection after invalid Next response %#", async (invalid) => {
    const onLoadPage = vi.fn().mockResolvedValueOnce(page).mockResolvedValueOnce(invalid)
      .mockResolvedValueOnce({ references: [second], nextCursor: null });
    const input = props({ onLoadPage }); render(<IntegrationWorkflowScheduleDraftPicker {...input} />);
    click("Load drafts"); await settle(); click("Next"); await settle();
    expect(screen.getByRole("status")).toHaveTextContent(scheduleDraftPickerCopy.failure);
    expect(screen.getByRole("status")).toHaveFocus();
    expect(row()).toBeDisabled(); expect(row(second.id, second.version)).toBeDisabled();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
    fireEvent.click(row()); expect(input.onSelect).not.toHaveBeenCalled();
    expect(document.body.textContent).not.toContain("PRIVATE_ERROR");
    click("Retry"); await settle();
    expect(onLoadPage.mock.calls[2][0]).toEqual({ limit: 25, after: cursor });
    expect(row(second.id, second.version)).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("blocks pending duplicate bursts and uses a fresh query/signal on exact retry after callback mutation", async () => {
    const host = deferred();
    const onLoadPage = vi.fn().mockResolvedValueOnce(page).mockImplementationOnce((query) => {
      query.after = "HOST_MUTATION"; return host.promise;
    }).mockResolvedValueOnce({ references: [], nextCursor: null });
    render(<IntegrationWorkflowScheduleDraftPicker {...props({ onLoadPage })} />);
    click("Load drafts"); await settle();
    const next = screen.getByRole("button", { name: "Next" });
    act(() => { fireEvent.click(next); fireEvent.click(next); fireEvent.click(next); });
    expect(onLoadPage).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("button", { name: "Refresh" })).toBeDisabled();
    expect(next).toBeDisabled(); expect(row()).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent(scheduleDraftPickerCopy.pending);
    await act(async () => host.reject(new Error("PRIVATE_ERROR")));
    click("Retry"); await settle();
    expect(onLoadPage.mock.calls[2][0]).toEqual({ limit: 25, after: cursor });
    expect(onLoadPage.mock.calls[2][1]).not.toBe(onLoadPage.mock.calls[1][1]);
    expect(document.body.textContent).not.toContain("HOST_MUTATION");
    expect(document.body.textContent).not.toContain("PRIVATE_ERROR");
  });

  it("handles synchronous read and selection failures with fixed copy", async () => {
    const onLoadPage = vi.fn().mockImplementationOnce(() => { throw new Error("PRIVATE_ERROR"); }).mockResolvedValue(page);
    const onSelect = vi.fn(() => { throw new Error("PRIVATE_ERROR"); });
    render(<IntegrationWorkflowScheduleDraftPicker {...props({ onLoadPage, onSelect })} />);
    click("Load drafts");
    expect(screen.getByRole("status")).toHaveTextContent(scheduleDraftPickerCopy.failure);
    click("Retry"); await settle(); fireEvent.click(row());
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("status")).toHaveTextContent(scheduleDraftPickerCopy.selectionFailure);
    expect(document.body.textContent).not.toContain("PRIVATE_ERROR");
  });

  it("aborts after 45 seconds, ignores late callbacks and offers only explicit retry", async () => {
    vi.useFakeTimers(); const host = deferred();
    const onLoadPage = vi.fn().mockImplementationOnce(() => host.promise).mockResolvedValueOnce(page);
    render(<IntegrationWorkflowScheduleDraftPicker {...props({ onLoadPage })} />);
    click("Load drafts");
    act(() => vi.advanceTimersByTime(44999));
    expect(screen.getByRole("status")).toHaveTextContent(scheduleDraftPickerCopy.pending);
    act(() => vi.advanceTimersByTime(1));
    expect(onLoadPage.mock.calls[0][1].aborted).toBe(true);
    expect(screen.getByRole("status")).toHaveTextContent(scheduleDraftPickerCopy.failure);
    await act(async () => host.resolve(page));
    act(() => vi.advanceTimersByTime(90000));
    expect(onLoadPage).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: /^Select draft/ })).toBeNull();
    expect(vi.getTimerCount()).toBe(0);
    click("Retry"); await settle();
    expect(onLoadPage.mock.calls[1][0]).toEqual({ limit: 25 });
    expect(row()).toBeEnabled(); expect(vi.getTimerCount()).toBe(0);
  });

  it.each(["success", "invalid", "rejected"])("clears the deadline after a %s read", async (outcome) => {
    vi.useFakeTimers(); const host = deferred(); const input = props({ onLoadPage: vi.fn(() => host.promise) });
    const expectDeadlineCleared = watchReadDeadline();
    render(<IntegrationWorkflowScheduleDraftPicker {...input} />); click("Load drafts");
    await act(async () => {
      if (outcome === "rejected") host.reject(new Error("PRIVATE_ERROR"));
      else host.resolve(outcome === "success" ? page : { references: [], nextCursor: "" });
    });
    expectDeadlineCleared();
    act(() => vi.advanceTimersByTime(45000));
    expect(input.onLoadPage).toHaveBeenCalledTimes(1);
    if (outcome === "success") expect(row()).toBeEnabled();
    else expect(screen.getByRole("status")).toHaveTextContent(scheduleDraftPickerCopy.failure);
  });

  it.each(["forbidden", "unavailable"] as const)("clears loaded/selected state and aborts on %s, even if readiness returns", async (availability) => {
    const host = deferred(); const onLoadPage = vi.fn().mockResolvedValueOnce(page).mockImplementationOnce(() => host.promise);
    const input = props({ onLoadPage }); const view = render(<IntegrationWorkflowScheduleDraftPicker {...input} />);
    click("Load drafts"); await settle(); fireEvent.click(row()); click("Next");
    const oldRow = row();
    view.rerender(<IntegrationWorkflowScheduleDraftPicker {...input} availability={availability} />);
    expect(onLoadPage.mock.calls[1][1].aborted).toBe(true);
    expect(screen.getByRole("status")).toHaveTextContent(scheduleDraftPickerCopy[availability]);
    expect(screen.getByRole("status")).toHaveFocus(); expect(screen.queryByRole("button")).toBeNull();
    view.rerender(<IntegrationWorkflowScheduleDraftPicker {...input} />);
    await act(async () => host.resolve(page)); fireEvent.click(oldRow);
    expect(screen.queryByRole("button", { name: /^Select draft/ })).toBeNull();
    expect(input.onSelect).toHaveBeenCalledTimes(1); expect(onLoadPage).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("button", { name: "Load drafts" })).toBeEnabled();
  });

  it.each(["context", "invalid", "unmount"])("cancels pending work and its timer on %s", async (transition) => {
    vi.useFakeTimers(); const host = deferred(); const input = props({ onLoadPage: vi.fn(() => host.promise) });
    const expectDeadlineCleared = watchReadDeadline();
    const view = render(<IntegrationWorkflowScheduleDraftPicker {...input} />); click("Load drafts");
    const signal = vi.mocked(input.onLoadPage).mock.calls[0][1];
    if (transition === "context") view.rerender(<IntegrationWorkflowScheduleDraftPicker {...input} contextKey="new-private-scope" />);
    else if (transition === "invalid") view.rerender(<IntegrationWorkflowScheduleDraftPicker {...input} onSelect={null as never} />);
    else view.unmount();
    expect(signal.aborted).toBe(true); expectDeadlineCleared();
    act(() => vi.advanceTimersByTime(45000));
    await act(async () => host.reject(new Error("PRIVATE_ERROR")));
    expect(input.onLoadPage).toHaveBeenCalledTimes(1);
    expect(document.body.textContent).not.toContain("PRIVATE_ERROR");
    expect(screen.queryByRole("button", { name: /^Select draft/ })).toBeNull();
  });

  it("retains a page across re-created callbacks but clears it on context change", async () => {
    const input = props(); const view = render(<IntegrationWorkflowScheduleDraftPicker {...input} />);
    click("Load drafts"); await settle(); fireEvent.click(row());
    const replacementSelect = vi.fn();
    view.rerender(<IntegrationWorkflowScheduleDraftPicker {...input} onLoadPage={vi.fn()} onSelect={replacementSelect} />);
    expect(row()).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(row(second.id, second.version)); expect(replacementSelect).toHaveBeenCalledWith(second);
    view.rerender(<IntegrationWorkflowScheduleDraftPicker {...input} contextKey="new-private-scope" />);
    expect(screen.queryByRole("button", { name: /^Select draft/ })).toBeNull();
    expect(input.onLoadPage).toHaveBeenCalledTimes(1);
  });

  it.each([{ contextKey: "" }, { contextKey: null }, { availability: "offline" }, { mode: "auto" }, { onLoadPage: null }, { onSelect: null }])(
    "fails closed on invalid runtime props %#", (override) => {
      render(<IntegrationWorkflowScheduleDraftPicker {...props(override as unknown as Partial<IntegrationWorkflowScheduleDraftPickerProps>)} />);
      expect(screen.getByRole("status")).toHaveTextContent(scheduleDraftPickerCopy.unavailable);
      expect(screen.queryByRole("button")).toBeNull();
    });

  it("stays idle under StrictMode and cancels an active read on unmount", () => {
    vi.useFakeTimers(); const host = deferred(); const input = props({ onLoadPage: vi.fn(() => host.promise) });
    const expectDeadlineCleared = watchReadDeadline();
    const view = render(<React.StrictMode><IntegrationWorkflowScheduleDraftPicker {...input} /></React.StrictMode>);
    act(() => vi.advanceTimersByTime(90000)); expect(input.onLoadPage).not.toHaveBeenCalled();
    click("Load drafts"); expect(input.onLoadPage).toHaveBeenCalledTimes(1); view.unmount();
    expect(vi.mocked(input.onLoadPage).mock.calls[0][1].aborted).toBe(true); expectDeadlineCleared();
    act(() => vi.advanceTimersByTime(45000));
    expect(input.onLoadPage).toHaveBeenCalledTimes(1);
  });

  it.each(["light", "dark"] as const)("uses %s tokens, wrapping native 44px targets and one focusable live region", async (mode) => {
    render(<IntegrationWorkflowScheduleDraftPicker {...props({ mode })} />);
    click("Load drafts"); await settle();
    const section = screen.getByRole("region", { name: "Schedule draft picker" });
    for (const [key, value] of Object.entries(tokenStyle(mode))) expect(section.style.getPropertyValue(key)).toBe(value);
    expect(section).toHaveStyle({ minWidth: "0", maxWidth: "100%", overflowWrap: "anywhere" });
    for (const button of screen.getAllByRole("button")) {
      expect(button).toHaveAttribute("type", "button"); expect(button).toHaveStyle({ minHeight: "44px" });
    }
    expect(row()).toHaveStyle({ width: "100%" });
    expect(screen.getAllByRole("status")).toHaveLength(1);
    expect(screen.getByRole("status")).toHaveAttribute("aria-live", "polite");
    expect(screen.getByRole("status")).toHaveAttribute("aria-atomic", "true");
    expect(screen.getByRole("status")).toHaveAttribute("tabindex", "-1");
  });
});
