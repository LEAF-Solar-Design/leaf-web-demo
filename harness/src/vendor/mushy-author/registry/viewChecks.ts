/**
 * Static checks for `view` artifacts — the fragment half of validate-tool.
 *
 * A view is an authored HTML FRAGMENT rendered by a consumer shell inside a
 * sandboxed, self-sizing, theme-injected iframe.
 *
 * WHAT THIS FILE STILL OWNS, and why each one belongs in a string check:
 *
 * - PAGE SKELETON. The host wraps the fragment in its own document (theme
 *   tokens, CSP, the height-sync script), so `<html>`/`<head>`/`<body>` would
 *   nest documents. This is a fact about the SOURCE, not about rendering: by
 *   the time a browser has a DOM the nesting has already been silently
 *   repaired, so the browser cannot report it and the string must.
 * - EXTERNAL ORIGINS. The frame's CSP allowlist is empty and violations fail
 *   SILENTLY (no error, the asset is simply absent), which makes an external
 *   URL the single worst thing to debug on this surface. Rejecting the request
 *   at commit is more useful than observing an absence later.
 * - SOURCE LIMITS. Bytes and NUL. Nothing to render.
 *
 * WHAT MOVED OUT, 2026-08-13, and why it had to (operator-directed):
 *
 *   position: fixed, inner scrolling, and theme-tokens-only now live in
 *   tools/view-render-check/, decided from COMPUTED STYLE in a real browser.
 *
 * Those three are CSS SEMANTICS, and a regex over CSS text cannot decide them.
 * It lost five consecutive review rounds, each to a different evasion of the
 * same class, and each fix bought exactly one round:
 *
 *   1. `position/x*x*x: fixed`     a comment is not whitespace
 *   2. content:"/*" ... "*\/"       comment markers inside quoted strings made
 *                                   comment-stripping DELETE the live payload
 *   3. `color: red`                 ~148 named colours were never covered
 *   4. `posi\74 ion: fixed`         CSS escapes decode before matching
 *   5. `posi\74<CR>ion: fixed`      CR and form feed are escape terminators too
 *
 * Every one of those was verified in Chromium as applying exactly like its
 * plain spelling. The pattern is the finding: the text has unbounded ways to
 * spell the same computed result, so the computed result is the only honest
 * place to check it. Reading it there also closes the case no string analysis
 * could ever reach, a script assigning the style at runtime, which was
 * previously documented here as a permanent gap and is now simply covered.
 *
 * THE TRADE, stated plainly: these three rules now run in CI rather than at
 * commit, because deciding them needs a browser and the author loop has none.
 * A fragment violating them can therefore land in a consumer repo and be caught
 * by the `view-render-check` job rather than refused at submit. That is a real
 * weakening of "validation before commit" for these three, accepted knowingly:
 * a check that anyone can walk past with one comment was never enforcing them,
 * it was only reporting the careless cases. Run
 * `node check.mjs --repo <consumer repo>` anywhere a browser exists to get the
 * commit-time answer back.
 */

export const VIEW_ENTRY_BASENAME = "view.html";

/** The theme tokens a consumer shell must inject into the frame document. */
export const VIEW_THEME_TOKENS = [
  "--bg", "--card", "--ink", "--muted", "--accent", "--edge", "--me", "--mono",
] as const;

export const MAX_VIEW_SOURCE_BYTES = 512 * 1024;

const SKELETON = /<\/?\s*(?:html|head|body)[\s>/]|<!doctype/i;
const EXTERNAL_URL = /\b(?:https?|wss?|ftp):\/\//i;
// The quote is OPTIONAL. `<img src=//cdn.example/x.png>` is valid HTML and the
// earlier pattern required a quote, so it walked straight past (review
// 2026-08-13). Unquoted attribute values end at whitespace or `>`.
const PROTOCOL_RELATIVE = /(?:src|href)\s*=\s*(?:["']\s*)?\/\/|url\(\s*["']?\/\//i;
const EXTERNAL_LOADER = /<link\b|<script[^>]*\ssrc\s*=|@import\b/i;

// The rules above are still STRING rules, so they still face the evasions that
// drove the CSS rules out of this file. The normalisation those needed is kept
// for exactly that reason, and only for these.
//
// Comments: `/**/` is not whitespace, so it splits any `a: b` pattern while the
// browser honours the declaration. Stripping ALONE is unsafe in the other
// direction, because a regex cannot see strings and comment markers inside
// quoted values look like a comment, which deletes a live payload before the
// patterns run. So both copies are tested and a hit in either counts.
//
// Escapes: a CSS identifier may be written `\68 ttps` and the browser decodes
// it before matching, so the decoded text is tested too. The terminator is any
// CSS whitespace: input preprocessing folds CR, CRLF and FORM FEED to LF.
const CSS_COMMENT = /\/\*[\s\S]*?\*\//g;
const CSS_ESCAPE = /\\([0-9a-fA-F]{1,6})(?:\r\n|[ \t\n\r\f])?|\\([^\n])/g;

function stripCssComments(source: string): string {
  return source.replace(CSS_COMMENT, "");
}

function decodeCssEscapes(source: string): string {
  return source.replace(CSS_ESCAPE, (_m, hex: string | undefined, ch: string | undefined) => {
    if (hex === undefined) return ch ?? "";
    const cp = parseInt(hex, 16);
    if (!Number.isFinite(cp) || cp === 0 || cp > 0x10ffff) return "�";
    try { return String.fromCodePoint(cp); } catch { return "�"; }
  });
}

/** Every textual form the browser could end up resolving this source to. */
function scanVariants(source: string): string[] {
  const stripped = stripCssComments(source);
  return [...new Set([
    source,
    stripped,
    decodeCssEscapes(source),
    decodeCssEscapes(stripped),
    stripCssComments(decodeCssEscapes(source)),
  ])];
}

/**
 * Validate one view fragment. Returns human-readable diagnostics, each
 * prefixed with the rule name it enforces; empty === valid.
 *
 * An empty result does NOT mean the fragment is renderable: see the header.
 * position/overflow/colour are decided by tools/view-render-check.
 */
export function checkViewFragment(source: string): string[] {
  const errs: string[] = [];
  const bytes = Buffer.byteLength(source ?? "", "utf8");
  if (!source || !source.trim() || bytes > MAX_VIEW_SOURCE_BYTES || source.includes("\0")) {
    errs.push(`view/source-limits: fragment must be 1-${MAX_VIEW_SOURCE_BYTES} UTF-8 bytes with no NUL`);
    if (!source || !source.trim()) return errs;
  }

  const variants = scanVariants(source);
  const hit = (re: RegExp) => variants.some((v) => re.test(v));
  const show = (re: RegExp) => {
    for (const v of variants) {
      const m = v.match(re);
      if (m) return JSON.stringify(m[0]);
    }
    return JSON.stringify("");
  };

  if (hit(SKELETON)) {
    errs.push(`view/no-page-skeleton: fragment must not contain ${show(SKELETON)} — ` +
      "the host wraps it in its own document (theme, CSP, height sync)");
  }
  if (hit(EXTERNAL_URL) || hit(PROTOCOL_RELATIVE)) {
    const shown = hit(EXTERNAL_URL) ? show(EXTERNAL_URL) : show(PROTOCOL_RELATIVE);
    errs.push(`view/no-external-origins: ${shown} — the frame's CSP allowlist is empty and ` +
      "violations fail SILENTLY; inline all assets (data: URIs are fine)");
  }
  if (hit(EXTERNAL_LOADER)) {
    errs.push(`view/no-external-origins: ${show(EXTERNAL_LOADER)} — <link>, ` +
      "<script src>, and @import cannot load under the frame's CSP; inline styles and scripts");
  }
  return errs;
}
