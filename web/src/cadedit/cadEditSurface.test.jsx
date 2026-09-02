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
import EngineSessionProvider from './EngineSessionProvider.jsx'
import EngineRibbonClusters from './EngineRibbonClusters.jsx'
import DraftingRibbon from '../site/DraftingRibbon.jsx'

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
// Bound for a reply from the real engine child (see openDocument).
const ENGINE_WAIT_MS = 20_000

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

// Adjacent valid u64 handles above Number.MAX_SAFE_INTEGER. They round to the
// same JavaScript number, so this fixture catches any numeric wasm projection
// that can redirect an edit onto a neighbouring entity.
const HIGH_HANDLE_DXF = [
  '0', 'SECTION', '2', 'ENTITIES',
  '0', 'LINE', '5', '20000000000000', '8', '0',
  '10', '0', '20', '0', '30', '0', '11', '1', '21', '0', '31', '0',
  '0', 'LINE', '5', '20000000000001', '8', '0',
  '10', '100', '20', '0', '30', '0', '11', '101', '21', '0', '31', '0',
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

// W4d: the session comes from the ONE provider; the surface is a consumer.
function renderSurface(providerProps = {}) {
  worker = new FakeEngineWorker()
  return render(
    <EngineSessionProvider createWorker={() => worker} {...providerProps}>
      <CadEditSurface enabled />
    </EngineSessionProvider>,
  )
}

// A provider that must never spawn: flag/notice cases that open nothing.
function withInertProvider(node) {
  return (
    <EngineSessionProvider createWorker={() => { throw new Error('unexpected engine spawn') }}>
      {node}
    </EngineSessionProvider>
  )
}

async function openDocument(text, name) {
  fireEvent.change(screen.getByLabelText('DXF file'), { target: { files: [fileOf(text, name)] } })
  // The engine runs in a real async child here: wait for the LOAD REPORT,
  // not merely for the count element (which renders while still busy).
  // A real engine in a real child process: under host load the default
  // 1 s waitFor is a flake generator, not an oracle. The bound is generous
  // because a MISSING reply still fails, only later.
  await waitFor(() => expect(screen.getByRole('status').textContent).toContain('Loaded'), { timeout: ENGINE_WAIT_MS })
}

function selectEntity(index = 0) {
  fireEvent.click(screen.getAllByRole('radio')[index])
}

async function statusContains(fragment) {
  await waitFor(() => expect(screen.getByRole('status').textContent).toContain(fragment), { timeout: ENGINE_WAIT_MS })
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
    render(<EngineSessionProvider createWorker={createWorker}><CadEditSurface enabled /></EngineSessionProvider>)
    expect(screen.getByTestId('cad-edit-workbench')).toBeInTheDocument()
    expect(createWorker).not.toHaveBeenCalled()
  })

  it('refuses a file over the byte cap without reading or spawning anything', async () => {
    const createWorker = vi.fn(() => new FakeEngineWorker())
    render(<EngineSessionProvider createWorker={createWorker}><CadEditSurface enabled /></EngineSessionProvider>)
    fireEvent.change(screen.getByLabelText('DXF file'), { target: { files: [oversizedFile()] } })
    await statusContains('exceeds')
    expect(createWorker).not.toHaveBeenCalled()
  })

  it('F-4: renders the runtime engine notice when the contract supplies one', () => {
    render(withInertProvider(<CadEditSurface enabled notice="Includes a third-party engine under MPL-2.0." />))
    expect(screen.getByTestId('cad-edit-engine-notice').textContent)
      .toContain('MPL-2.0')
  })

  it('F-4: renders no notice element when the contract supplies none (flag-off truth)', () => {
    render(withInertProvider(<CadEditSurface enabled />))
    expect(screen.queryByTestId('cad-edit-engine-notice')).toBeNull()
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

  it('keeps adjacent u64 handles above 2^53 distinct and deletes only the selected entity', async () => {
    renderSurface()
    await openDocument(HIGH_HANDLE_DXF, 'high-handles.dxf')
    const radios = screen.getAllByRole('radio')
    expect(radios.map((radio) => radio.value)).toEqual(['9007199254740992', '9007199254740993'])
    selectEntity(1)
    expect(radios[1].checked).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Delete selected' }))
    await statusContains('delete applied')
    expect(screen.getByTestId('cad-edit-entity-count').textContent).toBe('1')
    const remaining = screen.getByTestId('cad-edit-entity-list').textContent
    expect(remaining).toContain('(0,0 → 1,0)')
    expect(remaining).not.toContain('(100,0 → 101,0)')
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

  it('F-3b: saves edited bytes to the project with a client-computed digest and shows the receipt', async () => {
    const save = vi.fn(async (bytes, _parent, digest) => {
      const expected = await (async () => {
        const d = await crypto.subtle.digest('SHA-256', bytes)
        return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, '0')).join('')
      })()
      expect(digest).toBe(expected)
      return {
        new_version: { drawing_id: 'rooftop', version: 5, parent: 4 },
        head: 5,
        source_sha256: digest,
        cost: { engine_usd: 0, engine: 'client-wasm' },
      }
    })
    const onSaved = vi.fn()
    renderSurface({ saveTarget: { drawingId: 'rooftop', headVersion: 4, save }, onSaved })
    await openDocument(ONE_LINE_DXF)
    expect(screen.queryByTestId('cad-edit-save-version')).toBeNull()
    selectEntity(0)
    fireEvent.click(screen.getByRole('button', { name: 'Move selected' }))
    await statusContains('move applied')
    fireEvent.click(screen.getByTestId('cad-edit-save-version'))
    await statusContains('Saved as version 5')
    await statusContains('engine cost $0')
    expect(save).toHaveBeenCalledTimes(1)
    expect(onSaved).toHaveBeenCalledTimes(1)
  })

  it('F-3b: a moved head (409) reads back as a refusal, not a success', async () => {
    const conflict = Object.assign(new Error('stale parent 4: head is now 6; refresh'), { status: 409 })
    const save = vi.fn(async () => { throw conflict })
    renderSurface({ saveTarget: { drawingId: 'rooftop', headVersion: 4, save } })
    await openDocument(ONE_LINE_DXF)
    selectEntity(0)
    fireEvent.click(screen.getByRole('button', { name: 'Move selected' }))
    await statusContains('move applied')
    fireEvent.click(screen.getByTestId('cad-edit-save-version'))
    await statusContains('Save refused: stale parent')
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

describe.skipIf(!HAS_ENGINE)('acceptance: the Draw group creates real entities through the boundary (W4d Slice B)', () => {
  // The cockpit's exact shape: the ribbon's engine clusters and the import
  // pane under the ONE provider, the real compiled engine behind the worker.
  function renderCockpit() {
    worker = new FakeEngineWorker()
    return render(
      <EngineSessionProvider createWorker={() => worker}>
        <DraftingRibbon clusters={[]}>
          <EngineRibbonClusters importOpen={false} onToggleImport={() => {}} />
        </DraftingRibbon>
        <CadEditSurface enabled />
      </EngineSessionProvider>,
    )
  }
  const drawTool = (op) => document.querySelector(`.drafting-ribbon [data-tool="draw:${op}"]`)
  const modifyTool = (op) => document.querySelector(`.drafting-ribbon [data-tool="modify:${op}"]`)

  it('draws a line, a circle, an arc and a polyline; each lands selected, re-parsed from the written bytes', async () => {
    renderCockpit()
    await openDocument(ONE_LINE_DXF)
    expect(screen.getByTestId('cad-edit-entity-count').textContent).toBe('1')

    fireEvent.change(screen.getByLabelText('ribbon x2'), { target: { value: '40' } })
    fireEvent.change(screen.getByLabelText('ribbon y2'), { target: { value: '30' } })
    fireEvent.click(drawTool('createLine'))
    await statusContains('createLine applied: entity ')
    expect(screen.getByTestId('cad-edit-entity-count').textContent).toBe('2')
    expect(screen.getByTestId('cad-edit-entity-list').textContent).toContain('40,30')
    expect(screen.getAllByRole('radio')[1].checked).toBe(true)

    fireEvent.change(screen.getByLabelText('ribbon r'), { target: { value: '2.5' } })
    fireEvent.click(drawTool('createCircle'))
    await statusContains('createCircle applied: entity ')
    expect(screen.getByTestId('cad-edit-entity-list').textContent).toContain('CIRCLE on layer 0')

    fireEvent.change(screen.getByLabelText('ribbon end'), { target: { value: '180' } })
    fireEvent.click(drawTool('createArc'))
    await statusContains('createArc applied: entity ')
    expect(screen.getByTestId('cad-edit-entity-list').textContent).toContain('ARC on layer 0')

    fireEvent.change(screen.getByLabelText('ribbon points'), { target: { value: '0,0 10,0 10,4' } })
    fireEvent.click(screen.getByLabelText('ribbon closed'))
    fireEvent.click(drawTool('createPolyline'))
    await statusContains('createPolyline applied: entity ')
    expect(screen.getByTestId('cad-edit-entity-count').textContent).toBe('5')
    expect(screen.getByTestId('cad-edit-entity-list').textContent).toContain('3 vertices · closed')

    // What the ribbon drew, the ribbon can modify: the selection is on the
    // polyline; delete it and the count re-parses back down.
    expect(modifyTool('delete').disabled).toBe(false)
    fireEvent.click(modifyTool('delete'))
    await statusContains('delete applied')
    expect(screen.getByTestId('cad-edit-entity-count').textContent).toBe('4')
  })

  it('the engine refuses a degenerate create with its typed reason and mutates nothing', async () => {
    renderCockpit()
    await openDocument(ONE_LINE_DXF)
    // The client sentence catches r=0; force the engine-side refusal with a
    // sweep the client cannot see through: 0 -> 360 is a full turn = zero.
    fireEvent.change(screen.getByLabelText('ribbon end'), { target: { value: '360' } })
    fireEvent.click(drawTool('createArc'))
    await statusContains('Arc refused')
    fireEvent.change(screen.getByLabelText('ribbon end'), { target: { value: '720' } })
    fireEvent.click(drawTool('createArc'))
    await statusContains('Arc refused')
    expect(screen.getByTestId('cad-edit-entity-count').textContent).toBe('1')
  })

  it('a created circle takes a centre move and re-layer, and refuses vertex-list edits by kind', async () => {
    renderCockpit()
    await openDocument(ONE_LINE_DXF)
    fireEvent.click(drawTool('createCircle'))
    await statusContains('createCircle applied: entity ')
    fireEvent.change(screen.getByLabelText('ribbon dx'), { target: { value: '7' } })
    fireEvent.change(screen.getByLabelText('ribbon dy'), { target: { value: '3' } })
    fireEvent.click(modifyTool('move'))
    await statusContains('move applied')
    expect(screen.getByTestId('cad-edit-entity-list').textContent).toContain('7,3')
    fireEvent.click(modifyTool('addVertex'))
    await statusContains('entity_kind_has_no_vertex_list')
    fireEvent.change(screen.getByLabelText('ribbon set layer'), { target: { value: 'Moved' } })
    fireEvent.click(modifyTool('setLayer'))
    await statusContains('setLayer applied')
    expect(screen.getByTestId('cad-edit-entity-list').textContent).toContain('CIRCLE on layer Moved')
  })
})
