import { test, expect } from '@playwright/test'
import { execFile } from 'node:child_process'
import { createHash } from 'node:crypto'
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
  // Green device jobs retain their log, but discard attachment files.
  console.log('LEAF_WIDGET_BROWSER ' + JSON.stringify({
    schema: 'leaf.widget-browser.v1',
    expected: report.stats.expected, unexpected: report.stats.unexpected,
    skipped: report.stats.skipped, flaky: report.stats.flaky,
    errors: report.errors.length, report_sha256: createHash('sha256').update(stdout).digest('hex'),
  }))
})

// Separate child keeps the original six recovery assertions/count unchanged.
test('saved literal preview clock stays idle without skips', async ({}, testInfo) => {
  test.setTimeout(180000)
  const staticConfig = fileURLToPath(new URL('../../playwright.widget-static.config.mjs', import.meta.url))
  const { stdout, stderr } = await execute(process.execPath, [
    cli, 'test', '--config', staticConfig, '--reporter=json', '--workers=1', '--global-timeout=120000',
  ], { cwd: web, windowsHide: true, timeout: 160000, maxBuffer: 16 * 1024 * 1024 })
  const report = JSON.parse(stdout)
  const reportPath = testInfo.outputPath('widget-static-clock-report.json')
  await writeFile(reportPath, stdout)
  await testInfo.attach('widget-static-clock-report.json', { path: reportPath, contentType: 'application/json' })
  if (stderr) {
    const logPath = testInfo.outputPath('widget-static-clock-build.log')
    await writeFile(logPath, stderr)
    await testInfo.attach('widget-static-clock-build.log', { path: logPath, contentType: 'text/plain' })
  }
  expect(report.errors).toEqual([])
  expect(report.stats).toMatchObject({ expected: 1, unexpected: 0, skipped: 0, flaky: 0 })
  console.log('LEAF_WIDGET_BROWSER ' + JSON.stringify({
    schema: 'leaf.widget-static-clock.v1',
    expected: report.stats.expected, unexpected: report.stats.unexpected,
    skipped: report.stats.skipped, flaky: report.stats.flaky,
    errors: report.errors.length, report_sha256: createHash('sha256').update(stdout).digest('hex'),
  }))
})

// Input journeys run separately from the settled recovery and idle-clock counts.
test('keyboard, Unicode and text-boundary widget journeys pass without skips', async ({}, testInfo) => {
  test.setTimeout(180000)
  const inputConfig = fileURLToPath(new URL('../../playwright.widget-input.config.mjs', import.meta.url))
  const { stdout, stderr } = await execute(process.execPath, [
    cli, 'test', '--config', inputConfig, '--reporter=json', '--workers=1', '--global-timeout=120000',
  ], { cwd: web, windowsHide: true, timeout: 160000, maxBuffer: 16 * 1024 * 1024 })
  const report = JSON.parse(stdout)
  const reportPath = testInfo.outputPath('widget-input-report.json')
  await writeFile(reportPath, stdout)
  await testInfo.attach('widget-input-report.json', { path: reportPath, contentType: 'application/json' })
  if (stderr) {
    const logPath = testInfo.outputPath('widget-input-build.log')
    await writeFile(logPath, stderr)
    await testInfo.attach('widget-input-build.log', { path: logPath, contentType: 'text/plain' })
  }
  expect(report.errors).toEqual([])
  expect(report.stats).toMatchObject({ expected: 3, unexpected: 0, skipped: 0, flaky: 0 })
  console.log('LEAF_WIDGET_BROWSER ' + JSON.stringify({
    schema: 'leaf.widget-input.v1',
    expected: report.stats.expected, unexpected: report.stats.unexpected,
    skipped: report.stats.skipped, flaky: report.stats.flaky,
    errors: report.errors.length, report_sha256: createHash('sha256').update(stdout).digest('hex'),
  }))
})
