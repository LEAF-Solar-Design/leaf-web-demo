import assert from 'node:assert/strict'
import test from 'node:test'
import { availableMemory, parseVmStat } from './memory.mjs'

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
