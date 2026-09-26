import { deepStrictEqual, strictEqual } from 'node:assert';
import { mkdirSync, writeFileSync } from 'node:fs';
import { isAbsolute, relative, resolve, sep } from 'node:path';
import { pathToFileURL } from 'node:url';

function testReport(files, errors, root, vitestRoot, suite, attempt) {
  const ids = [];
  const failed = new Set();
  const counts = new Map();
  const count = (reason, n = 1) => counts.set(reason, (counts.get(reason) || 0) + n);
  if (errors.length) count('errors', errors.length);
  if (!files.length) count('no_files');
  const visit = (task, file, parents = []) => {
    const names = [...parents, task.name];
    if (task.type === 'test') {
      const path = relative(root, resolve(vitestRoot, file)).split(sep).join('/');
      if (path === '..' || path.startsWith('../') || isAbsolute(path)) count('path_escape');
      const id = `${suite}::${path}::${names.join('::')}`;
      ids.push(id);
      // Preserve the first failure even if Vitest's internal retry passes.
      if (task.result?.state === 'fail' || task.result?.retryCount > 0) failed.add(id);
      if (!['pass', 'fail', 'skip', 'todo'].includes(task.result?.state) &&
          !['skip', 'todo'].includes(task.mode)) count('task_without_result');
    }
    for (const child of task.tasks || []) visit(child, file, names);
  };
  for (const file of files) {
    if (file.result?.state === 'fail' && !(file.tasks || []).length) count('failed_file_without_tasks');
    for (const task of file.tasks || []) visit(task, file.filepath);
  }
  const duplicates = ids.length - new Set(ids).size;
  if (duplicates) count('duplicate_ids', duplicates);
  if (!ids.length) count('no_ids');
  const incomplete_reasons = [...counts].map(([reason, n]) =>
    ['no_files', 'no_ids'].includes(reason) ? reason : `${reason}:${n}`).sort();
  return {
    schema: 'leaf.ci.test-report.v1', suite_id: suite, attempt,
    test_ids: [...new Set(ids)].sort(), failed_test_ids: [...failed].sort(),
    complete: incomplete_reasons.length === 0, incomplete_reasons,
  };
}

// Add alongside the existing reporter. Never write test names to stdout.
export default class LeafVitestReporter {
  onInit(ctx) { this.root = ctx.config?.root || process.cwd(); }

  onFinished(files = [], errors = []) {
    const directory = process.env.LEAF_TEST_REPORT_DIR;
    const suite = process.env.LEAF_READSET_SUITE;
    if (!directory || !suite) return;
    const root = process.env.LEAF_READSET_ROOT || this.root || process.cwd();
    const report = testReport(files, errors, root, this.root || root, suite,
                              Number(process.env.LEAF_READSET_ATTEMPT || 1));
    if (!report.complete) {
      process.stderr.write(`WARNING: leaf Vitest report incomplete: ${report.incomplete_reasons.join(',')}\n`);
    }
    try {
      mkdirSync(directory, { recursive: true });
      writeFileSync(resolve(directory, `tests-vitest-${process.pid}.json`), JSON.stringify(report) +
                    '\n', { encoding: 'utf8', flag: 'wx' });
    } catch {
      process.stderr.write('WARNING: leaf Vitest report incomplete: report_write_failed\n');
    }
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href &&
    process.argv.includes('--self-test')) {
  const root = resolve('leaf-vitest-fixture');
  const test = (name, extra = {}) => ({ type: 'test', name, ...extra });
  const file = (tasks, extra = {}) => ({ filepath: resolve(root, 'case.test.js'), tasks, ...extra });
  const cases = [
    { files: [file([test('skip', { mode: 'skip' }), test('todo', { mode: 'todo' }),
                    test('state-skip', { result: { state: 'skip' } })])], reasons: [] },
    { files: [file([test('pass', { result: { state: 'pass' } }),
                    test('fail', { result: { state: 'fail' } })])], reasons: [] },
    { files: [], errors: [{}, {}], reasons: ['errors:2', 'no_files', 'no_ids'] },
    { files: [file([], { result: { state: 'fail' } })], reasons: ['failed_file_without_tasks:1', 'no_ids'] },
    { files: [file([test('pending'), test('running', { result: { state: 'run' } })])],
      reasons: ['task_without_result:2'] },
    { files: [file([test('same', { mode: 'skip' }), test('same', { mode: 'todo' })])],
      reasons: ['duplicate_ids:1'] },
    { files: [file([test('outside', { mode: 'skip' })], { filepath: resolve(root, '../outside.js') })],
      reasons: ['path_escape:1'] },
  ];
  for (const fixture of cases) {
    const report = testReport(fixture.files, fixture.errors || [], root, root, 'fixture', 1);
    deepStrictEqual(report.incomplete_reasons, fixture.reasons);
    strictEqual(report.complete, fixture.reasons.length === 0);
  }
  const retried = testReport([file([test('retry', { result: { state: 'pass', retryCount: 1 } })])],
                            [], root, root, 'fixture', 1);
  deepStrictEqual(retried.failed_test_ids, retried.test_ids);
  process.stdout.write('leaf Vitest reporter self-test passed\n');
}
