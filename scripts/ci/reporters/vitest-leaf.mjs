import { mkdirSync, writeFileSync } from 'node:fs';
import { relative, resolve, sep } from 'node:path';

// Add alongside the existing reporter. Never write test names to stdout.
export default class LeafVitestReporter {
  onInit(ctx) { this.root = ctx.config?.root || process.cwd(); }

  onFinished(files = [], errors = []) {
    const directory = process.env.LEAF_TEST_REPORT_DIR;
    const suite = process.env.LEAF_READSET_SUITE;
    if (!directory || !suite) return;
    const root = process.env.LEAF_READSET_ROOT || this.root || process.cwd();
    const ids = [];
    const failed = new Set();
    let complete = errors.length === 0 && files.length > 0;
    const visit = (task, file, parents = []) => {
      const names = [...parents, task.name];
      if (task.type === 'test') {
        const path = relative(root, resolve(this.root || root, file)).split(sep).join('/');
        if (path.startsWith('../')) complete = false;
        const id = `${suite}::${path}::${names.join('::')}`;
        ids.push(id);
        // Preserve the first failure even if Vitest's internal retry passes.
        if (task.result?.state === 'fail' || task.result?.retryCount > 0) failed.add(id);
        if (!['pass', 'fail', 'skip', 'todo'].includes(task.result?.state) &&
            !['skip', 'todo'].includes(task.mode)) complete = false;
      }
      for (const child of task.tasks || []) visit(child, file, names);
    };
    for (const file of files) {
      if (file.result?.state === 'fail' && !(file.tasks || []).length) complete = false;
      for (const task of file.tasks || []) visit(task, file.filepath);
    }
    if (new Set(ids).size !== ids.length || !ids.length) complete = false;
    try {
      mkdirSync(directory, { recursive: true });
      writeFileSync(resolve(directory, `tests-vitest-${process.pid}.json`), JSON.stringify({
        schema: 'leaf.ci.test-report.v1', suite_id: suite,
        attempt: Number(process.env.LEAF_READSET_ATTEMPT || 1),
        test_ids: [...new Set(ids)].sort(), failed_test_ids: [...failed].sort(), complete,
      }) + '\n', { encoding: 'utf8', flag: 'wx' });
    } catch {
      process.stderr.write('WARNING: leaf Vitest report incomplete\n');
    }
  }
}
