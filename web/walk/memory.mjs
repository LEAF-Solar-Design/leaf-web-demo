import { execFileSync } from 'node:child_process'
import { freemem as osFreemem } from 'node:os'

export function parseVmStat(text) {
  if (typeof text !== 'string') return null
  const header = text.match(/\(page size of ([^)]+) bytes\)/)
  if (!header || !/^\d+$/.test(header[1])) return null
  const pageSize = Number(header[1])
  if (!Number.isInteger(pageSize) || pageSize < 0) return null
  let pages = 0
  for (const name of ['free', 'inactive', 'speculative', 'purgeable']) {
    const match = text.match(new RegExp(`^Pages ${name}:\\s*([^\\r\\n]*)`, 'm'))
    if (!match) {
      if (name === 'free') return null
      continue
    }
    const raw = match[1].trim().replace(/\.$/, '')
    if (!/^\d+$/.test(raw)) return null
    const count = Number(raw)
    if (!Number.isInteger(count) || count < 0) return null
    pages += count
  }
  const bytes = pages * pageSize
  return Number.isFinite(bytes) ? bytes : null
}

export function availableMemory({ platform = process.platform, freemem = osFreemem, runVmStat = execFileSync } = {}) {
  if (platform === 'darwin') {
    try {
      const bytes = parseVmStat(runVmStat('/usr/bin/vm_stat', [], {
        encoding: 'utf8', timeout: 5000, maxBuffer: 64 * 1024, shell: false,
      }))
      if (bytes !== null) return bytes
    } catch { /* Preserve the existing RAM floor when vm_stat is unavailable. */ }
  }
  return freemem()
}
