import { afterEach, describe, expect, it, vi } from 'vitest';
import { canonicalJSON } from '../contract.js';
import { contract as canvasContract, registry, resolveManifest } from '../canvas/registry.js';
import { connectPipe } from '../canvas/graphDoc.js';
import { portCompatibility } from '../canvas/portCompat.js';
import { contract, evaluateGraph, evaluateOperation, exportValue, runtimeValue } from '../runtime/evaluate.js';
import { createHostProvider, fingerprint, HOST_READ_IDS, isSourceName, readManifest, transformManifests } from './hostProvider.js';
import { guardSink, residencyOf, taintedBlocks, validateResidency } from './hostResidency.js';
import { kashbotSamplePack as samplePack } from './kashbotSamplePack.js';

afterEach(() => vi.useRealTimers());
const row = (source, data, metadata = {}) => ({ source, data, as_of: 100, ...metadata });
const block = (id, manifest, settings = {}) => ({ id, registry: {
  id: manifest.id, version: manifest.version.semver, code_sha256: manifest.version.code_sha256,
}, settings });
function graphFor(provider, sourceId = 'host-source', settings = { source: 'totals' }) {
  const entries = { ...registry, ...provider.entries() };
  return { entries, document: { schema: 'leaf.lab-graph.v1', blocks: [
    block('source', entries[`${sourceId}@1.0.0`], settings),
    block('total', entries['count@1.0.0']),
  ], pipes: [{ from: { node: 'source', port: 'rows' }, to: { node: 'total', port: 'value' } }],
  outputs: { total: { node: 'total', port: 'result' } } } };
}

describe('host provider lifecycle', () => {
  it('keeps a placed host source resolved when another source is announced', () => {
    const provider = createHostProvider({ framed: true });
    provider.announce([row('markets', { price: 1 })]);
    const { document } = graphFor(provider, 'host-source', { source: 'markets' });
    provider.announce([row('orders', { price: 2 })]);
    const resolved = resolveManifest(document.blocks[0], provider.entries());
    expect(resolved.problem).toBe('');
    expect(resolved.manifest.settings_schema.properties.source.enum).toEqual(['markets', 'orders']);
    expect(contract.graph(document, { ...registry, ...provider.entries() })).toBe(true);
  });

  it('arms exactly one framed timer and reports Sample after two seconds', () => {
    vi.useFakeTimers();
    const setTimer = vi.fn(globalThis.setTimeout);
    const onChange = vi.fn();
    const provider = createHostProvider({ framed: true, setTimer, onChange });
    provider.start(); provider.start();
    expect(Object.isFrozen(provider)).toBe(true);
    expect(setTimer).toHaveBeenCalledTimes(1);
    expect(setTimer.mock.calls[0][1]).toBe(2000);
    vi.advanceTimersByTime(1999);
    expect(provider.state.reason).toBeNull();
    vi.advanceTimersByTime(1);
    expect(provider.state.reason).toBe('Sample');
    expect(provider.entries()['host-source@1.0.0']).toBeUndefined();
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith(provider.state);
    provider.stop();
  });

  it('keeps empty announcements disabled until usable rows arrive', () => {
    vi.useFakeTimers();
    const clearTimer = vi.fn(globalThis.clearTimeout);
    const onChange = vi.fn();
    const provider = createHostProvider({ framed: true, clearTimer, onChange });
    provider.start();
    for (const rows of [[], [{ source: 'x' }]]) {
      provider.announce(rows);
      expect(provider.entries()['host-source@1.0.0']).toBeUndefined();
      expect(provider.state).toMatchObject({ announced: false, reason: null, sources: [] });
      expect(onChange).toHaveBeenLastCalledWith(provider.state);
    }
    expect(provider.state.dropped).toBe(1);
    expect(onChange).toHaveBeenCalledTimes(2);
    expect(clearTimer).not.toHaveBeenCalled();
    vi.advanceTimersByTime(2000);
    expect(provider.state.reason).toBe('Sample');
    provider.announce([]);
    expect(provider.state).toMatchObject({ announced: false, reason: 'Sample' });
    expect(provider.entries()['host-source@1.0.0']).toBeUndefined();
    provider.announce([{ source: 'markets', as_of: 1, data: { a: 1 } }]);
    expect(provider.state).toMatchObject({ announced: true, reason: null, sources: ['markets'] });
    const { document, entries } = graphFor(provider, 'host-source', { source: 'markets' });
    const manifest = entries['host-source@1.0.0'];
    expect(manifest.settings_schema.properties.source.enum).toEqual(['markets']);
    expect(() => contract.schema(manifest, 'lab-block.v1.schema.json')).not.toThrow();
    expect(contract.graph(document, entries)).toBe(true);
    expect(canvasContract.graph(document, entries)).toBe(true);
  });

  it('uses the window frame relation by default and never arms a standalone timer', () => {
    const win = {}; win.parent = win;
    const setTimer = vi.fn();
    const provider = createHostProvider({ win, setTimer });
    provider.start(); provider.start(); provider.announce([row('totals', {})]);
    expect(setTimer).not.toHaveBeenCalled();
    expect(provider.state.framed).toBe(false);
    expect(provider.entries()).toHaveProperty('studio-jobs@1.0.0');
    expect(provider.entries()).toHaveProperty('studio-capabilities@1.0.0');
    expect(provider.entries()['host-source@1.0.0']).toBeUndefined();
    expect(createHostProvider({ win: { parent: {} } }).state.framed).toBe(true);
    expect(createHostProvider({ win: null }).state.framed).toBe(false);
  });

  it('restores the host group on a late announcement and rebuilds the selector enum', () => {
    vi.useFakeTimers();
    const provider = createHostProvider({ framed: true, now: () => 900 });
    provider.start(); vi.advanceTimersByTime(2000);
    provider.announce([row('totals', {}), row('activity', {})]);
    const first = provider.entries()['host-source@1.0.0'];
    expect(provider.state).toMatchObject({ reason: null, announced: true, receivedAt: 900,
      sources: ['activity', 'totals'] });
    expect(first.settings_schema.properties.source.enum).toEqual(['activity', 'totals']);
    provider.announce([row('progress', {})]);
    const second = provider.entries()['host-source@1.0.0'];
    expect(second.settings_schema.properties.source.enum).toEqual(['activity', 'progress', 'totals']);
    expect(second.version.code_sha256).toBe(first.version.code_sha256);
    expect(provider.entries()['studio-jobs@1.0.0']).toBeUndefined();
  });

  it('clears an active timer on announce and stop', () => {
    vi.useFakeTimers();
    const clearTimer = vi.fn(globalThis.clearTimeout);
    const onChange = vi.fn();
    const provider = createHostProvider({ framed: true, clearTimer, onChange });
    provider.start(); provider.announce([row('totals', {})]);
    expect(clearTimer).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(2000);
    expect(provider.state.reason).toBeNull();
    expect(onChange).toHaveBeenCalledTimes(1);
    const stopped = createHostProvider({ framed: true, clearTimer, onChange });
    stopped.start(); stopped.stop(); stopped.stop();
    expect(clearTimer).toHaveBeenCalledTimes(2);
    vi.advanceTimersByTime(2000);
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it('replaces only named sources, drops malformed rows and snapshots nested data', () => {
    const provider = createHostProvider({ framed: true });
    const input = [row('totals', { nested: { count: 1 } }), row('activity', { count: 4 })];
    provider.announce(input);
    input[0].data.nested.count = 99;
    let graph = graphFor(provider);
    const saved = provider.sources(graph.document).source;
    expect(saved.data).toEqual([{ nested: { count: 1 } }]);
    expect(Object.isFrozen(saved.data[0].nested)).toBe(true);
    provider.announce([row('totals', { count: 2 }), row('', {}), row(' ', {}),
      row('bad', []), row('bad', null), row(1, {}), null]);
    expect(provider.state.dropped).toBe(6);
    expect(provider.state.sources).toEqual(['activity', 'totals']);
    graph = graphFor(provider);
    expect(provider.sources(graph.document).source.data).toEqual([{ count: 2 }]);
    graph.document.blocks[0].settings.source = 'activity';
    expect(provider.sources(graph.document).source.data).toEqual([{ count: 4 }]);
    graph.document.blocks[0].settings.source = 'missing';
    expect(provider.sources(graph.document)).toEqual({});
    const state = provider.state;
    expect(Object.isFrozen(state)).toBe(true);
    expect(Object.isFrozen(state.sources)).toBe(true);
    expect(provider.state).not.toBe(state);
  });

  it('preserves source times and metadata without using receipt time as source time', () => {
    const provider = createHostProvider({ framed: true, now: () => 10000 });
    provider.announce([row('totals', { count: 1 }, { as_of: 20, simulated: true }),
      row('totals', { count: 2 }, { as_of: 10, stale: true, truncated: true })]);
    let graph = graphFor(provider);
    expect(provider.sources(graph.document).source).toMatchObject({ as_of: 10, host_tainted: true,
      simulated: true, stale: true, truncated: true });
    provider.announce(Array.from({ length: 200 }, () => row('totals', {})));
    graph = graphFor(provider);
    expect(provider.sources(graph.document).source.truncated).toBe(true);
    provider.announce([row('totals', {}, { as_of: undefined })]);
    expect(provider.sources(graphFor(provider).document).source.as_of).toBeNull();
  });
});

describe('registry manifests and runtime integration', () => {
  it('fingerprints synchronously, stably and with settings sensitivity', () => {
    const first = readManifest('host-source', 'Dashboard source', { sourceNames: ['totals'] });
    const second = readManifest('host-source', 'Dashboard source', { sourceNames: ['totals'] });
    expect(fingerprint(first)).toMatch(/^[a-f0-9]{64}$/);
    expect(fingerprint(first)).toBe(fingerprint(second));
    expect(first.version.code_sha256).toBe(fingerprint(first));
    expect(fingerprint({ ...first, settings_schema: { type: 'string' } })).not.toBe(fingerprint(first));
    expect(HOST_READ_IDS).toEqual(['host-source', 'studio-jobs', 'studio-capabilities']);
    expect(Object.isFrozen(HOST_READ_IDS)).toBe(true);
  });

  it('derives one closed row schema, including nested objects and empty arrays', () => {
    const provider = createHostProvider({ framed: true });
    provider.announce([row('totals', { a: 1, b: 'x', pnl: { n: 2 } }),
      row('totals', { a: null, tags: [] })]);
    const entries = provider.entries();
    const expected = { type: 'object', additionalProperties: false, properties: {
      a: { type: ['null', 'number'] }, b: { type: ['string'] },
      pnl: { type: 'object', additionalProperties: false, properties: { n: { type: ['number'] } } },
      tags: { type: 'array', items: { type: ['string', 'number', 'boolean', 'null'] } },
    } };
    expect(entries['host-source@1.0.0'].outputs.rows.schema).toEqual({ type: 'array', items: expected });
    for (const manifest of Object.values(entries)) {
      if (manifest.effect === 'pure') expect(manifest.inputs.value.schema).toEqual(
        manifest.id === 'field' ? expected : { type: 'array', items: expected });
    }
    expect(entries['latest@1.0.0'].outputs.result.schema).toEqual(expected);
    const { document, entries: merged } = graphFor(provider);
    expect(canvasContract.graph(document, merged)).toBe(true);
    expect(evaluateGraph(document, merged, { sources: provider.sources(document) }).outputs.total)
      .toMatchObject({ data: 2, error: null, host_tainted: true });
  });

  it('unions mixed kinds recursively and updates all pins without changing placed fingerprints', () => {
    const provider = createHostProvider({ framed: true });
    provider.announce([row('totals', {})]);
    const { document, entries: before } = graphFor(provider);
    provider.announce([row('totals', { mixed: 1, list: [{ n: true }, null] }),
      row('other', { mixed: { nested: 'text' }, list: [2] })]);
    provider.setStudioValue('studio-jobs', runtimeValue([{ studio: false }]));
    const entries = { ...registry, ...provider.entries() };
    const schema = entries['host-source@1.0.0'].outputs.rows.schema.items;
    expect(schema.properties.mixed).toEqual({ type: ['number', 'object'], additionalProperties: false,
      properties: { nested: { type: ['string'] } } });
    expect(schema.properties.list).toEqual({ type: 'array', items: {
      type: ['null', 'number', 'object'], additionalProperties: false,
      properties: { n: { type: ['boolean'] } },
    } });
    expect(schema.properties.studio).toEqual({ type: ['boolean'] });
    for (const [key, manifest] of Object.entries(before)) {
      expect(entries[key].version.code_sha256).toBe(manifest.version.code_sha256);
    }
    const hostBefore = entries['host-source@1.0.0'];
    provider.announce([row('totals', { changed: 'shape' })]);
    const hostAfter = provider.entries()['host-source@1.0.0'];
    expect(hostAfter.outputs).not.toEqual(hostBefore.outputs);
    expect(hostAfter.version.code_sha256).toBe(hostBefore.version.code_sha256);
    document.blocks[0] = block('source', hostAfter, { source: 'totals' });
    expect(canvasContract.graph(document, { ...registry, ...provider.entries() })).toBe(true);
  });

  it('connects host rows through latest to a nullable field and validates the result', () => {
    const provider = createHostProvider({ framed: true });
    provider.announce([row('totals', { a: 1 }), row('totals', { a: 2 })]);
    const entries = { ...registry, ...provider.entries() };
    let document = { schema: 'leaf.lab-graph.v1', blocks: [
      block('source', entries['host-source@1.0.0'], { source: 'totals' }),
      block('recent', entries['latest@1.0.0']),
      block('picked', entries['field@1.0.0'], { field: 'a' }),
    ], pipes: [], outputs: { value: { node: 'picked', port: 'result' } } };
    document = connectPipe(document, { from: { node: 'source', port: 'rows' },
      to: { node: 'recent', port: 'value' } }, entries);
    document = connectPipe(document, { from: { node: 'recent', port: 'result' },
      to: { node: 'picked', port: 'value' } }, entries);
    expect(canvasContract.graph(document, entries)).toBe(true);
    expect(contract.graph(document, entries)).toBe(true);
    expect(evaluateGraph(document, entries, { sources: provider.sources(document) }).outputs.value)
      .toMatchObject({ data: 2, error: null, host_tainted: true });
  });

  it('connects host to count through port compatibility and refuses a numeric registry input', () => {
    const provider = createHostProvider({ framed: true });
    provider.announce([row('totals', { a: 1 })]);
    const { document, entries } = graphFor(provider);
    const pipe = document.pipes[0];
    document.pipes = [];
    expect(portCompatibility(entries['host-source@1.0.0'].outputs.rows,
      entries['count@1.0.0'].inputs.value).ok).toBe(true);
    expect(connectPipe(document, pipe, entries).pipes).toEqual([pipe]);
    document.blocks.push(block('numeric', registry['adjust-none@1.0.0']));
    expect(() => connectPipe(document, { from: pipe.from,
      to: { node: 'numeric', port: 'value' } }, entries)).toThrow('Schema cannot safely widen');
  });

  it('validates supplied row items against the derived output pin during evaluation', () => {
    const provider = createHostProvider({ framed: true });
    provider.announce([row('totals', { a: 1 })]);
    const { document, entries } = graphFor(provider);
    const result = evaluateGraph(document, entries, { sources: {
      source: runtimeValue([{ a: 'wrong kind' }], { host_tainted: true }),
    } });
    expect(result.values.source.rows.error.code).toBe('output-refused');
    expect(result.outputs.total).toMatchObject({ host_tainted: true, error: { code: 'upstream-failed' } });
  });

  it('provides exactly the requested browser operators with valid pin samples', () => {
    const transforms = transformManifests();
    expect(Object.values(transforms).map(value => value.id).sort()).toEqual([
      'count', 'field', 'filter', 'latest', 'max', 'mean', 'min', 'select', 'sort', 'sum', 'take',
    ]);
    for (const manifest of Object.values(transforms)) {
      expect(manifest).toMatchObject({ kind: 'builtin', effect: 'pure', lane: 'processing' });
      expect(canvasContract.block(manifest, manifest)).toBe(true);
      for (const [key, pin] of Object.entries(manifest.inputs)) contract.validate(manifest.sample_input[key], pin.schema);
      for (const [key, pin] of Object.entries(manifest.outputs)) {
        if (manifest.sample_output[key] !== null || !pin.nullable) contract.validate(manifest.sample_output[key], pin.schema);
      }
      expect(fingerprint(manifest)).toBe(manifest.version.code_sha256);
    }
  });

  // Keep these assertions against the real contracts and evaluator. Substituting
  // test-only pin schemas would hide incompatibilities at the integration seam.
  it('accepts the host-to-count proposal under both contracts and evaluates tainted rows', () => {
    const provider = createHostProvider({ framed: true });
    provider.announce([row('totals', { count: 1 }), row('totals', { count: 2 })]);
    const { document, entries } = graphFor(provider);
    expect(canvasContract.graph(document, entries)).toBe(true);
    expect(contract.graph(document, entries)).toBe(true);
    const result = evaluateGraph(document, entries, { sources: provider.sources(document) });
    expect(result.outputs.total).toMatchObject({ data: 2, host_tainted: true, error: null });
  });

  it('keeps CANARY data out of documents, state, telemetry and exported evaluation results', () => {
    const provider = createHostProvider({ framed: true });
    provider.announce([row('totals', { text: 'CANARY-7f3a' }, { handle: 'CANARY-7f3a' })]);
    const { document, entries } = graphFor(provider);
    const source = provider.sources(document).source;
    expect(JSON.stringify(provider.serialize())).not.toContain('CANARY');
    expect(JSON.stringify(provider.state)).not.toContain('CANARY');
    expect(canonicalJSON(document)).not.toContain('CANARY');
    const spy = vi.fn();
    expect(() => guardSink('telemetry', spy)({ cached: source })).toThrow('Test: host data cannot leave the runtime (telemetry)');
    expect(spy).not.toHaveBeenCalled();
    const result = evaluateGraph(document, entries, { sources: provider.sources(document) });
    expect(result.outputs.total.data).toBe(1);
    expect(() => exportValue(result.outputs.total)).toThrow('host data cannot leave the runtime');
    expect(() => guardSink('telemetry', spy)(result)).toThrow('host data cannot leave the runtime (telemetry)');
    expect(spy).not.toHaveBeenCalled();
  });
});

describe('registry residency and sink boundaries', () => {
  it('derives residency only from resolved manifests and propagates taint transitively', () => {
    const provider = createHostProvider({ framed: true });
    provider.announce([row('totals', {})]);
    const { document, entries } = graphFor(provider);
    document.blocks[0].residency = 'tenant';
    document.blocks.push(block('next', entries['count@1.0.0']));
    document.pipes.push({ from: { node: 'total', port: 'result' }, to: { node: 'next', port: 'value' } });
    expect([...taintedBlocks(document, entries)].sort()).toEqual(['next', 'source', 'total']);
    expect(validateResidency(document, entries)).toEqual({ ok: true, problems: [], tainted: ['next', 'source', 'total'] });
    expect(residencyOf(entries['host-source@1.0.0'])).toBe('host');
    expect(residencyOf({ ...entries['count@1.0.0'], residency: 'host' })).toBe('tenant');
    expect(residencyOf({ id: 'host-source', effect: 'pure', residency: 'host' })).toBe('tenant');
    document.blocks[0].registry.code_sha256 = 'f'.repeat(64);
    expect(taintedBlocks(document, entries).size).toBe(0);
  });

  it('refuses direct and transitive host edges into the server Strategy', () => {
    const provider = createHostProvider({ framed: true });
    provider.announce([row('totals', {})]);
    const { document, entries } = graphFor(provider);
    document.blocks.push(block('server', registry['custom-strategy@1.0.0']));
    const pipe = { from: { node: 'source', port: 'rows' }, to: { node: 'server', port: 'value' } };
    document.pipes.push(pipe);
    expect(validateResidency(document, entries).problems).toContain(
      'host data cannot reach Strategy (custom-strategy): it does not run in the browser');
    pipe.from = { node: 'total', port: 'result' };
    expect(validateResidency(document, entries).ok).toBe(false);
  });

  it.each(['a'.repeat(16), '12345678-abcd-abcd-abcd-123456789abc', 'sample@example.test', `opaque:${'a'.repeat(64)}`])(
    'refuses identifier-shaped host selectors without echoing them: %s', selector => {
      const provider = createHostProvider({ framed: true });
      provider.announce([row('totals', {})]);
      const { document, entries } = graphFor(provider);
      document.blocks[0].settings.source = selector;
      const result = validateResidency(document, entries);
      expect(result.problems).toEqual(['host block source settings must be non-identifying selectors']);
      expect(JSON.stringify(result)).not.toContain(selector);
    });

  it('guards aggregates, errors, caches, saves and widget bindings while preserving safe calls', () => {
    const source = runtimeValue([{ text: 'CANARY-7f3a' }], { host_tainted: true });
    const aggregate = evaluateOperation('count', { value: source });
    const error = evaluateOperation('divide', { value: aggregate, auxiliary: runtimeValue(0) });
    expect(aggregate.data).toBe(1);
    for (const value of [source, aggregate, error, { cache: [aggregate] },
      new Map([['cached', error]]), new Set([source]), { widget: { value: aggregate } }]) {
      for (const name of ['telemetry', 'save', 'serialize', 'export', 'widget']) {
        const spy = vi.fn();
        expect(() => guardSink(name, spy)(value)).toThrow(`host data cannot leave the runtime (${name})`);
        expect(spy).not.toHaveBeenCalled();
      }
    }
    const spy = vi.fn(function (value) { return (this?.prefix ?? '') + value; });
    const safe = guardSink('save', spy);
    expect(safe.call({ prefix: 'safe ' }, 'value')).toBe('safe value');
    safe(runtimeValue(2));
    const cycle = {}; cycle.self = cycle;
    safe(cycle);
    expect(spy).toHaveBeenCalledTimes(3);
  });

  it('fails closed when a nested graph exceeds the guard depth', () => {
    let value = runtimeValue('CANARY-7f3a', { host_tainted: true });
    for (let i = 0; i < 34; i += 1) value = { child: value };
    const spy = vi.fn();
    expect(() => guardSink('save', spy)(value)).toThrow('host data cannot leave the runtime (save)');
    expect(spy).not.toHaveBeenCalled();
  });
});

describe('Studio values and operator samples', () => {
  it('never serves sample values to non-operators and never adds sample entries', () => {
    const provider = createHostProvider({ framed: false, samplePack });
    const { document } = graphFor(provider, 'studio-jobs', {});
    expect(provider.sources(document)).toEqual({});
    expect(provider.state.sample).toBe(false);
    expect(provider.entries()).toEqual(createHostProvider({ framed: false }).entries());
    const truthy = createHostProvider({ framed: false, samplePack, operator: 'true' });
    expect(truthy.sources(document)).toEqual({});
  });

  it('serves only labelled simulated rows to operators, with real values taking precedence', () => {
    const provider = createHostProvider({ framed: false, samplePack, operator: true });
    const { document, entries } = graphFor(provider, 'studio-jobs', {});
    document.blocks.push(block('capabilities', entries['studio-capabilities@1.0.0']));
    const samples = provider.sources(document);
    const sampledEntries = { ...registry, ...provider.entries() };
    expect(sampledEntries['studio-jobs@1.0.0'].outputs.rows.schema.items.properties.label)
      .toEqual({ type: ['string'] });
    expect(canvasContract.graph(document, sampledEntries)).toBe(true);
    expect(evaluateGraph(document, sampledEntries, { sources: samples }).outputs.total)
      .toMatchObject({ data: 3, simulated: true, host_tainted: true, error: null });
    expect(provider.state.sample).toBe(true);
    for (const value of Object.values(samples)) {
      expect(value).toMatchObject({ simulated: true, host_tainted: true });
      expect(value.data.length).toBeGreaterThanOrEqual(3);
      expect(value.data.length).toBeLessThanOrEqual(5);
      expect(value.data.every(row => row.label === 'Sample')).toBe(true);
    }
    const real = runtimeValue([{ count: 8 }], { host_tainted: true, as_of: 80, stale: true });
    provider.setStudioValue('studio-jobs', real);
    expect(provider.sources(document).source).toMatchObject({ data: [{ count: 8 }], simulated: false, as_of: 80, stale: true });
    expect(provider.state.sample).toBe(true);
    provider.setStudioValue('studio-capabilities', runtimeValue([]));
    expect(provider.sources(document).capabilities.host_tainted).toBe(true);
    expect(provider.state.sample).toBe(false);
    expect(() => provider.setStudioValue('host-source', real)).toThrow('source unavailable');
    expect(() => provider.setStudioValue('studio-jobs', { data: [] })).toThrow('source shape refused');
    expect(createHostProvider({ framed: false, operator: true }).sources(document)).toEqual({});
  });

  it('passes branded Studio values to the block-id source map without requiring operator access', () => {
    const provider = createHostProvider({ framed: false });
    const { document } = graphFor(provider, 'studio-jobs', {});
    provider.setStudioValue('studio-jobs', runtimeValue([{ count: 3 }], { as_of: 42, truncated: true }));
    expect(provider.sources(document).source).toMatchObject({ data: [{ count: 3 }], as_of: 42,
      truncated: true, host_tainted: true, simulated: false });
    expect(provider.state.sample).toBe(false);
  });
});

describe('host provider full-diff regressions', () => {
  it('refuses copied tainted envelopes and taint forwarded through the receiver', () => {
    const tainted = runtimeValue([{ a: 1 }], { host_tainted: true });
    const spy = vi.fn();
    const guarded = guardSink('save', spy);
    for (const copy of [{ ...tainted }, JSON.parse(JSON.stringify(tainted))]) {
      expect(() => guarded({ cache: copy })).toThrow('host data cannot leave the runtime (save)');
    }
    expect(() => guarded.call({ cache: tainted }, 'safe')).toThrow('host data cannot leave the runtime (save)');
    expect(spy).not.toHaveBeenCalled();
  });

  it('drops identifying and malformed source names before deriving manifests', () => {
    const provider = createHostProvider({ framed: true });
    provider.announce([{ source: 'opaque:' + 'a'.repeat(64), data: {} }]);
    provider.announce([{ source: '\uD800', data: {} }]);
    expect(provider.state).toMatchObject({ sources: [], dropped: 2, announced: false });
    expect(() => provider.entries()).not.toThrow();
    expect(provider.entries()['host-source@1.0.0']).toBeUndefined();
    expect(provider.serialize().sources).toEqual([]);
    for (const name of ['a', 'studio-jobs', 'a' + '0'.repeat(31)]) expect(isSourceName(name)).toBe(true);
    for (const name of ['', 'Totals', 'a'.repeat(33), 'opaque:' + 'a'.repeat(64), '\uD800', null, 3]) {
      expect(isSourceName(name)).toBe(false);
    }
  });

  it('fails closed when a host-source manifest has no available names', () => {
    expect(() => readManifest('host-source', 'x')).toThrow('Test: source unavailable');
    expect(() => readManifest('host-source', 'x', { sourceNames: [] })).toThrow('Test: source unavailable');
  });

  it('evaluates host-source through select to a tainted array of primitives', () => {
    const provider = createHostProvider({ framed: true });
    provider.announce([row('totals', { a: 1 }), row('totals', { a: null })]);
    const entries = { ...registry, ...provider.entries() };
    const document = { schema: 'leaf.lab-graph.v1', blocks: [
      block('source', entries['host-source@1.0.0'], { source: 'totals' }),
      block('picked', entries['select@1.0.0'], { field: 'a' }),
    ], pipes: [{ from: { node: 'source', port: 'rows' }, to: { node: 'picked', port: 'value' } }],
    outputs: { picked: { node: 'picked', port: 'result' } } };
    expect(entries['select@1.0.0'].outputs.result).toMatchObject({
      schema: { type: 'array', items: { type: ['string', 'number', 'boolean', 'null'] } }, nullable: false,
    });
    expect(entries['select@1.0.0'].sample_output).toEqual({ result: [] });
    expect(contract.graph(document, entries)).toBe(true);
    expect(evaluateGraph(document, entries, { sources: provider.sources(document) }).outputs.picked)
      .toMatchObject({ data: [1, null], host_tainted: true, error: null });
  });

  it('validates primitive filter operands through the graph contract', () => {
    const provider = createHostProvider({ framed: true });
    provider.announce([row('totals', { a: 1 }), row('totals', { a: 2 })]);
    const entries = { ...registry, ...provider.entries() };
    const document = { schema: 'leaf.lab-graph.v1', blocks: [
      block('source', entries['host-source@1.0.0'], { source: 'totals' }),
      block('filtered', entries['filter@1.0.0'], { field: 'a', comparison: 'eq', value: 1 }),
    ], pipes: [{ from: { node: 'source', port: 'rows' }, to: { node: 'filtered', port: 'value' } }],
    outputs: { filtered: { node: 'filtered', port: 'result' } } };
    expect(entries['filter@1.0.0'].settings_schema.properties.value)
      .toEqual({ type: ['null', 'string', 'number', 'boolean'] });
    for (const value of [null, 'one', 1, true]) {
      document.blocks[1].settings.value = value;
      expect(contract.graph(document, entries)).toBe(true);
    }
    for (const value of [{}, []]) {
      document.blocks[1].settings.value = value;
      expect(() => contract.graph(document, entries)).toThrow();
    }
    document.blocks[1].settings.value = 1;
    expect(evaluateGraph(document, entries, { sources: provider.sources(document) }).outputs.filtered)
      .toMatchObject({ data: [{ a: 1 }], host_tainted: true, error: null });
  });

  it('refuses Studio values unless they are branded scalar arrays of plain rows', () => {
    const provider = createHostProvider({ framed: false });
    const { document } = graphFor(provider, 'studio-jobs', {});
    for (const value of [runtimeValue({ count: 3 }), runtimeValue([1]),
      runtimeValue([{ count: 3 }], { mode: 'stream' }), { ...runtimeValue([{ count: 3 }]) }]) {
      expect(() => provider.setStudioValue('studio-jobs', value)).toThrow('Test: source shape refused');
      expect(provider.sources(document)).toEqual({});
    }
  });

  it('derives sample row pins before the first Studio graph evaluation', () => {
    const provider = createHostProvider({ framed: false, operator: true, samplePack });
    const { document, entries } = graphFor(provider, 'studio-jobs', {});
    const schema = entries['studio-jobs@1.0.0'].outputs.rows.schema.items;
    expect(schema.properties.task).toEqual({ type: ['string'] });
    expect(schema.properties.feature).toEqual({ type: ['string'] });
    expect(provider.state.sample).toBe(false);
    expect(contract.graph(document, entries)).toBe(true);
    const result = evaluateGraph(document, entries, { sources: provider.sources(document, entries) });
    expect(result.outputs.total).toMatchObject({ data: 3, simulated: true, host_tainted: true, error: null });
    expect(provider.state.sample).toBe(true);
    provider.setStudioValue('studio-jobs', runtimeValue([{ count: 8 }]));
    expect(provider.state.sample).toBe(true);
    expect(provider.entries()['studio-jobs@1.0.0'].outputs.rows.schema.items.properties.task).toBeUndefined();
    provider.sources({ ...document, blocks: [] }, entries);
    expect(provider.state.sample).toBe(false);
    expect(provider.entries()['studio-jobs@1.0.0'].outputs.rows.schema.items.properties.feature)
      .toEqual({ type: ['string'] });
  });
});
