import { useEffect, useState } from "react";
import type { CSSProperties } from "react";
import tokenFile from "./leaf-tokens.v1.json";

// The Leaf look (K-D10) comes from a package-local copy of contract/leaf-tokens.v1.json,
// kept equal by test/self-contained.test.ts so this package vendors on its own.
// The Mushy profile resolves shared roles; legacy-only keys retain their pinned values.
// Components reference `var(--leaf-*)`
// only; the colour values live in the token file and reach the DOM through
// tokenStyle() on the thread root, so no component carries a literal colour.

export type ColorMode = "light" | "dark";
export type ColorToken = keyof typeof tokenFile.color.light;
/** Tone of a state glyph or StateInk. Colour follows tone, never the other way round. */
export type StateTone = "neutral" | "progress" | "attention" | "critical" | "healthy" | "stale";

const MUSHY_PROFILE = tokenFile.profiles.mushy;

/** Map shared roles onto the existing assistant colour names. */
function resolvedColors(mode: ColorMode): Record<ColorToken, string> {
  const values: Record<ColorToken, string> = { ...tokenFile.color[mode] };
  const roles = MUSHY_PROFILE.color[mode];
  for (const [role, token] of Object.entries(tokenFile.roles.color)) {
    values[token as ColorToken] = roles[role as keyof typeof roles];
  }
  return values;
}

export const LEAF_TOKENS = {
  ...tokenFile,
  color: { light: resolvedColors("light"), dark: resolvedColors("dark") },
  font: MUSHY_PROFILE.font,
  radius: { ...tokenFile.radius, base: MUSHY_PROFILE.radius.control },
};
export const COLOR_TOKENS = Object.keys(tokenFile.color.light) as ColorToken[];

const COLOR_VALUE = /^#[0-9a-f]{6}$/;

/** Fails closed at import: a malformed or lopsided token file never renders. */
function assertTokenFile(): void {
  if (tokenFile.schema !== "leaf.tokens.v1") throw new Error(`unknown token schema ${String(tokenFile.schema)}`);
  const light = LEAF_TOKENS.color.light;
  const dark = LEAF_TOKENS.color.dark;
  if (Object.keys(dark).length !== COLOR_TOKENS.length) throw new Error("light and dark token sets differ");
  for (const token of COLOR_TOKENS) {
    if (!COLOR_VALUE.test(light[token] ?? "") || !COLOR_VALUE.test(dark[token] ?? "")) {
      throw new Error(`token ${token} is not a six-digit colour in both modes`);
    }
  }
  for (const token of tokenFile.series) {
    if (!(token in light)) throw new Error(`series token ${token} is not a colour token`);
  }
}
assertTokenFile();

export function cssVar(token: ColorToken): string {
  return `var(--leaf-${token})`;
}

/** `color["muted-foreground"]` is `var(--leaf-muted-foreground)`. */
export const color = Object.fromEntries(COLOR_TOKENS.map((token) => [token, cssVar(token)])) as Record<ColorToken, string>;
export const font = { sans: "var(--leaf-font-sans)", mono: "var(--leaf-font-mono)" } as const;
export const radius = "var(--leaf-radius)";
/** The series palette for charts, in order. */
export const SERIES: readonly string[] = tokenFile.series.map((token) => cssVar(token as ColorToken));
/** 44 px minimum for every interactive target. */
export const TARGET_PX: number = tokenFile.size.target_min_px;
export const REDUCED_MOTION_QUERY: string = tokenFile.motion.reduced_query;

/** The custom properties a thread root sets for one colour mode. */
export function tokenStyle(mode: ColorMode): CSSProperties {
  const values = LEAF_TOKENS.color[mode];
  const style: Record<string, string> = {};
  for (const token of COLOR_TOKENS) style[`--leaf-${token}`] = values[token];
  for (const [role, value] of Object.entries(MUSHY_PROFILE.color[mode])) style[`--leaf-color-${role}`] = value;
  for (const [role, value] of Object.entries(MUSHY_PROFILE.font)) style[`--leaf-font-${role}`] = value;
  for (const [role, value] of Object.entries(MUSHY_PROFILE.spacing)) style[`--leaf-spacing-${role}`] = value;
  for (const [role, value] of Object.entries(MUSHY_PROFILE.radius)) style[`--leaf-radius-${role}`] = value;
  style["--leaf-radius"] = LEAF_TOKENS.radius.base;
  return style as CSSProperties;
}

const TONE_TOKEN: Record<StateTone, ColorToken> = {
  neutral: "muted-foreground",
  progress: "foreground",
  attention: "state-aging",
  critical: "state-critical",
  healthy: "state-healthy",
  stale: "state-stale",
};

export function toneColor(tone: StateTone): string {
  return color[TONE_TOKEN[tone]];
}

// Shared style fragments. Every button spreads `target`, so no control is
// smaller than 44 px.
export const target: CSSProperties = { minHeight: TARGET_PX, minWidth: TARGET_PX };

export const eyebrow: CSSProperties = {
  fontSize: tokenFile.type.section_label.size_px,
  fontWeight: tokenFile.type.section_label.weight,
  lineHeight: tokenFile.type.section_label.line_height,
  letterSpacing: `${tokenFile.type.section_label.letter_spacing_em}em`,
  textTransform: "uppercase",
  color: color["muted-foreground"],
};

export const figure: CSSProperties = { fontFamily: font.mono, fontVariantNumeric: "tabular-nums" };

export const meta: CSSProperties = { fontSize: 13, color: color["muted-foreground"] };

const buttonBase: CSSProperties = {
  ...target,
  padding: "0 16px",
  borderRadius: radius,
  font: "inherit",
  fontWeight: 600,
  cursor: "pointer",
};

export const primaryButton: CSSProperties = {
  ...buttonBase,
  border: `1px solid ${color.primary}`,
  background: color.primary,
  color: color["primary-foreground"],
};

export const secondaryButton: CSSProperties = {
  ...buttonBase,
  border: `1px solid ${color.border}`,
  background: color.card,
  color: color.foreground,
};

export const panel: CSSProperties = {
  background: color.card,
  color: color["card-foreground"],
  border: `1px solid ${color.border}`,
  borderRadius: radius,
  padding: 16,
};

export const srOnly: CSSProperties = {
  position: "absolute",
  width: 1,
  height: 1,
  padding: 0,
  margin: -1,
  overflow: "hidden",
  clip: "rect(0, 0, 0, 0)",
  whiteSpace: "nowrap",
  border: 0,
};

/** The one stylesheet a thread root injects: focus ring, the progress pulse, and reduced motion honoured. */
export const BASE_CSS = `
.leaf-assistant { box-sizing: border-box; }
.leaf-assistant *, .leaf-assistant *::before, .leaf-assistant *::after { box-sizing: inherit; }
.leaf-assistant button:focus-visible, .leaf-assistant textarea:focus-visible, .leaf-assistant summary:focus-visible {
  outline: 2px solid var(--leaf-ring); outline-offset: 2px;
}
@keyframes leaf-assistant-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
.leaf-assistant-pulse { animation: leaf-assistant-pulse 1.2s ease-in-out infinite; }
@media ${tokenFile.motion.reduced_query} {
  .leaf-assistant *, .leaf-assistant *::before, .leaf-assistant *::after {
    animation-duration: ${tokenFile.motion.reduced_duration_ms}ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: ${tokenFile.motion.reduced_duration_ms}ms !important;
    scroll-behavior: auto !important;
  }
}
`;

function queryReducedMotion(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return false;
  return window.matchMedia(REDUCED_MOTION_QUERY).matches;
}

/** True when the user asked for reduced motion; JS-driven motion (smooth scroll) checks this, CSS motion is cut by BASE_CSS. */
export function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(queryReducedMotion);
  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return undefined;
    const query = window.matchMedia(REDUCED_MOTION_QUERY);
    const update = () => setReduced(query.matches);
    update();
    if (typeof query.addEventListener !== "function") return undefined;
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  return reduced;
}
