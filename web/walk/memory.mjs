import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { freemem as osFreemem } from 'node:os'
import { join } from 'node:path'

export const DEFAULT_RAM_FLOOR = 6 * 1024 ** 3

export function readCgroupMemory({ readFile = (path) => readFileSync(path, 'utf8') } = {}) {
  const directories = []
  try {
    const path = readFile('/proc/self/cgroup').match(/^0::([^\r\n]+)$/m)?.[1]
    if (path && path !== '/') directories.push(join('/sys/fs/cgroup', path))
  } catch { /* A namespace may only expose the cgroup root. */ }
  directories.push('/sys/fs/cgroup')
  const decimal = (text) => {
    const raw = text.trim()
    const value = /^\d+$/.test(raw) ? Number(raw) : NaN
    return Number.isFinite(value) && Number.isInteger(value) ? value : null
  }
  for (const directory of directories) {
    try {
      const limit = decimal(readFile(join(directory, 'memory.max')))
      if (limit === null || limit <= 0) continue
      const current = decimal(readFile(join(directory, 'memory.current')))
      if (current === null) continue
      let inactiveFile = 0
      try {
        const raw = readFile(join(directory, 'memory.stat')).match(/^inactive_file\s+([^\r\n]+)$/m)?.[1]
        if (raw !== undefined) inactiveFile = decimal(raw) ?? 0
      } catch { /* Reclaimable file memory is optional. */ }
      return { limit, available: Math.max(0, limit - Math.max(0, current - inactiveFile)), source: 'cgroup' }
    } catch { /* Try the next directory when a required file is unavailable. */ }
  }
  return null
}

export function ramFloorFor(limit) {
  return typeof limit === 'number' && Number.isFinite(limit) && limit > 0
    ? Math.min(DEFAULT_RAM_FLOOR, Math.floor(limit / 2)) : DEFAULT_RAM_FLOOR
}

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

function hostMemoryReading({ platform = process.platform, freemem = osFreemem, runVmStat = execFileSync } = {}) {
  if (platform === 'darwin') {
    try {
      const bytes = parseVmStat(runVmStat('/usr/bin/vm_stat', [], {
        encoding: 'utf8', timeout: 5000, maxBuffer: 64 * 1024, shell: false,
      }))
      if (bytes !== null) return { available: bytes, source: 'vm_stat' }
    } catch { /* Preserve the existing RAM floor when vm_stat is unavailable. */ }
  }
  return { available: freemem(), source: 'host' }
}

export function availableMemory(options = {}) {
  return hostMemoryReading(options).available
}

export function memoryReading({ platform = process.platform, freemem = osFreemem, runVmStat = execFileSync, readFile } = {}) {
  if (platform === 'linux') {
    const reading = readCgroupMemory({ readFile })
    if (reading !== null) return { ...reading, floor: ramFloorFor(reading.limit) }
  }
  return { ...hostMemoryReading({ platform, freemem, runVmStat }), limit: null, floor: DEFAULT_RAM_FLOOR }
}

export function describeMemoryShortfall({ available, floor, limit, source }) {
  const gib = (bytes) => (bytes / 1024 ** 3).toFixed(1)
  return `Less than ${gib(floor)} GiB free RAM (${source} reading: ${gib(available)} GiB free` +
    (limit != null ? ` of ${gib(limit)} GiB limit` : '') + ')'
}
