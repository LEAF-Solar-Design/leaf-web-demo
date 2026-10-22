import { afterEach, describe, expect, it, vi } from 'vitest';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { canonicalJSON, compatible } from '../contract.js';
import cases from '../contract/fixtures/ops-conformance.json';
import { contract, runtimeValue, sourceAge, evaluateOperation, evaluateGraph, exportValue,
  createPoller, pollIntervalMs, createStudioSources, MAX_ROWS, runOperator } from './index.js';

// Canonical test inputs are read by Node, never part of the browser import graph.
// Vitest runs from web/; path strings bypass Vite's asset-URL glob transform.
const fixture = name => JSON.parse(readFileSync(resolve(process.cwd(), '../contract/fixtures/lab', name), 'utf8'));
const blocks = fixture('blocks.json');
const firstCut = fixture('graphs/first-cut.json');
const golden = fixture('lab-embed.v1.golden.json');
const ports = fixture('port-compat.v1.json');
const vectors = fixture('digest-vectors.json');

const clone = value => JSON.parse(JSON.stringify(value));
const trusted = Object.fromEntries(blocks.manifests.map(block => [`${block.id}@${block.version.semver}`, block]));

function scenario(test) {
  let value = clone(test.target === 'graph' ? firstCut : test.target === 'block' ? blocks.manifests[0]
    : { message: golden.messages[0], context: golden.context, state: golden.state });
  if (test.generate) {
    const count = test.generate.blocks;
    value.blocks = Array.from({ length: count }, (_, i) => ({ ...clone(firstCut.blocks[0]), id: `block-${i}` }));
    value.pipes = [];
    for (let i = 1; i < count; i += 1) ['value', 'auxiliary', 'extra'].forEach((port, j) => {
      if (i >= j + 1) value.pipes.push({ from: { node: `block-${i - j - 1}`, port: 'result' }, to: { node: `block-${i}`, port } });
    });
    value.pipes = value.pipes.slice(0, test.generate.pipes); value.outputs = {};
  }
  for (const change of test.changes) {
    const path = change.path.slice(1).split('/');
    let parent = value;
    for (const key of path.slice(0, -1)) parent = parent[key];
    const key = path.at(-1);
    if (change.op === 'delete') delete parent[key];
    else if (change.op === 'append') parent[key].push(clone(change.value));
    else parent[key] = clone(change.value);
  }
  return value;
}

describe('shared Lab conformance and canonical fixtures', () => {
  it.each(cases.cases)('$name', test => {
    const value = scenario(test); const registry = clone(trusted); const before = clone(value);
    if (test.registry_input) registry['level-fast-index@1.0.0'].inputs.value = test.registry_input;
    const run = () => {
      if (test.target === 'graph') contract.graph(value, registry, test.previous ? firstCut : null, test.wire_bytes);
      else if (test.target === 'block') contract.block(value, test.name.startsWith('spoof-') ? blocks.manifests[0] : value, test.previous ? blocks.manifests[0] : null);
      else contract.embed(value.message, value.context, value.state);
    };
    if (test.expected) expect(run).not.toThrow();
    else {
      expect(run).toThrow();
      if (test.target === 'embed') expect(value.state).toEqual(before.state);
    }
  });
  it.each(ports.cases)('pins: $name', test => expect(compatible(test.output, test.input)).toBe(test.expected));
  it.each(vectors.vectors)('canonical: $name', vector => {
    const canonical = canonicalJSON(vector.input);
    expect(canonical).toBe(vector.canonical);
    expect(Buffer.from(canonical).toString('hex')).toBe(vector.utf8_hex);
    expect(createHash('sha256').update(canonical).digest('hex')).toBe(vector.sha256);
  });
});

const pin = (schema = { type: 'array', items: { type: 'number' } }, mode = 'scalar') => ({
  mode, schema, unit: 'none', nullable: false, required: true, has_default: false,
});
function manifest(id, effect = 'pure') {
  return { ...clone(blocks.manifests[0]), id, kind: 'builtin', effect,
    inputs: effect === 'read' ? {} : { value: pin() }, outputs: { result: pin() },
    settings_schema: { type: 'object', additionalProperties: false, properties: {} },
    sample_input: effect === 'read' ? {} : { value: [1, 2] }, sample_output: { result: [1, 2] } };
}
function chain(count = 3, effect = 'pure') {
  const source = manifest('host-source', 'read'); const operation = manifest('identity', effect);
  return {
    registry: { 'host-source@1.0.0': source, 'identity@1.0.0': operation },
    graph: { schema: 'leaf.lab-graph.v1',
      blocks: Array.from({ length: count }, (_, i) => ({ id: `block-${i}`, registry: {
        id: i ? 'identity' : 'host-source', version: '1.0.0', code_sha256: source.version.code_sha256,
      }, settings: {} })),
      pipes: Array.from({ length: count - 1 }, (_, i) => ({ from: { node: `block-${i}`, port: 'result' }, to: { node: `block-${i + 1}`, port: 'value' } })),
      outputs: { result: { node: `block-${count - 1}`, port: 'result' } },
    },
  };
}

describe('fixed operators and metadata', () => {
  const data = runtimeValue([{ n: 3 }, { n: 1 }, { n: 2 }], { host_tainted: true, as_of: 10, simulated: true, stale: true, truncated: true });
  it('executes fixed data operators without mutating inputs', () => {
    const filtered = evaluateOperation('filter', { value: data }, { field: 'n', comparison: 'gt', value: 1 });
    const sorted = evaluateOperation('sort', { value: filtered }, { field: 'n' });
    const selected = evaluateOperation('select', { value: sorted }, { field: 'n' });
    expect(selected.data).toEqual([2, 3]);
    expect(evaluateOperation('sum', { value: selected }).data).toBe(5);
    expect(data.data).toEqual([{ n: 3 }, { n: 1 }, { n: 2 }]);
    expect(Object.isFrozen(data.data[0])).toBe(true);
    expect(evaluateOperation('mean', { value: runtimeValue([]) }).data).toBeNull();
    expect(evaluateOperation('min', { value: selected }).data).toBe(2);
    expect(evaluateOperation('max', { value: selected }).data).toBe(3);
    expect(evaluateOperation('take', { value: selected }, { count: 1 }).data).toEqual([2]);
    expect(evaluateOperation('count', { value: selected }).data).toBe(2);
  });
  it('propagates independent metadata to aggregates, errors and export refusals', () => {
    const result = evaluateOperation('count', { value: data });
    expect(result).toMatchObject({ data: 3, as_of: 10, host_tainted: true, simulated: true, stale: true, truncated: true });
    expect(sourceAge(result, 110)).toBe(100);
    expect(() => exportValue(result)).toThrow('host data');
    const failure = evaluateOperation('divide', { value: result, auxiliary: runtimeValue(0, { as_of: 20 }) });
    expect(failure).toMatchObject({ data: null, host_tainted: true, as_of: 10, error: { code: 'operator-refused' } });
    expect(evaluateOperation('identity', { value: failure }).error.code).toBe('upstream-failed');
    expect(() => exportValue(failure)).toThrow();
    expect(exportValue(runtimeValue(2)).data).toBe(2);
    expect(sourceAge(runtimeValue(2), 110)).toBeNull();
    const mixed = evaluateOperation('add', { value: runtimeValue(1, { host_tainted: true }), auxiliary: runtimeValue(2, { as_of: 20 }) });
    expect(mixed).toMatchObject({ data: 3, as_of: null, host_tainted: true });
  });
  it('refuses code, prototype paths, forged values and non-finite numbers', () => {
    expect(() => runOperator('eval', {}, {})).toThrow();
    expect(evaluateOperation('field', { value: data }, { field: '__proto__' }).error).toBeTruthy();
    expect(() => evaluateOperation('identity', { value: { data: 1 } })).toThrow();
    expect(() => runtimeValue(NaN)).toThrow();
    expect(evaluateOperation('constant', {}, { value: 'text' }).data).toBe('text');
  });
  it('requires explicit stream reduction and bounds every edge including nested arrays', () => {
    const stream = runtimeValue([1, 2, 3], { mode: 'stream', host_tainted: true });
    expect(evaluateOperation('count', { value: stream }).error.code).toBe('stream-adapter-required');
    expect(evaluateOperation('latest', { value: stream })).toMatchObject({ data: 3, mode: 'scalar', host_tainted: true });
    expect(evaluateOperation('window', { value: stream }, { count: 2 }).data).toEqual([2, 3]);
    expect(evaluateOperation('window', { value: stream }, { count: 0 }).data).toEqual([]);
    expect(evaluateOperation('collect', { value: stream }).data).toEqual([1, 2, 3]);
    const bounded = runtimeValue({ rows: Array.from({ length: 6000 }, (_, i) => i) });
    expect(bounded.data.rows).toHaveLength(MAX_ROWS); expect(bounded.truncated).toBe(true);
  });
  it('snapshots caller data and reuses only runtime-owned immutable values', () => {
    const input = [{ rows: [1, 2] }];
    const value = runtimeValue(input);
    input[0].rows.push(3);
    expect(value.data).toEqual([{ rows: [1, 2] }]);
    const identity = evaluateOperation('identity', { value });
    expect(identity.data).toBe(value.data);
    expect(Object.isFrozen(identity.data[0].rows)).toBe(true);
    const frozen = Object.freeze({ rows: [1] });
    const copied = runtimeValue(frozen);
    frozen.rows.push(2);
    expect(copied.data.rows).toEqual([1]);
  });
  it('carries taint through fan-out caches and does not execute effects', () => {
    const { graph, registry } = chain();
    graph.outputs.cached = { node: 'block-1', port: 'result' };
    const sources = { 'block-0': runtimeValue([1, 2], { as_of: 1, simulated: true }) };
    const result = evaluateGraph(graph, registry, { sources });
    expect(result.outputs.result).toMatchObject({ data: [1, 2], host_tainted: true, as_of: 1, simulated: true });
    expect(result.outputs.cached).toBe(result.values['block-1'].result);
    expect(() => exportValue(result.outputs.result)).toThrow();
    const large = evaluateGraph(graph, registry, { sources: { 'block-0': runtimeValue(Array.from({ length: 6000 }, (_, i) => i)) } });
    for (const output of Object.values(large.values)) {
      expect(output.result.data).toHaveLength(MAX_ROWS); expect(output.result.truncated).toBe(true);
    }
    registry['identity@1.0.0'].effect = 'write';
    expect(evaluateGraph(graph, registry, { sources }).outputs.result.error).toBeTruthy();
    expect(evaluateGraph(graph, registry, { sources }).outputs.result.host_tainted).toBe(true);
    expect(evaluateGraph(graph, registry).outputs.result.data).toBeNull();
    expect(Number(result.timing.finished_ns)).toBeGreaterThanOrEqual(Number(result.timing.started_ns));
  });
  it('requires stream adapters in the graph as well as the operator API', () => {
    const { graph, registry } = chain(2);
    const source = registry['host-source@1.0.0'];
    source.outputs.result = pin({ type: 'number' }, 'stream'); source.sample_output = { result: 1 };
    expect(() => evaluateGraph(graph, registry)).toThrow('incompatible pins');
    const latest = manifest('latest');
    latest.inputs.value = pin({ type: 'number' }, 'stream'); latest.sample_input = { value: 1 };
    latest.outputs.result = pin({ type: 'number' }); latest.sample_output = { result: 1 };
    registry['latest@1.0.0'] = latest; graph.blocks[1].registry.id = 'latest';
    const result = evaluateGraph(graph, registry, { sources: { 'block-0': runtimeValue([1, 2], { mode: 'stream' }) } });
    expect(result.outputs.result).toMatchObject({ data: 2, mode: 'scalar', host_tainted: true });
  });
  it('evaluates 10 blocks x 100 rows correctly', () => {
    const { graph, registry } = chain(10);
    const rows = Array.from({ length: 100 }, (_, i) => i);
    const result = evaluateGraph(graph, registry, { sources: {
      'block-0': runtimeValue(rows),
    } });
    expect(Object.keys(result.values)).toHaveLength(10);
    expect(result.outputs.result.data).toEqual(rows);
    expect(result.outputs.result.host_tainted).toBe(true);
  });
});

afterEach(() => vi.useRealTimers());
const flush = async () => { for (let i = 0; i < 8; i += 1) await Promise.resolve(); };
function visibility() {
  const target = new EventTarget(); target.hidden = false;
  target.setHidden = value => { target.hidden = value; target.dispatchEvent(new Event('visibilitychange')); };
  return target;
}

describe('single-flight polling', () => {
  it('clamps 5..300 seconds and respects source minimums', () => {
    expect(pollIntervalMs(0)).toBe(5000); expect(pollIntervalMs(1000)).toBe(300000);
    expect(pollIntervalMs(5, 30)).toBe(30000);
    expect(() => pollIntervalMs(NaN)).toThrow(); expect(() => pollIntervalMs(5, 301)).toThrow();
  });
  it('shares a flight, times out at 15 seconds and never overlaps an abort-ignoring source', async () => {
    vi.useFakeTimers(); let resolve; let signal;
    const read = vi.fn(context => { signal = context.signal; return new Promise(done => { resolve = done; }); });
    const onValue = vi.fn(); const onError = vi.fn();
    const poller = createPoller({ read, onValue, onError, visibility: visibility() });
    const first = poller.start(); expect(poller.tick()).toBe(first); await flush();
    await vi.advanceTimersByTimeAsync(15000);
    expect(signal.aborted).toBe(true); expect(onError).toHaveBeenCalledWith({ code: 'source-timeout' });
    expect(await first).toBeUndefined();
    await vi.advanceTimersByTimeAsync(60000); poller.tick(); expect(read).toHaveBeenCalledTimes(1);
    resolve('late'); await flush(); expect(onValue).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(5000); expect(read).toHaveBeenCalledTimes(2); poller.dispose();
  });
  it('pauses while hidden, suppresses obsolete delivery, resumes and cleans up', async () => {
    vi.useFakeTimers(); const document = visibility(); let resolve;
    const read = vi.fn(() => new Promise(done => { resolve = done; })); const onValue = vi.fn();
    const poller = createPoller({ read, onValue, visibility: document });
    poller.start(); await flush(); document.setHidden(true);
    resolve('obsolete'); await flush(); await vi.advanceTimersByTimeAsync(300000);
    expect(read).toHaveBeenCalledTimes(1); expect(onValue).not.toHaveBeenCalled();
    document.setHidden(false); await flush(); expect(read).toHaveBeenCalledTimes(2);
    resolve('current'); await flush(); expect(onValue).toHaveBeenCalledWith('current');
    poller.dispose(); await vi.advanceTimersByTimeAsync(300000); expect(read).toHaveBeenCalledTimes(2);
    expect(() => poller.start()).toThrow();
  });
  it('sanitizes failures and schedules another read', async () => {
    vi.useFakeTimers(); const onError = vi.fn();
    const read = vi.fn().mockRejectedValue(new Error('private payload'));
    const poller = createPoller({ read, onError, visibility: visibility() });
    await poller.start(); expect(onError).toHaveBeenCalledWith({ code: 'source-failed' });
    await vi.advanceTimersByTimeAsync(5000); expect(read).toHaveBeenCalledTimes(2); poller.stop(); poller.dispose();
  });
});

describe('Studio read-only sources', () => {
  it('only calls the two GET routes and preserves source metadata independently of receipt time', async () => {
    const fetchImpl = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ jobs: [1], families: [2], as_of: 1, simulated: true, has_more: true }) });
    const sources = createStudioSources({ fetchImpl, now: () => 100000 });
    expect(await sources.jobs()).toMatchObject({ data: [1], as_of: 1, stale: true, simulated: true, truncated: true, host_tainted: true });
    expect((await sources.capabilities()).data).toEqual([2]);
    expect(fetchImpl.mock.calls.map(([path, options]) => [path, options.method])).toEqual([
      ['/api/jobs', 'GET'], ['/api/capabilities', 'GET'],
    ]);
    await expect(sources.read('https://other.invalid')).rejects.toThrow();
    const poller = sources.poll('studio-capabilities', { seconds: 5 }); expect(poller.intervalMs).toBe(30000); poller.dispose();
  });
  it('keeps unknown source age unknown and refuses HTTP errors and malformed responses', async () => {
    const fetchImpl = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ jobs: [] }) });
    const sources = createStudioSources({ fetchImpl }); expect((await sources.jobs()).as_of).toBeNull();
    fetchImpl.mockResolvedValue({ ok: false });
    expect(await sources.jobs()).toMatchObject({ host_tainted: true, error: { code: 'source-failed' } });
    fetchImpl.mockResolvedValue({ ok: true, json: async () => ({ jobs: 'private' }) });
    expect(await sources.jobs()).toMatchObject({ data: null, host_tainted: true, error: { code: 'source-failed' } });
    fetchImpl.mockRejectedValue(new Error('private response'));
    const failed = await sources.jobs();
    expect(JSON.stringify(failed)).not.toContain('private'); expect(() => exportValue(failed)).toThrow();
  });
});
