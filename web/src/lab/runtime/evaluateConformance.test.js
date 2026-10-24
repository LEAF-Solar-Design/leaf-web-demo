import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { evaluateGraph, runtimeValue } from './evaluate.js';

const fixture = JSON.parse(readFileSync(
  `${process.cwd()}/../server/tests/fixtures/lab-graph-cases.v1.json`, 'utf8',
));
const fields = ['data', 'mode', 'error', 'truncated', 'host_tainted', 'simulated', 'stale'];

describe('shared Lab graph evaluation conformance', () => {
  it.each([
    [{}, 'evaluation-failed'], [[], 'evaluation-failed'],
    [['ignored'], 'evaluation-failed'], [{ code: 'example' }, 'example'],
    [null, null], [false, null], [0, null], [NaN, null],
  ])('normalizes error metadata %j to %s', (error, expected) => {
    expect(runtimeValue(null, { error }).error?.code ?? null).toBe(expected);
  });

  it('uses the versioned shared case contract', () => {
    expect(fixture.schema).toBe('leaf.lab-graph-cases.v1');
    expect(fixture.cases.length).toBeGreaterThanOrEqual(25);
    expect(new Set(fixture.cases.map(test => test.name)).size).toBe(fixture.cases.length);
    expect(fixture.cases.filter(test => test.admission === 'refused').map(test => test.name)).toEqual(
      expect.arrayContaining([
        'admission-settings', 'admission-incompatible-unit', 'admission-duplicate-input',
        'admission-required-input', 'admission-missing-output', 'admission-integer-output-name',
      ]),
    );
  });

  it.each(fixture.cases)('$name', test => {
    const registry = Object.fromEntries(test.registry.map(manifest => [
      `${manifest.id}@${manifest.version.semver}`, manifest,
    ]));
    const before = structuredClone(test);
    if (test.admission === 'refused') {
      expect(() => evaluateGraph(test.graph, registry)).toThrow();
      expect(test).toEqual(before);
      return;
    }
    const result = evaluateGraph(test.graph, registry);
    expect(Object.keys(result.outputs).sort()).toEqual(Object.keys(test.expect).sort());
    for (const [name, expected] of Object.entries(test.expect)) {
      const value = result.outputs[name];
      const actual = Object.fromEntries(fields.map(key => [key, key === 'error' ? value.error?.code ?? null : value[key]]));
      expect(actual).toEqual({ ...expected, error: expected.error === 'server-execution-refused'
        ? 'browser-execution-refused' : expected.error });
    }
    expect(test).toEqual(before);
  });
});
