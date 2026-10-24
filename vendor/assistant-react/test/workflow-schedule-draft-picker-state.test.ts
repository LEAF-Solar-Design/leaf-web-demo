import { describe, expect, it } from "vitest";
import {
  cloneScheduleDraftReference, parseScheduleDraftReferencePage, scheduleDraftPageMessage, scheduleDraftPickerCopy,
} from "../src/actions/workflow-schedule-draft-picker-state";

const reference = { id: "daily-review", version: 2, digest: "ab".repeat(32) };
const page = { references: [reference], nextCursor: null };

describe("schedule draft reference pages", () => {
  it("keeps exact canonical references and host order with independent copies", () => {
    const second = { ...reference, id: "earlier-in-host-order", version: 1 };
    const source = { references: [reference, second], nextCursor: "opaque cursor/+?=" };
    const parsed = parseScheduleDraftReferencePage(source)!;
    expect(parsed).toEqual(source);
    expect(parsed).not.toBe(source);
    expect(parsed.references).not.toBe(source.references);
    expect(parsed.references[0]).not.toBe(reference);
    const selected = cloneScheduleDraftReference(parsed.references[0]);
    expect(selected).toEqual(reference);
    selected.id = "host-mutation";
    expect(parsed.references[0].id).toBe(reference.id);
  });

  it("accepts both terminal and continuing empty pages, and the size boundaries", () => {
    for (const nextCursor of [null, "x", "x".repeat(2048)]) {
      expect(parseScheduleDraftReferencePage({ references: [], nextCursor })).toEqual({ references: [], nextCursor });
    }
    expect(parseScheduleDraftReferencePage({ references: Array.from({ length: 25 }, () => reference), nextCursor: null })?.references).toHaveLength(25);
    expect(scheduleDraftPageMessage({ references: [], nextCursor: null })).toBe(scheduleDraftPickerCopy.empty);
    expect(scheduleDraftPageMessage({ references: [], nextCursor: "next" })).toBe(scheduleDraftPickerCopy.emptyContinuation);
  });

  it.each([
    null, [], "private-error", {}, { references: [] }, { nextCursor: null },
    { ...page, title: "invented" }, { ...page, next_cursor: null },
    { ...page, references: null }, { ...page, references: {} },
    { ...page, references: Array.from({ length: 26 }, () => reference) },
    { ...page, references: [reference, { ...reference, digest: "invalid" }] },
    { ...page, references: [{ ...reference, title: "invented" }] },
    { ...page, references: [{ ...reference, version: 0 }] },
    { ...page, references: [{ ...reference, version: 1.5 }] },
    { ...page, references: [{ ...reference, id: "Invalid ID" }] },
    { ...page, nextCursor: "" }, { ...page, nextCursor: "x".repeat(2049) },
    { ...page, nextCursor: undefined }, { ...page, nextCursor: 1 },
  ])("rejects the entire malformed page %#", (value) => {
    expect(parseScheduleDraftReferencePage(value)).toBeUndefined();
  });

  it("rejects a nonadvancing continuation only against the requested cursor", () => {
    expect(parseScheduleDraftReferencePage({ ...page, nextCursor: "same" }, "same")).toBeUndefined();
    expect(parseScheduleDraftReferencePage({ ...page, nextCursor: "different" }, "same")).toBeTruthy();
    expect(parseScheduleDraftReferencePage(page, "same")).toEqual(page);
  });

  it("fails closed for throwing accessors and sparse reference arrays", () => {
    const throwing = { get references() { throw new Error("private"); }, nextCursor: null };
    expect(parseScheduleDraftReferencePage(throwing)).toBeUndefined();
    expect(parseScheduleDraftReferencePage({ references: new Array(1), nextCursor: null })).toBeUndefined();
  });
});
