import { defineConfig } from '@playwright/test'
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
  // Detached child sessions can outlive a failed boot before the launcher's
  // SIGTERM handler is installed. Clean BEFORE boot: failed boots skip teardown.
  const cleanup = async () => {
    const { execFile } = require('node:child_process')
    const { promisify } = require('node:util')
    const { writeFileSync } = require('node:fs')
    const ports = [5275, 8230, 8240, 8250]
    const receipt = { ok: false, remaining: [], signalled: [], error: null }
    const deadline = Date.now() + 10_000
    const save = () => writeFileSync(process.env.LEAF_E2E_PORT_CLEANUP, JSON.stringify(receipt) + '\n')
    const finish = (error) => {
      receipt.error = error
      save()
      if (error || receipt.signalled.length) console.log('[link-flow cleanup] ' + JSON.stringify(receipt))
    }
    const timer = setTimeout(() => {
      finish('Port cleanup exceeded 10 seconds')
      process.exit(1)
    }, 10_000)
    const run = (file, args) => promisify(execFile)(file, args, {
      timeout: Math.max(1, Math.min(750, deadline - Date.now())),
      killSignal: 'SIGKILL', windowsHide: true,
    })
    const listeners = async () => {
      const found = []
      if (process.platform === 'win32') {
        const { stdout } = await run('netstat', ['-ano'])
        for (const line of stdout.split(/\r?\n/)) {
          const fields = line.trim().split(/\s+/)
          const port = Number(fields[1]?.split(':').pop())
          if (fields[0] === 'TCP' && fields[3] === 'LISTENING' && ports.includes(port)) {
            found.push({ port, pid: Number(fields[4]) || null })
          }
        }
      } else {
        try {
          for (const port of ports) {
            let stdout
            try {
              ;({ stdout } = await run('lsof', ['-nP', '-ti', 'tcp:' + port, '-sTCP:LISTEN']))
            } catch (error) {
              if (error.code === 1 && !error.stdout && !error.stderr) continue
              throw error
            }
            for (const pid of stdout.trim().split(/\s+/).filter(Boolean)) {
              found.push({ port, pid: Number(pid) || null })
            }
          }
        } catch {
          found.length = 0
          const { stdout } = await run('ss', ['-ltnp'])
          for (const line of stdout.split(/\r?\n/)) {
            const fields = line.trim().split(/\s+/)
            const port = Number(fields[3]?.split(':').pop())
            if (fields[0] !== 'LISTEN' || !ports.includes(port)) continue
            const pids = [...line.matchAll(/pid=(\d+)/g)]
            if (!pids.length) found.push({ port, pid: null })
            for (const match of pids) found.push({ port, pid: Number(match[1]) })
          }
        }
      }
      return found.filter((item, index) => found.findIndex((other) => other.port === item.port && other.pid === item.pid) === index)
    }
    const signal = (items, name) => {
      for (const { port, pid } of items) {
        if (!Number.isInteger(pid) || pid <= 0 || pid === process.pid) {
          throw new Error('Cannot kill listener on port ' + port + ' pid ' + pid)
        }
        try {
          process.kill(pid, name)
          receipt.signalled.push({ port, pid, signal: name })
          console.log('[link-flow cleanup] ' + name + ' pid ' + pid + ' port ' + port)
        } catch (error) {
          if (error.code !== 'ESRCH') throw error
        }
      }
    }
    try {
      receipt.remaining = await listeners()
      if (receipt.remaining.length) {
        signal(receipt.remaining, 'SIGTERM')
        await new Promise((resolve) => setTimeout(resolve, 3000))
        receipt.remaining = await listeners()
        if (receipt.remaining.length) {
          signal(receipt.remaining, 'SIGKILL')
          await new Promise((resolve) => setTimeout(resolve, 200))
          receipt.remaining = await listeners()
        }
      }
      if (receipt.remaining.length) throw new Error('Listeners remain: ' + JSON.stringify(receipt.remaining))
      receipt.ok = true
      finish(null)
    } catch (error) {
      finish(error.message)
      process.exitCode = 1
    } finally {
      clearTimeout(timer)
    }
  }
  // Base64 keeps the inline Node source literal in both cmd.exe and POSIX shells.
  const cleanupSource = Buffer.from('(' + cleanup.toString() + ')()').toString('base64')
  const cleanupCommand = `node -e "eval(Buffer.from('${cleanupSource}','base64').toString())"`
  webServer = {
    command: cleanupCommand + ' && npm --prefix harness run build && python scripts/start-leaf.py --with-harness --broker-port 8240 --app-port 8230 --harness-port 8250 --web-port 5275',
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
