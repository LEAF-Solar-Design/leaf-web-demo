/**
 * ONE named engine-session boundary (convergence W1,
 * docs/convergence/ACCEPTANCE.md flag matrix + "Engine-session ownership").
 *
 * The frozen contract has two halves and this file pins the half a build
 * cannot: the license fence CI (scripts/check_license_fence.py) proves the
 * engine is only ever reached through the ONE legal
 * `new Worker(new URL(..., import.meta.url))` spawn shape, but by design it
 * enumerates no entry-point files, so it would stay green if a second
 * production module grew its own spawn. "ONE engine session owner" would then
 * be false with every gate green.
 *
 * So: exactly one NON-TEST module under web/src may carry that spawn. The
 * check is behavioural, not a filename pin — nothing here names the module,
 * the directory, or the engine, so a rename or a W7 move cannot make it
 * stale, and it cannot itself become the standing evasion hole the fence doc
 * warns about (this file spells no engine identifier, which is exactly why it
 * detects by SHAPE rather than by name).
 *
 * TWO SHAPES, not one (panel W1 finding 3). The ACCEPTANCE clause bans a
 * second module CONSTRUCTING a boundary, and counting spawn sites does not
 * detect that: the boundary class is EXPORTED and takes its worker factory as
 * an argument, so `new EngineBoundary(injectedFactory)` in a second module
 * spawns nothing itself and passed this file unchallenged. The construction
 * shape is now counted too — still by shape, still naming neither the engine
 * nor the worker path.
 */
import { readdirSync, readFileSync, statSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

const WEB_SRC = path.dirname(path.dirname(fileURLToPath(import.meta.url)))

// The one legal spawn shape, with its URL literal captured:
// `new Worker(new URL('<literal>', import.meta.url))`.
const LEGAL_SPAWN = /new\s+Worker\s*\(\s*new\s+URL\s*\(\s*(['"`])((?:(?!\1).)*?)\1/g
// A spawn is an ENGINE spawn when its URL literal leaves the web tree for the
// repo's vendored sources — where the compiled engine is required to live.
const VENDORED = /(^|[\\/])vendor[\\/]/
const IS_TEST = /\.test\.(js|jsx|mjs)$/

// The OTHER banned shape: constructing the schema-validating boundary. The
// class is exported and takes its worker factory as an argument, so this is
// reachable without any spawn of its own. A bare mention (an import, a type
// position, a `new`-less call) is deliberately NOT a construction.
const BOUNDARY_CONSTRUCTION = /\bnew\s+EngineBoundary\s*\(/g

const spawnLiterals = (source) => [...source.matchAll(new RegExp(LEGAL_SPAWN))].map((m) => m[2])
const boundaryConstructions = (source) => source.match(new RegExp(BOUNDARY_CONSTRUCTION)) || []

function sourceFiles(dir, acc = []) {
  for (const entry of readdirSync(dir)) {
    if (entry === 'node_modules') continue
    const full = path.join(dir, entry)
    if (statSync(full).isDirectory()) sourceFiles(full, acc)
    else if (/\.(js|jsx|mjs)$/.test(entry)) acc.push(full)
  }
  return acc
}

function engineSpawnSites(files) {
  const hits = []
  for (const file of files) {
    for (const literal of spawnLiterals(readFileSync(file, 'utf8'))) {
      if (VENDORED.test(literal)) hits.push(path.relative(WEB_SRC, file).replace(/\\/g, '/'))
    }
  }
  return [...new Set(hits)]
}

function boundaryConstructionSites(files) {
  const hits = []
  for (const file of files) {
    if (boundaryConstructions(readFileSync(file, 'utf8')).length) {
      hits.push(path.relative(WEB_SRC, file).replace(/\\/g, '/'))
    }
  }
  return [...new Set(hits)]
}

describe('engine-session ownership', () => {
  const files = sourceFiles(WEB_SRC)

  it('exactly ONE non-test module under web/src spawns the engine worker', () => {
    const owners = engineSpawnSites(files.filter((file) => !IS_TEST.test(file)))
    expect(owners).toHaveLength(1)
  })

  it('the engine-session store itself never names the worker path (the factory is injected)', () => {
    const store = path.join(WEB_SRC, 'cadedit', 'engineSession.js')
    expect(engineSpawnSites([store])).toEqual([])
    // It reaches the engine ONLY through the schema-validating boundary.
    expect(readFileSync(store, 'utf8')).toMatch(/from '\.\.\/cad\/engineWorker\.js'/)
  })

  it('exactly ONE non-test module under web/src CONSTRUCTS the session boundary', () => {
    // Not the same claim as the spawn count above: the boundary takes its
    // worker factory as an argument, so a second owner can construct one
    // while spawning nothing. Counted by shape so a rename or a move of the
    // blessed owner cannot make this stale.
    const owners = boundaryConstructionSites(files.filter((file) => !IS_TEST.test(file)))
    expect(owners).toHaveLength(1)
  })

  it('the construction detector is not vacuous: it finds the shape and ignores a bare mention', () => {
    // Fixtures built here, never committed under a scanned path — the same
    // discipline the license fence doc requires.
    expect(boundaryConstructions('const b = new EngineBoundary({ flags })')).toHaveLength(1)
    expect(boundaryConstructions('const b = new   EngineBoundary(opts)')).toHaveLength(1)
    expect(boundaryConstructions('new\n  EngineBoundary({})')).toHaveLength(1)
    expect(boundaryConstructions('two: new EngineBoundary(a); new EngineBoundary(b)')).toHaveLength(2)
    // A mention is not a construction.
    expect(boundaryConstructions("import { EngineBoundary } from './x.js'")).toHaveLength(0)
    expect(boundaryConstructions('EngineBoundary(notNew)')).toHaveLength(0)
    expect(boundaryConstructions('const renewEngineBoundary = () => 1')).toHaveLength(0)
  })

  it('the detector is not vacuous: it finds the shape and rejects a non-vendored one', () => {
    // Both directions, on fixtures built here — never committed under a
    // scanned path, the same discipline the license fence doc requires.
    const vendored = spawnLiterals("new Worker(new URL('../../../vendor/engine/worker.mjs', import.meta.url))")
    const local = spawnLiterals("new Worker(new URL('./localWorker.js', import.meta.url))")
    expect(vendored).toHaveLength(1)
    expect(local).toHaveLength(1)
    expect(VENDORED.test(vendored[0])).toBe(true)
    expect(VENDORED.test(local[0])).toBe(false)
  })
})
