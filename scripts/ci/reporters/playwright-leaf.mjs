import { mkdirSync, writeFileSync } from 'node:fs';
import { relative, resolve, sep } from 'node:path';

export default class LeafPlaywrightReporter {
  constructor() { this.ids = new Set(); this.failed = new Set(); this.seen = new Set(); this.complete = true; }
  printsToStdio() { return false; }
  identity(test) {
    const root = process.env.LEAF_READSET_ROOT || this.root;
    const path = relative(root, test.location.file).split(sep).join('/');
    if (path.startsWith('../')) this.complete = false;
    return `${process.env.LEAF_READSET_SUITE}::${path}::${test.titlePath().join('::')}::${test.id}`;
  }
  onBegin(config, suite) {
    this.root = config.rootDir;
    const tests = suite.allTests();
    for (const test of tests) this.ids.add(this.identity(test));
    if (this.ids.size !== tests.length || !tests.length) this.complete = false;
  }
  onTestEnd(test, result) {
    const id = this.identity(test);
    this.seen.add(id);
    if (['failed', 'timedOut', 'interrupted'].includes(result.status)) this.failed.add(id);
    if (!['passed', 'failed', 'skipped', 'timedOut'].includes(result.status)) this.complete = false;
  }
  onError() { this.complete = false; }
  onEnd(result) {
    const directory = process.env.LEAF_TEST_REPORT_DIR;
    const suite = process.env.LEAF_READSET_SUITE;
    if (!directory || !suite) return;
    const complete = this.complete && ['passed', 'failed'].includes(result.status) &&
      this.ids.size > 0 && this.ids.size === this.seen.size && [...this.ids].every(id => this.seen.has(id));
    try {
      mkdirSync(directory, { recursive: true });
      writeFileSync(resolve(directory, `tests-playwright-${process.pid}.json`), JSON.stringify({
        schema: 'leaf.ci.test-report.v1', suite_id: suite,
        attempt: Number(process.env.LEAF_READSET_ATTEMPT || 1),
        test_ids: [...this.ids].sort(), failed_test_ids: [...this.failed].sort(), complete,
      }) + '\n', { encoding: 'utf8', flag: 'wx' });
    } catch {
      process.stderr.write('WARNING: leaf Playwright report incomplete\n');
    }
  }
}
