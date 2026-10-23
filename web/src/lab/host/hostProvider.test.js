import { afterEach, describe, expect, it, vi } from 'vitest';
import { canonicalJSON } from '../contract.js';
import { contract as canvasContract, registry } from '../canvas/registry.js';
import { connectPipe } from '../canvas/graphDoc.js';
import { portCompatibility } from '../canvas/portCompat.js';
import { contract, evaluateGraph, evaluateOperation, exportValue, runtimeValue } from '../runtime/evaluate.js';
import { createHostProvider, fingerprint, HOST_READ_IDS, readManifest, transformManifests } from './hostProvider.js';
import { guardSink, residencyOf, taintedBlocks, validateResidency } from './hostResidency.js';
import { kashbotSamplePack as samplePack } from './kashbotSamplePack.js';

afterEach(() => vi.useRealTimers());
const row = (source, data, metadata = {}) => ({ source, data, as_of: 100, ...metadata });
const block = (id, manifest, settings = {}) => ({ id, registry: {
  id: manifest.id, version: manifest.version.semver, code_sha256: manifest.version.code_sha256,
}, settings });
function graphFor(provider, sourceId = 'host-source', settings = { source: 'Totals' }) {
  const entries = { ...registry, ...provider.entries() };
  return { entries, document: { schema: 'leaf.lab-graph.v1', blocks: [
    block('source', entries[`${sourceId}@1.0.0`], settings),
    block('total', entries['count@1.0.0']),
  ], pipes: [{ from: { node: 'source', port: 'rows' }, to: { node: 'total', port: 'value' } }],
  outputs: { total: { node: 'total', port: 'result' } } } };
}

describe('host provider lifecycle', () => {
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

  it('uses the window frame relation by default and never arms a standalone timer', () => {
    const win = {}; win.parent = win;
    const setTimer = vi.fn();
    const provider = createHostProvider({ win, setTimer });
    provider.start(); provider.start(); provider.announce([row('Totals', {})]);
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
    provider.announce([row('Totals', {}), row('Activity', {})]);
    const first = provider.entries()['host-source@1.0.0'];
    expect(provider.state).toMatchObject({ reason: null, announced: true, receivedAt: 900,
      sources: ['Activity', 'Totals'] });
    expect(first.settings_schema.properties.source.enum).toEqual(['Activity', 'Totals']);
    provider.announce([row('Progress', {})]);
    const second = provider.entries()['host-source@1.0.0'];
    expect(second.settings_schema.properties.source.enum).toEqual(['Activity', 'Progress', 'Totals']);
    expect(second.version.code_sha256).not.toBe(first.version.code_sha256);
    expect(provider.entries()['studio-jobs@1.0.0']).toBeUndefined();
  });

  it('clears an active timer on announce and stop', () => {
    vi.useFakeTimers();
    const clearTimer = vi.fn(globalThis.clearTimeout);
    const onChange = vi.fn();
    const provider = createHostProvider({ framed: true, clearTimer, onChange });
    provider.start(); provider.announce([row('Totals', {})]);
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
    const input = [row('Totals', { nested: { count: 1 } }), row('Activity', { count: 4 })];
    provider.announce(input);
    input[0].data.nested.count = 99;
    let graph = graphFor(provider);
    const saved = provider.sources(graph.document).source;
    expect(saved.data).toEqual([{ nested: { count: 1 } }]);
    expect(Object.isFrozen(saved.data[0].nested)).toBe(true);
    provider.announce([row('Totals', { count: 2 }), row('', {}), row(' ', {}),
      row('Bad', []), row('Bad', null), row(1, {}), null]);
    expect(provider.state.dropped).toBe(6);
    expect(provider.state.sources).toEqual(['Activity', 'Totals']);
    graph = graphFor(provider);
    expect(provider.sources(graph.document).source.data).toEqual([{ count: 2 }]);
    graph.document.blocks[0].settings.source = 'Activity';
    expect(provider.sources(graph.document).source.data).toEqual([{ count: 4 }]);
    graph.document.blocks[0].settings.source = 'Missing';
    expect(provider.sources(graph.document)).toEqual({});
    const state = provider.state;
    expect(Object.isFrozen(state)).toBe(true);
    expect(Object.isFrozen(state.sources)).toBe(true);
    expect(provider.state).not.toBe(state);
  });

  it('preserves source times and metadata without using receipt time as source time', () => {
    const provider = createHostProvider({ framed: true, now: () => 10000 });
    provider.announce([row('Totals', { count: 1 }, { as_of: 20, simulated: true }),
      row('Totals', { count: 2 }, { as_of: 10, stale: true, truncated: true })]);
    let graph = graphFor(provider);
    expect(provider.sources(graph.document).source).toMatchObject({ as_of: 10, host_tainted: true,
      simulated: true, stale: true, truncated: true });
    provider.announce(Array.from({ length: 200 }, () => row('Totals', {})));
    graph = graphFor(provider);
    expect(provider.sources(graph.document).source.truncated).toBe(true);
    provider.announce([row('Totals', {}, { as_of: undefined })]);
    expect(provider.sources(graphFor(provider).document).source.as_of).toBeNull();
  });
});

describe('registry manifests and runtime integration', () => {
  it('fingerprints synchronously, stably and with settings sensitivity', () => {
    const first = readManifest('host-source', 'Dashboard source', { sourceNames: ['Totals'] });
    const second = readManifest('host-source', 'Dashboard source', { sourceNames: ['Totals'] });
    expect(fingerprint(first)).toMatch(/^[a-f0-9]{64}$/);
    expect(fingerprint(first)).toBe(fingerprint(second));
    expect(first.version.code_sha256).toBe(fingerprint(first));
    expect(fingerprint({ ...first, settings_schema: { type: 'string' } })).not.toBe(fingerprint(first));
    expect(HOST_READ_IDS).toEqual(['host-source', 'studio-jobs', 'studio-capabilities']);
    expect(Object.isFrozen(HOST_READ_IDS)).toBe(true);
  });

  it('derives one closed row schema, including nested objects and empty arrays', () => {
    const provider = createHostProvider({ framed: true });
    provider.announce([row('Totals', { a: 1, b: 'x', pnl: { n: 2 } }),
      row('Totals', { a: null, tags: [] })]);
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
    provider.announce([row('Totals', {})]);
    const { document, entries: before } = graphFor(provider);
    provider.announce([row('Totals', { mixed: 1, list: [{ n: true }, null] }),
      row('Other', { mixed: { nested: 'text' }, list: [2] })]);
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
      // A new source name changes only the host selector settings fingerprint.
      if (manifest.id !== 'host-source') expect(entries[key].version.code_sha256).toBe(manifest.version.code_sha256);
    }
    const hostBefore = entries['host-source@1.0.0'];
    provider.announce([row('Totals', { changed: 'shape' })]);
    const hostAfter = provider.entries()['host-source@1.0.0'];
    expect(hostAfter.outputs).not.toEqual(hostBefore.outputs);
    expect(hostAfter.version.code_sha256).toBe(hostBefore.version.code_sha256);
    document.blocks[0] = block('source', hostAfter, { source: 'Totals' });
    expect(canvasContract.graph(document, { ...registry, ...provider.entries() })).toBe(true);
  });

  it('connects host rows through latest to a nullable field and validates the result', () => {
    const provider = createHostProvider({ framed: true });
    provider.announce([row('Totals', { a: 1 }), row('Totals', { a: 2 })]);
    const entries = { ...registry, ...provider.entries() };
    let document = { schema: 'leaf.lab-graph.v1', blocks: [
      block('source', entries['host-source@1.0.0'], { source: 'Totals' }),
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
    provider.announce([row('Totals', { a: 1 })]);
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
    provider.announce([row('Totals', { a: 1 })]);
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
    provider.announce([row('Totals', { count: 1 }), row('Totals', { count: 2 })]);
    const { document, entries } = graphFor(provider);
    expect(canvasContract.graph(document, entries)).toBe(true);
    expect(contract.graph(document, entries)).toBe(true);
    const result = evaluateGraph(document, entries, { sources: provider.sources(document) });
    expect(result.outputs.total).toMatchObject({ data: 2, host_tainted: true, error: null });
  });

  it('keeps CANARY data out of documents, state, telemetry and exported evaluation results', () => {
    const provider = createHostProvider({ framed: true });
    provider.announce([row('Totals', { text: 'CANARY-7f3a' }, { handle: 'CANARY-7f3a' })]);
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
    provider.announce([row('Totals', {})]);
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
    provider.announce([row('Totals', {})]);
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
      provider.announce([row('Totals', {})]);
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
    expect(() => provider.setStudioValue('studio-jobs', { data: [] })).toThrow('runtime value required');
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
