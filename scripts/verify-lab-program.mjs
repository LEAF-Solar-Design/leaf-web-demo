#!/usr/bin/env node
// Offline evidence consistency only. Never emit paths, payloads or error text.
import { constants } from 'node:fs';
import { lstat, open } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { resolve, relative, parse, join, sep, win32 } from 'node:path';
import { fileURLToPath } from 'node:url';

const LIMIT = 256 * 1024;
const slices = ['SEL-008', 'SEL-107', 'SEL-112', 'SEL-213', 'SEL-220', 'SEL-221', 'SEL-316', 'SEL-403'];
const headings = ['Program dossier', 'Save, conflict, replay and commit signals',
  'Stopping the worker', 'Paging', 'Production migration order',
  'Ratification and production gates', 'Receipts', 'Glossary'];
const entryFields = ['id', 'slice', 'receipt_path', 'receipt_sha256', 'limits',
  'identity', 'rollback', 'operator_steps', 'gate'];
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const exact = (value, fields) => object(value) && Object.keys(value).length === fields.length
  && fields.every(field => Object.hasOwn(value, field));
const text = value => typeof value === 'string' && value.trim().length > 0;
const digest = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const safePath = value => text(value) && !value.includes('\\') && !value.includes('\0')
  && !value.includes(':') && !value.includes('%') && !win32.isAbsolute(value)
  && !value.startsWith('/') && value.split('/').every(part => part && part !== '.' && part !== '..');
const uuid = /\b[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\b/i;
const privacy = {
  uuid,
  'account-run': /\baccount-[^\s`|]+:[a-f0-9]{64}\b/i,
  email: /[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+/i,
  jwt: /\b(?:(?:eyJ[a-z0-9_-]*|e30)\.[a-z0-9_-]+\.[a-z0-9_-]+|[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\.[a-z0-9_-]{8,})\b/i,
  // Symbolic API placeholders remain legal; assigned concrete identifiers do not.
  identifiers: /\b(?:nonce|approval[_ -]?id|tenant[_ -]?id|client_request_id)\s*[=:]\s*["']?(?![<{])[a-z0-9][a-z0-9_-]{3,}/i,
  'host-row': /^\s*\|[^\n]*\|?\s*\b(?:host|hostname|host_id|host name)\s*\|/im,
};

function strings(value) {
  const result = [];
  const stack = [value];
  while (stack.length) {
    const next = stack.pop();
    if (typeof next === 'string') result.push(next);
    else if (object(next) || Array.isArray(next)) {
      for (const [key, child] of Object.entries(next)) { result.push(key); stack.push(child); }
    }
  }
  return result.join('\n');
}

const disk = {
  stat: path => lstat(path),
  async bytes(path, before) {
    const handle = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    try {
      const after = await handle.stat();
      if (!after.isFile() || before.dev !== after.dev || before.ino !== after.ino) throw new Error();
      const buffer = Buffer.alloc(LIMIT + 1);
      let size = 0;
      while (size < buffer.length) {
        const { bytesRead } = await handle.read(buffer, size, buffer.length - size, size);
        if (!bytesRead) break;
        size += bytesRead;
      }
      return buffer.subarray(0, size);
    } finally { await handle.close(); }
  },
};

// Check every component, including the root and its ancestors. No symlink is
// accepted even when its target would remain beneath the requested root.
async function boundedRead(path, io) {
  const absolute = resolve(path);
  const root = parse(absolute).root;
  const parts = relative(root, absolute).split(sep).filter(Boolean);
  let current = root;
  const parents = [];
  for (let index = -1; index < parts.length; index++) {
    if (index >= 0) current = join(current, parts[index]);
    const stat = await io.stat(current);
    if (stat.isSymbolicLink()) throw Object.assign(new Error(), { reason: 'containment' });
    if (index < parts.length - 1) {
      if (!stat.isDirectory()) throw Object.assign(new Error(), { reason: 'regular' });
      parents.push([current, stat]);
      continue;
    }
    if (!stat.isFile()) throw Object.assign(new Error(), { reason: 'regular' });
    if (stat.size > LIMIT) throw Object.assign(new Error(), { reason: 'size' });
    const bytes = await io.bytes(current, stat);
    if (bytes.length > LIMIT) throw Object.assign(new Error(), { reason: 'size' });
    for (const [parent, before] of parents) {
      const after = await io.stat(parent);
      if (!after.isDirectory() || after.isSymbolicLink() || before.dev !== after.dev || before.ino !== after.ino)
        throw Object.assign(new Error(), { reason: 'containment' });
    }
    return { bytes, source: new TextDecoder('utf-8', { fatal: true }).decode(bytes) };
  }
  throw new Error();
}

function receiptPass(slice, value) {
  if (!object(value)) return false;
  if (slice === 'SEL-220') return value.schema === 'leaf.unified-surface-proof.v1'
    && object(value.result) && value.result.verdict === 'pass';
  if (slice === 'SEL-403') return value.schema === 'leaf.sel403.verdict.v1'
    && value.verdict === 'pass' && Array.isArray(value.checks) && value.checks.length > 0
    && value.checks.every(check => object(check) && check.ok === true)
    && value.checks.some(check => check.code === 'device');
  if (slice === 'SEL-316') return value.schema === 'leaf.wave3-verdict.v1' && value.verdict === 'pass';
  if (value.schema !== 'leaf.program-receipt.v1' || value.slice !== slice) return false;
  if (slice === 'SEL-213') return value.kind === 'canvas-perf-proof' && value.budgets_pass === true;
  const kinds = { 'SEL-008': 'smoke-proof', 'SEL-107': 'host-embed-proof',
    'SEL-112': 'practice-release-walk', 'SEL-221': 'money-walk-proof' };
  return Object.hasOwn(kinds, slice) && value.kind === kinds[slice] && value.pass === true
    && (slice !== 'SEL-221' || value.checker_exit === 0);
}

const bindings = [
  ['test-trade', 'SEL-221', /\bTest trade\b/i],
  ['practice', 'SEL-112', /\bPractice\b/i],
  ['rust-activation', 'SEL-221', /^(?=[\s\S]*\bRust\b)(?=[\s\S]*\bpending[- ]version\b)(?=[\s\S]*\bactivation\b)/i],
  ['restart', 'SEL-221', /^(?=[\s\S]*\bRust\b)(?=[\s\S]*\brestart\b)/i],
  ['keeper', 'SEL-221', /(?:\breport-only\b[\s\S]*\bKeeper\b|\bKeeper\b[\s\S]*\breport-only\b)/i],
];

function docChecks(doc, entries, check) {
  check('doc.readable', typeof doc === 'string');
  if (typeof doc !== 'string') return;
  for (const [rule, pattern] of Object.entries(privacy)) check(`doc.privacy.${rule}`, !pattern.test(doc));
  // Code identifiers, links and commands are not company-name prose.
  const prose = doc.replace(/```[^]*?```/g, '').replace(/`[^`]*`/g, '')
    .replace(/\]\([^)]*\)/g, ']');
  check('doc.naming', (prose.match(/\bleaf[\s_-]*automation\b/gi) || [])
    .every(name => name === 'Leaf Automation'));
  for (const heading of headings) check(`doc.heading.${headings.indexOf(heading)}`,
    doc.split('\n').filter(line => line === `## ${heading}`).length === 1);
  const section = heading => doc.split(`## ${heading}\n`)[1]?.split(/^## /m)[0] || '';
  const gates = section('Ratification and production gates');
  check('doc.gates', /ratification/i.test(gates) && /production authorization/i.test(gates)
    && /Keeper[\s\S]*report-only/.test(gates) && /fresh Studio exposure yes/.test(gates));
  const migration = section('Production migration order');
  check('doc.migration', /migrate-leaf-platform-production-db\.yml/.test(migration)
    && /forward-normal/.test(migration) && /G3\/P-176/.test(migration) && /fresh Studio exposure yes/.test(migration));
  check('doc.paging', /Staging has no paging alarm/.test(section('Paging')));
  check('doc.worker', /LEAF_LAB_WORKER_DISABLED/.test(section('Stopping the worker'))
    && /worker\.paused/.test(section('Stopping the worker')) && /cooperative/.test(section('Stopping the worker')));
  const receiptSection = section('Receipts');
  const rows = receiptSection.split('\n').filter(line => /^\s*\|/.test(line));
  check('doc.receipts.table', rows.length >= 2 && rows[0] === '| Path | SHA-256 |' && rows[1] === '| --- | --- |');
  const citations = rows.slice(2).map(line => {
    const cells = line.split('|').slice(1, -1).map(cell => cell.trim().replace(/^`(.*)`$/, '$1'));
    return cells.length === 2 && safePath(cells[0]) && digest(cells[1]) ? cells.join('\0') : null;
  });
  const expected = entries.map(entry => `${entry.receipt_path}\0${entry.receipt_sha256}`).sort();
  check('doc.receipts.agreement', citations.every(Boolean)
    && JSON.stringify(citations.sort()) === JSON.stringify(expected));
  const glossary = section('Glossary');
  check('doc.glossary', ['Capability', 'Test trade', 'Practice', 'Rust', 'Keeper', 'Ratification',
    'Production gate', 'Replay', 'Commit', 'Publication'].every(term => glossary.includes(`${term}:`)));
}

async function verify(options, io = disk) {
  const checks = [];
  let failed = false;
  let missing = false;
  const check = (code, ok, absence = false) => {
    checks.push({ code, ok: Boolean(ok) });
    if (!ok && !absence) failed = true;
  };
  const read = async (path, code, receipt = false) => {
    try { return await boundedRead(path, io); }
    catch (error) {
      if (receipt && error.code === 'ENOENT') { missing = true; check(`${code}.present`, false, true); }
      else check(`${code}.${['size', 'regular', 'containment'].includes(error.reason) ? error.reason : 'readable'}`, false);
      return null;
    }
  };
  const json = (readback, code) => {
    if (!readback) return null;
    try { return JSON.parse(readback.source); }
    catch { check(`${code}.json`, false); return null; }
  };
  const manifest = json(await read(options.manifest, 'manifest'), 'manifest');
  check('manifest.fields', exact(manifest, ['schema', 'capabilities'])
    && manifest.schema === 'leaf.lab-program-manifest.v1' && Array.isArray(manifest.capabilities));
  const entries = object(manifest) && Array.isArray(manifest.capabilities) ? manifest.capabilities : [];
  check('manifest.coverage', slices.every(slice => entries.some(entry => object(entry) && entry.slice === slice))
    && entries.every(entry => object(entry) && slices.includes(entry.slice)));
  check('manifest.ids', entries.every(entry => object(entry) && text(entry.id))
    && new Set(entries.map(entry => entry?.id)).size === entries.length);
  check('manifest.privacy', !Object.values(privacy).some(pattern => pattern.test(strings(manifest))));
  for (const [name, slice, pattern] of bindings) check(`binding.${name}`,
    entries.some(entry => object(entry) && entry.slice === slice && typeof entry.identity === 'string' && pattern.test(entry.identity)));
  for (let index = 0; index < entries.length; index++) {
    const entry = entries[index];
    const code = `receipt.${index}`;
    const valid = exact(entry, entryFields) && text(entry.id) && slices.includes(entry.slice)
      && safePath(entry.receipt_path) && digest(entry.receipt_sha256)
      && ['limits', 'identity', 'rollback'].every(field => text(entry[field]))
      && Array.isArray(entry.operator_steps) && entry.operator_steps.length > 0 && entry.operator_steps.every(text)
      && ['ratification', 'production'].includes(entry.gate);
    check(`${code}.metadata`, valid);
    if (!valid) continue;
    const root = resolve(options.root);
    const path = resolve(root, entry.receipt_path);
    const inside = relative(root, path);
    check(`${code}.containment`, Boolean(inside) && !inside.startsWith('..') && !win32.isAbsolute(inside));
    if (!inside || inside.startsWith('..') || win32.isAbsolute(inside)) continue;
    const readback = await read(path, code, true);
    if (!readback) continue;
    check(`${code}.hash`, hash(readback.bytes) === entry.receipt_sha256);
    const value = json(readback, code);
    check(`${code}.pass`, receiptPass(entry.slice, value));
    check(`${code}.privacy`, !Object.values(privacy).some(pattern => pattern.test(strings(value))));
  }
  const doc = await read(options.doc, 'doc');
  docChecks(doc?.source, entries.filter(object), check);
  const verdict = failed ? 'fail' : missing ? (options.required ? 'missing' : 'pending') : 'pass';
  return { output: { schema: 'leaf.lab-program-verdict.v1', verdict, checks },
    exit: failed ? 1 : missing && options.required ? 3 : 0 };
}

function argumentsFor(args) {
  const options = { manifest: 'artifacts/lab-program/manifest.json', root: 'receipts', required: false,
    self: false, doc: fileURLToPath(new URL('../docs/lab/OPERATOR.md', import.meta.url)) };
  const seen = new Set();
  for (let index = 0; index < args.length; index++) {
    const flag = args[index];
    if (!['--manifest', '--receipts-root', '--require-selected-receipts', '--self-test'].includes(flag) || seen.has(flag)) return null;
    seen.add(flag);
    if (flag === '--manifest' || flag === '--receipts-root') {
      const value = args[++index];
      if (!text(value) || value.startsWith('-')) return null;
      options[flag === '--manifest' ? 'manifest' : 'root'] = value;
    } else if (flag === '--self-test') options.self = true;
    else options.required = true;
  }
  return options.self && seen.size !== 1 ? null : options;
}

// Entirely virtual: the same bounded reader and verifier run without live files.
function fixture() {
  const values = [
    { schema: 'leaf.program-receipt.v1', slice: 'SEL-008', kind: 'smoke-proof', pass: true },
    { schema: 'leaf.program-receipt.v1', slice: 'SEL-107', kind: 'host-embed-proof', pass: true },
    { schema: 'leaf.program-receipt.v1', slice: 'SEL-112', kind: 'practice-release-walk', pass: true },
    { schema: 'leaf.program-receipt.v1', slice: 'SEL-213', kind: 'canvas-perf-proof', budgets_pass: true },
    { schema: 'leaf.unified-surface-proof.v1', result: { verdict: 'pass' } },
    { schema: 'leaf.program-receipt.v1', slice: 'SEL-221', kind: 'money-walk-proof', pass: true, checker_exit: 0 },
    { schema: 'leaf.wave3-verdict.v1', verdict: 'pass' },
    { schema: 'leaf.sel403.verdict.v1', verdict: 'pass', checks: [{ code: 'device', ok: true }] },
  ];
  const capabilities = slices.map((slice, index) => ({ id: `cap-${index}`, slice,
    receipt_path: `${slice}.json`, receipt_sha256: hash(Buffer.from(JSON.stringify(values[index]))),
    limits: 'Bounded proof only', identity: slice === 'SEL-221'
      ? 'Test trade; Rust pending-version activation and restart; report-only Keeper'
      : slice === 'SEL-112' ? 'Practice sanitized proof binding' : 'Sanitized proof binding',
    rollback: 'Pause and retain evidence', operator_steps: ['Inspect proof'], gate: 'ratification' }));
  const manifest = { schema: 'leaf.lab-program-manifest.v1', capabilities };
  const sections = Object.fromEntries(headings.map(heading => [heading, '']));
  sections['Program dossier'] = 'Leaf Automation program';
  sections['Stopping the worker'] = 'LEAF_LAB_WORKER_DISABLED; worker.paused; cooperative';
  sections.Paging = 'Staging has no paging alarm';
  sections['Production migration order'] = 'migrate-leaf-platform-production-db.yml forward-normal G3/P-176 fresh Studio exposure yes';
  sections['Ratification and production gates'] = 'Evidence ratification is separate from production authorization. Keeper remains report-only. Require fresh Studio exposure yes.';
  sections.Receipts = '| Path | SHA-256 |\n| --- | --- |\n'
    + capabilities.map(entry => `| ${entry.receipt_path} | ${entry.receipt_sha256} |`).join('\n');
  sections.Glossary = 'Capability: Test trade: Practice: Rust: Keeper: Ratification: Production gate: Replay: Commit: Publication:';
  return { manifest, values, doc: headings.map(heading => `## ${heading}\n${sections[heading]}\n`).join('\n') };
}

async function selfTest() {
  const outcomes = [];
  const run = async (mutate = () => {}, settings = {}) => {
    const data = fixture();
    mutate(data);
    const files = new Map([
      ['/virtual/manifest.json', Buffer.from(JSON.stringify(data.manifest))],
      ['/virtual/operator.md', Buffer.from(data.doc)],
      ...data.values.map((value, index) => [`/virtual/receipts/${slices[index]}.json`, Buffer.from(JSON.stringify(value))]),
    ]);
    for (const [path, value] of Object.entries(settings.files || {})) {
      if (value === null) files.delete(path); else files.set(path, value);
    }
    const virtualPath = path => path.replace(/\\/g, '/').replace(/^[a-z]:/i, '');
    const io = {
      async stat(path) {
        path = virtualPath(path);
        if (settings.denied === path) throw Object.assign(new Error(), { code: 'EACCES' });
        const file = files.has(path);
        if (!file && ![...files.keys()].some(key => key.startsWith(path === '/' ? '/' : `${path}/`)))
          throw Object.assign(new Error(), { code: 'ENOENT' });
        return { dev: 1, ino: path, size: settings.large === path ? LIMIT + 1 : (files.get(path)?.length || 0),
          isFile: () => file && settings.directory !== path,
          isDirectory: () => !file || settings.directory === path,
          isSymbolicLink: () => settings.link === path };
      },
      async bytes(path) {
        path = virtualPath(path);
        if (settings.unreadable === path) throw new Error();
        return settings.grown === path ? Buffer.alloc(LIMIT + 1) : files.get(path);
      },
    };
    return verify({ manifest: '/virtual/manifest.json', root: '/virtual/receipts',
      doc: '/virtual/operator.md', required: settings.required ?? true }, io);
  };
  const expect = async (code, mutate, settings = {}, exit = 1) => {
    const result = await run(mutate, settings);
    outcomes.push(result.exit === exit && result.output.checks.some(check => check.code === code && !check.ok));
  };
  outcomes.push((await run()).output.verdict === 'pass');
  for (let index = 0; index < slices.length; index++) {
    const path = `/virtual/receipts/${slices[index]}.json`;
    const code = `receipt.${index}`;
    // Test adapter fields directly so their failures are independent of hashing.
    const adapterBad = async mutate => {
      const data = fixture();
      mutate(data.values[index]);
      outcomes.push(!receiptPass(slices[index], data.values[index]));
    };
    await adapterBad(value => { value.schema = 'unknown'; });
    for (const field of Object.keys(fixture().values[index]).filter(field => field !== 'schema')) {
      await adapterBad(value => { delete value[field]; });
      await adapterBad(value => { value[field] = 'true'; });
    }
    await expect(`${code}.hash`, data => { data.values[index].extra = true; });
    await expect(`${code}.present`, undefined, { files: { [path]: null } }, 3);
    for (const mode of ['link', 'directory', 'large', 'grown', 'unreadable'])
      await expect(`${code}.${mode === 'link' ? 'containment' : mode === 'directory' ? 'regular' : ['large', 'grown'].includes(mode) ? 'size' : 'readable'}`,
        undefined, { [mode]: path });
    await expect(`${code}.json`, undefined, { files: { [path]: Buffer.from('{') } });
    await expect(`${code}.readable`, undefined, { files: { [path]: Buffer.from([0xff]) } });
    for (const field of entryFields) await expect(`${code}.metadata`, data => { delete data.manifest.capabilities[index][field]; });
  }
  await expect('manifest.containment', undefined, { link: '/virtual' });
  await expect('receipt.0.containment', undefined, { link: '/virtual/receipts' });
  outcomes.push((await run(data => {
    data.manifest.capabilities.push({ ...data.manifest.capabilities[0], id: 'second-capability' });
    const entry = data.manifest.capabilities[0];
    data.doc = data.doc.replace('## Glossary\n', `| ${entry.receipt_path} | ${entry.receipt_sha256} |\n\n## Glossary\n`);
  })).exit === 0);
  outcomes.push((await run(data => {
    data.doc += '\ntenant_id=<tenant_id>; nonce={nonce}; approval_id=<approval_id>';
    data.manifest.capabilities[0].gate = 'production';
  })).exit === 0);
  for (const path of ['../escape', '/absolute', 'a/../b', 'a//b', 'C:\\private', 'a\\b', 'a/./b', 'a%2fb', ''])
    await expect('receipt.0.metadata', data => { data.manifest.capabilities[0].receipt_path = path; });
  await expect('manifest.fields', data => { data.manifest.extra = true; });
  await expect('manifest.fields', data => { data.manifest.schema = 'wrong'; });
  await expect('manifest.coverage', data => { data.manifest.capabilities.pop(); });
  await expect('manifest.coverage', data => { data.manifest.capabilities[0].slice = 'SEL-999'; });
  await expect('manifest.ids', data => { data.manifest.capabilities[1].id = data.manifest.capabilities[0].id; });
  for (const [field, value] of [['limits', ''], ['rollback', ' '], ['identity', ''], ['operator_steps', []],
    ['operator_steps', ['']], ['receipt_sha256', 'A'.repeat(64)], ['gate', 'approved']])
    await expect('receipt.0.metadata', data => { data.manifest.capabilities[0][field] = value; });
  for (const [name, slice] of bindings) await expect(`binding.${name}`, data => {
    data.manifest.capabilities.find(entry => entry.slice === slice).identity = 'Sanitized proof';
  });
  const samples = { uuid: '12345678-1234-1234-1234-123456789abc',
    'account-run': `account-private:${'a'.repeat(64)}`, email: 'reader@example.invalid',
    jwt: 'eyJhbGciOiJIUzI1NiJ9.e30.abcdefghijk', identifiers: 'nonce=concrete-private',
    'host-row': '| host | private-machine |' };
  for (const [rule, sample] of Object.entries(samples)) {
    await expect(`doc.privacy.${rule}`, data => { data.doc += `\n${sample}`; });
    await expect('manifest.privacy', data => { data.manifest.capabilities[0].identity = sample; });
  }
  for (let index = 0; index < headings.length; index++) await expect(`doc.heading.${index}`, data => {
    data.doc = data.doc.replace(`## ${headings[index]}\n`, '## Removed\n');
  });
  await expect('doc.naming', data => { data.doc += '\nLeaf-Automation'; });
  await expect('doc.naming', data => { data.doc += '\nLEAF Automation'; });
  await expect('doc.naming', data => { data.doc += '\nleaf automation'; });
  const identifiers = await run(data => { data.doc += '\nleaf-web-demo leaf.lab-replay.v1 LEAF_LAB_STORE'; });
  outcomes.push(identifiers.output.verdict === 'pass'
    && identifiers.output.checks.some(check => check.code === 'doc.naming' && check.ok));
  await expect('doc.gates', data => { data.doc = data.doc.replace('Keeper remains report-only', 'Keeper approves'); });
  await expect('doc.receipts.agreement', data => { data.doc = data.doc.replace(data.manifest.capabilities[0].receipt_sha256, 'a'.repeat(64)); });
  await expect('doc.receipts.table', data => { data.doc = data.doc.replace('| Path | SHA-256 |', '| Path | Payload |'); });
  await expect('doc.glossary', data => { data.doc = data.doc.replace('Commit:', 'Unknown:'); });
  await expect('doc.worker', data => { data.doc = data.doc.replace('worker.paused', 'unknown'); });
  await expect('doc.paging', data => { data.doc = data.doc.replace('Staging has no paging alarm', 'Paging available'); });
  await expect('doc.migration', data => { data.doc = data.doc.replace('forward-normal', 'unknown'); });
  for (const [path, code] of [['/virtual/manifest.json', 'manifest'], ['/virtual/operator.md', 'doc']]) {
    await expect(`${code}.readable`, undefined, { files: { [path]: null } });
    await expect(`${code}.readable`, undefined, { denied: path });
    await expect(`${code}.size`, undefined, { grown: path });
  }
  await expect('manifest.json', undefined, { files: { '/virtual/manifest.json': Buffer.from('{') } });
  await expect('manifest.readable', undefined, { files: { '/virtual/manifest.json': Buffer.from([0xff]) } });
  const missingFiles = Object.fromEntries(slices.map(slice => [`/virtual/receipts/${slice}.json`, null]));
  const missing = await run(undefined, { files: missingFiles });
  const pending = await run(undefined, { files: missingFiles, required: false });
  outcomes.push(missing.exit === 3 && missing.output.verdict === 'missing'
    && pending.exit === 0 && pending.output.verdict === 'pending');
  for (const required of [false, true]) await expect('receipt.0.metadata', data => {
    data.manifest.capabilities[0].gate = 'unknown';
  }, { files: missingFiles, required });
  // Exact booleans, zero, device presence and partial verdicts cannot be truthy substitutes.
  outcomes.push(!receiptPass('SEL-221', { ...fixture().values[5], checker_exit: false }));
  outcomes.push(!receiptPass('SEL-403', { ...fixture().values[7], checks: [{ code: 'other', ok: true }] }));
  outcomes.push(!receiptPass('SEL-403', { ...fixture().values[7], checks: [{ code: 'device', ok: 1 }] }));
  outcomes.push(!receiptPass('SEL-220', { schema: 'leaf.unified-surface-proof.v1', verdict: 'pass' }));
  const cliBad = [['--unknown'], ['--manifest'], ['--receipts-root'], ['--manifest', '--self-test'],
    ['--manifest', 'a', '--manifest', 'b'], ['--receipts-root', 'a', '--receipts-root', 'b'],
    ['--require-selected-receipts', '--require-selected-receipts'], ['--self-test', '--self-test'],
    ['--self-test', '--manifest', 'a'], ['--self-test', '--receipts-root', 'a'],
    ['--self-test', '--require-selected-receipts'], ['extra']];
  outcomes.push(cliBad.every(args => argumentsFor(args) === null));
  outcomes.push(Boolean(argumentsFor([])) && Boolean(argumentsFor(['--self-test']))
    && Boolean(argumentsFor(['--manifest', 'a', '--receipts-root', 'b', '--require-selected-receipts'])));
  const ok = outcomes.every(Boolean);
  return { output: { schema: 'leaf.lab-program-verdict.v1', verdict: ok ? 'pass' : 'fail',
    checks: [{ code: 'self-test.fixtures', ok }] }, exit: ok ? 0 : 1 };
}

const options = argumentsFor(process.argv.slice(2));
if (!options) {
  process.stderr.write('Usage: node scripts/verify-lab-program.mjs [--manifest FILE] [--receipts-root DIR] [--require-selected-receipts] | --self-test\n');
  process.exitCode = 2;
} else {
  let result;
  try { result = options.self ? await selfTest() : await verify(options); }
  catch { result = { output: { schema: 'leaf.lab-program-verdict.v1', verdict: 'fail',
    checks: [{ code: 'checker.internal', ok: false }] }, exit: 1 }; }
  process.stdout.write(`${JSON.stringify(result.output)}\n`);
  process.exitCode = result.exit;
}
