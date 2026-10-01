import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { writeFileSync } from 'node:fs'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const helperPath = fileURLToPath(new URL('./_harness.mjs', import.meta.url))
const reporterPath = fileURLToPath(import.meta.url)
const webDir = fileURLToPath(new URL('../../', import.meta.url))
const defaultConfig = join(webDir, 'playwright.regressions.config.mjs')
const playwrightCLI = join(webDir, 'node_modules', '@playwright', 'test', 'cli.js')
const hash = (data) => createHash('sha256').update(data).digest('hex')
const stripANSI = (value) => String(value).replace(/\u001b\[[0-9;]*m/g, '')
const errorText = (error) => stripANSI(error.message || error.value || '')

export function exactSpecPattern(path) {
  return '^' + resolve(path).split(/[\\/]/).map((part) => part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('[\\\\/]') + '$'
}

function specialResult(report) {
  const errors = [...report.errors, ...report.results.flatMap((result) => result.errors)]
  for (const [marker, code] of [['STOPPED', 76], ['QUEUED', 75]]) {
    if (errors.some((error) => new RegExp(`^(?:Error: )?${marker}:`, 'm').test(error))) {
      return { verdict: marker, exitCode: code }
    }
  }
  if (!report.selected || report.results.length !== report.selected || report.results.some((result) => result.status === 'skipped')) {
    return { verdict: 'INVALID_SELECTION', exitCode: 3 }
  }
  if (errors.some((error) => /^(?:Error: )?UNSUPPORTED_LOCAL:/m.test(error))) {
    return { verdict: 'UNSUPPORTED_LOCAL', exitCode: 4 }
  }
  return null
}

// This module doubles as the config's strict reporter. Only recorded failure
// messages are oracles; test titles, source excerpts and stdout are not.
export default class RegressionReporter {
  constructor() { this.report = { selected: 0, results: [], errors: [], status: 'failed' } }
  printsToStdio() { return true }
  onBegin(config, suite) { this.report.selected = suite.allTests().length }
  onTestEnd(test, result) {
    this.report.results.push({
      file: test.location.file,
      title: test.title,
      status: result.status,
      expectedStatus: test.expectedStatus,
      errors: result.errors.map(errorText),
    })
  }
  onError(error) { this.report.errors.push(errorText(error)) }
  onEnd(result) {
    this.report.status = result.status
    this.special = specialResult(this.report)
    try {
      if (process.env.LEAF_REGRESSION_REPORT) {
        writeFileSync(process.env.LEAF_REGRESSION_REPORT, JSON.stringify(this.report) + '\n')
      } else {
        for (const error of [...this.report.errors, ...this.report.results.flatMap((test) => test.errors)]) console.error(error)
        console.log(JSON.stringify({ ...this.report, verdict: this.special?.verdict || result.status }))
      }
    } catch (error) {
      console.error(`Regression reporter failed: ${error.message}`)
      this.special = { verdict: 'REPORTER_ERROR', exitCode: 2 }
    }
    return { status: this.special ? 'failed' : result.status }
  }
  onExit() {
    // All worker fixtures have torn down before this hook. Playwright's CLI
    // otherwise collapses every failure to 1, losing admission distinctions.
    if (this.special) process.exit(this.special.exitCode)
  }
}

function classify(report, childCode, marker, spec) {
  const special = specialResult(report)
  if (special) return special
  if (report.results.some((result) => resolve(result.file) !== spec)) return { verdict: 'WRONG_FAILURE', exitCode: 2 }
  if (childCode === 0 && report.status === 'passed' && report.results.every((result) => result.status === 'passed')) {
    return { verdict: 'NOT_RED', exitCode: 1 }
  }
  const failure = report.results.some((result) => result.status === 'failed'
    && result.expectedStatus === 'passed' && result.errors.some((error) => error.includes(marker)))
  if (childCode === 1 && report.status === 'failed' && !report.errors.length && failure) {
    return { verdict: 'RED', exitCode: 0 }
  }
  return { verdict: 'WRONG_FAILURE', exitCode: 2 }
}

async function runCLI(argv) {
  const receipt = { verdict: 'WRONG_FAILURE', exitCode: 2, specSha256: null, helperSha256: null }
  let temporary
  try {
    const options = {}
    for (let index = 0; index < argv.length; index += 2) {
      const flag = argv[index]
      if (!['--spec', '--marker', '--config'].includes(flag) || !argv[index + 1] || options[flag]) {
        throw new Error('Usage: expectRed.mjs --spec <file> --marker <text> [--config <file>]')
      }
      options[flag] = argv[index + 1]
    }
    if (!options['--spec'] || !options['--marker']?.trim()) throw new Error('--spec and a nonempty --marker are required')
    const spec = resolve(options['--spec'])
    receipt.spec = spec
    receipt.specSha256 = hash(await readFile(spec))
    receipt.helperSha256 = hash(await readFile(helperPath))
    temporary = await mkdtemp(join(tmpdir(), 'leaf-expect-red-'))
    const reportPath = join(temporary, 'result.json')
    const child = spawn(process.execPath, [playwrightCLI, 'test', exactSpecPattern(spec),
      '--config', resolve(options['--config'] || defaultConfig), '--reporter', reporterPath, '--retries=0'], {
      cwd: webDir,
      windowsHide: true,
      env: { ...process.env, LEAF_REGRESSION_SPEC: spec, LEAF_REGRESSION_REPORT: reportPath },
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    let output = ''
    for (const stream of [child.stdout, child.stderr]) stream.on('data', (chunk) => { output = (output + chunk).slice(-64000) })
    const code = await new Promise((accept, reject) => { child.once('error', reject); child.once('close', accept) })
    let report
    try { report = JSON.parse(await readFile(reportPath, 'utf8')) } catch {
      throw new Error(`Playwright did not produce a regression report (exit ${code}): ${output}`)
    }
    Object.assign(receipt, classify(report, code, options['--marker'], spec), {
      selected: report.selected,
      ran: report.results.filter((result) => result.status !== 'skipped').length,
      skipped: report.results.filter((result) => result.status === 'skipped').length,
    })
    if (hash(await readFile(spec)) !== receipt.specSha256 || hash(await readFile(helperPath)) !== receipt.helperSha256) {
      Object.assign(receipt, { verdict: 'WRONG_FAILURE', exitCode: 2, error: 'Spec or harness changed during execution' })
    }
    for (const error of [...report.errors, ...report.results.flatMap((result) => result.errors)]) console.error(error)
    if (output.trim()) console.error(output.trim())
  } catch (error) {
    Object.assign(receipt, { verdict: 'WRONG_FAILURE', exitCode: 2, error: error.message })
  } finally {
    if (temporary) {
      try { await rm(temporary, { recursive: true, force: true }) } catch (error) {
        Object.assign(receipt, { verdict: 'WRONG_FAILURE', exitCode: 2, error: `Receipt cleanup failed: ${error.message}` })
      }
    }
  }
  console.log(JSON.stringify(receipt))
  process.exitCode = receipt.exitCode
}

if (process.argv[1] && resolve(process.argv[1]) === reporterPath) await runCLI(process.argv.slice(2))
