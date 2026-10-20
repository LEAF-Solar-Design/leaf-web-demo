import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// Globals are off, so Testing Library cannot register its own cleanup.
afterEach(() => {
  cleanup();
});

// jsdom implements no layout; the stream calls scrollIntoView defensively anyway.
if (typeof Element !== "undefined" && typeof Element.prototype.scrollIntoView !== "function") {
  Element.prototype.scrollIntoView = () => {};
}
