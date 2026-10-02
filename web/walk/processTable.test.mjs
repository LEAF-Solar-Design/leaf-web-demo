import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { PassThrough } from 'node:stream'
import test from 'node:test'
import { readProcessTable, measureWithRetry } from './processTable.mjs'

function fakeSpawn(action = () => {}) {
  const child = new EventEmitter()
  child.stdout = new PassThrough()
  child.stderr = new PassThrough()
  child.kills = []
  child.kill = (signal) => { child.kills.push(signal); return true }
  const calls = []
  const spawn = (...args) => {
    calls.push(args)
    queueMicrotask(() => action(child))
    return child
  }
  return { child, calls, spawn }
}

test('timeout kills the child and rejects without waiting for close', { timeout: 1500 }, async () => {
  const fake = fakeSpawn()
  await assert.rejects(readProcessTable({ spawn: fake.spawn, platform: 'win32', timeoutMs: 10 }), {
    message: 'Process table probe timed out',
  })
  assert.deepEqual(fake.child.kills, ['SIGKILL'])
})

test('stdout overflow kills the child and rejects', { timeout: 1500 }, async () => {
  const fake = fakeSpawn((child) => child.stdout.write('ééé'))
  await assert.rejects(readProcessTable({ spawn: fake.spawn, platform: 'linux', timeoutMs: 100, maxBytes: 5 }), {
    message: 'Process table exceeds 8 MiB',
  })
  assert.deepEqual(fake.child.kills, ['SIGKILL'])
})

test('nonzero exit reports only the stderr tail', { timeout: 1500 }, async () => {
  const tail = 'x'.repeat(3996) + 'tail'
  const fake = fakeSpawn((child) => {
    child.stderr.write('discarded prefix' + tail)
    child.emit('close', 1)
  })
  await assert.rejects(readProcessTable({ spawn: fake.spawn, platform: 'linux', timeoutMs: 100 }), {
    message: `Cannot measure stack process tree: ${tail}`,
  })
  assert.deepEqual(fake.child.kills, [])
})

for (const array of [true, false]) {
  test(`Windows JSON ${array ? 'array' : 'single object'} maps process fields`, { timeout: 1500 }, async () => {
    const row = { ProcessId: '101', ParentProcessId: '100', WorkingSetSize: '4096', Name: 'node.exe' }
    const fake = fakeSpawn((child) => {
      child.stdout.write(JSON.stringify(array ? [row] : row))
      child.emit('close', 0)
    })
    assert.deepEqual(await readProcessTable({ spawn: fake.spawn, platform: 'win32', timeoutMs: 100 }), [
      { pid: 101, parent: 100, rss: 4096, name: 'node.exe' },
    ])
    assert.equal(fake.calls[0][0], 'powershell.exe')
    assert.deepEqual(fake.calls[0][1], ['-NoProfile', '-NonInteractive', '-Command', 'Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,WorkingSetSize,Name | ConvertTo-Json -Compress'])
    assert.deepEqual(fake.child.kills, [])
  })
}

test('POSIX ps rows convert RSS from KiB to bytes', { timeout: 1500 }, async () => {
  const fake = fakeSpawn((child) => {
    child.stdout.write(' 101 100 4\n 102 101 8\n')
    child.emit('close', 0)
  })
  assert.deepEqual(await readProcessTable({ spawn: fake.spawn, platform: 'linux', timeoutMs: 100 }), [
    { pid: 101, parent: 100, rss: 4096 },
    { pid: 102, parent: 101, rss: 8192 },
  ])
  assert.equal(fake.calls[0][0], 'ps')
  assert.deepEqual(fake.calls[0][1], ['-e', '-o', 'pid=,ppid=,rss='])
})

test('invalid Windows JSON reports a measurement error', { timeout: 1500 }, async () => {
  const fake = fakeSpawn((child) => {
    child.stdout.write('invalid JSON')
    child.emit('close', 0)
  })
  await assert.rejects(readProcessTable({ spawn: fake.spawn, platform: 'win32', timeoutMs: 100 }), (error) => {
    assert.equal(error.constructor, Error)
    assert.match(error.message, /^Cannot measure stack process tree: /)
    assert.ok(error.cause instanceof SyntaxError)
    return true
  })
})

test('spawn error rejects and clears the probe timer', { timeout: 1500 }, async () => {
  const failure = new Error('spawn failed')
  const fake = fakeSpawn((child) => child.emit('error', failure))
  await assert.rejects(readProcessTable({ spawn: fake.spawn, platform: 'linux', timeoutMs: 10 }), (error) => error === failure)
  // Wait past the deadline to observe whether a stale timer kills the child.
  await new Promise((resolve) => setTimeout(resolve, 20))
  assert.deepEqual(fake.child.kills, [])
})

test('retry returns the second measurement after one fixed delay', { timeout: 1500 }, async () => {
  let calls = 0
  const sleeps = []
  const result = await measureWithRetry(() => {
    if (++calls === 1) throw new Error('cold probe')
    return 4096
  }, { sleep: async (ms) => { sleeps.push(ms) } })
  assert.equal(result, 4096)
  assert.equal(calls, 2)
  assert.deepEqual(sleeps, [500])
})

test('three failures name the attempt count and retain the last cause', { timeout: 1500 }, async () => {
  const failures = [new Error('first'), new Error('second'), new Error('last')]
  let calls = 0
  const sleeps = []
  await assert.rejects(measureWithRetry(async () => { throw failures[calls++] }, {
    delayMs: 7, sleep: async (ms) => { sleeps.push(ms) },
  }), (error) => {
    assert.match(error.message, /3 attempts/)
    assert.equal(error.cause, failures[2])
    return true
  })
  assert.equal(calls, 3)
  assert.deepEqual(sleeps, [7, 7])
})

for (const attempts of [0, 6, 1.5]) {
  test(`retry rejects invalid attempts: ${attempts}`, { timeout: 1500 }, async () => {
    await assert.rejects(measureWithRetry(() => assert.fail('must not measure'), { attempts }), RangeError)
  })
}
