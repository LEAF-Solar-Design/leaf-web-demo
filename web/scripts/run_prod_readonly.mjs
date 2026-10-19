import { spawnSync } from 'node:child_process'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { resolve } from 'node:path'
import { validateProdConfig } from '../e2e/prod/prodConfig.mjs'
import { CANARY_INVENTORY, PROD_INVENTORY, validateReceipt } from '../e2e/prod/prodInventory.mjs'

const require = createRequire(import.meta.url)
const WEB = fileURLToPath(new URL('../', import.meta.url))
const PREFIX = 'PROD_READONLY_RECEIPT '
const INPUTS = ['LEAF_E2E_PROD_REQUIRED', 'LEAF_E2E_PROD_BASE_URL', 'LEAF_E2E_PROD_ACCEPTANCE_TOKEN', 'LEAF_E2E_EXPECTED_SHA', 'LEAF_E2E_PROD_PROJECT_ID']

// An allowlist also clears NODE_OPTIONS, DEBUG, PWDEBUG, PW_TEST_* and file loggers.
export function childEnvironment(env) {
  const clean = {}
  for (const name of ['PATH', 'Path', 'SystemRoot', 'WINDIR', 'COMSPEC', 'PATHEXT', 'HOME', 'USERPROFILE', 'TMP', 'TEMP', 'TMPDIR', 'LD_LIBRARY_PATH', 'PLAYWRIGHT_BROWSERS_PATH', ...INPUTS]) {
    if (env[name] !== undefined) clean[name] = env[name]
  }
  clean.CI = '1'
  clean.NO_COLOR = '1'
  return clean
}

export function launchProof({ env = process.env, args = [], canary = false, outputDir, emit = (line) => process.stdout.write(line) } = {}) {
  const failure = (code) => { emit(`${code}\n`); return { exitCode: 1, code } }
  if (args.length) return failure('PROD_ARGUMENTS_FORBIDDEN')
  try {
    if (!canary && !validateProdConfig(env).enabled) return failure('PROD_OPTIONAL_DISABLED')
    if (require('@playwright/test/package.json').version !== '1.60.0' || require('playwright/package.json').version !== '1.60.0') return failure('PROD_PLAYWRIGHT_VERSION')
    const clean = childEnvironment(env)
    if (canary) {
      // Never admit credentials or target configuration from the caller to a canary.
      for (const name of INPUTS) delete clean[name]
      if (!outputDir) return failure('PROD_CANARY_OUTPUT_REQUIRED')
      clean.LEAF_PROD_CANARY_OUTPUT = outputDir
    }
    const child = spawnSync(process.execPath, [resolve(require.resolve('playwright/package.json'), '..', 'cli.js'), 'test', '--config',
      canary ? 'e2e/prod-contract/playwright.canary.config.mjs' : 'playwright.prod.config.mjs'], {
      cwd: WEB, env: clean, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'],
      maxBuffer: 4 * 1024 * 1024, timeout: 10 * 60 * 1000,
    })
    // All child diagnostics are private, including collection errors and worker output.
    const lines = (child.stdout || '').split(/\r?\n/u).filter((line) => line.startsWith(PREFIX))
    if (child.error || child.signal || lines.length !== 1) return failure('PROD_RECEIPT_MISSING')
    const receipt = validateReceipt(JSON.parse(lines[0].slice(PREFIX.length)), canary ? CANARY_INVENTORY : PROD_INVENTORY)
    if (child.status !== 0 && receipt.status === 'passed') return failure('PROD_EXIT_MISMATCH')
    emit(`${PREFIX}${JSON.stringify(receipt)}\n`)
    return { exitCode: child.status === 0 && receipt.status === 'passed' ? 0 : 1, receipt }
  } catch { return failure('PROD_LAUNCH_FAILED') }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = launchProof({ args: process.argv.slice(2) }).exitCode
}
