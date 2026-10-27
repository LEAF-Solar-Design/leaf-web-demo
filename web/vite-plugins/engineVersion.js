import { createHash } from 'node:crypto'
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import path from 'node:path'
import { bundleGraph } from '../scripts/bundle-assets.mjs'

export const ENGINE_FILES = ['engine.js', 'engine_bg.wasm']
export const ENGINE_VERSION_RE = /^[0-9a-f]{16}$/

export function findEnginePkgDir(webRoot, env = process.env) {
  const override = env.LEAF_CAD_ENGINE_PKG_DIR
  if (typeof override === 'string' && override.length > 0) return path.resolve(override)
  const vendorRoot = path.resolve(webRoot, '..', 'vendor')
  if (!existsSync(vendorRoot)) return undefined
  return readdirSync(vendorRoot).sort()
    .map((name) => path.join(vendorRoot, name, 'pkg-web'))
    .find((candidate) => existsSync(candidate) && statSync(candidate).isDirectory())
}

export function engineVersion(pkgDir) {
  if (!pkgDir) return ''
  const files = ENGINE_FILES.map((name) => path.join(pkgDir, name))
  if (files.some((file) => !existsSync(file))) return ''
  const hashes = files.map((file) => {
    if (statSync(file).size > 64 * 1024 * 1024) {
      throw new Error(`engine file exceeds 64 MiB: ${file}`)
    }
    return createHash('sha256').update(readFileSync(file)).digest('hex')
  })
  return createHash('sha256').update(`leaf-engine-v1\n${hashes.join('\n')}`).digest('hex').slice(0, 16)
}

export function assertWorkerCarriesVersion(distDir, version) {
  if (typeof version !== 'string' || !ENGINE_VERSION_RE.test(version)) {
    throw new Error(`invalid engine version: ${version}`)
  }
  let workers
  if (existsSync(path.join(distDir, '_next/static'))) {
    const graph = bundleGraph(distDir)
    workers = [...graph.assets].filter(([file, code]) => graph.reachable.has(file) &&
      file.startsWith('_next/') && code.includes('engine_unavailable:') && code.includes('documentLoaded'))
      .map(([, code]) => code)
  } else {
    // Retain verification of an existing Vite artifact during rollback.
    const assetsDir = path.join(distDir, 'assets')
    workers = existsSync(assetsDir) ? readdirSync(assetsDir, { withFileTypes: true })
      .filter((entry) => entry.isFile() && /^worker-browser.*\.js$/.test(entry.name))
      .map((entry) => readFileSync(path.join(assetsDir, entry.name), 'utf8')) : []
  }
  if (workers.length === 0) throw new Error('no built worker-browser chunk found')
  if (workers.some((code) => code.includes(version))) return true
  throw new Error(`engine version ${version} not found in the built worker bundle`)
}
