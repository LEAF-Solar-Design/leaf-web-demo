import { canonicalJSON, createContract } from '../contract.js';
import blockSchema from '../contract/lab-block.v1.schema.json';
import pinSchema from '../contract/lab-pin.v1.schema.json';
import graphSchema from '../contract/lab-graph.v1.schema.json';
import embedSchema from '../contract/lab-embed.v1.schema.json';
import manifestSchema from '../contract/leaf-block-manifest.v1.schema.json';
import { MAX_ROWS, STREAM_ADAPTERS, hasOperator, runOperator } from './ops.js';

export const contract = createContract({
  'lab-block.v1.schema.json': blockSchema,
  'lab-pin.v1.schema.json': pinSchema,
  'lab-graph.v1.schema.json': graphSchema,
  'lab-embed.v1.schema.json': embedSchema,
  'fixtures/lab/leaf-block-manifest.v1.schema.json': manifestSchema,
});
const envelopes = new WeakSet();
// Only snapshots made here are reusable. Object.freeze on caller data is not trust.
const snapshots = new WeakSet();
const own = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
const clock = () => performance.now();
const freeze = value => {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    Object.values(value).forEach(freeze); Object.freeze(value);
  }
  return value;
};

// Only branded envelopes can enter evaluation. JSON cannot impersonate metadata.
export function runtimeValue(data, metadata = {}) {
  if (!data || typeof data !== 'object' || !snapshots.has(data)) canonicalJSON(data);
  const modes = ['scalar', 'stream'];
  const mode = metadata.mode ?? 'scalar';
  if (!modes.includes(mode) || (mode === 'stream' && !Array.isArray(data))) throw new Error('Test: value mode refused');
  const asOf = metadata.as_of ?? null;
  if (asOf !== null && (!Number.isFinite(asOf) || asOf < 0)) throw new Error('Test: source time refused');
  let truncated = metadata.truncated === true;
  function bounded(value) {
    if (value && typeof value === 'object' && snapshots.has(value)) return value;
    let copy;
    if (Array.isArray(value)) {
      truncated ||= value.length > MAX_ROWS;
      copy = value.slice(0, MAX_ROWS).map(bounded);
    } else if (value && typeof value === 'object') {
      copy = Object.fromEntries(Object.entries(value).map(([key, child]) => [key, bounded(child)]));
    } else {
      return value;
    }
    Object.freeze(copy);
    snapshots.add(copy);
    return copy;
  }
  const value = freeze({
    data: bounded(data), mode, as_of: asOf,
    host_tainted: metadata.host_tainted === true,
    simulated: metadata.simulated === true, stale: metadata.stale === true, truncated,
    error: metadata.error ? { code: metadata.error.code || 'evaluation-failed' } : null,
  });
  envelopes.add(value);
  return value;
}

function requireValue(value) {
  if (!envelopes.has(value)) throw new Error('Test: runtime value required');
  return value;
}

export function sourceAge(value, now = Date.now()) {
  requireValue(value);
  return value.as_of === null ? null : Math.max(0, now - value.as_of);
}

function inherited(values) {
  const dated = values.map(value => value.as_of).filter(time => time !== null);
  return {
    as_of: values.some(value => value.host_tainted && value.as_of === null) ? null : dated.length ? Math.min(...dated) : null,
    host_tainted: values.some(value => value.host_tainted),
    simulated: values.some(value => value.simulated), stale: values.some(value => value.stale),
    truncated: values.some(value => value.truncated),
  };
}

export function evaluateOperation(id, inputs = {}, settings = {}, options = {}) {
  const values = Object.values(inputs).map(requireValue);
  const metadata = inherited(values);
  try {
    if (!hasOperator(id)) throw new Error();
    if (values.some(value => value.error)) return runtimeValue(null, { ...metadata, error: { code: 'upstream-failed' } });
    const stream = values.some(value => value.mode === 'stream');
    const adapter = STREAM_ADAPTERS.includes(id);
    if (stream && !adapter && options.outputMode !== 'stream') {
      return runtimeValue(null, { ...metadata, error: { code: 'stream-adapter-required' } });
    }
    const data = runOperator(id, Object.fromEntries(Object.entries(inputs).map(([key, value]) => [key, value.data])), settings);
    return runtimeValue(data, { ...metadata, mode: adapter ? 'scalar' : options.outputMode || 'scalar' });
  } catch {
    // Operator messages and input values never become diagnostics.
    return runtimeValue(null, { ...metadata, error: { code: 'operator-refused' } });
  }
}

// Export, telemetry and persistence sinks share the same refusal rule. Local
// widgets can display the envelope directly; they must retain its metadata.
export function exportValue(value) {
  requireValue(value);
  if (value.host_tainted) throw new Error('Test: host data cannot leave the runtime');
  return JSON.parse(canonicalJSON(value));
}

export function evaluateGraph(graph, registry, { sources = {}, now = clock } = {}) {
  const started = now();
  contract.graph(graph, registry);
  const blocks = new Map(graph.blocks.map(block => [block.id, block]));
  const incoming = new Map(graph.blocks.map(block => [block.id, []]));
  for (const pipe of graph.pipes) incoming.get(pipe.to.node).push(pipe);
  // This cache is evaluation-local and stores whole envelopes, including errors.
  const cache = new Map();
  // Immutable data can share a successful check only for this exact pin.
  const validated = new Map();
  function visit(id) {
    if (cache.has(id)) return cache.get(id);
    const block = blocks.get(id);
    const manifest = registry[`${block.registry.id}@${block.registry.version}`];
    const inputs = Object.fromEntries(incoming.get(id).map(pipe => [pipe.to.port, visit(pipe.from.node)[pipe.from.port]]));
    for (const [port, pin] of Object.entries(manifest.inputs)) {
      if (!own(inputs, port) && pin.has_default) inputs[port] = runtimeValue(pin.default, { mode: pin.mode });
    }
    const metadata = inherited(Object.values(inputs));
    let result;
    if (manifest.effect === 'read' && ['studio-jobs', 'studio-capabilities', 'host-source'].includes(manifest.id)) {
      const supplied = own(sources, id) ? requireValue(sources[id]) : null;
      result = supplied ? runtimeValue(supplied.data, { ...supplied, ...inherited([...Object.values(inputs), supplied]), host_tainted: true })
        : runtimeValue(null, { ...metadata, host_tainted: true, error: { code: 'source-unavailable' } });
    } else if (manifest.effect !== 'pure' || manifest.kind !== 'builtin' || !hasOperator(manifest.id)) {
      result = runtimeValue(null, { ...metadata, error: { code: 'browser-execution-refused' } });
    } else {
      const outputMode = Object.values(manifest.outputs)[0].mode;
      result = evaluateOperation(manifest.id, inputs, block.settings, { outputMode });
    }
    const outputs = Object.fromEntries(Object.entries(manifest.outputs).map(([port, pin]) => {
      let value = result;
      try {
        if (!value.error) {
          if (pin.mode !== value.mode) throw new Error();
          let checked = validated.get(pin);
          if (!checked) { checked = new WeakSet(); validated.set(pin, checked); }
          if (!value.data || typeof value.data !== 'object' || !checked.has(value.data)) {
            const check = item => { if (!(item === null && pin.nullable)) contract.validate(item, pin.schema); };
            if (pin.mode === 'stream') value.data.forEach(check); else check(value.data);
            if (value.data && typeof value.data === 'object') checked.add(value.data);
          }
        }
      } catch { value = runtimeValue(null, { ...value, mode: 'scalar', error: { code: 'output-refused' } }); }
      return [port, value];
    }));
    cache.set(id, Object.freeze(outputs));
    return outputs;
  }
  graph.blocks.forEach(block => visit(block.id));
  const finished = now();
  return Object.freeze({
    values: Object.freeze(Object.fromEntries(cache)),
    outputs: Object.freeze(Object.fromEntries(Object.entries(graph.outputs).map(([name, endpoint]) => [name, visit(endpoint.node)[endpoint.port]]))),
    timing: Object.freeze({ started_ns: String(Math.round(started * 1e6)), finished_ns: String(Math.round(finished * 1e6)), duration_ms: Math.max(0, finished - started) }),
  });
}
