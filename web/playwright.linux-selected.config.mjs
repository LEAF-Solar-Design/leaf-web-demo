import { defineConfig } from '@playwright/test'
import { realpathSync, statSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import base from './playwright.config.mjs'

const fail = () => { throw new Error('Invalid Linux selected application environment') }
const required = (key) => process.env[key] || fail()
const root = required('LEAF_LINUX_APPLICATION_ROOT')
const spec = required('LEAF_LINUX_APPLICATION_SPEC')
const title = required('LEAF_LINUX_APPLICATION_TITLE')
const executable = required('LEAF_LINUX_APPLICATION_BROWSER')
const worker = required('LEAF_NATIVE_GATE_WORKER')
const web = path.dirname(fileURLToPath(import.meta.url))
if (process.platform !== 'linux' || !/^[0-7]$/.test(worker)
    || !path.isAbsolute(root) || realpathSync(root) !== root
    || !path.basename(root).startsWith('studio-application-e2e-')
    || !web.startsWith(root + path.sep)
    || spec.length > 512 || !/^web\/e2e\/(?:[^/]+\/)*[^/]+\.spec\.(?:mjs|js)$/.test(spec)
    || spec.split('/').some(part => part === '.' || part === '..')
    || title.length > 1024 || /[\x00-\x1f\x7f]/.test(title)
    || executable !== '/home/offhost-seat/.cache/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell'
    || !statSync(executable).isFile()
    || required('NEXT_TELEMETRY_DISABLED') !== '1'
    || !/^[0-9a-f]{40}$/.test(required('LEAF_SOURCE_SHA'))) fail()
const selected = realpathSync(path.join(web, '..', spec))
if (!selected.startsWith(path.join(web, 'e2e') + path.sep) || !statSync(selected).isFile()) fail()
const literal = value => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

export default defineConfig({
  ...base,
  testDir: path.join(web, 'e2e'),
  testMatch: new RegExp(`^${literal(selected)}$`),
  testIgnore: [],
  grep: new RegExp(`^${literal(title)}$`),
  grepInvert: undefined,
  projects: [{ name: 'linux-selected' }],
  workers: 1,
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  repeatEach: 1,
  timeout: 120_000,
  globalTimeout: 360_000,
  outputDir: path.join(root, 'test-results'),
  reporter: [['json', { outputFile: path.join(root, 'report.json') }]],
  use: {
    ...base.use,
    video: 'off', trace: 'off', screenshot: 'on',
    launchOptions: { executablePath: executable, chromiumSandbox: true },
  },
  webServer: {
    ...base.webServer,
    timeout: 120_000,
    reuseExistingServer: false,
    env: { ...base.webServer.env, NEXT_TELEMETRY_DISABLED: '1' },
  },
})
