import { CHECKS } from './BlockChecks.jsx'

const object = value => value !== null && typeof value === 'object' && !Array.isArray(value)
const digestOf = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
const text = value => typeof value === 'string' && value.length > 0 && value.length <= 128
const bounded = value => new TextEncoder().encode(value).length <= 2048
const count = value => Number.isSafeInteger(value) && value >= 0

function checksOf(value) {
  if (!object(value)) return null
  const checks = {}
  for (const [name] of CHECKS) {
    const check = value[name]
    if (!object(check) || !['pass', 'fail', 'pending'].includes(check.status ?? check.state)) return null
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
      if (!Array.isArray(check.differences) || check.differences.length > 64) return null
      const json = JSON.stringify(check.differences)
      if (!bounded(json)) return null
      result.differences = JSON.parse(json)
    }
    checks[name] = result
  }
  return checks
}

function versionOf(value) {
  if (!object(value) || !['active', 'pending', 'prior'].includes(value.state)
    || !text(value.version) || typeof value.restart_required !== 'boolean') return null
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
    let remaining = 64
    const read = name => {
      const rows = provider.rowsOf(name)
      if (!Array.isArray(rows)) return []
      const selected = rows.slice(0, remaining)
      remaining -= selected.length
      return selected.map(row => row?.data).filter(row => object(row)
        && row.kind === block.registry.id && row.version === block.registry.version)
    }
    let evidence = null
    for (const row of read('block-checks')) {
      const draftDigest = row.draftDigest ?? row.draft_digest
      if (!digestOf(row.digest) || !digestOf(draftDigest)) continue
      const checks = checksOf(row.checks)
      if (checks) evidence = { checks, digest: row.digest, draftDigest, versions: [] }
    }
    const versions = []
    for (const row of read('block-versions')) {
      if (!Array.isArray(row.versions)) continue
      for (const value of row.versions.slice(0, 64 - versions.length)) {
        const version = versionOf(value)
        if (version) versions.push(version)
      }
    }
    if (versions.length) return { checks: null, digest: null, draftDigest: null, ...evidence, versions }
    return evidence
  } catch {
    return null
  }
}
