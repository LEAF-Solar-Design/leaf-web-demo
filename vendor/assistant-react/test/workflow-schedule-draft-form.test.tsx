import React from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { IntegrationWorkflowScheduleDraftV1 } from "../../assistant-contract/src/integration";
import {
  IntegrationWorkflowScheduleDraftForm, IntegrationWorkflowScheduleDraftSaveError,
  type IntegrationWorkflowScheduleDraftFormProps,
} from "../src/actions";
import { scheduleDraftCopy } from "../src/actions/workflow-schedule-draft-form-state";
import { tokenStyle } from "../src/tokens";

const draft = IntegrationWorkflowScheduleDraftV1.parse({
  schema_version: 1, id: "daily-review", version: 2, title: "Daily review",
  workflow_ref: { id: "review", version: 3, digest: "ab".repeat(32) },
  schedule: { kind: "daily", timezone: "UTC", hour: 9, minute: 5,
    gap_policy: "next_valid_minute", fold_policy: "first_occurrence" }, state: "draft",
});
const ack = { id: draft.id, version: draft.version, digest: "cd".repeat(32) };
function props(overrides: Partial<IntegrationWorkflowScheduleDraftFormProps> = {}): IntegrationWorkflowScheduleDraftFormProps {
  return { initialDraft: draft, contextKey: "private-org/project/principal/auth-1", availability: "ready",
    onSave: vi.fn().mockResolvedValue(ack), ...overrides };
}
function deferred() {
  let resolve!: (value: unknown) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<unknown>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function field(name: string) { return screen.getByLabelText(name); }
function edit(name: string, value: string) { fireEvent.change(field(name), { target: { value } }); }
function submit() { fireEvent.submit(screen.getByRole("button", { name: "Save draft" }).closest("form")!); }
async function settle() { await act(async () => { await Promise.resolve(); }); }
function watchSaveDeadline() {
  const scheduled = vi.spyOn(globalThis, "setTimeout");
  const cleared = vi.spyOn(globalThis, "clearTimeout");
  return () => {
    const index = scheduled.mock.calls.findIndex((call) => call[1] === 45000);
    expect(index).toBeGreaterThanOrEqual(0);
    expect(cleared).toHaveBeenCalledWith(scheduled.mock.results[index].value);
  };
}
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers(); });

describe("inert schedule draft form", () => {
  it.each(["UTC", "America/New_York", "Etc/GMT+5"])("explicitly saves direct canonical values in %s and locks the acknowledged version", async (timezone) => {
    const host = deferred();
    const input = props({ onSave: vi.fn(() => host.promise) });
    render(<IntegrationWorkflowScheduleDraftForm {...input} />);
    expect(input.onSave).not.toHaveBeenCalled();
    expect(document.body.textContent).not.toContain(input.contextKey);
    expect(screen.getByText("Draft ID: daily-review. Version: 2.")).toBeTruthy();
    expect(screen.getByText("Workflow ID: review. Workflow version: 3.")).toBeTruthy();
    expect(screen.getByText(/If daylight saving time skips this minute/)).toBeTruthy();
    edit("Draft title", "Edited review"); edit("Timezone", timezone); edit("Hour (0 to 23)", "0"); edit("Minute (0 to 59)", "59");
    const form = screen.getByRole("button", { name: "Save draft" }).closest("form")!;
    act(() => { fireEvent.click(screen.getByRole("button", { name: "Save draft" })); fireEvent.submit(form); fireEvent.submit(form); });
    expect(input.onSave).toHaveBeenCalledTimes(1);
    expect(vi.mocked(input.onSave).mock.calls[0][0]).toEqual({ ...draft, title: "Edited review",
      schedule: { ...draft.schedule, timezone, hour: 0, minute: 59 } });
    expect(screen.getByRole("status")).toHaveTextContent(scheduleDraftCopy.pending);
    expect(screen.getByRole("status")).toHaveFocus();
    expect(field("Draft title")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save draft" })).toBeDisabled();
    await act(async () => host.resolve(ack));
    expect(screen.getByRole("status")).toHaveTextContent(scheduleDraftCopy.saved);
    expect(screen.getByRole("status")).toHaveFocus();
    expect(field("Timezone")).toHaveValue(timezone);
    submit(); expect(input.onSave).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "Retry save" })).toBeNull();
  });

  it.each([
    { contextKey: "" }, { contextKey: null }, { availability: "offline" }, { mode: "automatic" }, { onSave: null },
    { initialDraft: { ...draft, version: 0 } }, { initialDraft: { ...draft, title: "" } },
    { initialDraft: { ...draft, workflow_ref: { ...draft.workflow_ref, digest: "sentinel" } } },
    { initialDraft: { ...draft, schedule: { ...draft.schedule, timezone: "Bad Zone" } } },
    { initialDraft: { ...draft, state: "active" } },
  ])("rejects invalid runtime props without exposing input data: %j", (override) => {
    const input = props(override as unknown as Partial<IntegrationWorkflowScheduleDraftFormProps>);
    render(<IntegrationWorkflowScheduleDraftForm {...input} />);
    expect(screen.getByRole("status")).toHaveTextContent(scheduleDraftCopy.unavailable);
    expect(screen.queryByRole("button")).toBeNull();
    expect(document.body.textContent).not.toContain("sentinel");
    if (typeof input.onSave === "function") expect(input.onSave).not.toHaveBeenCalled();
  });

  it.each([["Draft title", ""], ["Draft title", "a".repeat(121)], ["Timezone", "Bad Zone"],
    ["Hour (0 to 23)", ""], ["Hour (0 to 23)", "1.5"], ["Hour (0 to 23)", "0x10"], ["Minute (0 to 59)", "60"]])(
    "associates errors and focuses invalid %s=%j", (label, value) => {
      const input = props(); render(<IntegrationWorkflowScheduleDraftForm {...input} />);
      edit(label, value); submit();
      expect(input.onSave).not.toHaveBeenCalled();
      expect(field(label)).toHaveFocus();
      expect(field(label)).toHaveAttribute("aria-invalid", "true");
      const errorId = field(label).getAttribute("aria-describedby")!.split(" ").at(-1)!;
      expect(document.getElementById(errorId)?.textContent).toBeTruthy();
    });

  it("focuses the first invalid field in form order", () => {
    render(<IntegrationWorkflowScheduleDraftForm {...props()} />);
    edit("Draft title", ""); edit("Timezone", ""); edit("Hour (0 to 23)", ""); submit();
    expect(field("Draft title")).toHaveFocus();
  });

  it("handles a synchronous host throw as an unknown outcome", () => {
    const input = props({ onSave: vi.fn(() => { throw new Error("PRIVATE_SENTINEL"); }) });
    render(<IntegrationWorkflowScheduleDraftForm {...input} />); submit();
    expect(screen.getByRole("status")).toHaveTextContent(scheduleDraftCopy.unknown);
    expect(field("Draft title")).toBeDisabled();
    expect(document.body.textContent).not.toContain("PRIVATE_SENTINEL");
  });

  it.each([new Error("PRIVATE_SENTINEL"), "PRIVATE_SENTINEL", { code: "invalid_timezone", message: "PRIVATE_SENTINEL" }])(
    "keeps ordinary rejections safe and immutable despite callback mutation", async (thrown) => {
      const onSave = vi.fn().mockImplementationOnce((value: IntegrationWorkflowScheduleDraftV1) => {
        value.title = "mutated"; value.schedule.timezone = "mutated"; value.workflow_ref.id = "mutated";
        return Promise.reject(thrown);
      }).mockResolvedValueOnce(ack);
      render(<IntegrationWorkflowScheduleDraftForm {...props({ onSave })} />);
      submit(); await settle();
      expect(screen.getByRole("status")).toHaveTextContent(scheduleDraftCopy.unknown);
      expect(document.body.textContent).not.toContain("PRIVATE_SENTINEL");
      expect(field("Draft title")).toBeDisabled(); expect(field("Draft title")).toHaveValue(draft.title);
      expect(field("Timezone")).toHaveValue("UTC");
      fireEvent.click(screen.getByRole("button", { name: "Retry save" })); await settle();
      expect(onSave.mock.calls[1][0]).toEqual(draft);
      expect(onSave.mock.calls[1][0]).not.toBe(onSave.mock.calls[0][0]);
      expect(onSave.mock.calls[1][1]).not.toBe(onSave.mock.calls[0][1]);
      expect(screen.getByRole("status")).toHaveTextContent(scheduleDraftCopy.saved);
    });

  it.each([null, { ...ack, version: 99 }, { ...ack, id: "other" }, { ...ack, digest: "PRIVATE_SENTINEL" }])(
    "treats invalid acknowledgement as unknown with identical explicit retry", async (response) => {
      const onSave = vi.fn().mockResolvedValueOnce(response).mockResolvedValueOnce(ack);
      render(<IntegrationWorkflowScheduleDraftForm {...props({ onSave })} />);
      submit(); await settle();
      expect(screen.getByRole("status")).toHaveTextContent(scheduleDraftCopy.unknown);
      expect(document.body.textContent).not.toContain("PRIVATE_SENTINEL");
      fireEvent.click(screen.getByRole("button", { name: "Retry save" })); await settle();
      expect(onSave.mock.calls[1][0]).toEqual(onSave.mock.calls[0][0]);
    });

  it("aborts at 45 seconds, ignores late success, and never retries automatically", async () => {
    vi.useFakeTimers(); const host = deferred(); const input = props({ onSave: vi.fn(() => host.promise) });
    render(<IntegrationWorkflowScheduleDraftForm {...input} />); submit();
    act(() => vi.advanceTimersByTime(44999));
    expect(screen.getByRole("status")).toHaveTextContent(scheduleDraftCopy.pending);
    act(() => vi.advanceTimersByTime(1));
    expect(vi.mocked(input.onSave).mock.calls[0][1].aborted).toBe(true);
    expect(screen.getByRole("status")).toHaveTextContent(scheduleDraftCopy.unknown);
    expect(screen.getByRole("status")).toHaveFocus();
    await act(async () => host.resolve(ack));
    act(() => vi.advanceTimersByTime(90000));
    expect(input.onSave).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("status")).toHaveTextContent(scheduleDraftCopy.unknown);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("permits correction only after trusted unsupported timezone refusal", async () => {
    const onSave = vi.fn().mockRejectedValueOnce(new IntegrationWorkflowScheduleDraftSaveError("invalid_timezone")).mockResolvedValueOnce(ack);
    render(<IntegrationWorkflowScheduleDraftForm {...props({ onSave })} />);
    edit("Timezone", "Not/A_Host_Zone"); submit(); await settle();
    expect(field("Timezone")).toHaveFocus(); expect(field("Timezone")).toBeEnabled();
    expect(field("Timezone")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("status")).toHaveTextContent(scheduleDraftCopy.invalid_timezone);
    edit("Timezone", "America/New_York"); submit(); await settle();
    expect(onSave.mock.calls[1][0].schedule.timezone).toBe("America/New_York");
    expect(screen.getByRole("status")).toHaveTextContent(scheduleDraftCopy.saved);
  });

  it("does not unlock a possibly persisted version on a later timezone refusal", async () => {
    const onSave = vi.fn().mockRejectedValueOnce(new Error()).mockRejectedValueOnce(new IntegrationWorkflowScheduleDraftSaveError("invalid_timezone"));
    render(<IntegrationWorkflowScheduleDraftForm {...props({ onSave })} />); submit(); await settle();
    fireEvent.click(screen.getByRole("button", { name: "Retry save" })); await settle();
    expect(screen.getByRole("status")).toHaveTextContent(scheduleDraftCopy.unknown);
    expect(field("Timezone")).toBeDisabled();
  });

  it("locks trusted conflict without retry or overwrite", async () => {
    const input = props({ onSave: vi.fn().mockRejectedValue(new IntegrationWorkflowScheduleDraftSaveError("conflict")) });
    render(<IntegrationWorkflowScheduleDraftForm {...input} />); submit(); await settle();
    expect(screen.getByRole("status")).toHaveTextContent(scheduleDraftCopy.conflict);
    expect(screen.getByRole("status")).toHaveFocus();
    expect(field("Timezone")).toBeDisabled(); expect(screen.queryByRole("button", { name: "Retry save" })).toBeNull();
    submit(); expect(input.onSave).toHaveBeenCalledTimes(1);
  });

  it.each(["unavailable", "forbidden"] as const)("aborts on %s and ignores an old completion after readiness returns", async (availability) => {
    const first = deferred(); const second = deferred();
    const onSave = vi.fn().mockImplementationOnce(() => first.promise).mockImplementationOnce(() => second.promise);
    const input = props({ onSave }); const view = render(<IntegrationWorkflowScheduleDraftForm {...input} />);
    submit(); view.rerender(<IntegrationWorkflowScheduleDraftForm {...input} availability={availability} />);
    expect(onSave.mock.calls[0][1].aborted).toBe(true);
    expect(screen.getAllByRole("status")).toHaveLength(1);
    expect(screen.getByRole("status")).toHaveTextContent(scheduleDraftCopy[availability]);
    expect(screen.getByRole("button", { name: "Retry save" })).toBeDisabled();
    view.rerender(<IntegrationWorkflowScheduleDraftForm {...input} />);
    expect(onSave).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Retry save" }));
    await act(async () => first.resolve(ack));
    expect(screen.getByRole("status")).toHaveTextContent(scheduleDraftCopy.pending);
    expect(onSave.mock.calls[1][0]).toEqual(draft);
    await act(async () => second.resolve(ack));
    expect(screen.getByRole("status")).toHaveTextContent(scheduleDraftCopy.saved);
  });

  it("retains edits on equivalent recreated props and resets synchronously on canonical change", async () => {
    const host = deferred(); const input = props({ onSave: vi.fn(() => host.promise) });
    const view = render(<IntegrationWorkflowScheduleDraftForm {...input} />);
    edit("Draft title", "Retained edit");
    view.rerender(<IntegrationWorkflowScheduleDraftForm {...input} initialDraft={JSON.parse(JSON.stringify(draft))} />);
    expect(field("Draft title")).toHaveValue("Retained edit");
    submit(); const signal = vi.mocked(input.onSave).mock.calls[0][1];
    view.rerender(<IntegrationWorkflowScheduleDraftForm {...input} initialDraft={{ ...draft, version: 3, title: "New version" }} />);
    expect(signal.aborted).toBe(true); expect(field("Draft title")).toHaveValue("New version");
    expect(field("Draft title")).toBeEnabled();
    await act(async () => host.resolve(ack));
    expect(screen.getByRole("status")).toHaveTextContent(scheduleDraftCopy.ready);
    expect(input.onSave).toHaveBeenCalledTimes(1);
  });

  it("keeps a saved version locked on equivalent rerender and allows an explicit save of a new host version", async () => {
    const onSave = vi.fn().mockImplementation((value: IntegrationWorkflowScheduleDraftV1) =>
      Promise.resolve({ ...ack, id: value.id, version: value.version }));
    const input = props({ onSave }); const view = render(<IntegrationWorkflowScheduleDraftForm {...input} />);
    submit(); await settle();
    view.rerender(<IntegrationWorkflowScheduleDraftForm {...input} initialDraft={JSON.parse(JSON.stringify(draft))} />);
    submit(); expect(onSave).toHaveBeenCalledTimes(1);
    view.rerender(<IntegrationWorkflowScheduleDraftForm {...input} initialDraft={{ ...draft, version: 3 }} />);
    expect(onSave).toHaveBeenCalledTimes(1);
    submit(); await settle();
    expect(onSave.mock.calls[1][0].version).toBe(3);
  });

  it("remains idle and preserves cancellation under React StrictMode", () => {
    vi.useFakeTimers(); const host = deferred(); const input = props({ onSave: vi.fn(() => host.promise) });
    const expectDeadlineCleared = watchSaveDeadline();
    const view = render(<React.StrictMode><IntegrationWorkflowScheduleDraftForm {...input} /></React.StrictMode>);
    act(() => vi.advanceTimersByTime(90000));
    expect(input.onSave).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
    submit(); expect(input.onSave).toHaveBeenCalledTimes(1);
    view.unmount();
    expect(vi.mocked(input.onSave).mock.calls[0][1].aborted).toBe(true);
    expectDeadlineCleared();
    act(() => vi.advanceTimersByTime(45000));
    expect(input.onSave).toHaveBeenCalledTimes(1);
  });

  it.each(["context", "invalid", "unmount"])("cancels and clears timers on %s transition", async (transition) => {
    vi.useFakeTimers(); const host = deferred(); const input = props({ onSave: vi.fn(() => host.promise) });
    const expectDeadlineCleared = watchSaveDeadline();
    const view = render(<IntegrationWorkflowScheduleDraftForm {...input} />);
    edit("Draft title", "Old private input"); submit();
    if (transition === "context") view.rerender(<IntegrationWorkflowScheduleDraftForm {...input} contextKey="different-scope" />);
    else if (transition === "invalid") view.rerender(<IntegrationWorkflowScheduleDraftForm {...input} contextKey="" />);
    else view.unmount();
    expect(vi.mocked(input.onSave).mock.calls[0][1].aborted).toBe(true);
    expectDeadlineCleared();
    act(() => vi.advanceTimersByTime(45000));
    await act(async () => host.reject(new Error("PRIVATE_SENTINEL")));
    expect(document.body.textContent).not.toContain("Old private input");
    expect(document.body.textContent).not.toContain("PRIVATE_SENTINEL");
    expect(input.onSave).toHaveBeenCalledTimes(1);
  });

  it.each(["light", "dark"] as const)("uses %s tokens, semantic controls, one live region and single-column sizing", (mode) => {
    render(<IntegrationWorkflowScheduleDraftForm {...props({ mode })} />);
    const section = screen.getByRole("region", { name: "Schedule draft" });
    for (const [key, value] of Object.entries(tokenStyle(mode))) expect(section.style.getPropertyValue(key)).toBe(value);
    expect(section).toHaveStyle({ minWidth: "0", maxWidth: "100%", overflowWrap: "anywhere" });
    expect(screen.getByRole("button", { name: "Save draft" })).toHaveStyle({ minHeight: "44px" });
    expect(screen.getAllByRole("textbox")).toHaveLength(4);
    for (const control of screen.getAllByRole("textbox")) expect(control).toHaveStyle({ width: "100%", minWidth: "0" });
    expect(screen.getAllByRole("status")).toHaveLength(1);
    expect(screen.getByRole("status")).toHaveAttribute("aria-live", "polite");
    expect(screen.getByRole("status")).toHaveAttribute("aria-atomic", "true");
    expect(screen.getByRole("status")).toHaveAttribute("tabindex", "-1");
  });
});
