import { defineConfig } from '@playwright/test'
import { execFileSync } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

// The focused CI row uses the SAME start-leaf launcher as the managed local
// proof script. An already managed stack remains owned by its caller.
const linkFlowBoot = process.env.LEAF_E2E_MANAGED !== '1'
  && process.argv.some((arg) => arg.endsWith('link-service-flow.spec.mjs'))
let webServer
if (linkFlowBoot) {
  const repo = fileURLToPath(new URL('../', import.meta.url))
  const run = mkdtempSync(join(tmpdir(), 'leaf-link-flow-'))
  const paths = {
    LEAF_STORE_DIR: 'drawings', LEAF_GUEST_STORE_DIR: 'guest-drawings',
    LEAF_UPLOADS_DIR: 'uploads', LEAF_GRANTS_DIR: 'grants',
    LEAF_TENANTS_DIR: 'tenants', LEAF_TENANT_GIT_DIR: 'tenant-git',
    LEAF_TENANT_MCP_DIR: 'tenant-mcp',
  }
  for (const [key, name] of Object.entries(paths)) {
    process.env[key] = join(run, name)
    mkdirSync(process.env[key])
  }
  // Exercise the real capability gate with an isolated policy, never a
  // fake-mode authorization bypass or an edit to the shipped demo tier.
  const policy = JSON.parse(readFileSync(join(repo, 'server/entitlements.json'), 'utf8'))
  policy.demo.link_service = true
  const policyFile = join(run, 'entitlements.json')
  writeFileSync(policyFile, JSON.stringify(policy) + '\n')
  Object.assign(process.env, {
    TENANT_MCP_FAKE_OAUTH: '1', LEAF_ENTITLEMENTS_FILE: policyFile,
    LEAF_AUTH_LIVE: '0', LEAF_AGENT_MOCK: '1',
    LEAF_OPS_SECRET: randomUUID(), LEAF_GUEST_SECRET: randomUUID(),
    LEAF_E2E_MANAGED: '1', LEAF_CUSTOMIZATION_R5_MODE: 'off', LEAF_CUSTOMIZATION_R6_MODE: 'off',
    LEAF_E2E_BASE_URL: 'http://127.0.0.1:5275', LEAF_E2E_API_BASE: 'http://127.0.0.1:8230',
    LEAF_APP_PUBLIC_BASE_URL: 'http://127.0.0.1:8230', LEAF_CORS_ORIGINS: 'http://127.0.0.1:5275',
    VITE_TENANT_ID: 'demo-tenant', VITE_STARTUP_FETCH_TIMEOUT_MS: '15000',
    JOBS_DB: join(run, 'jobs.db'), SESSIONS_DB: join(run, 'sessions.db'),
    LEAF_AGENT_LEDGER: join(run, 'agent-ledger.jsonl'), BROKER_LEDGER: join(run, 'broker-ledger.jsonl'),
    BROKER_TENANTS: join(run, 'broker-tenants.json'), LEAF_GRANT_FILE: join(run, 'no-legacy-grant.token'),
    CLAUDE_CODE_OAUTH_TOKEN: '', ANTHROPIC_API_KEY: '',
    LEAF_E2E_PORT_CLEANUP: join(run, 'port-cleanup.json'),
  })
  // Playwright checks URL availability before webServer.command (Astra r2 P1).
  // Clean at load time or a stale server on 5275 would skip cleanup entirely.
  if (process.env.LEAF_LINK_FLOW_PORTS_CLEARED !== '1' && !process.argv.includes('--list')) {
    try {
      execFileSync(process.execPath, [
        fileURLToPath(new URL('./e2e/local/clear-stale-ports.mjs', import.meta.url)),
        '--ports', '5275,8230,8240,8250',
        '--receipt', process.env.LEAF_E2E_PORT_CLEANUP,
      ], { stdio: 'inherit', timeout: 15_000, windowsHide: true })
    } catch {
      throw new Error('link-flow port cleanup failed; receipt: ' + process.env.LEAF_E2E_PORT_CLEANUP)
    }
    process.env.LEAF_LINK_FLOW_PORTS_CLEARED = '1'
  }
  webServer = {
    command: 'npm --prefix harness run build && python scripts/start-leaf.py --with-harness --broker-port 8240 --app-port 8230 --harness-port 8250 --web-port 5275',
    cwd: repo,
    url: 'http://127.0.0.1:5275',
    reuseExistingServer: false,
    stdout: 'pipe',
    timeout: 180_000,
    gracefulShutdown: { signal: 'SIGTERM', timeout: 10_000 },
    env: { ...process.env },
  }
}

export default defineConfig({
  testDir: './e2e/local',
  workers: 1,
  webServer,
  timeout: 60_000,
  outputDir: '../artifacts/unified-surface-proof/local/test-results',
  reporter: [['list'], ['html', {
    open: 'never',
    outputFolder: '../artifacts/unified-surface-proof/local/report',
  }]],
  use: {
    baseURL: process.env.LEAF_E2E_BASE_URL || 'http://127.0.0.1:5275',
    browserName: 'chromium',
    headless: true,
    video: 'on',
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    viewport: { width: 1600, height: 1000 },
  },
})
