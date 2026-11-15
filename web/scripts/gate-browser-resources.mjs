import { randomUUID } from 'node:crypto'
import { mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs'
import path from 'node:path'

export const GATE_BROWSER_SUITES = Object.freeze([
  'web-studio-panels-proof',
  'web-author-quota-gate',
  'web-version-restore-proof',
  'web-async-author-stage',
  'web-lifecycle-panel-proof',
  'web-ios-ship-surface-proof',
])

const legacyRoots = {
  base: '../artifacts/cat-operator-proof',
  lifecycle: '../artifacts/project-lifecycle-proof',
  ios: '../artifacts/ios-ship-surface',
}

// Integration opts in per suite with LEAF_GATE_BROWSER_SUITE; worker remains 0-7.
// These resources cover mocked proofs sharing immutable source/dependencies after
// the writer prelude. They do not make installs, production builds or other
// writer suites safe to overlap, and do not authorize removing runner locks.
export function gateBrowserResources(env = process.env, kind = 'base') {
  const worker = env.LEAF_NATIVE_GATE_WORKER ?? '0'
  if (typeof worker !== 'string' || worker.length !== 1 || !/^[0-7]$/.test(worker)) throw new Error('Invalid native gate worker')
  if (!Object.hasOwn(legacyRoots, kind)) throw new Error(`Invalid browser config kind: ${kind}`)
  const suite = env.LEAF_GATE_BROWSER_SUITE
  if (suite === undefined) {
    const port = 5185 + 100 * Number(worker)
    const ports = kind === 'ios' ? [port, port + 50] : [port]
    return {
      isolated: false,
      worker: Number(worker),
      ports,
      instances: ports.map((value) => kind === 'ios' ? `ios-${value}` : null),
      reportRoot: legacyRoots[kind],
    }
  }
  const index = GATE_BROWSER_SUITES.indexOf(suite)
  if (index < 0) throw new Error(`Invalid LEAF_GATE_BROWSER_SUITE: ${JSON.stringify(suite)}`)
  const expectedKind = index === 5 ? 'ios' : index === 4 ? 'lifecycle' : 'base'
  if (kind !== expectedKind) throw new Error(`LEAF_GATE_BROWSER_SUITE ${suite} requires ${expectedKind} config`)
  // Eight 100-port worker blocks; ten ports reserved per suite. Kept separate
  // from legacy 5185 + 100*w blocks and fixed 5275/8230/8240/8250 services.
  const port = 15000 + 100 * Number(worker) + 10 * index
  const identity = `gate-${suite}-w${worker}`
  return {
    isolated: true,
    worker: Number(worker),
    ports: kind === 'ios' ? [port, port + 1] : [port],
    instances: kind === 'ios' ? [`${identity}-off`, `${identity}-on`] : [identity],
    reportRoot: `../artifacts/gate-browser/${suite}/worker-${worker}`,
  }
}

// Synchronous staging finishes before Next launches. Each rename publishes a
// complete file, including when several servers stage the same immutable source.
export function stageEngineAsset(source, target, { writeFile = writeFileSync, rename = renameSync } = {}) {
  let bytes
  try {
    bytes = source ? readFileSync(source) : null
  } catch (error) {
    if (error.code !== 'ENOENT') throw error
    bytes = null
  }
  if (bytes === null) {
    rmSync(target, { force: true })
    return
  }
  try {
    if (readFileSync(target).equals(bytes)) return
  } catch (error) {
    if (error.code !== 'ENOENT') throw error
  }
  mkdirSync(path.dirname(target), { recursive: true })
  const temporary = `${target}.${process.pid}.${randomUUID()}.tmp`
  try {
    writeFile(temporary, bytes, { flag: 'wx' })
    rename(temporary, target)
  } finally {
    rmSync(temporary, { force: true })
  }
}
