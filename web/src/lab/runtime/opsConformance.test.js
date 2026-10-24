import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { runOperator } from './ops.js';

const fixture = JSON.parse(readFileSync(
  `${process.cwd()}/../server/tests/fixtures/lab-operator-cases.v1.json`, 'utf8',
));
function caseArguments(test) {
  const input = structuredClone(test.input);
  const settings = structuredClone(test.settings);
  if (test.non_json) {
    // JSON cannot carry these values. Map represents non-string object keys.
    const cycle = [];
    cycle.push(cycle);
    const values = {
      undefined, nan: NaN, infinity: Infinity, 'negative-infinity': -Infinity,
      'non-string-key': new Map([[1, null]]), unsupported: () => {}, cycle,
    };
    expect(Object.prototype.hasOwnProperty.call(values, test.non_json.kind)).toBe(true);
    expect(['selected', 'target']).toContain(test.non_json.at);
    if (test.non_json.at === 'selected') input.value[0].x = values[test.non_json.kind];
    else settings.value = values[test.non_json.kind];
  }
  return [input, settings];
}

describe('shared Lab operator conformance', () => {
  it('uses the versioned shared case contract', () => {
    expect(fixture.schema).toBe('leaf.lab-operator-cases.v1');
    expect(fixture.cases.length).toBeGreaterThanOrEqual(60);
    expect(new Set(fixture.cases.map(test => test.name)).size).toBe(fixture.cases.length);
  });

  it.each(fixture.cases)('$name', test => {
    const [input, settings] = caseArguments(test);
    const run = () => runOperator(test.op, input, settings);
    if (Object.prototype.hasOwnProperty.call(test, 'error')) {
      expect(['refused', 'unavailable']).toContain(test.error);
      let thrown;
      try { run(); } catch (error) { thrown = error; }
      expect(thrown).toBeInstanceOf(Error);
      expect(thrown.message === 'Test: operator unavailable in browser' ? 'unavailable' : 'refused').toBe(test.error);
    } else if (test.expect_undefined) {
      expect(run()).toBe(undefined);
    } else {
      expect(run()).toEqual(test.expect);
    }
  });

  it.each([new Date(0), 1n, Symbol('value'), Array(1), Object.assign([], { extra: 1 }), { nested: [undefined] }].map(value => [value]))(
    'refuses browser-only non-JSON values: %s', value => {
      for (const comparison of ['eq', 'ne']) {
        expect(() => runOperator('filter', { value: [{ x: value }] }, { field: 'x', comparison, value: null })).toThrow();
        expect(() => runOperator('filter', { value: [{ x: null }] }, { field: 'x', comparison, value })).toThrow();
      }
    },
  );

  it('preserves explicit undefined fields and coalesces latest to null', () => {
    expect(runOperator('field', { value: { x: undefined } }, { field: 'x' })).toBe(undefined);
    expect(runOperator('select', { value: [{ x: undefined }] }, { field: 'x' })).toEqual([undefined]);
    expect(runOperator('latest', { value: [undefined] })).toBe(null);
  });
});
