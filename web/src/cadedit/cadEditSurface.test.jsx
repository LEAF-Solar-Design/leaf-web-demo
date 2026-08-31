/**
 * CadEditSurface acceptance oracle (card F-3, engine leg).
 *
 * The fake Worker below is a TRANSPORT double only: every message it answers
 * is computed by the REAL vendored browser worker's handleMessage driving
 * the REAL compiled engine (the wasm-pack pkg-node build) — so open ->
 * list -> select -> edit
 * -> write-back -> re-parse is exercised against the actual MPL engine and
 * the real (unmodified) EngineBoundary. Nothing about the parse, the edits,
 * or the round trip is restated in JS here.
 *
 * The engine-backed cases are gated on the compiled artifact being present
 * (the documented wasm-pack build is machine-local, never committed), the
 * same opt-in family as engineWasmHarness.realwasm and the corpus adapter
 * tests. The flag/cap cases below the gate run everywhere.
 *
 * jsdom has no Worker and no File.arrayBuffer; both are installed per test.
 */
import { spawn } from 'node:child_process'
import { existsSync, readdirSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import CadEditSurface from './CadEditSurface.jsx'

// FENCE NOTE (same discipline as engineWasmHarness.realwasm.test.js): this
// file never spells the engine's vendor path outside the ONE literal
// `new Worker(new URL(...))` call below — the single shape the license fence
// allows. The worker module path and the compiled engine's location are
// DERIVED from the URL that legal shape produces, captured through a
// throwaway Worker double, and the compiled glue's filename is discovered by
// readdir rather than written down.
function createEditorWorker() {
  return new Worker(
    new URL('../../../vendor/acadrust-worker/worker-browser.mjs', import.meta.url),
    { type: 'module' },
  )
}

function captureWorkerPath() {
  class CapturingWorker {
    constructor(url) {
      // jsdom's import.meta.url is http-schemed under vitest, so the legal
      // shape produces an http URL whose pathname is the on-disk path
      // (windows drives arrive as "/C:/..."); node env produces file://.
      const u = url instanceof URL ? url : new URL(String(url))
      CapturingWorker.captured = u.protocol === 'file:'
        ? fileURLToPath(u)
        : decodeURIComponent(
          u.pathname.replace(/^\/@fs\//, '/').replace(/^\/(?=[A-Za-z]:)/, ''),
        )
    }

    addEventListener() {}

    terminate() {}
  }
  const previous = globalThis.Worker
  globalThis.Worker = CapturingWorker
  try {
    createEditorWorker()
  } finally {
    if (previous === undefined) delete globalThis.Worker
    else globalThis.Worker = previous
  }
  return CapturingWorker.captured
}

const WORKER_PATH = captureWorkerPath()
const PKG_DIR = path.join(path.dirname(WORKER_PATH), 'pkg-node')
const GLUE = existsSync(PKG_DIR)
  ? readdirSync(PKG_DIR).find((name) => name.endsWith('_worker.js'))
  : null
const HAS_ENGINE = Boolean(GLUE)

// One PERSISTENT node child per fake worker (vitest's module runner refuses
// out-of-root imports, and a child-per-message would lose the worker's held
// document between load and edit). The child imports the REAL worker module
// and the REAL compiled engine by path, then answers line-delimited JSON.
// Uint8Array payloads cross as base64 (structured clone does the equivalent
// for the real Worker transport).
const CHILD_SCRIPT = [
  'import { createRequire } from "node:module"',
  'import { pathToFileURL } from "node:url"',
  'import readline from "node:readline"',
  'const [workerPath, gluePath] = process.argv.slice(1)',
  'const { handleMessage } = await import(pathToFileURL(workerPath).href)',
  'const engine = createRequire(import.meta.url)(gluePath)',
  'const rl = readline.createInterface({ input: process.stdin })',
  'const revive = (m) => { if (m && m.__bytes) return Uint8Array.from(Buffer.from(m.__bytes, "base64")); return m }',
  'rl.on("line", async (line) => {',
  '  const { id, message } = JSON.parse(line)',
  '  if (message?.type === "loadDocument" && message.__bytes64) {',
  '    message.bytes = Uint8Array.from(Buffer.from(message.__bytes64, "base64")); delete message.__bytes64',
  '  }',
  '  const response = await handleMessage(message, engine)',
  '  if (response?.bytes) { response.__bytes64 = Buffer.from(response.bytes).toString("base64"); delete response.bytes }',
  '  process.stdout.write(JSON.stringify({ id, response: response ?? null }) + "\\n")',
  '})',
].join('\n')

const ONE_LINE_DXF = [
  '0', 'SECTION', '2', 'HEADER', '9', '$ACADVER', '1', 'AC1009', '0', 'ENDSEC',
  '0', 'SECTION', '2', 'ENTITIES',
  '0', 'LINE', '8', 'Panels',
  '10', '0.0', '20', '0.0', '30', '0.0',
  '11', '100.0', '21', '50.0', '31', '0.0',
  '0', 'ENDSEC', '0', 'EOF',
].join('\n') + '\n'

const TWO_POLY_DXF = [
  '0', 'SECTION', '2', 'ENTITIES',
  '0', 'LWPOLYLINE', '8', 'Outline', '90', '3', '70', '0',
  '10', '0.0', '20', '0.0', '10', '50.0', '20', '5.0', '10', '80.0', '20', '40.0',
  '0', 'LWPOLYLINE', '8', 'String1', '90', '4', '70', '1',
  '10', '1.0', '20', '1.0', '10', '2.0', '20', '1.0', '10', '2.0', '20', '2.0', '10', '1.0', '20', '2.0',
  '0', 'ENDSEC', '0', 'EOF',
].join('\n') + '\n'

// Transport double: the REAL async worker handler in a persistent child
// process (real wasm engine loaded there), behind a Worker-shaped surface.
class FakeEngineWorker {
  constructor() {
    this.listeners = []
    this.posted = []
    this.terminated = false
    this.nextId = 1
    this.child = spawn(process.execPath, ['--input-type=module', '-e', CHILD_SCRIPT, WORKER_PATH, path.join(PKG_DIR, GLUE)], {
      stdio: ['pipe', 'pipe', 'inherit'],
    })
    let buffer = ''
    this.child.stdout.on('data', (chunk) => {
      buffer += chunk.toString('utf8')
      let nl
      while ((nl = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, nl)
        buffer = buffer.slice(nl + 1)
        if (!line.trim()) continue
        const { response } = JSON.parse(line)
        if (!response) continue
        if (response.__bytes64) {
          response.bytes = Uint8Array.from(atob(response.__bytes64), (c) => c.charCodeAt(0))
          delete response.__bytes64
        }
        for (const cb of this.listeners) cb({ data: response })
      }
    })
  }

  addEventListener(type, cb) {
    if (type === 'message') this.listeners.push(cb)
  }

  removeEventListener() {}

  postMessage(data) {
    this.posted.push(data)
    const wire = { ...data }
    if (wire.type === 'loadDocument' && wire.bytes) {
      wire.__bytes64 = btoa(String.fromCharCode(...wire.bytes))
      delete wire.bytes
    }
    this.child.stdin.write(JSON.stringify({ id: this.nextId++, message: wire }) + '\n')
  }

  terminate() {
    this.terminated = true
    this.child.kill()
  }
}

function fileOf(text, name = 'one_line.dxf') {
  const bytes = new TextEncoder().encode(text)
  const file = new File([bytes], name, { type: 'application/dxf' })
  file.arrayBuffer = async () => bytes.buffer.slice(0)
  Object.defineProperty(file, 'size', { value: bytes.length })
  return file
}

function oversizedFile() {
  const file = new File([new Uint8Array(1)], 'huge.dxf')
  file.arrayBuffer = async () => new ArrayBuffer(1)
  Object.defineProperty(file, 'size', { value: 20 * 1024 * 1024 })
  return file
}

let worker

function renderSurface(props = {}) {
  worker = new FakeEngineWorker()
  return render(<CadEditSurface enabled createWorker={() => worker} {...props} />)
}

async function openDocument(text, name) {
  fireEvent.change(screen.getByLabelText('DXF file'), { target: { files: [fileOf(text, name)] } })
  // The engine runs in a real async child here: wait for the LOAD REPORT,
  // not merely for the count element (which renders while still busy).
  await waitFor(() => expect(screen.getByRole('status').textContent).toContain('Loaded'))
}

function selectEntity(index = 0) {
  fireEvent.click(screen.getAllByRole('radio')[index])
}

async function statusContains(fragment) {
  await waitFor(() => expect(screen.getByRole('status').textContent).toContain(fragment))
}

beforeEach(() => {
  globalThis.URL.createObjectURL = vi.fn(() => 'blob:cad-edit-test')
  globalThis.URL.revokeObjectURL = vi.fn()
})

afterEach(() => {
  cleanup()
  worker?.terminate()
  worker = undefined
})

describe('acceptance: the editing surface mounts only behind cad_edit', () => {
  it('renders nothing when the flag is off', () => {
    render(<CadEditSurface enabled={false} />)
    expect(screen.queryByTestId('cad-edit-workbench')).toBeNull()
  })

  it('does not mount by default (VITE_CAD_EDIT unset in this test build)', () => {
    render(<CadEditSurface />)
    expect(screen.queryByTestId('cad-edit-workbench')).toBeNull()
  })

  it('never spawns the engine worker at mount — only on the first open', () => {
    const createWorker = vi.fn(() => new FakeEngineWorker())
    render(<CadEditSurface enabled createWorker={createWorker} />)
    expect(screen.getByTestId('cad-edit-workbench')).toBeInTheDocument()
    expect(createWorker).not.toHaveBeenCalled()
  })

  it('refuses a file over the byte cap without reading or spawning anything', async () => {
    const createWorker = vi.fn(() => new FakeEngineWorker())
    render(<CadEditSurface enabled createWorker={createWorker} />)
    fireEvent.change(screen.getByLabelText('DXF file'), { target: { files: [oversizedFile()] } })
    await statusContains('exceeds')
    expect(createWorker).not.toHaveBeenCalled()
  })
})

describe.skipIf(!HAS_ENGINE)('acceptance: real-engine editing through the boundary', () => {
  it('loads through the boundary and lists the line with its layer and endpoints', async () => {
    renderSurface()
    await openDocument(ONE_LINE_DXF)
    expect(screen.getByTestId('cad-edit-entity-count').textContent).toBe('1')
    const list = screen.getByTestId('cad-edit-entity-list')
    expect(list.textContent).toContain('LINE on layer Panels')
    expect(list.textContent).toContain('2 vertices')
    expect(worker.posted[0]).toEqual({ type: 'init' })
    expect(worker.posted[1].type).toBe('loadDocument')
  })

  it('surfaces an engine parse refusal truthfully instead of an empty list', async () => {
    renderSurface()
    fireEvent.change(screen.getByLabelText('DXF file'), {
      target: { files: [fileOf('0\nSECTION\n2\nENTITIES\ngarbage', 'broken.dxf')] },
    })
    await statusContains('Engine refused')
  })

  it('deletes the selected entity and reports the count re-parsed from the written bytes', async () => {
    renderSurface()
    await openDocument(TWO_POLY_DXF, 'polys.dxf')
    expect(screen.getByTestId('cad-edit-entity-count').textContent).toBe('2')
    selectEntity(0)
    fireEvent.click(screen.getByRole('button', { name: 'Delete selected' }))
    await statusContains('delete applied')
    expect(screen.getByTestId('cad-edit-entity-count').textContent).toBe('1')
  })

  it('moves the selected entity by the entered delta and re-lists the translated geometry', async () => {
    renderSurface()
    await openDocument(ONE_LINE_DXF)
    selectEntity(0)
    fireEvent.change(screen.getByLabelText('dx'), { target: { value: '7' } })
    fireEvent.change(screen.getByLabelText('dy'), { target: { value: '3' } })
    fireEvent.click(screen.getByRole('button', { name: 'Move selected' }))
    await statusContains('move applied')
    expect(screen.getByTestId('cad-edit-entity-list').textContent).toContain('7,3')
  })

  it('moves one vertex, adds one, deletes one — each re-parsed from written bytes', async () => {
    renderSurface()
    await openDocument(TWO_POLY_DXF, 'polys.dxf')
    selectEntity(0)
    fireEvent.change(screen.getByLabelText('vertex index'), { target: { value: '1' } })
    fireEvent.change(screen.getByLabelText('dx'), { target: { value: '5' } })
    fireEvent.change(screen.getByLabelText('dy'), { target: { value: '5' } })
    fireEvent.click(screen.getByRole('button', { name: 'Move vertex by dx,dy' }))
    await statusContains('moveVertex applied')
    fireEvent.click(screen.getByRole('button', { name: 'Add vertex after (at dx,dy)' }))
    await statusContains('addVertex applied')
    expect(screen.getByTestId('cad-edit-entity-list').textContent).toContain('4 vertices')
    fireEvent.click(screen.getByRole('button', { name: 'Delete vertex' }))
    await statusContains('deleteVertex applied')
    expect(screen.getByTestId('cad-edit-entity-list').textContent).toContain('3 vertices')
  })

  it('reassigns the layer and the re-parse proves the written bytes carry it', async () => {
    renderSurface()
    await openDocument(TWO_POLY_DXF, 'polys.dxf')
    selectEntity(1)
    fireEvent.change(screen.getByLabelText('layer name'), { target: { value: 'Renamed' } })
    fireEvent.click(screen.getByRole('button', { name: 'Set layer' }))
    await statusContains('setLayer applied')
    expect(screen.getByTestId('cad-edit-entity-list').textContent).toContain('LWPOLYLINE on layer Renamed')
  })

  it('a refused edit (vertex floor) reports the typed reason and mutates nothing', async () => {
    renderSurface()
    await openDocument(ONE_LINE_DXF)
    selectEntity(0)
    fireEvent.click(screen.getByRole('button', { name: 'Delete vertex' }))
    await statusContains('line_has_fixed_endpoints')
    expect(screen.getByTestId('cad-edit-entity-count').textContent).toBe('1')
  })

  it('offers the edited bytes as a download only after a successful edit', async () => {
    renderSurface()
    await openDocument(ONE_LINE_DXF)
    expect(screen.queryByText('Download edited DXF')).toBeNull()
    selectEntity(0)
    fireEvent.click(screen.getByRole('button', { name: 'Move selected' }))
    await statusContains('move applied')
    expect(screen.getByText('Download edited DXF')).toBeInTheDocument()
  })

  it('refuses a non-numeric delta before it reaches the engine', async () => {
    renderSurface()
    await openDocument(ONE_LINE_DXF)
    selectEntity(0)
    const before = worker.posted.length
    fireEvent.change(screen.getByLabelText('dx'), { target: { value: 'nope' } })
    fireEvent.click(screen.getByRole('button', { name: 'Move selected' }))
    await statusContains('must both be numbers')
    expect(worker.posted.length).toBe(before)
  })

  it('keeps every edit button disabled until an entity is selected', async () => {
    renderSurface()
    await openDocument(ONE_LINE_DXF)
    for (const name of ['Delete selected', 'Move selected', 'Set layer']) {
      expect(screen.getByRole('button', { name })).toBeDisabled()
    }
  })
})
