import { bench, describe } from 'vitest';
import { evaluateGraph, runtimeValue } from './evaluate.js';

// Bench-only measurement. No host data is used. Report nearest-rank p50 and p99
// in milliseconds; the 50 ms budget remains fixed even if a machine misses it.
export function measureRuntime(graph, registry, { samples = 9, sources = {}, now = () => performance.now() } = {}) {
  if (!Number.isInteger(samples) || samples < 1) throw new Error('Test: measurement samples required');
  evaluateGraph(graph, registry, { sources, now });
  const durations = Array.from({ length: samples }, () => {
    const started = now(); evaluateGraph(graph, registry, { sources, now });
    return Math.max(0, now() - started);
  }).sort((a, b) => a - b);
  return Object.freeze({
    blocks: graph.blocks.length, samples, target_ms: 50,
    p50_ms: durations[Math.ceil(samples * 0.5) - 1],
    p99_ms: durations[Math.ceil(samples * 0.99) - 1],
  });
}

describe('Lab runtime 100 blocks x 1000 rows', () => {
  const pin = () => ({ mode: 'scalar', schema: { type: 'array', items: { type: 'number' } },
    unit: 'none', nullable: false, required: true, has_default: false });
  const manifest = (id, effect) => ({ schema_version: 1, id, title: 'Strategy', profile: 'lab-registry',
    version: { semver: '1.0.0', code_sha256: '0'.repeat(64) }, kind: 'builtin', effect,
    inputs: effect === 'read' ? {} : { value: pin() }, outputs: { result: pin() },
    settings_schema: { type: 'object', additionalProperties: false, properties: {} },
    ui_schema: {}, latency_budget_us: { profile: 'legacy-us-to-std4-ns-ms-v1', p50: 10, p99: 100 },
    lane: 'engines', execution_target: 'runner', residency: 'tenant', permissions: [], state: 'stateless', tests: ['sample'],
    sample_input: effect === 'read' ? {} : { value: [1, 2] }, sample_output: { result: [1, 2] } });
  const source = manifest('host-source', 'read');
  const registry = { 'host-source@1.0.0': source, 'identity@1.0.0': manifest('identity', 'pure') };
  const graph = {
    schema: 'leaf.lab-graph.v1',
    blocks: Array.from({ length: 100 }, (_, i) => ({ id: `block-${i}`, registry: {
      id: i ? 'identity' : 'host-source', version: '1.0.0', code_sha256: source.version.code_sha256,
    }, settings: {} })),
    pipes: Array.from({ length: 99 }, (_, i) => ({ from: { node: `block-${i}`, port: 'result' }, to: { node: `block-${i + 1}`, port: 'value' } })),
    outputs: { result: { node: 'block-99', port: 'result' } },
  };
  const sources = { 'block-0': runtimeValue(Array.from({ length: 1000 }, (_, i) => i)) };
  const measurement = measureRuntime(graph, registry, { sources });
  console.info('Lab runtime benchmark', { ...measurement, rows: 1000, target_met: measurement.p50_ms <= measurement.target_ms });
  bench('evaluate 100 x 1000 including graph validation', () => {
    evaluateGraph(graph, registry, { sources });
  });
});
