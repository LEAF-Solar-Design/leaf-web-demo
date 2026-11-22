import assert from 'node:assert/strict'
import test from 'node:test'
import { availableMemory, parseVmStat, DEFAULT_RAM_FLOOR, readCgroupMemory, ramFloorFor, memoryReading, describeMemoryShortfall } from './memory.mjs'

const mini = `Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               390727.
Pages inactive:                           980972.
Pages speculative:                          7427.
Pages purgeable:                           27782.
`
const miniBytes = (390727 + 980972 + 7427 + 27782) * 16384

test('parseVmStat counts free and reclaimable pages on the mini', () => {
  assert.equal(parseVmStat(mini), miniBytes)
})

test('parseVmStat requires the page size header and free pages', () => {
  assert.equal(parseVmStat(mini.split('\n').slice(1).join('\n')), null)
  assert.equal(parseVmStat(mini.replace(/^Pages free:.*\n/m, '')), null)
})

test('parseVmStat rejects invalid counts and page sizes', () => {
  for (const value of ['1.5', '-1', 'Infinity', 'NaN', '9'.repeat(400)]) {
    assert.equal(parseVmStat(mini.replace('980972', value)), null)
    assert.equal(parseVmStat(mini.replace('16384', value)), null)
  }
})

test('parseVmStat treats absent optional reclaimable counts as zero', () => {
  assert.equal(parseVmStat('Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free: 3.\n'), 3 * 16384)
})

test('darwin uses vm_stat with a bounded direct invocation', () => {
  const bytes = availableMemory({
    platform: 'darwin',
    freemem: () => { throw new Error('unexpected freemem fallback') },
    runVmStat: (file, args, options) => {
      assert.equal(file, '/usr/bin/vm_stat')
      assert.deepEqual(args, [])
      assert.deepEqual(options, { encoding: 'utf8', timeout: 5000, maxBuffer: 64 * 1024, shell: false })
      return mini
    },
  })
  assert.equal(bytes, miniBytes)
})

test('darwin falls back to freemem on vm_stat failure or invalid output', () => {
  for (const runVmStat of [() => { throw new Error('vm_stat failed') }, () => 'invalid output']) {
    assert.equal(availableMemory({ platform: 'darwin', freemem: () => 123, runVmStat }), 123)
  }
})

for (const platform of ['win32', 'linux']) {
  test(`${platform} returns freemem without running vm_stat`, () => {
    assert.equal(availableMemory({
      platform, freemem: () => 456,
      runVmStat: () => { throw new Error('unexpected vm_stat invocation') },
    }), 456)
  })
}

const GiB = 1024 ** 3
const root = '/sys/fs/cgroup'
const fakeReadFile = (files) => (path) => {
  if (!Object.hasOwn(files, path)) throw new Error(`Missing file: ${path}`)
  return files[path]
}

test('readCgroupMemory prefers the nested path from the v2 cgroup line', () => {
  assert.deepEqual(readCgroupMemory({ readFile: fakeReadFile({
    '/proc/self/cgroup': '1:cpu:/other\n0::/jobs/walk\n',
    [`${root}/jobs/walk/memory.max`]: '1000\n',
    [`${root}/jobs/walk/memory.current`]: '250\n',
    [`${root}/memory.max`]: '2000',
    [`${root}/memory.current`]: '0',
  }) }), { limit: 1000, available: 750, source: 'cgroup' })
})

test('readCgroupMemory falls back to the root when nested memory.max is absent', () => {
  assert.deepEqual(readCgroupMemory({ readFile: fakeReadFile({
    '/proc/self/cgroup': '0::/jobs/walk\n',
    [`${root}/memory.max`]: '1000',
    [`${root}/memory.current`]: '0',
  }) }), { limit: 1000, available: 1000, source: 'cgroup' })
})

test('readCgroupMemory returns null for unlimited or unavailable cgroups', () => {
  assert.equal(readCgroupMemory({ readFile: fakeReadFile({}) }), null)
  assert.equal(readCgroupMemory({ readFile: fakeReadFile({
    '/proc/self/cgroup': '0::/\n',
    [`${root}/memory.max`]: 'max\n',
    [`${root}/memory.current`]: '0',
  }) }), null)
})

test('readCgroupMemory rejects malformed limits and current usage', () => {
  for (const value of ['', '-1', '1.5', '1e3', 'Infinity', 'NaN', '12junk', '9'.repeat(400)]) {
    for (const field of ['memory.max', 'memory.current']) {
      assert.equal(readCgroupMemory({ readFile: fakeReadFile({
        [`${root}/memory.max`]: '1000',
        [`${root}/memory.current`]: '250',
        [`${root}/${field}`]: value,
      }) }), null, `${field}: ${value}`)
    }
  }
  for (const files of [
    { [`${root}/memory.max`]: '0', [`${root}/memory.current`]: '0' },
    { [`${root}/memory.max`]: '1000' },
  ]) assert.equal(readCgroupMemory({ readFile: fakeReadFile(files) }), null)
})

test('readCgroupMemory subtracts inactive_file and clamps available memory', () => {
  for (const [current, inactive, available] of [[800, 200, 400], [1200, 0, 0], [100, 200, 1000]]) {
    assert.deepEqual(readCgroupMemory({ readFile: fakeReadFile({
      [`${root}/memory.max`]: '1000',
      [`${root}/memory.current`]: String(current),
      [`${root}/memory.stat`]: `anon 20\ninactive_file ${inactive}\nactive_file 10\n`,
    }) }), { limit: 1000, available, source: 'cgroup' })
  }
})

test('readCgroupMemory treats absent or malformed inactive_file as zero', () => {
  for (const stat of ['anon 20\n', 'inactive_file -10\n', 'inactive_file 1.5\n']) {
    assert.equal(readCgroupMemory({ readFile: fakeReadFile({
      [`${root}/memory.max`]: '1000',
      [`${root}/memory.current`]: '250',
      [`${root}/memory.stat`]: stat,
    }) }).available, 750)
  }
})

test('ramFloorFor defaults invalid limits and halves valid limits up to 6 GiB', () => {
  assert.equal(DEFAULT_RAM_FLOOR, 6 * GiB)
  for (const limit of [null, undefined, 0, -1, NaN, Infinity, '1000']) {
    assert.equal(ramFloorFor(limit), DEFAULT_RAM_FLOOR)
  }
  assert.equal(ramFloorFor(5), 2)
  assert.equal(ramFloorFor(8 * GiB), 4 * GiB)
  assert.equal(ramFloorFor(16 * GiB), 6 * GiB)
})

test('memoryReading uses Linux cgroup headroom and scales the floor to its limit', () => {
  for (const [limitGiB, floorGiB] of [[8, 4], [16, 6]]) {
    assert.deepEqual(memoryReading({
      platform: 'linux',
      freemem: () => { throw new Error('unexpected host reading') },
      readFile: fakeReadFile({
        [`${root}/memory.max`]: String(limitGiB * GiB),
        [`${root}/memory.current`]: String(2 * GiB),
      }),
    }), { available: (limitGiB - 2) * GiB, floor: floorGiB * GiB, limit: limitGiB * GiB, source: 'cgroup' })
  }
})

test('memoryReading falls back to Linux host memory without a cgroup limit', () => {
  assert.deepEqual(memoryReading({ platform: 'linux', freemem: () => 456, readFile: fakeReadFile({}) }), {
    available: 456, floor: DEFAULT_RAM_FLOOR, limit: null, source: 'host',
  })
})

test('memoryReading identifies darwin vm_stat and host fallback readings', () => {
  assert.deepEqual(memoryReading({ platform: 'darwin', runVmStat: () => mini }), {
    available: miniBytes, floor: DEFAULT_RAM_FLOOR, limit: null, source: 'vm_stat',
  })
  assert.deepEqual(memoryReading({ platform: 'darwin', runVmStat: () => 'invalid', freemem: () => 123 }), {
    available: 123, floor: DEFAULT_RAM_FLOOR, limit: null, source: 'host',
  })
})

test('memoryReading on win32 never reads cgroup files', () => {
  assert.deepEqual(memoryReading({
    platform: 'win32', freemem: () => 456,
    readFile: () => { assert.fail('unexpected cgroup read') },
  }), { available: 456, floor: DEFAULT_RAM_FLOOR, limit: null, source: 'host' })
})

test('describeMemoryShortfall includes the source and cgroup limit when present', () => {
  assert.equal(describeMemoryShortfall({ available: 3.25 * GiB, floor: 4 * GiB, limit: 8 * GiB, source: 'cgroup' }),
    'Less than 4.0 GiB free RAM (cgroup reading: 3.3 GiB free of 8.0 GiB limit)')
  assert.equal(describeMemoryShortfall({ available: 5 * GiB, floor: DEFAULT_RAM_FLOOR, limit: null, source: 'host' }),
    'Less than 6.0 GiB free RAM (host reading: 5.0 GiB free)')
})
