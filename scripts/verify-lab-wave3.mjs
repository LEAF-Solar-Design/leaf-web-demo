#!/usr/bin/env node
// Offline readback only: no receipt contents or filesystem errors reach output.
import { constants } from 'node:fs';
import { lstat, open, readFile } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const LIMIT = 256 * 1024;
const names = ['replay', 'ledger', 'run', 'activation', 'relay', 'rollback'];
const ids = ['worker-disable', 'worker-pause', 'run-cancel', 'git-flip-back',
  'activation-pause', 'activation-off', 'tool-nodes', 'image-downgrade', 'surface-rail', 'relay-gate'];
const liveIds = new Set(['worker-pause', 'run-cancel', 'activation-pause']);
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const exact = (value, fields) => object(value) && Object.keys(value).length === fields.length
  && fields.every(key => Object.hasOwn(value, key));
const hex = length => value => typeof value === 'string' && new RegExp(`^[a-f0-9]{${length}}$`).test(value);
const integer = minimum => value => Number.isSafeInteger(value) && value >= minimum;
const yes = value => value === true;
const literal = expected => value => value === expected;
const utc = value => {
  if (typeof value !== 'string' || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z$/.test(value)) return false;
  const date = new Date(value);
  return Number.isFinite(date.getTime())
    && date.toISOString() === value.replace(/(?:\.(\d+))?Z$/, (_, fraction) => `.${(fraction || '').padEnd(3, '0').slice(0, 3)}Z`);
};
// Compare fractional seconds without losing precision to Date's milliseconds.
function compareTime(left, right) {
  const secondOrder = left.slice(0, 19).localeCompare(right.slice(0, 19));
  if (secondOrder) return secondOrder;
  const fraction = text => text.includes('.') ? text.slice(20, -1) : '';
  const a = fraction(left);
  const b = fraction(right);
  const width = Math.max(a.length, b.length);
  return a.padEnd(width, '0').localeCompare(b.padEnd(width, '0'));
}
const revision = integer(1);
const published = value => value === null || revision(value);
const common = { schema: null, sha: hex(40), capturedAt: utc, proofKey: hex(32), pass: yes };
const fields = {
  replay: { status: literal('complete'), revision, publishedRevision: published, headCommit: hex(40), divergent: literal(false) },
  ledger: { revision, publishedRevision: published, digest: hex(64), publicationCount: integer(0) },
  run: { revision, digest: hex(64), toolNodeCount: integer(1), runState: literal('succeeded'), brokerState: literal('succeeded'), brokerDigest: hex(64) },
  activation: { revision, digest: hex(64), publicationCount: integer(0), activatedAt: utc, editorClosedAt: utc,
    observedAt: utc, widgetRendered: yes, widgetSurface: literal('/mini') },
  relay: { opsSha: hex(40), listOk: yes, detailOk: yes, intentByIdOk: yes, opaqueHandlesOnly: yes, capsHeld: yes, fallbackPolicyOk: yes },
  rollback: { controls: value => Array.isArray(value) && value.length === ids.length },
};
const uuid = '[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}';
const privacy = {
  'account-run': new RegExp(`(?:account-)?${uuid}:[a-f0-9]{64}`, 'i'),
  uuid: new RegExp(uuid, 'i'),
  'client-request-id': /client_request_id/i,
  nonce: /nonce/i,
  'approval-id': /approval_id/i,
  tenant: /tenant/i,
  subject: /subject/i,
  email: /[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+/i,
  jwt: /(?:^|[^a-z0-9_-])(?:(?:eyJ[a-z0-9_-]*|e30)\.[a-z0-9_-]+\.[a-z0-9_-]+|[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\.[a-z0-9_-]{8,})(?:$|[^a-z0-9_-])/i,
};

// Iterative traversal also handles hostile nesting without recursive stack growth.
function privateStrings(value) {
  const strings = [];
  const stack = [value];
  while (stack.length) {
    const item = stack.pop();
    if (typeof item === 'string') strings.push(item);
    else if (item && typeof item === 'object') {
      for (const [key, child] of Object.entries(item)) {
        strings.push(key);
        stack.push(child);
      }
    }
  }
  return strings;
}

// Read at most LIMIT + 1 bytes, even if a file grows after lstat. O_NOFOLLOW
// and the opened inode comparison close the lstat/open replacement window.
const disk = {
  stat: path => lstat(path),
  async bytes(path, before) {
    const handle = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
    try {
      const after = await handle.stat();
      if (!after.isFile() || after.dev !== before.dev || after.ino !== before.ino) throw new Error();
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

async function verify(directory, required, doc, io = disk) {
  const checks = [];
  const receipts = {};
  let failed = false;
  let missing = false;
  const check = (code, ok, absence = false) => {
    checks.push({ code, ok: Boolean(ok) });
    if (!ok && !absence) failed = true;
  };
  for (const name of names) {
    const path = join(directory, `${name}.json`);
    let stat;
    try { stat = await io.stat(path); }
    catch (error) {
      if (error.code === 'ENOENT') {
        missing = true;
        check(`${name}.present`, false, true);
      } else check(`${name}.readable`, false);
      continue;
    }
    check(`${name}.present`, true);
    check(`${name}.regular`, stat.isFile() && !stat.isSymbolicLink());
    check(`${name}.size`, stat.size <= LIMIT);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.size > LIMIT) continue;
    let bytes;
    try { bytes = await io.bytes(path, stat); check(`${name}.readable`, true); }
    catch { check(`${name}.readable`, false); continue; }
    if (bytes.length > LIMIT) { check(`${name}.size`, false); continue; }
    let value;
    try {
      value = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
      check(`${name}.json`, true);
    } catch { check(`${name}.json`, false); continue; }
    const strings = privateStrings(value);
    for (const [rule, pattern] of Object.entries(privacy)) {
      check(`${name}.privacy.${rule}`, !strings.some(text => pattern.test(text)));
    }
    const validators = { ...common, schema: literal(`leaf.wave3-${name}.v1`), ...fields[name] };
    check(`${name}.fields`, exact(value, Object.keys(validators)));
    for (const [field, valid] of Object.entries(validators)) check(`${name}.${field}`, object(value) && valid(value[field]));
    if (!object(value)) continue;
    receipts[name] = value;
    if (name === 'rollback') {
      const controls = Array.isArray(value.controls) ? value.controls : [];
      check('rollback.control-fields', controls.every(control => exact(control, ['id', 'method', 'exercised', 'restored'])));
      check('rollback.control-ids', controls.length === ids.length && ids.every(id => controls.filter(c => object(c) && c.id === id).length === 1));
      for (const id of ids) {
        const control = controls.find(c => object(c) && c.id === id);
        check(`rollback.${id}`, Boolean(control) && (control.method === 'live'
          ? control.exercised === true && control.restored === true
          : !liveIds.has(id) && control.method === 'documented' && control.exercised === false && control.restored === false));
      }
    }
  }
  const equality = (code, left, leftField, right, rightField = leftField) => {
    if (receipts[left] && receipts[right]) check(code, receipts[left][leftField] === receipts[right][rightField]);
  };
  equality('agree.replay-ledger-revision', 'replay', 'revision', 'ledger');
  equality('agree.ledger-run-revision', 'ledger', 'revision', 'run');
  equality('agree.run-activation-revision', 'run', 'revision', 'activation');
  equality('agree.published-revision', 'replay', 'publishedRevision', 'ledger');
  equality('agree.ledger-run-digest', 'ledger', 'digest', 'run');
  equality('agree.run-activation-digest', 'run', 'digest', 'activation');
  equality('agree.broker-digest', 'ledger', 'digest', 'run', 'brokerDigest');
  equality('agree.publication-count', 'ledger', 'publicationCount', 'activation');
  const activation = receipts.activation;
  if (activation) {
    check('activation.time-order', utc(activation.activatedAt) && utc(activation.editorClosedAt) && utc(activation.observedAt)
      && compareTime(activation.activatedAt, activation.editorClosedAt) <= 0
      && compareTime(activation.editorClosedAt, activation.observedAt) < 0);
  }
  const available = Object.values(receipts);
  for (const field of ['proofKey', 'sha']) check(`agree.${field}`, available.every(value => value[field] === available[0][field]));
  check('doc.readable', typeof doc === 'string');
  for (const [rule, pattern] of Object.entries(privacy)) check(`doc.privacy.${rule}`, typeof doc === 'string' && !pattern.test(doc));
  const verdict = failed ? 'fail' : missing ? (required ? 'missing' : 'pending') : 'pass';
  return { output: { schema: 'leaf.wave3-verdict.v1', verdict, checks }, exit: failed ? 1 : missing && required ? 3 : 0 };
}

function fixture() {
  const base = { sha: 'a'.repeat(40), capturedAt: '2026-10-26T12:00:00Z', proofKey: 'b'.repeat(32), pass: true };
  const digest = 'c'.repeat(64);
  const data = {
    replay: { status: 'complete', revision: 1, publishedRevision: 1, headCommit: 'd'.repeat(40), divergent: false },
    ledger: { revision: 1, publishedRevision: 1, digest, publicationCount: 1 },
    run: { revision: 1, digest, toolNodeCount: 1, runState: 'succeeded', brokerState: 'succeeded', brokerDigest: digest },
    activation: { revision: 1, digest, publicationCount: 1, activatedAt: '2026-10-26T10:00:00Z',
      editorClosedAt: '2026-10-26T10:01:00Z', observedAt: '2026-10-26T10:02:00Z', widgetRendered: true, widgetSurface: '/mini' },
    relay: { opsSha: 'e'.repeat(40), listOk: true, detailOk: true, intentByIdOk: true, opaqueHandlesOnly: true, capsHeld: true, fallbackPolicyOk: true },
    rollback: { controls: ids.map(id => ({ id, method: liveIds.has(id) ? 'live' : 'documented', exercised: liveIds.has(id), restored: liveIds.has(id) })) },
  };
  return Object.fromEntries(names.map(name => [name, { ...base, schema: `leaf.wave3-${name}.v1`, ...data[name] }]));
}

// The virtual filesystem exercises the production reader without writing files.
async function selfTest(doc) {
  const covered = new Set();
  const outcomes = [];
  const run = async (data, options = {}) => {
    const io = {
      async stat(path) {
        const name = names.find(n => path.endsWith(`${n}.json`));
        if (!Object.hasOwn(data, name)) throw Object.assign(new Error(), { code: options.denied ? 'EACCES' : 'ENOENT' });
        return { size: options.large === name ? LIMIT + 1 : 100, isFile: () => options.directory !== name,
          isSymbolicLink: () => options.link === name };
      },
      async bytes(path) {
        const name = names.find(n => path.endsWith(`${n}.json`));
        if (options.unreadable === name) throw new Error();
        return options.grown === name ? Buffer.alloc(LIMIT + 1) : Buffer.from(options.raw === name ? '{' : JSON.stringify(data[name]));
      },
    };
    return verify('fixtures', options.required ?? true, Object.hasOwn(options, 'doc') ? options.doc : doc, io);
  };
  const good = await run(fixture());
  outcomes.push(good.exit === 0 && good.output.verdict === 'pass');
  const bad = async (code, mutate = () => {}, options = {}, exit = 1) => {
    const data = fixture();
    mutate(data);
    const result = await run(data, options);
    const found = result.output.checks.some(check => check.code === code && !check.ok);
    outcomes.push(result.exit === exit && found);
    if (found && result.exit === exit) covered.add(code);
  };
  const samples = {
    'account-run': `account-12345678-1234-1234-1234-123456789abc:${'a'.repeat(64)}`,
    uuid: '12345678-1234-1234-1234-123456789abc', 'client-request-id': 'client_request_id',
    nonce: 'nonce', 'approval-id': 'approval_id', tenant: 'tenant', subject: 'subject',
    email: 'reader@example.invalid', jwt: 'eyJhbGciOiJIUzI1NiJ9.eyJ0ZXN0Ijp0cnVlfQ.abcdefghijk',
  };
  for (const name of names) {
    await bad(`${name}.present`, data => { delete data[name]; }, {}, 3);
    await bad(`${name}.regular`, undefined, { link: name });
    await bad(`${name}.regular`, undefined, { directory: name });
    await bad(`${name}.size`, undefined, { large: name });
    await bad(`${name}.size`, undefined, { grown: name });
    await bad(`${name}.readable`, undefined, { unreadable: name });
    await bad(`${name}.readable`, data => { delete data[name]; }, { denied: true });
    await bad(`${name}.json`, undefined, { raw: name });
    await bad(`${name}.fields`, data => { data[name].extra = false; });
    for (const field of Object.keys({ ...common, ...fields[name] })) {
      await bad(`${name}.${field}`, data => { data[name][field] = 'invalid'; });
    }
    for (const [rule, sample] of Object.entries(samples)) {
      await bad(`${name}.privacy.${rule}`, data => { data[name].extra = sample; });
      await bad(`${name}.privacy.${rule}`, data => { data[name][sample] = false; });
    }
  }
  for (const [rule, sample] of Object.entries(samples)) await bad(`doc.privacy.${rule}`, undefined, { doc: sample });
  await bad('doc.readable', undefined, { doc: null });
  await bad('rollback.control-fields', data => { data.rollback.controls[0].extra = false; });
  await bad('rollback.control-ids', data => { data.rollback.controls[0].id = ids[1]; });
  for (const id of ids) await bad(`rollback.${id}`, data => {
    const control = data.rollback.controls.find(c => c.id === id);
    control.restored = !control.restored;
  });
  const mutations = {
    'agree.replay-ledger-revision': data => { data.replay.revision = 2; },
    'agree.ledger-run-revision': data => { data.run.revision = 2; },
    'agree.run-activation-revision': data => { data.activation.revision = 2; },
    'agree.published-revision': data => { data.replay.publishedRevision = null; },
    'agree.ledger-run-digest': data => { data.ledger.digest = 'd'.repeat(64); },
    'agree.run-activation-digest': data => { data.activation.digest = 'd'.repeat(64); },
    'agree.broker-digest': data => { data.run.brokerDigest = 'd'.repeat(64); },
    'agree.publication-count': data => { data.activation.publicationCount = 2; },
    'activation.time-order': data => { data.activation.editorClosedAt = data.activation.observedAt; },
    'agree.proofKey': data => { data.relay.proofKey = 'f'.repeat(32); },
    'agree.sha': data => { data.relay.sha = 'f'.repeat(40); },
  };
  for (const [code, mutate] of Object.entries(mutations)) await bad(code, mutate);
  await bad('run.pass', data => { delete data.replay; data.run.pass = false; });
  await bad('run.pass', data => { delete data.replay; data.run.pass = false; }, { required: false });
  const pending = await run({}, { required: false });
  outcomes.push(pending.exit === 0 && pending.output.verdict === 'pending');
  const missing = await run({});
  outcomes.push(missing.exit === 3 && missing.output.verdict === 'missing');
  // Valid nullable publication pointers, live alternatives, and fractional time.
  const alternate = fixture();
  alternate.replay.publishedRevision = alternate.ledger.publishedRevision = null;
  alternate.rollback.controls = ids.map(id => ({ id, method: 'live', exercised: true, restored: true }));
  alternate.activation.activatedAt = '2026-10-26T10:00:00.00001Z';
  alternate.activation.editorClosedAt = '2026-10-26T10:00:00.00001Z';
  alternate.activation.observedAt = '2026-10-26T10:00:00.00002Z';
  outcomes.push((await run(alternate)).exit === 0);
  await bad('activation.activatedAt', data => { data.activation.activatedAt = '2026-02-30T10:00:00Z'; });
  await bad('ledger.revision', data => { data.ledger.revision = true; });
  await bad('rollback.worker-pause', data => {
    Object.assign(data.rollback.controls.find(c => c.id === 'worker-pause'), { method: 'documented', exercised: false, restored: false });
  });
  await bad('doc.privacy.jwt', undefined, { doc: 'e30.e30.c2ln' });
  const invalidArguments = [
    ['--unknown'], ['--receipts'], ['--receipts', '--require-receipts'],
    ['--receipts', 'a', '--receipts', 'b'], ['--require-receipts', '--require-receipts'],
    ['--self-test', '--self-test'], ['--self-test', '--receipts', 'a'],
    ['--self-test', '--require-receipts'], ['--receipts', 'a', 'extra'],
  ];
  outcomes.push(invalidArguments.every(args => argumentsFor(args) === null));
  outcomes.push(Boolean(argumentsFor([])) && Boolean(argumentsFor(['--self-test']))
    && Boolean(argumentsFor(['--require-receipts', '--receipts', 'a'])));
  outcomes.push(good.output.checks.every(check => covered.has(check.code)));
  const ok = outcomes.every(Boolean);
  return { output: { schema: 'leaf.wave3-verdict.v1', verdict: ok ? 'pass' : 'fail',
    checks: [{ code: 'self-test.fixtures', ok }] }, exit: ok ? 0 : 1 };
}

function argumentsFor(args) {
  const seen = new Set();
  const options = { directory: 'artifacts/lab-wave3', required: false, self: false };
  for (let index = 0; index < args.length; index++) {
    const flag = args[index];
    if (!['--receipts', '--require-receipts', '--self-test'].includes(flag) || seen.has(flag)) return null;
    seen.add(flag);
    if (flag === '--receipts') {
      const value = args[++index];
      if (!value || value.startsWith('--')) return null;
      options.directory = value;
    } else if (flag === '--require-receipts') options.required = true;
    else options.self = true;
  }
  return options.self && seen.size !== 1 ? null : options;
}

const options = argumentsFor(process.argv.slice(2));
if (!options) {
  process.stderr.write('Usage: node scripts/verify-lab-wave3.mjs [--receipts DIR] [--require-receipts] | --self-test\n');
  process.exitCode = 2;
} else {
  let result;
  try {
    let doc = null;
    try { doc = await readFile(fileURLToPath(new URL('../docs/lab/WAVE3-REPLAY.md', import.meta.url)), 'utf8'); } catch { /* fixed failure below */ }
    result = options.self ? await selfTest(doc) : await verify(resolve(options.directory), options.required, doc);
  } catch {
    result = { output: { schema: 'leaf.wave3-verdict.v1', verdict: 'fail', checks: [{ code: 'checker.internal', ok: false }] }, exit: 1 };
  }
  process.stdout.write(`${JSON.stringify(result.output)}\n`);
  process.exitCode = result.exit;
}
