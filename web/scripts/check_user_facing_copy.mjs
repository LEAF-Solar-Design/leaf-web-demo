#!/usr/bin/env node
// User-facing copy lint (learning UI-L7). Fails CI when copy that reaches a
// customer carries a placeholder marker, a TODO, lorem ipsum, statistics
// notation (n=18) or an em or en dash. No parser and no dependency: bounded
// regular expressions over web/src and web/index.html, deterministic order.
// Fails closed: an unreadable or non-UTF-8 file counts as a hit, and an
// allowlist entry that matches nothing is itself a failure.
import { readFileSync, readdirSync, statSync, existsSync } from 'node:fs';
import { join, relative, dirname, extname, sep, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
export const WEB_ROOT = resolve(HERE, '..');
export const DEFAULT_ALLOWLIST = join(HERE, 'user_facing_copy_allowlist.json');

export const MAX_FILES = 5000;
export const MAX_FILE_BYTES = 1024 * 1024;
const MAX_REPORT_CHARS = 120;
const SCAN_EXTENSIONS = new Set(['.jsx', '.tsx', '.js', '.mjs', '.html']);
const SKIP_DIRS = new Set(['node_modules', 'dist', '__fixtures__', 'fixtures', 'e2e']);

const ATTRIBUTE_NAMES = ['aria-label', 'helperText', 'placeholder', 'description', 'tooltip', 'title', 'label', 'hint', 'alt'];
const OBJECT_KEYS = ['description', 'caption', 'summary', 'reason', 'status', 'effect', 'label', 'title', 'name', 'hint', 'text'];

// A quoted literal: double, single, or backtick. Escapes are honored; a
// string never spans a newline unless it is a backtick literal.
const LITERAL = String.raw`("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|` + '`(?:[^`\\\\]|\\\\.)*`)';
const ATTRIBUTE_RE = new RegExp(
  String.raw`(?<![\w-])(?:${ATTRIBUTE_NAMES.join('|')})\s*=\s*(?:\{\s*)?` + LITERAL,
  'g',
);
const OBJECT_KEY_RE = new RegExp(
  String.raw`(?<![\w$.-])(["']?)(?:${OBJECT_KEYS.join('|')})\1\s*:\s*` + LITERAL,
  'g',
);
// JSX text: a tag close (not an arrow, not a spaced comparison) up to the
// next tag open. Bounded to 2000 chars so a stray `>` cannot swallow a file.
const JSX_TEXT_RE = /(?<![\s=\-])>([^<>]{1,2000})<(?=[A-Za-z/>])/g;

const MARKERS = [
  { name: 'placeholder', re: /\bplaceholder\b/i },
  { name: 'todo', re: /\btodo\b/i },
  { name: 'tbd', re: /\btbd\b/i },
  { name: 'fixme', re: /\bfixme\b/i },
  { name: 'lorem', re: /\blorem\b/i },
  { name: 'xxx', re: /\bxxx\b/i },
  { name: 'wip', re: /\bwip\b/i },
  { name: 'n=', re: /\bn=\d/i },
  { name: 'em dash', re: /—|&mdash;|&#8212;|&#x2014;|\\u2014/i },
  { name: 'en dash', re: /–|&ndash;|&#8211;|&#x2013;|\\u2013/i },
];

// Blank comments to spaces (newlines kept) so offsets, lines and columns stay
// true and a `// TODO` in code is never read as JSX text.
function blankComments(text, isHtml) {
  const blank = (m) => m.replace(/[^\n]/g, ' ');
  let out = text.replace(/<!--[\s\S]*?-->/g, blank);
  if (!isHtml) {
    out = out
      .replace(/(^|[\s{])\/\*[\s\S]*?\*\//g, (m, lead) => lead + blank(m.slice(lead.length)))
      .replace(/(^|[\s;{}(),])\/\/[^\n]*/gm, (m, lead) => lead + blank(m.slice(lead.length)));
  }
  return out;
}

function lineStarts(text) {
  const starts = [0];
  for (let i = 0; i < text.length; i += 1) if (text.charCodeAt(i) === 10) starts.push(i + 1);
  return starts;
}

function position(starts, offset) {
  let lo = 0;
  let hi = starts.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (starts[mid] <= offset) lo = mid;
    else hi = mid - 1;
  }
  return { line: lo + 1, col: offset - starts[lo] + 1 };
}

/** Candidate user-facing strings in one source text: [{offset, text}], sorted, one per offset. */
export function extractStrings(text, relPath = '') {
  const isHtml = relPath.endsWith('.html');
  const source = blankComments(text, isHtml);
  const found = new Map();

  for (const m of source.matchAll(JSX_TEXT_RE)) {
    const raw = m[1];
    const prose = stripExpressions(raw);
    // A leftover brace or arrow means the span is JavaScript between two
    // tags, not JSX text (JSX text cannot hold a raw brace).
    if (prose === null || !/[A-Za-z]/.test(prose)) continue;
    const trimmed = raw.trim();
    if (!trimmed) continue;
    const offset = m.index + 1 + raw.indexOf(trimmed);
    if (!found.has(offset)) found.set(offset, { text: trimmed, prose });
  }

  for (const re of [ATTRIBUTE_RE, OBJECT_KEY_RE]) {
    re.lastIndex = 0;
    for (const m of source.matchAll(re)) {
      const literal = m[m.length - 1];
      if (literal.startsWith('`') && literal.includes('${')) continue;
      const value = literal.slice(1, -1);
      if (!/[A-Za-z–—]/.test(value) && !/&[mn]dash;|\\u201[34]/i.test(value)) continue;
      const offset = m.index + m[0].length - literal.length;
      if (!found.has(offset)) found.set(offset, { text: value, prose: value });
    }
  }

  return [...found.entries()].sort((a, b) => a[0] - b[0]).map(([offset, s]) => ({ offset, ...s }));
}

// Remove `{...}` expression containers (nested up to 8 deep). Returns null
// when what is left still looks like code: a stray brace or an arrow.
function stripExpressions(raw) {
  let out = raw;
  for (let depth = 0; depth < 8; depth += 1) {
    const next = out.replace(/\{[^{}]*\}/g, ' ');
    if (next === out) break;
    out = next;
  }
  if (/[{}]|=>/.test(out)) return null;
  return out;
}

/** Lint hits in one source text: [{file, line, col, marker, text}]. */
export function scanSource(text, relPath = '') {
  const starts = lineStarts(text);
  const hits = [];
  for (const { offset, text: value, prose } of extractStrings(text, relPath)) {
    for (const marker of MARKERS) {
      if (!marker.re.test(prose)) continue;
      hits.push({ file: relPath, ...position(starts, offset), marker: marker.name, text: value });
    }
  }
  return hits;
}

function listFiles(root) {
  const files = [];
  let overflow = false;
  const walk = (dir) => {
    if (overflow) return;
    let entries;
    try {
      entries = readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    entries.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
    for (const entry of entries) {
      if (entry.isSymbolicLink()) continue;
      const full = join(dir, entry.name);
      if (entry.isDirectory()) {
        if (!SKIP_DIRS.has(entry.name)) walk(full);
      } else if (entry.isFile()) {
        if (!SCAN_EXTENSIONS.has(extname(entry.name))) continue;
        if (entry.name.includes('.test.') || entry.name.includes('.spec.')) continue;
        if (files.length >= MAX_FILES) {
          overflow = true;
          return;
        }
        files.push(full);
      }
    }
  };
  walk(join(root, 'src'));
  const indexHtml = join(root, 'index.html');
  if (!overflow && existsSync(indexHtml)) {
    if (files.length >= MAX_FILES) overflow = true;
    else files.push(indexHtml);
  }
  return { files, overflow };
}

function loadAllowlist(path) {
  const parsed = JSON.parse(readFileSync(path, 'utf8'));
  if (!parsed || !Array.isArray(parsed.entries)) throw new Error('allowlist must be {"entries": [...]}');
  return parsed.entries.map((entry, index) => {
    if (!entry || typeof entry.file !== 'string' || typeof entry.text !== 'string' || typeof entry.reason !== 'string' || !entry.reason.trim()) {
      throw new Error(`allowlist entry ${index} needs string file, text and a non-empty reason`);
    }
    return { file: entry.file, text: entry.text, reason: entry.reason, used: false };
  });
}

function clip(value) {
  const flat = value.replace(/\s+/g, ' ');
  return flat.length > MAX_REPORT_CHARS ? `${flat.slice(0, MAX_REPORT_CHARS - 3)}...` : flat;
}

/** Scan root/src and root/index.html against the allowlist. Returns the exit code (0 clean, 1 failure). */
export function main({ root = WEB_ROOT, allowlistPath = DEFAULT_ALLOWLIST, log = console.log } = {}) {
  let allowlist;
  try {
    allowlist = loadAllowlist(allowlistPath);
  } catch (error) {
    log(`check:copy: cannot read allowlist ${allowlistPath}: ${error.message}`);
    return 1;
  }

  const { files, overflow } = listFiles(root);
  const decoder = new TextDecoder('utf-8', { fatal: true });
  let stringCount = 0;
  let failures = 0;
  let suppressed = 0;

  if (overflow) {
    log(`check:copy: more than ${MAX_FILES} files under ${root}; refusing a partial scan`);
    failures += 1;
  }

  for (const full of files) {
    const rel = relative(root, full).split(sep).join('/');
    let text;
    try {
      if (statSync(full).size > MAX_FILE_BYTES) continue;
      text = decoder.decode(readFileSync(full));
    } catch (error) {
      log(`${rel}:1:1  unreadable  "${clip(String(error.message))}"`);
      failures += 1;
      continue;
    }
    stringCount += extractStrings(text, rel).length;
    for (const hit of scanSource(text, rel)) {
      const entry = allowlist.find((e) => e.file === hit.file && e.text === hit.text);
      if (entry) {
        entry.used = true;
        suppressed += 1;
        continue;
      }
      log(`${hit.file}:${hit.line}:${hit.col}  ${hit.marker}  "${clip(hit.text)}"`);
      failures += 1;
    }
  }

  for (const entry of allowlist) {
    if (entry.used) continue;
    log(`${entry.file}  stale allowlist entry  "${clip(entry.text)}"`);
    failures += 1;
  }

  log(`check:copy: ${files.length} files, ${stringCount} strings, ${failures} hits (${suppressed} allowlisted)`);
  return failures > 0 ? 1 : 0;
}

if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
  process.exitCode = main();
}
