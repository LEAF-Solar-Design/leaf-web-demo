import { CHECKS } from './BlockChecks.jsx'

const object = value => value !== null && typeof value === 'object' && !Array.isArray(value)
const digestOf = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
const text = value => typeof value === 'string' && value.length > 0 && value.length <= 128
const bounded = value => new TextEncoder().encode(value).length <= 2048
const count = value => Number.isSafeInteger(value) && value >= 0

// Inspect before serialization: even rejected input must have bounded work.
function safeValue(value, depth = 0) {
  if (depth > 4) return false
  if (typeof value === 'string') return value.length <= 2048 && bounded(value)
  if (value === null || typeof value === 'boolean') return true
  if (typeof value === 'number') return Number.isFinite(value)
  if (typeof value !== 'object' || (Array.isArray(value) && value.length > 64)) return false
  const keys = Object.keys(value)
  return keys.length <= 64 && keys.every(key => key.length <= 2048 && bounded(key) && safeValue(value[key], depth + 1))
}

function checkOf(check) {
  if (!object(check) || !safeValue(check) || !['pass', 'fail', 'pending'].includes(check.status ?? check.state)) return null
  const result = { status: check.status ?? check.state }
  if (check.detail !== undefined) {
    if (typeof check.detail !== 'string' || !bounded(check.detail)) return null
    result.detail = check.detail
  }
  for (const key of ['passed', 'total', 'difference_count', 'p50_us', 'p99_us']) {
    if (check[key] === undefined) continue
    if (key.endsWith('_us') ? !Number.isFinite(check[key]) || check[key] < 0 : !count(check[key])) return null
    result[key] = check[key]
  }
  if (result.passed > result.total) return null
  if (check.differences !== undefined) {
    if (!Array.isArray(check.differences)) return null
    const json = JSON.stringify(check.differences)
    if (!bounded(json)) return null
    result.differences = JSON.parse(json)
  }
  return result
}

function checksOf(value) {
  const checks = {}
  for (const [name] of CHECKS) {
    checks[name] = checkOf(value?.[name]) || { status: 'pending' }
  }
  return checks
}

function versionOf(value) {
  if (!object(value) || !safeValue(value) || !['active', 'pending', 'prior'].includes(value.state)
    || !text(value.version) || typeof value.restart_required !== 'boolean') return null
  if ((value.state === 'active' && value.restart_required) || (value.state === 'pending' && !value.restart_required)) return null
  const result = { state: value.state, version: value.version, restart_required: value.restart_required }
  if (value.kind !== undefined) {
    if (!text(value.kind)) return null
    result.kind = value.kind
  }
  for (const key of ['digest', 'library_digest', 'crate_digest']) {
    if (value[key] === undefined) continue
    if (!digestOf(value[key])) return null
    result[key] = value[key]
  }
  return result
}

// Source row data is { kind, version, checks, digest, draftDigest } or
// { kind, version, versions: [{ state, version, restart_required, ... }] }.
// Only normalized, bounded evidence leaves this adapter; it never enters a graph.
export function evidenceFor(provider, block) {
  if (!block?.registry || typeof provider?.rowsOf !== 'function') return null
  try {
    const read = name => {
      const rows = provider.rowsOf(name)
      if (!Array.isArray(rows)) return []
      const selected = rows.slice(-32)
      return selected.map(row => row?.data).filter(row => object(row)
        && row.kind === block.registry.id && row.version === block.registry.version)
    }
    let evidence = null
    const row = read('block-checks').at(-1)
    if (row) {
      const draftDigest = row.draftDigest ?? row.draft_digest
      const checks = checksOf(row.checks)
      evidence = { checks, digest: digestOf(row.digest) ? row.digest : null,
        draftDigest: digestOf(draftDigest) ? draftDigest : null, versions: [] }
    }
    let versions = []
    for (const row of read('block-versions')) {
      if (!Array.isArray(row.versions) || row.versions.length > 64) continue
      const snapshot = row.versions.map(versionOf).filter(Boolean)
      if ((snapshot.length || row.versions.length === 0) && snapshot.filter(value => value.state === 'active').length <= 1) versions = snapshot
    }
    if (versions.length) return { checks: null, digest: null, draftDigest: null, ...evidence, versions }
    return evidence
  } catch {
    return null
  }
}
