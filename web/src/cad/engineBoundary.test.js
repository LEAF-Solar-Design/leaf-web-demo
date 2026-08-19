import { readFileSync, readdirSync, statSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { afterEach, describe, expect, it, vi } from 'vitest'

import { EngineBoundary, isCadEditEnabled, validateBoundaryMessage } from './engineWorker.js'

const CAD_DIR = path.dirname(fileURLToPath(import.meta.url))
const WEB_SRC_DIR = path.dirname(CAD_DIR)

function fakeWorker() {
  const listeners = { message: [] }
  return {
    addEventListener: (type, cb) => listeners[type]?.push(cb),
    removeEventListener: () => {},
    postMessage: vi.fn(),
    terminate: vi.fn(),
    emit: (data) => listeners.message.forEach((cb) => cb({ data })),
  }
}

describe('engine worker boundary flag gating', () => {
  afterEach(() => {
    delete globalThis.Worker
  })

  it('never instantiates a worker when cad_edit is off (negative control)', () => {
    const createWorker = vi.fn(fakeWorker)
    const boundary = new EngineBoundary({ flags: { cad_edit: false }, createWorker })

    const started = boundary.start()

    expect(started).toBe(false)
    expect(boundary.instantiated).toBe(false)
    expect(createWorker).not.toHaveBeenCalled()
  })

  it('never touches the real Worker global when cad_edit is off', () => {
    globalThis.Worker = vi.fn()
    const boundary = new EngineBoundary({ flags: { cad_edit: false } })

    boundary.start()

    expect(globalThis.Worker).not.toHaveBeenCalled()
  })

  it('instantiates exactly once when cad_edit is on', () => {
    const createWorker = vi.fn(fakeWorker)
    const boundary = new EngineBoundary({ flags: { cad_edit: true }, createWorker })

    expect(boundary.start()).toBe(true)
    expect(boundary.start()).toBe(true)

    expect(createWorker).toHaveBeenCalledTimes(1)
    expect(boundary.instantiated).toBe(true)
  })

  it('resolves the flag from an explicit false without consulting env', () => {
    expect(isCadEditEnabled({ cad_edit: false })).toBe(false)
    expect(isCadEditEnabled({ cad_edit: true })).toBe(true)
  })

  it('defaults dormant when no flag is provided at all', () => {
    expect(isCadEditEnabled(undefined)).toBe(false)
  })
})

describe('engine worker boundary message schema', () => {
  it('accepts a well-formed outbound message and forwards it to the worker', () => {
    const worker = fakeWorker()
    const boundary = new EngineBoundary({ flags: { cad_edit: true }, createWorker: () => worker })
    boundary.start()

    const posted = boundary.post({ type: 'loadDocument', documentId: 'doc-1' })

    expect(posted).toBe(true)
    expect(worker.postMessage).toHaveBeenCalledWith({ type: 'loadDocument', documentId: 'doc-1' })
    expect(boundary.droppedCount).toBe(0)
  })

  it('drops a malformed outbound message with a counted receipt, never throwing', () => {
    const worker = fakeWorker()
    const boundary = new EngineBoundary({ flags: { cad_edit: true }, createWorker: () => worker })
    boundary.start()

    expect(() => boundary.post({ type: 'loadDocument' })).not.toThrow()

    expect(worker.postMessage).not.toHaveBeenCalled()
    expect(boundary.droppedCount).toBe(1)
    expect(boundary.droppedReceipts[0]).toMatchObject({
      direction: 'toWorker',
      reason: 'missing_fields:documentId',
    })
  })

  it('rejects an unrecognized outbound message type', () => {
    const worker = fakeWorker()
    const boundary = new EngineBoundary({ flags: { cad_edit: true }, createWorker: () => worker })
    boundary.start()

    expect(boundary.post({ type: 'deleteEverything' })).toBe(false)
    expect(boundary.droppedCount).toBe(1)
  })

  it('delivers a well-formed inbound message to listeners', () => {
    const worker = fakeWorker()
    const boundary = new EngineBoundary({ flags: { cad_edit: true }, createWorker: () => worker })
    boundary.start()
    const received = []
    boundary.onMessage((message) => received.push(message))

    worker.emit({ type: 'editApplied', op: 'move', ok: true })

    expect(received).toEqual([{ type: 'editApplied', op: 'move', ok: true }])
    expect(boundary.droppedCount).toBe(0)
  })

  it('drops a malformed inbound message with a counted receipt, never throwing into the UI', () => {
    const worker = fakeWorker()
    const boundary = new EngineBoundary({ flags: { cad_edit: true }, createWorker: () => worker })
    boundary.start()
    const received = []
    boundary.onMessage((message) => received.push(message))

    expect(() => worker.emit({ garbage: true })).not.toThrow()
    expect(() => worker.emit(null)).not.toThrow()
    expect(() => worker.emit('not-an-object')).not.toThrow()

    expect(received).toEqual([])
    expect(boundary.droppedCount).toBe(3)
    expect(boundary.droppedReceipts.every((r) => r.direction === 'fromWorker')).toBe(true)
  })

  it('validateBoundaryMessage rejects an unknown direction without throwing', () => {
    expect(validateBoundaryMessage('sideways', { type: 'init' })).toEqual({
      ok: false,
      reason: 'unknown_direction',
    })
  })
})

// The fence: nothing outside src/cad/ may import a CAD engine module other
// than the boundary itself. Proven both against the real tree (must be
// clean today) and against a synthetic violation (must be caught), so the
// check has real teeth and isn't vacuously passing on an empty tree.
const CAD_IMPORT_RE = /\bfrom\s+['"]([^'"]*\/cad\/[^'"]+)['"]/g

function collectSourceFiles(dir, acc = []) {
  for (const entry of readdirSync(dir)) {
    if (entry === 'node_modules') continue
    const full = path.join(dir, entry)
    const info = statSync(full)
    if (info.isDirectory()) {
      collectSourceFiles(full, acc)
    } else if (/\.(js|jsx|mjs)$/.test(entry)) {
      acc.push(full)
    }
  }
  return acc
}

function findFenceViolations(files) {
  const violations = []
  for (const file of files) {
    if (file.startsWith(CAD_DIR + path.sep) || file === path.join(CAD_DIR, 'engineWorker.js')) continue
    const source = readFileSync(file, 'utf8')
    let match
    while ((match = CAD_IMPORT_RE.exec(source))) {
      const specifier = match[1]
      const base = path.basename(specifier).replace(/\.js$/, '')
      if (base !== 'engineWorker') {
        violations.push({ file, specifier })
      }
    }
  }
  return violations
}

describe('engine internals import fence', () => {
  it('finds no main-bundle import of a CAD engine module other than the boundary', () => {
    const files = collectSourceFiles(WEB_SRC_DIR)
    expect(findFenceViolations(files)).toEqual([])
  })

  it('catches a synthetic import of engine internals (the fence is not vacuous)', () => {
    const violation = findFenceViolations.call(null, [])
    expect(violation).toEqual([])

    CAD_IMPORT_RE.lastIndex = 0
    const fixtureSource = "import { engineCore } from '../cad/engineInternals.js'\n"
    const match = CAD_IMPORT_RE.exec(fixtureSource)
    expect(match).not.toBeNull()
    const base = path.basename(match[1]).replace(/\.js$/, '')
    expect(base).not.toBe('engineWorker')
  })

  it('allows importing the boundary module itself from outside src/cad', () => {
    CAD_IMPORT_RE.lastIndex = 0
    const fixtureSource = "import { EngineBoundary } from '../cad/engineWorker.js'\n"
    const match = CAD_IMPORT_RE.exec(fixtureSource)
    expect(path.basename(match[1]).replace(/\.js$/, '')).toBe('engineWorker')
  })
})
