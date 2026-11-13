import { createHash } from 'node:crypto'
import { lstatSync, readdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { publicEnvironment, productionFeatureFlags } from './next-runtime.mjs'

export const RECEIPT_SCHEMA = 'leaf.web-prebuilt.v1'

function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical)
  if (value !== null && typeof value === 'object') {
    return Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])]))
  }
  return value
}

export function buildFingerprint(env = process.env, args = []) {
  return createHash('sha256').update(JSON.stringify(canonical({
    public: publicEnvironment(env, false), flags: productionFeatureFlags(env),
    element_ref: env.LEAF_ELEMENT_REF_STAMP ?? null, node_env: env.NODE_ENV ?? null, args,
  }))).digest('hex')
}

export function distDigest(distDir) {
  if (!lstatSync(distDir).isDirectory()) throw new Error('dist must be a directory')
  const files = []
  function visit(directory, prefix = '') {
    for (const name of readdirSync(directory)) {
      const filename = path.join(directory, name)
      const relative = prefix + name
      const entry = lstatSync(filename)
      if (entry.isDirectory()) visit(filename, relative + '/')
      else if (entry.isFile()) files.push(relative)
      else throw new Error('dist contains a non-regular entry')
    }
  }
  visit(distDir)
  const hash = createHash('sha256')
  for (const relative of files.sort()) {
    const bytes = readFileSync(path.join(distDir, ...relative.split('/')))
    hash.update(relative + '\0' + bytes.length + '\0').update(bytes)
  }
  return hash.digest('hex')
}

export function recordPrebuilt({ webRoot, receiptPath, sourceSha, env = process.env, args = [] }) {
  const receipt = {
    schema: RECEIPT_SCHEMA, source_sha: sourceSha, fingerprint: buildFingerprint(env, args),
    dist_digest: distDigest(path.join(webRoot, 'dist')), written_at: new Date().toISOString(),
  }
  const temporary = receiptPath + '.tmp-' + process.pid
  writeFileSync(temporary, JSON.stringify(receipt) + '\n')
  renameSync(temporary, receiptPath)
}

export function reusablePrebuilt(options = {}) {
  let reason = 'unreadable'
  try {
    const { webRoot, receiptPath, sourceSha, env = process.env, args = [] } = options
    if (typeof receiptPath !== 'string' || !receiptPath || !path.isAbsolute(receiptPath)) {
      return { ok: false, reason: 'no-receipt-path' }
    }
    let receipt
    try {
      const entry = lstatSync(receiptPath)
      if (!entry.isFile() || entry.size >= 64 * 1024) return { ok: false, reason: 'unreadable' }
      const bytes = readFileSync(receiptPath)
      if (bytes.length >= 64 * 1024) return { ok: false, reason: 'unreadable' }
      receipt = JSON.parse(bytes.toString('utf8'))
    } catch (error) {
      return { ok: false, reason: error.code === 'ENOENT' ? 'missing' : 'unreadable' }
    }
    if (receipt?.schema !== RECEIPT_SCHEMA) return { ok: false, reason: 'schema' }
    if (sourceSha === 'unknown' || receipt.source_sha !== sourceSha) return { ok: false, reason: 'source' }
    reason = 'fingerprint'
    if (receipt.fingerprint !== buildFingerprint(env, args)) return { ok: false, reason }
    reason = 'dist-changed'
    if (receipt.dist_digest !== distDigest(path.join(webRoot, 'dist'))) return { ok: false, reason }
    reason = 'manifest'
    const manifest = JSON.parse(readFileSync(path.join(webRoot, 'dist/web-build-manifest.json'), 'utf8'))
    if (manifest?.source_sha !== sourceSha) return { ok: false, reason }
    return { ok: true, reason: 'reused' }
  } catch {
    return { ok: false, reason }
  }
}
