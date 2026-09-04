// @vitest-environment jsdom
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import LiveRegion, {
  HIDE_WITH_CLASS,
  HIDE_WITH_STYLE,
  SR_ONLY_STYLE,
} from "./LiveRegion.jsx";

afterEach(cleanup);

const sites = [
  {
    name: "App golden path",
    props: { role: "status", visuallyHidden: HIDE_WITH_STYLE },
    tag: "DIV",
    role: "status",
    atomic: null,
    className: "",
    label: null,
    hidden: "style",
  },
  {
    name: "ToolCast run status",
    props: {
      role: "status",
      visuallyHidden: HIDE_WITH_CLASS,
      atomic: true,
      label: "Run status announcements",
    },
    tag: "DIV",
    role: "status",
    atomic: "true",
    className: "sr-only",
    label: "Run status announcements",
    hidden: "class",
  },
  {
    name: "ConversePanel log",
    props: {
      role: "log",
      atomic: false,
      className: "converse-log",
      label: "Assistant conversation",
    },
    tag: "DIV",
    role: "log",
    atomic: "false",
    className: "converse-log",
    label: "Assistant conversation",
    hidden: null,
  },
  {
    name: "AnnotationDecisionCard",
    props: { as: "p", className: "dim" },
    tag: "P",
    role: null,
    atomic: null,
    className: "dim",
    label: null,
    hidden: null,
  },
  {
    name: "EditSurface",
    props: { as: "p", role: "status" },
    tag: "P",
    role: "status",
    atomic: null,
    className: "",
    label: null,
    hidden: null,
  },
  {
    name: "CadEditSurface",
    props: { as: "p", role: "status" },
    tag: "P",
    role: "status",
    atomic: null,
    className: "",
    label: null,
    hidden: null,
  },
];

describe("LiveRegion six-site attribute parity", () => {
  it.each(sites)("$name preserves its exact accessibility wire", (site) => {
    const { container } = render(
      <LiveRegion {...site.props}>announcement</LiveRegion>,
    );
    const region = container.firstElementChild;

    expect(region.tagName).toBe(site.tag);
    expect(region.getAttribute("role")).toBe(site.role);
    expect(region.getAttribute("aria-live")).toBe("polite");
    expect(region.getAttribute("aria-atomic")).toBe(site.atomic);
    expect(region.getAttribute("aria-label")).toBe(site.label);
    expect(region.className).toBe(site.className);
    expect(region.textContent).toBe("announcement");

    if (site.hidden === "style") {
      expect(region.style.position).toBe("absolute");
      expect(region.style.width).toBe("1px");
      expect(region.style.height).toBe("1px");
      expect(region.style.overflow).toBe("hidden");
      // jsdom drops the deprecated CSS `clip` property while parsing. Pin the
      // exact exported value as well as the rendered properties it supports.
      expect(SR_ONLY_STYLE.clip).toBe("rect(0 0 0 0)");
    } else {
      expect(region.getAttribute("style")).toBeNull();
    }
  });
});
