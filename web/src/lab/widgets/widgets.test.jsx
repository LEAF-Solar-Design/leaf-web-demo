import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import WidgetsView from './WidgetsView.jsx'
import WidgetPreview from './WidgetPreview.jsx'
import { createTextElement, validateElement, validateElements, validateWidgetDoc } from './widgetDoc.js'
import { createWidgetDoc } from './useWidgetEditor.js'
import { DESIGNER_DEFAULT_DOC } from './designerDefault.js'

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
const add = () => fireEvent.click(screen.getByRole('button', { name: 'Add text' }))
const text = (value) => fireEvent.change(screen.getByLabelText('Text'), { target: { value } })
const preview = () => screen.getByRole('region', { name: 'Preview' })
const download = () => screen.getByRole('button', { name: 'Download widget definition' })
function captureDownload() {
  const createObjectURL = vi.fn(() => 'blob:widget-definition'), revokeObjectURL = vi.fn()
  vi.stubGlobal('URL', class extends URL {
    static createObjectURL = createObjectURL
    static revokeObjectURL = revokeObjectURL
  })
  let anchor
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function () {
    expect(this.isConnected).toBe(true)
    anchor = this
  })
  return { createObjectURL, revokeObjectURL, anchor: () => anchor }
}
function readBlob(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(JSON.parse(reader.result))
    reader.onerror = () => reject(reader.error)
    reader.readAsText(blob)
  })
}

describe('widget definition download', () => {
  it('downloads a new valid document locally with a safe filename and cleans up', async () => {
    const captured = captureDownload(), fetchImpl = vi.fn(), requestPublication = vi.fn()
    vi.stubGlobal('fetch', fetchImpl)
    render(<WidgetsView requestPublication={requestPublication} />)
    expect(download()).toBeDisabled()
    fireEvent.click(download())
    expect(captured.createObjectURL).not.toHaveBeenCalled()
    for (const value of ['../escape', 'bad/name', 'bad?']) {
      fireEvent.change(screen.getByLabelText('Document ID'), { target: { value } })
      expect(screen.getByRole('button', { name: 'New' })).toBeDisabled()
      expect(download()).toBeDisabled()
    }
    fireEvent.change(screen.getByLabelText('Document ID'), { target: { value: 'CON' } })
    fireEvent.click(screen.getByRole('button', { name: 'New' }))
    expect(download()).toBeEnabled()
    expect(screen.getByText('Downloading does not save edits to the project.')).toBeInTheDocument()
    fireEvent.click(download())
    const blob = captured.createObjectURL.mock.calls[0][0]
    expect(blob).toBeInstanceOf(Blob)
    expect(blob.type).toBe('application/json')
    expect(await readBlob(blob)).toEqual(validateWidgetDoc(createWidgetDoc(DESIGNER_DEFAULT_DOC)))
    expect(captured.anchor().download).toBe('widget-CON.json')
    expect(captured.anchor().getAttribute('href')).toBe('blob:widget-definition')
    expect(captured.anchor().isConnected).toBe(false)
    expect(document.querySelector('a[download]')).toBeNull()
    await waitFor(() => expect(captured.revokeObjectURL).toHaveBeenCalledExactlyOnceWith('blob:widget-definition'))
    expect(fetchImpl).not.toHaveBeenCalled()
    expect(requestPublication).not.toHaveBeenCalled()
  })

  it('exports the validated loaded structure with current unsaved edits, without a request', async () => {
    const captured = captureDownload(), doc = createWidgetDoc()
    doc.formats.la.els = [{ ...createTextElement('text1'), preset: 'text', min_renderer: '1.0.0' }]
    const fetchImpl = vi.fn(async () => ({ ok: true, json: async () => ({ graph: doc, revision: 1, kind: 'widget' }) }))
    vi.stubGlobal('fetch', fetchImpl)
    render(<WidgetsView />)
    fireEvent.change(screen.getByLabelText('Document ID'), { target: { value: 'my-widget.v1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Load', exact: true }))
    expect(download()).toBeDisabled()
    await waitFor(() => expect(download()).toBeEnabled())
    fireEvent.click(within(screen.getByRole('list', { name: 'Text elements' })).getByRole('button', { name: 'Text 1: Your text' }))
    text('Current unsaved text')
    const requestCount = fetchImpl.mock.calls.length
    fireEvent.click(download())
    const exported = await readBlob(captured.createObjectURL.mock.calls[0][0])
    const expected = createWidgetDoc(doc)
    expected.formats.la.els[0].text = 'Current unsaved text'
    expect(exported).toEqual(validateWidgetDoc(expected))
    expect(exported.schema).toBe('widget-doc.v1')
    expect(exported.formats.la.els[0].text).toBe('Current unsaved text')
    expect(captured.anchor().download).toBe('widget-my-widget.v1.json')
    expect(fetchImpl).toHaveBeenCalledTimes(requestCount)
    expect(screen.getByRole('status')).toHaveTextContent('Local edits')
  })

  it('blocks mismatched IDs, invalid edits and unfinished editor transactions', () => {
    const captured = captureDownload()
    vi.stubGlobal('PointerEvent', class extends MouseEvent {
      constructor(type, options) { super(type, options); this.pointerId = options.pointerId ?? 1 }
    })
    render(<WidgetsView />)
    fireEvent.change(screen.getByLabelText('Document ID'), { target: { value: 'widget' } })
    fireEvent.click(screen.getByRole('button', { name: 'New' }))
    add()
    fireEvent.change(screen.getByLabelText('Document ID'), { target: { value: 'other' } })
    expect(download()).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Document ID'), { target: { value: 'widget' } })
    expect(download()).toBeEnabled()
    fireEvent.change(screen.getByLabelText('Binding source'), { target: { value: 'graph.output' } })
    expect(download()).toBeDisabled()
    fireEvent.click(download())
    fireEvent.change(screen.getByLabelText('Binding source'), { target: { value: 'none' } })
    expect(download()).toBeEnabled()
    const surface = screen.getByTestId('widget-surface')
    fireEvent.pointerDown(surface.querySelector('.widget-interaction [data-element-id]'), { clientX: 20, clientY: 20, pointerId: 1, button: 0 })
    expect(download()).toBeDisabled()
    fireEvent.click(download())
    fireEvent.pointerCancel(surface, { pointerId: 1 })
    expect(download()).toBeEnabled()
    expect(captured.createObjectURL).not.toHaveBeenCalled()
  })

  it.each([422, 409, 503])('blocks saving and the resulting server status %s', async (status) => {
    const captured = captureDownload()
    let finish
    vi.stubGlobal('fetch', vi.fn(() => new Promise((resolve) => { finish = resolve })))
    render(<WidgetsView />)
    fireEvent.change(screen.getByLabelText('Document ID'), { target: { value: 'widget' } })
    fireEvent.click(screen.getByRole('button', { name: 'New' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(download()).toBeDisabled()
    fireEvent.click(download())
    finish({ ok: false, status, json: async () => ({ detail: 'Server refused the document' }) })
    await screen.findByRole('alert')
    expect(download()).toBeDisabled()
    fireEvent.click(download())
    expect(captured.createObjectURL).not.toHaveBeenCalled()
  })

  it('blocks an existing document during reload and after an invalid loaded document', async () => {
    const captured = captureDownload()
    let finish
    vi.stubGlobal('fetch', vi.fn(() => new Promise((resolve) => { finish = resolve })))
    render(<WidgetsView />)
    fireEvent.change(screen.getByLabelText('Document ID'), { target: { value: 'widget' } })
    fireEvent.click(screen.getByRole('button', { name: 'New' }))
    expect(download()).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: 'Load', exact: true }))
    expect(download()).toBeDisabled()
    fireEvent.click(download())
    finish({ ok: true, json: async () => ({ graph: { schema: 'invalid' }, revision: 1, kind: 'widget' }) })
    await screen.findByRole('alert')
    expect(download()).toBeDisabled()
    fireEvent.click(download())
    expect(captured.createObjectURL).not.toHaveBeenCalled()
  })
})

describe('scoped widget designer', () => {
  it('hides publication in standalone Studio', () => {
    render(<WidgetsView />)
    expect(screen.queryByRole('button', { name: 'Request publication' })).not.toBeInTheDocument()
  })

  it('requests publication only for a saved clean document, retaining load and save digests', async () => {
    const doc = createWidgetDoc()
    const loadedDigest = 'a'.repeat(64), savedDigest = 'b'.repeat(64)
    const response = (body) => ({ ok: true, json: async () => body })
    const fetchImpl = vi.fn().mockResolvedValueOnce(response({ graph: doc, revision: 1, sha256: loadedDigest, kind: 'widget' }))
      .mockResolvedValueOnce(response({ revision: 2, sha256: savedDigest }))
      .mockResolvedValue(response({ values: [] }))
    vi.stubGlobal('fetch', fetchImpl)
    const requestPublication = vi.fn().mockResolvedValueOnce({ ok: true }).mockResolvedValueOnce({ ok: false, reason: 'not_admitted' })
    render(<WidgetsView requestPublication={requestPublication} />)
    const button = screen.getByRole('button', { name: 'Request publication' })
    expect(button).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Document ID'), { target: { value: 'widget' } })
    fireEvent.click(screen.getByRole('button', { name: 'New' }))
    expect(button).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Load', exact: true }))
    await waitFor(() => expect(button).toBeEnabled())
    fireEvent.click(button)
    expect(requestPublication).toHaveBeenLastCalledWith({ doc, source: { artifact_id: 'widget', revision: 1, digest: loadedDigest } })
    expect(await screen.findByLabelText('Publication request')).toHaveTextContent('Publication requested')
    add()
    expect(button).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(button).toBeEnabled())
    fireEvent.click(button)
    await waitFor(() => expect(screen.getByLabelText('Publication request')).toHaveTextContent('not_admitted'))
    expect(requestPublication).toHaveBeenLastCalledWith({ doc: expect.objectContaining({ formats: expect.any(Object) }), source: { artifact_id: 'widget', revision: 2, digest: savedDigest } })
    expect(requestPublication.mock.calls[1][0].doc.formats.la.els).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: 'New' }))
    expect(button).toBeDisabled()
  })

  it('edits and previews literal text and removes it', () => {
    render(<WidgetsView scopeKey="a/p" />)
    expect(preview().querySelectorAll('[data-element-id]')).toHaveLength(0)
    add()
    const literal = '<img onerror="bad()"> & text'
    text(literal)
    expect(within(preview()).getByText(literal)).toBeInTheDocument()
    expect(preview().querySelector('img, script')).toBeNull()
    expect(preview().querySelectorAll('[data-element-id]')).toHaveLength(1)
    expect(screen.getByLabelText('Text')).toHaveAttribute('maxlength', '40')
    fireEvent.click(screen.getByRole('button', { name: 'Remove', exact: true }))
    expect(within(preview()).queryByText(literal)).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Text')).not.toBeInTheDocument()
  })
  it('restores drafts and selection per scope, including hidden scopes, without network calls', () => {
    const fetchImpl = vi.fn()
    vi.stubGlobal('fetch', fetchImpl)
    const { rerender, unmount } = render(<WidgetsView scopeKey="alice/a" />)
    add(); text('Alice first'); add(); text('Alice second')
    fireEvent.click(within(screen.getByRole('list', { name: 'Text elements' })).getByRole('button', { name: 'Text 1: Alice first' }))
    const ids = [...preview().querySelectorAll('[data-element-id]')].map((e) => e.dataset.elementId)
    for (const scopeKey of ['bob/a', 'alice/b', '__proto__']) {
      rerender(<WidgetsView scopeKey={scopeKey} />)
      expect(screen.queryByLabelText('Text')).not.toBeInTheDocument()
      add(); text(scopeKey)
    }
    rerender(<WidgetsView scopeKey="alice/a" hidden />)
    expect(screen.queryByRole('region', { name: 'Text widget editor' })).not.toBeInTheDocument()
    rerender(<WidgetsView scopeKey="alice/a" />)
    expect(screen.getByLabelText('Text')).toHaveValue('Alice first')
    expect([...preview().querySelectorAll('[data-element-id]')].map((e) => e.dataset.elementId)).toEqual(ids)
    expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument()
    expect(fetchImpl).not.toHaveBeenCalled()
    unmount(); render(<WidgetsView scopeKey="alice/a" />)
    expect(screen.queryByLabelText('Text')).not.toBeInTheDocument()
  })
  it('starts the authenticated designer only after explicit document entry', () => {
    const fetchImpl = vi.fn()
    vi.stubGlobal('fetch', fetchImpl)
    render(<WidgetsView />)
    expect(screen.getByRole('status')).toHaveTextContent('Enter a Document ID, then choose New or Load.')
    expect(screen.queryByRole('button', { name: 'Add text' })).not.toBeInTheDocument()
    expect(fetchImpl).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText('Document ID'), { target: { value: 'my-widget' } })
    fireEvent.click(screen.getByRole('button', { name: 'New' }))
    add()
    expect(screen.getByLabelText('Text')).toHaveValue('Your text')
    expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled()
    expect(fetchImpl).not.toHaveBeenCalled()
  })
  it('limits each format to 32 elements and permits adding after removal', () => {
    render(<WidgetsView scopeKey="a/p" />)
    const addText = screen.getByRole('button', { name: 'Add text' })
    for (let n = 0; n < 32; n += 1) fireEvent.click(addText)
    const elements = preview().querySelectorAll('[data-element-id]')
    expect(new Set([...elements].map((e) => e.dataset.elementId)).size).toBe(32)
    expect(screen.getByRole('button', { name: 'Add text' })).toBe(addText)
    expect(addText).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Remove', exact: true }))
    expect(addText).toBeEnabled()
  })
})

describe('existing text primitive boundary', () => {
  it('still validates generic defaults and refuses executable fields and duplicate ids', () => {
    expect(createTextElement('text-1').text).toBe('Your text')
    for (const field of ['action', 'html', 'url', 'provider']) expect(() => validateElement({ ...createTextElement('a'), [field]: 'untrusted' })).toThrow('Unsupported')
    expect(() => validateElements([createTextElement('a'), createTextElement('a')])).toThrow('unique')
    expect(() => validateElements(new Array(1))).toThrow()
    expect(() => validateElements(Array.from({ length: 33 }, (_, n) => createTextElement(String(n))))).toThrow('32')
  })
  it.each([{ x: NaN }, { y: Infinity }, { w: -1 }, { h: 0 }, { x: -1 }, { y: 145 }, { x: 242 }, { weight: 501 }, { color: 'url(evil)' }, { visible: 'true' }, { text: 'x'.repeat(513) }])('rejects invalid model values %j', (patch) => {
    expect(() => validateElement({ ...createTextElement('a'), ...patch })).toThrow()
  })
  it('retains the standalone preview boundary and visibility behavior', () => {
    const doc = createWidgetDoc()
    doc.formats.la.els = [{ ...createTextElement('a'), min_renderer: '1.0.0', x: 241, y: 144, text: 'Boundary' }, { ...createTextElement('b'), min_renderer: '1.0.0', visible: false }]
    render(<WidgetPreview doc={doc} />)
    const livePreview = screen.getByRole('region', { name: 'Live widget preview' })
    expect(livePreview.querySelectorAll('[data-element-id]')).toHaveLength(1)
    expect(within(livePreview).getByText('Boundary')).toBeInTheDocument()
    expect(livePreview.querySelector('[data-element-id="a"]')).toHaveAttribute('data-freshness', 'sample')
  })
})
