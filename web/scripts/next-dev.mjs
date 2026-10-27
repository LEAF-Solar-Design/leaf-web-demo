import { spawn } from 'node:child_process'
import { copyFileSync, existsSync, mkdirSync, rmSync } from 'node:fs'
import { createRequire } from 'node:module'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { ENGINE_FILES, findEnginePkgDir } from '../vite-plugins/engineVersion.js'

const require = createRequire(import.meta.url)
const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
// Supported Next env loading, before engine discovery and child launch.
const { loadEnvConfig } = require('@next/env')
loadEnvConfig(webRoot, true)
const pkgDir = findEnginePkgDir(webRoot)
const engineDir = path.join(webRoot, 'public/engine')
for (const name of ENGINE_FILES) {
  const target = path.join(engineDir, name)
  const source = pkgDir && path.join(pkgDir, name)
  if (source && existsSync(source)) {
    mkdirSync(engineDir, { recursive: true })
    copyFileSync(source, target)
  } else rmSync(target, { force: true })
}

const args = process.argv.slice(2).filter((arg) => arg !== '--strictPort')
// Accept existing managed-proof Vite CLI spelling during migration.
const normalized = args.map((arg) => arg === '--host' ? '--hostname' : arg)
if (!normalized.some((arg) => arg === '--port' || arg === '-p' || arg.startsWith('--port='))) normalized.push('--port', '5175')
const child = spawn(process.execPath, [require.resolve('next/dist/bin/next'), 'dev', '--webpack', ...normalized], {
  cwd: webRoot, stdio: 'inherit', env: process.env,
})
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => child.kill(signal))
child.on('error', (error) => { console.error(error); process.exitCode = 1 })
child.on('exit', (code, signal) => { process.exitCode = code ?? (signal ? 1 : 0) })
