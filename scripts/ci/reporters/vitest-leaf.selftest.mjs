import { deepStrictEqual, strictEqual } from 'node:assert';
import { resolve } from 'node:path';
import { testReport } from './vitest-leaf.mjs';

const root = resolve('leaf-vitest-fixture');
const test = (name, extra = {}) => ({ type: 'test', name, ...extra });
const file = (name, tasks) => ({ filepath: resolve(root, name), tasks });
const report = testReport([
  file('case.test.js', [{ type: 'suite', name: 'group', tasks: [
    test('same', { result: { state: 'pass' } }),
    test('same', { result: { state: 'fail' } }),
    test('same', { result: { state: 'pass', retryCount: 1 } }),
    test('skip', { mode: 'skip' }),
    test('todo', { mode: 'todo' }),
  ] }]),
  file('other.test.js', [test('same', { result: { state: 'pass' } })]),
], [], root, root, 'fixture', 1);
const prefix = 'fixture::case.test.js::group::';
deepStrictEqual(report.test_ids, [
  prefix + 'same', prefix + 'same#2', prefix + 'same#3',
  prefix + 'skip', prefix + 'todo', 'fixture::other.test.js::same',
].sort());
deepStrictEqual(report.failed_test_ids, [prefix + 'same#2', prefix + 'same#3']);
strictEqual(report.renamed_duplicate_ids, 2);
strictEqual(report.complete, true);
deepStrictEqual(report.incomplete_reasons, []);

// Literal suffixes must not collide with generated suffixes in either order.
for (const names of [['same', 'same', 'same#2'], ['same#2', 'same', 'same']]) {
  const collision = testReport([file('case.test.js', names.map(name =>
    test(name, { mode: 'skip' })))], [], root, root, 'fixture', 1);
  strictEqual(collision.test_ids.length, names.length);
  strictEqual(collision.complete, true);
  deepStrictEqual(collision.incomplete_reasons, []);
}
process.stdout.write('leaf Vitest reporter standalone self-test passed\n');
