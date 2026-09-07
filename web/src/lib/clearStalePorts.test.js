// @vitest-environment node
import { afterEach, expect, it } from 'vitest'
import { spawn, spawnSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync, existsSync } from 'node:fs'
import { createConnection, createServer } from 'node:net'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { clearStalePorts } from '../../e2e/local/clear-stale-ports.mjs'

const script = fileURLToPath(new URL('../../e2e/local/clear-stale-ports.mjs', import.meta.url))
const directories = []
const children = []

function receiptFile() {
  const directory = mkdtempSync(join(tmpdir(), 'leaf-port-pin-'))
  directories.push(directory)
  return join(directory, 'receipt.json')
}

afterEach(() => {
  for (const child of children.splice(0)) {
    if (child.exitCode === null && child.signalCode === null) {
      try { child.kill('SIGKILL') } catch (error) {
        if (error.code !== 'ESRCH') throw error
      }
    }
  }
  for (const directory of directories.splice(0)) rmSync(directory, { recursive: true, force: true })
})

function refusedConnection(port) {
  return new Promise((resolve, reject) => {
    const socket = createConnection({ host: '127.0.0.1', port })
    socket.setTimeout(2000)
    socket.once('connect', () => {
      socket.destroy()
      reject(new Error('Listener still accepts connections'))
    })
    socket.once('timeout', () => {
      socket.destroy()
      reject(new Error('Connection did not refuse within 2 seconds'))
    })
    socket.once('error', (error) => {
      socket.destroy()
      if (error.code === 'ECONNREFUSED') resolve()
      else reject(error)
    })
  })
}

it('clears a detached listener and writes the exact receipt', async () => {
  const receiptPath = receiptFile()
  const child = spawn(process.execPath, ['-e',
    "const s = require('node:net').createServer(); s.listen(0, '127.0.0.1', () => process.stdout.write(String(s.address().port) + '\\n'))",
  ], { detached: true, stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true })
  children.push(child)
  const port = await new Promise((resolve, reject) => {
    let output = ''
    const timer = setTimeout(() => reject(new Error('Listener did not start')), 5000)
    child.once('error', (error) => { clearTimeout(timer); reject(error) })
    child.once('exit', (code) => { clearTimeout(timer); reject(new Error('Listener exited: ' + code)) })
    child.stdout.on('data', (chunk) => {
      output += chunk
      const end = output.indexOf('\n')
      if (end !== -1) {
        clearTimeout(timer)
        const line = output.slice(0, end).trim()
        const port = Number(line)
        if (!/^\d+$/.test(line) || !Number.isInteger(port) || port < 1 || port > 65535) {
          reject(new Error('Listener printed an invalid port: ' + JSON.stringify(line)))
        } else {
          resolve(port)
        }
      }
    })
  })
  const receipt = await clearStalePorts({ ports: [port], receiptPath })
  expect(receipt.ok).toBe(true)
  expect(receipt.remaining).toEqual([])
  expect(receipt.signalled).toContainEqual({ port, pid: child.pid, signal: expect.stringMatching(/^SIG(TERM|KILL)$/) })
  await refusedConnection(port)
  expect(JSON.parse(readFileSync(receiptPath, 'utf8'))).toEqual(receipt)
}, 20_000)

it('writes an empty receipt when there is no listener', async () => {
  const server = createServer()
  await new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', resolve)
  })
  const port = server.address().port
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()))
  const receiptPath = receiptFile()
  const receipt = await clearStalePorts({ ports: [port], receiptPath })
  expect(receipt).toEqual({ ok: true, remaining: [], signalled: [], error: null })
  expect(JSON.parse(readFileSync(receiptPath, 'utf8'))).toEqual(receipt)
}, 15_000)

it.each([
  ['zero port', ['--ports', '0']],
  ['non-numeric port', ['--ports', 'abc']],
  ['missing receipt', ['--ports', '5275']],
])('refuses %s before writing a receipt', (name, args) => {
  const receiptPath = receiptFile()
  const result = spawnSync(process.execPath, [script, ...args,
    ...(name === 'missing receipt' ? [] : ['--receipt', receiptPath]),
  ], { encoding: 'utf8', timeout: 5000, windowsHide: true })
  expect(result.error).toBeUndefined()
  expect(result.status).toBe(2)
  expect(existsSync(receiptPath)).toBe(false)
})

it('runs guarded cleanup at config load before declaring the web server', () => {
  const source = readFileSync(new URL('../../playwright.local.config.mjs', import.meta.url), 'utf8')
  const block = source.indexOf('if (linkFlowBoot) {')
  const guard = source.indexOf("if (process.env.LEAF_LINK_FLOW_PORTS_CLEARED !== '1'", block)
  const call = source.indexOf('execFileSync(process.execPath, [', guard)
  const scriptPath = source.indexOf('./e2e/local/clear-stale-ports.mjs', call)
  const latch = source.indexOf("process.env.LEAF_LINK_FLOW_PORTS_CLEARED = '1'", scriptPath)
  const server = source.indexOf('webServer = {', block)
  expect(block).toBeGreaterThan(-1)
  expect(guard).toBeGreaterThan(block)
  expect(call).toBeGreaterThan(guard)
  expect(scriptPath).toBeGreaterThan(call)
  expect(latch).toBeGreaterThan(scriptPath)
  expect(server).toBeGreaterThan(latch)
  expect(source.slice(guard, call)).toContain("!process.argv.includes('--list')")
  expect(source.slice(block, call)).toContain('Object.assign(process.env, {')
  expect(source).not.toContain('cleanupCommand')
  expect(source).not.toContain('Buffer.from(')
  expect(source.slice(server)).not.toMatch(/command:\s*['"`]node -e/)
  expect(source.slice(server)).toContain("command: 'npm --prefix harness run build && python scripts/start-leaf.py")
})
