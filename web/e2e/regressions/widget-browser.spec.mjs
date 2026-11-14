import { test, expect } from '@playwright/test'
import { execFile } from 'node:child_process'
import { writeFile } from 'node:fs/promises'
import { promisify } from 'node:util'
import { fileURLToPath } from 'node:url'

const execute = promisify(execFile)
const web = fileURLToPath(new URL('../../', import.meta.url))
const cli = fileURLToPath(new URL('../../node_modules/@playwright/test/cli.js', import.meta.url))
const config = fileURLToPath(new URL('../../playwright.widgets.config.mjs', import.meta.url))

// The existing studio-walk-regressions native gate discovers this bridge.
// The child owns its fixture build/server; all six cases must actually pass.
test('widget browser journey executes all six fixture regressions without skips', async ({}, testInfo) => {
  test.setTimeout(180000)
  const { stdout, stderr } = await execute(process.execPath, [
    cli, 'test', '--config', config, '--reporter=json', '--workers=1', '--global-timeout=120000',
  ], { cwd: web, windowsHide: true, timeout: 160000, maxBuffer: 16 * 1024 * 1024 })
  const report = JSON.parse(stdout)
  const reportPath = testInfo.outputPath('widget-browser-report.json')
  await writeFile(reportPath, stdout)
  await testInfo.attach('widget-browser-report.json', { path: reportPath, contentType: 'application/json' })
  if (stderr) {
    const logPath = testInfo.outputPath('widget-browser-build.log')
    await writeFile(logPath, stderr)
    await testInfo.attach('widget-browser-build.log', { path: logPath, contentType: 'text/plain' })
  }
  expect(report.errors).toEqual([])
  expect(report.stats).toMatchObject({ expected: 6, unexpected: 0, skipped: 0, flaky: 0 })
})
