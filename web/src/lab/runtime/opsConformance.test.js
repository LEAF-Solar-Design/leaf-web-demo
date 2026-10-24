import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { runOperator } from './ops.js';

const fixture = JSON.parse(readFileSync(
  `${process.cwd()}/../server/tests/fixtures/lab-operator-cases.v1.json`, 'utf8',
));
const errors = {
  refused: 'Test: operator input refused',
  unavailable: 'Test: operator unavailable in browser',
};

describe('shared Lab operator conformance', () => {
  it('uses the versioned shared case contract', () => {
    expect(fixture.schema).toBe('leaf.lab-operator-cases.v1');
    expect(fixture.cases.length).toBeGreaterThanOrEqual(60);
    expect(new Set(fixture.cases.map(test => test.name)).size).toBe(fixture.cases.length);
  });

  it.each(fixture.cases)('$name', test => {
    const run = () => runOperator(test.op, test.input, test.settings);
    if (Object.prototype.hasOwnProperty.call(test, 'error')) {
      expect(Object.keys(errors)).toContain(test.error);
      expect(run).toThrow(new Error(errors[test.error]));
    } else {
      expect(run()).toEqual(test.expect);
    }
  });
});
