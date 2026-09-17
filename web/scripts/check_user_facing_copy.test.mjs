import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { scanSource, main } from './check_user_facing_copy.mjs';

const markersOf = (source, rel = 'src/X.jsx') => scanSource(source, rel).map((h) => h.marker);

test('each marker in JSX text is a hit', () => {
  const cases = [
    ['PRICING PLACEHOLDER', 'placeholder'],
    ['Todo: write this', 'todo'],
    ['Price TBD', 'tbd'],
    ['fixme later', 'fixme'],
    ['Lorem ipsum dolor', 'lorem'],
    ['Call XXX today', 'xxx'],
    ['WIP section', 'wip'],
    ['survey n=18', 'n='],
    ['Fast — and honest', 'em dash'],
    ['2018–2026 releases', 'en dash'],
  ];
  for (const [copy, marker] of cases) {
    assert.deepEqual(markersOf(`const A = () => <p>${copy}</p>;\n`), [marker], copy);
  }
});

test('clean JSX text is not a hit, and a word containing a marker is not either', () => {
  assert.deepEqual(markersOf('const A = () => <p>Wipe the todos list, then n=x</p>;\n'), []);
});

test('placeholder attribute value is a hit, the attribute name alone is not', () => {
  assert.deepEqual(markersOf('<input placeholder="Placeholder text" />\n'), ['placeholder']);
  assert.deepEqual(markersOf('<input placeholder="Search tools" />\n'), []);
});

test('object key literal values are scanned', () => {
  assert.deepEqual(markersOf("const S = [{ label: 'Pricing TODO', hint: 'ok' }];\n"), ['todo']);
});

test('em dash in JSX text reports path, line and column', () => {
  const hits = scanSource('const A = () => (\n  <p>Leaf — CAD</p>\n);\n', 'src/site/A.jsx');
  assert.equal(hits.length, 1);
  assert.deepEqual(
    { file: hits[0].file, line: hits[0].line, col: hits[0].col, marker: hits[0].marker, text: hits[0].text },
    { file: 'src/site/A.jsx', line: 2, col: 6, marker: 'em dash', text: 'Leaf — CAD' },
  );
});

test('an expression-only child is not a hit', () => {
  assert.deepEqual(markersOf("const A = () => <p>{value || 'TODO — later'}</p>;\n"), []);
});

test('a template literal with ${ is skipped', () => {
  assert.deepEqual(markersOf('<div title={`TODO ${name}`} />\n'), []);
  assert.deepEqual(markersOf('<div title={`TODO name`} />\n'), ['todo']);
});

test('code comments are not copy', () => {
  assert.deepEqual(markersOf('const A = () => (\n  <div>\n    {/* TODO — later */}\n  </div>\n);\n// TODO\n'), []);
});

function fixture(files, entries) {
  const root = mkdtempSync(join(tmpdir(), 'copy-lint-'));
  for (const [rel, body] of Object.entries(files)) {
    const full = join(root, rel);
    mkdirSync(join(full, '..'), { recursive: true });
    writeFileSync(full, body);
  }
  const allowlistPath = join(root, 'allowlist.json');
  writeFileSync(allowlistPath, JSON.stringify({ entries }));
  return { root, allowlistPath };
}

function run(files, entries) {
  const { root, allowlistPath } = fixture(files, entries);
  const lines = [];
  try {
    const code = main({ root, allowlistPath, log: (line) => lines.push(line) });
    return { code, lines };
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}

test('main fails on a hit and passes on a clean tree', () => {
  const dirty = run({ 'src/site/Hero.jsx': 'export const H = () => <h1>PRICING PLACEHOLDER</h1>;\n' }, []);
  assert.equal(dirty.code, 1);
  assert.match(dirty.lines[0], /^src\/site\/Hero\.jsx:1:28 {2}placeholder {2}"PRICING PLACEHOLDER"$/);
  const clean = run({ 'src/site/Hero.jsx': 'export const H = () => <h1>Leaf Automation</h1>;\n' }, []);
  assert.equal(clean.code, 0);
});

test('test, spec, fixtures and e2e files are not scanned', () => {
  const body = 'export const H = () => <h1>TODO</h1>;\n';
  const result = run(
    { 'src/Hero.test.jsx': body, 'src/Hero.spec.jsx': body, 'src/fixtures/H.jsx': body, 'src/e2e/H.jsx': body },
    [],
  );
  assert.equal(result.code, 0);
});

test('allowlist suppresses an exact match', () => {
  const result = run(
    { 'src/code/Sample.jsx': 'export const S = () => <code>TODO</code>;\n' },
    [{ file: 'src/code/Sample.jsx', text: 'TODO', reason: 'a code sample rendered as code' }],
  );
  assert.equal(result.code, 0, result.lines.join('\n'));
});

test('a stale allowlist entry is a failure', () => {
  const result = run(
    { 'src/code/Sample.jsx': 'export const S = () => <code>clean</code>;\n' },
    [{ file: 'src/code/Sample.jsx', text: 'TODO', reason: 'was a code sample' }],
  );
  assert.equal(result.code, 1);
  assert.ok(result.lines.some((line) => line.includes('stale allowlist entry')));
});

test('a non-UTF-8 file fails closed', () => {
  const result = run({ 'src/Bad.jsx': Buffer.from([0x3c, 0x70, 0x3e, 0xff, 0xfe, 0x3c, 0x2f, 0x70, 0x3e]) }, []);
  assert.equal(result.code, 1);
  assert.ok(result.lines.some((line) => line.includes('unreadable')));
});
