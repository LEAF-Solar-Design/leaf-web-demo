/**
 * CadEditSurface acceptance oracle.
 *
 * The fake Worker below is a TRANSPORT double only: every message it answers
 * is computed by the REAL documentWorker.handleMessage, so open -> list ->
 * select -> edit -> write-back is exercised against the real engine code and
 * the real (unmodified) EngineBoundary. Nothing about the parse, the edit,
 * or the round trip is restated here.
 *
 * jsdom has no Worker and no File.arrayBuffer; both are installed per test.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import CadEditSurface from './CadEditSurface.jsx'
import { handleMessage } from './documentWorker.js'

const ONE_LINE_DXF = [
  '0', 'SECTION', '2', 'HEADER', '9', '$ACADVER', '1', 'AC1009', '0', 'ENDSEC',
  '0', 'SECTION', '2', 'ENTITIES',
  '0', 'LINE', '8', '0',
  '10', '0.0', '20', '0.0', '30', '0.0',
  '11', '100.0', '21', '50.0', '31', '0.0',
  '0', 'ENDSEC', '0', 'EOF',
].join('\n') + '\n'

const WITH_CIRCLE = ONE_LINE_DXF.replace(
  '0\nENDSEC\n0\nEOF\n', '0\nCIRCLE\n8\n0\n10\n1.0\n20\n1.0\n40\n5.0\n0\nENDSEC\n0\nEOF\n')

// Transport double: real handleMessage behind a Worker-shaped surface.
class FakeEngineWorker {
  constructor() {
    this.listeners = []
    this.posted = []
    this.terminated = false
  }

  addEventListener(type, cb) {
    if (type === 'message') this.listeners.push(cb)
  }

  removeEventListener() {}

  postMessage(data) {
    this.posted.push(data)
    const response = handleMessage(data)
    if (response) for (const cb of this.listeners) cb({ data: response })
  }

  terminate() {
    this.terminated = true
  }
}

function fileOf(text, name = 'one_line.dxf') {
  const bytes = new TextEncoder().encode(text)
  const file = new File([bytes], name, { type: 'application/dxf' })
  // jsdom's File has no arrayBuffer() in this environment.
  file.arrayBuffer = async () => bytes.buffer.slice(0)
  Object.defineProperty(file, 'size', { value: bytes.length })
  return file
}

function oversizedFile() {
  const file = new File([new Uint8Array(1)], 'huge.dxf')
  file.arrayBuffer = async () => new ArrayBuffer(1)
  Object.defineProperty(file, 'size', { value: 5 * 1024 * 1024 })
  return file
}

let worker

function renderSurface(props = {}) {
  worker = new FakeEngineWorker()
  return render(<CadEditSurface enabled createWorker={() => worker} {...props} />)
}

async function openDocument(text, name) {
  fireEvent.change(screen.getByLabelText('DXF file'), { target: { files: [fileOf(text, name)] } })
  await waitFor(() => expect(screen.getByTestId('cad-edit-entity-count')).toBeInTheDocument())
}

beforeEach(() => {
  handleMessage({ type: 'dispose' })
  globalThis.URL.createObjectURL = vi.fn(() => 'blob:cad-edit-test')
  globalThis.URL.revokeObjectURL = vi.fn()
})

afterEach(cleanup)

describe('acceptance: the editing surface mounts only behind cad_edit', () => {
  it('renders nothing when the flag is off', () => {
    const { container } = render(<CadEditSurface enabled={false} />)
    expect(container.firstChild).toBeNull()
  })

  it('does not mount by default (VITE_CAD_EDIT unset in this test build)', () => {
    const { container } = render(<CadEditSurface />)
    expect(container.firstChild).toBeNull()
  })

  it('never spawns the engine worker at mount — only on the first open', () => {
    const createWorker = vi.fn(() => new FakeEngineWorker())
    render(<CadEditSurface enabled createWorker={createWorker} />)

    expect(screen.getByTestId('cad-edit-workbench')).toBeInTheDocument()
    expect(createWorker).not.toHaveBeenCalled()
  })
})

describe('acceptance: opening a DXF lists the parsed entities', () => {
  it('loads through the boundary and lists one entity with its real coordinates', async () => {
    renderSurface()
    await openDocument(ONE_LINE_DXF)

    expect(screen.getByTestId('cad-edit-entity-count')).toHaveTextContent('1')
    const items = within(screen.getByTestId('cad-edit-entity-list')).getAllByRole('listitem')
    expect(items).toHaveLength(1)
    expect(items[0]).toHaveTextContent('LINE on layer 0: (0, 0, 0) to (100, 50, 0)')
    expect(screen.getByRole('status')).toHaveTextContent('Loaded one_line.dxf: 1 editable entities.')
  })

  it('sends init then loadDocument through the boundary, in that order', async () => {
    renderSurface()
    await openDocument(ONE_LINE_DXF)

    expect(worker.posted.map((m) => m.type)).toEqual(['init', 'loadDocument'])
    expect(worker.posted[1].documentId).toBe('one_line.dxf')
  })

  it('refuses a file over the byte cap without reading or spawning anything', async () => {
    const createWorker = vi.fn(() => new FakeEngineWorker())
    render(<CadEditSurface enabled createWorker={createWorker} />)

    fireEvent.change(screen.getByLabelText('DXF file'), { target: { files: [oversizedFile()] } })

    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(/exceeds the 4194304-byte limit/))
    expect(createWorker).not.toHaveBeenCalled()
  })

  it('surfaces an engine parse refusal truthfully instead of an empty list', async () => {
    renderSurface()
    fireEvent.change(screen.getByLabelText('DXF file'), {
      target: { files: [fileOf('NOTACODE\nSECTION\n', 'bad.dxf')] },
    })

    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(/Engine refused: parse_failed:bad_group_code/))
    expect(screen.queryByTestId('cad-edit-entity-list')).toBeNull()
  })
})

describe('acceptance: one real edit, end to end, with a truthful write-back', () => {
  it('deletes the selected entity and reports the count re-parsed from the written bytes', async () => {
    renderSurface()
    await openDocument(ONE_LINE_DXF)

    fireEvent.click(screen.getByRole('radio'))
    fireEvent.click(screen.getByRole('button', { name: 'Delete selected' }))

    await waitFor(() => expect(screen.getByTestId('cad-edit-entity-count')).toHaveTextContent('0'))
    expect(screen.getByRole('status')).toHaveTextContent(/delete applied\. Re-parsed from the written bytes: 0 entities, \d+ bytes\./)
    expect(screen.queryByTestId('cad-edit-entity-list')).toBeNull()
  })

  it('moves the selected entity by the entered delta and re-lists the translated geometry', async () => {
    renderSurface()
    await openDocument(ONE_LINE_DXF)

    fireEvent.click(screen.getByRole('radio'))
    fireEvent.change(screen.getByLabelText('dx'), { target: { value: '10' } })
    fireEvent.change(screen.getByLabelText('dy'), { target: { value: '20' } })
    fireEvent.click(screen.getByRole('button', { name: 'Move selected' }))

    await waitFor(() => expect(screen.getByTestId('cad-edit-entity-list'))
      .toHaveTextContent('LINE on layer 0: (10, 20, 0) to (110, 70, 0)'))
    expect(screen.getByTestId('cad-edit-entity-count')).toHaveTextContent('1')
  })

  it('offers the edited bytes as a download only after a successful edit', async () => {
    renderSurface()
    await openDocument(ONE_LINE_DXF)
    expect(screen.queryByRole('link', { name: 'Download edited DXF' })).toBeNull()

    fireEvent.click(screen.getByRole('radio'))
    fireEvent.click(screen.getByRole('button', { name: 'Move selected' }))

    await waitFor(() => expect(screen.getByRole('link', { name: 'Download edited DXF' })).toBeInTheDocument())
    expect(screen.getByRole('link', { name: 'Download edited DXF' })).toHaveAttribute('download', 'one_line.dxf')
  })

  it('refuses a non-numeric delta before it reaches the engine', async () => {
    renderSurface()
    await openDocument(ONE_LINE_DXF)

    fireEvent.click(screen.getByRole('radio'))
    fireEvent.change(screen.getByLabelText('dx'), { target: { value: 'over there' } })
    fireEvent.click(screen.getByRole('button', { name: 'Move selected' }))

    expect(screen.getByRole('status')).toHaveTextContent('Move refused: dx and dy must both be numbers.')
    expect(worker.posted.filter((m) => m.type === 'applyEdit')).toHaveLength(0)
  })

  it('keeps both edit buttons disabled until an entity is selected', async () => {
    renderSurface()
    await openDocument(ONE_LINE_DXF)

    expect(screen.getByRole('button', { name: 'Delete selected' })).toBeDisabled()
    fireEvent.click(screen.getByRole('radio'))
    expect(screen.getByRole('button', { name: 'Delete selected' })).toBeEnabled()
  })
})

describe('acceptance: a document it can read but not rewrite is read-only, by name', () => {
  it('lists the readable entities but disables every edit control and says why', async () => {
    renderSurface()
    await openDocument(WITH_CIRCLE, 'with_circle.dxf')

    expect(screen.getByTestId('cad-edit-entity-count')).toHaveTextContent('1')
    expect(screen.getByRole('alert')).toHaveTextContent(/read but not rewrite entity types CIRCLE/)
    expect(screen.getByRole('radio')).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Delete selected' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Move selected' })).toBeDisabled()
  })
})
