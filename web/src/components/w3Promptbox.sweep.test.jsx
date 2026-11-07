// @vitest-environment jsdom
import { useState } from 'react'
import { act, cleanup, createEvent, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import PromptBox from './PromptBox.jsx'
import * as api from '../api.js'
import { getDrawingVersions, searchIndex } from '../api.js'

vi.mock('../api.js', async (importOriginal) => ({
  ...await importOriginal(),
  getStoredOrgId: vi.fn(() => null),
  getDrawingVersions: vi.fn(),
}))

function Harness({ initialValue = '', ...props }) {
  const [value, setValue] = useState(initialValue)
  return <PromptBox value={value} onChange={setValue} onDispatch={vi.fn()}
    liveCallsAllowed mcpDiscoveryEnabled={false} {...props} />
}

const input = () => screen.getByLabelText('Command bar')
const bar = () => document.querySelector('[data-tour="command-bar"]')
function chooseScope(scope) {
  fireEvent.click(document.querySelector('.bar-scope'))
  fireEvent.click(within(screen.getByRole('listbox', { name: 'Scope' })).getByText(scope))
}
function deferred() {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}

beforeEach(() => {
  vi.clearAllMocks()
  api.getStoredOrgId.mockReturnValue(null)
  getDrawingVersions.mockResolvedValue({ versions: [] })
  vi.spyOn(api, 'listOperatorSessions').mockResolvedValue({ sessions: [] })
  vi.spyOn(api, 'searchIndex').mockResolvedValue({ results: [] })
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ servers: [] }) }))
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

it('fix4 runs an Act client command through its handler and hides commands without handlers', () => {
  const help = { kind: 'command', name: 'help', client_action: 'show_help' }
  const onHelp = vi.fn()
  const onDispatch = vi.fn()
  render(<Harness tools={[
    { kind: 'tool', name: 'panel-count' }, help,
    { kind: 'command', name: 'unavailable-command', client_action: 'missing' },
  ]} commandActions={{ show_help: onHelp }} onDispatch={onDispatch} />)
  chooseScope('act')
  const menu = screen.getByRole('listbox', { name: 'Actions and artifacts' })
  expect(within(menu).getByRole('option', { name: /panel-count/ })).toBeTruthy()
  expect(within(menu).queryByRole('option', { name: /unavailable-command/ })).toBeNull()
  fireEvent.click(within(menu).getByRole('option', { name: /help/ }))
  expect(onHelp).toHaveBeenCalledExactlyOnceWith(help)
  expect(onDispatch).not.toHaveBeenCalled()
  expect(input()).toHaveValue('')
})

it('fix4 accepts an image drag with withheld metadata through dragover and drop', () => {
  vi.stubGlobal('URL', class extends URL {
    static createObjectURL = vi.fn(() => 'blob:withheld-image')
    static revokeObjectURL = vi.fn()
  })
  render(<Harness imageAttachmentsEnabled />)
  const metadata = { types: [], files: [] }
  fireEvent.dragEnter(bar(), { dataTransfer: metadata })
  expect(document.querySelector('.bar-drop-hint')).toBeTruthy()
  const over = createEvent.dragOver(bar(), { dataTransfer: metadata })
  fireEvent(bar(), over)
  expect(over.defaultPrevented).toBe(true)
  fireEvent.drop(bar(), { dataTransfer: { files: [new File(['png'], 'roof.png', { type: 'image/png' })] } })
  expect(screen.getByTestId('attachment-name')).toHaveTextContent('roof.png')
  expect(document.querySelector('.bar-drop-hint')).toBeNull()
  expect(screen.queryByRole('alert')).toBeNull()
})

it('fix5 clears the withheld-metadata drag hint when the drop has no files', () => {
  render(<Harness imageAttachmentsEnabled />)
  const metadata = { types: [], files: [] }
  fireEvent.dragEnter(bar(), { dataTransfer: metadata })
  expect(document.querySelector('.bar-drop-hint')).toBeTruthy()
  const over = createEvent.dragOver(bar(), { dataTransfer: metadata })
  fireEvent(bar(), over)
  expect(over.defaultPrevented).toBe(true)
  const drop = createEvent.drop(bar(), { dataTransfer: metadata })
  fireEvent(bar(), drop)
  expect(drop.defaultPrevented).toBe(false)
  expect(document.querySelector('.bar-drop-hint')).toBeNull()
  expect(screen.queryByTestId('attachment-chip')).toBeNull()
})

it('fix4 warns for an image plus HTML clipboard markup without plain text', () => {
  render(<Harness imageAttachmentsEnabled={false} />)
  const getData = vi.fn((type) => type === 'text/html' ? '<img src="clipboard.png">' : '')
  const event = createEvent.paste(input(), { clipboardData: {
    items: [{ kind: 'file', type: 'image/png' }, { kind: 'string', type: 'text/html' }],
    getData,
  } })
  fireEvent(input(), event)
  expect(getData).toHaveBeenCalledWith('text/plain')
  expect(event.defaultPrevented).toBe(true)
  expect(screen.getByRole('alert')).toHaveTextContent('Image paste is available in the assistant reply box.')
  expect(input()).toHaveValue('')
  expect(screen.queryByTestId('attachment-chip')).toBeNull()
})

it('fix4 reports a real sessions 503 and retries the fetch', async () => {
  api.listOperatorSessions.mockRestore()
  const fetch = vi.fn()
    .mockResolvedValueOnce({ ok: false, status: 503 })
    .mockResolvedValue({ ok: true, json: async () => ({ sessions: [] }) })
  vi.stubGlobal('fetch', fetch)
  render(<Harness />)
  chooseScope('act')
  expect(await screen.findByRole('alert')).toHaveTextContent('Some saved items could not be loaded. Try again.')
  expect(fetch.mock.calls[0][0]).toContain('/api/operator/sessions')
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2))
  expect(fetch.mock.calls[1][0]).toBe(fetch.mock.calls[0][0])
  await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
})

it('fix4 treats a real sessions 404 as the normal empty index', async () => {
  api.listOperatorSessions.mockRestore()
  const fetch = vi.fn().mockResolvedValue({ ok: false, status: 404 })
  vi.stubGlobal('fetch', fetch)
  render(<Harness />)
  chooseScope('act')
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1))
  await waitFor(() => expect(screen.queryByRole('status')).toBeNull())
  expect(fetch.mock.calls[0][0]).toContain('/api/operator/sessions')
  expect(screen.getByText(/No action or artifact matches/)).toBeTruthy()
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
})

it('fix4 waits for late project matches before declaring search empty', async () => {
  api.getStoredOrgId.mockReturnValue('org-a')
  const pending = deferred()
  vi.spyOn(api, 'listProjects').mockReturnValue(pending.promise)
  render(<Harness initialValue="roof" mcpDiscoveryEnabled />)
  chooseScope('find')
  await waitFor(() => expect(searchIndex).toHaveBeenCalledTimes(1))
  await waitFor(() => expect(api.listProjects).toHaveBeenCalledWith('org-a'))
  expect(screen.getByText('Searching…')).toBeTruthy()
  expect(screen.queryByText('No results for “roof”')).toBeNull()
  await act(async () => pending.resolve([{ project_id: 'p1', name: 'Roof design' }]))
  expect(await screen.findByRole('option', { name: /Roof design/ })).toBeTruthy()
  expect(screen.queryByText('No results for “roof”')).toBeNull()
})

it('fix4 tracks project loading again when Retry clears the project list', async () => {
  api.getStoredOrgId.mockReturnValue('org-a')
  const pending = deferred()
  vi.spyOn(api, 'listProjects').mockRejectedValueOnce(new Error('unavailable')).mockReturnValueOnce(pending.promise)
  render(<Harness initialValue="roof" mcpDiscoveryEnabled />)
  chooseScope('find')
  expect(await screen.findByRole('alert')).toHaveTextContent('Projects could not be loaded. Try again.')
  await waitFor(() => expect(searchIndex).toHaveBeenCalledTimes(1))
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  await waitFor(() => expect(searchIndex).toHaveBeenCalledTimes(2))
  expect(api.listProjects).toHaveBeenCalledTimes(2)
  expect(screen.getByText('Searching…')).toBeTruthy()
  expect(screen.queryByText('No results for “roof”')).toBeNull()
  await act(async () => pending.resolve([{ project_id: 'p1', name: 'Roof design' }]))
  expect(await screen.findByRole('option', { name: /Roof design/ })).toBeTruthy()
})

it('S1-46 preserves text paste when an office clipboard also carries an image', () => {
  render(<Harness />)
  const event = createEvent.paste(input(), { clipboardData: {
    items: [{ kind: 'file', type: 'image/png' }, { kind: 'string', type: 'text/plain' }],
    getData: () => 'Panel count\t24',
  } })
  fireEvent(input(), event)
  expect(event.defaultPrevented).toBe(false)
  expect(screen.queryByRole('alert')).toBeNull()
})

it('S1-46 lets catalog text and image clipboard items paste as text', () => {
  render(<Harness imageAttachmentsEnabled={false} />)
  const event = createEvent.paste(input(), { clipboardData: {
    items: [{ kind: 'file', type: 'image/png' }, { kind: 'string', type: 'text/html' }],
    getData: () => 'Panel count\t24',
  } })
  fireEvent(input(), event)
  expect(event.defaultPrevented).toBe(false)
  // jsdom does not perform the browser's default text insertion.
  fireEvent.change(input(), { target: { value: event.clipboardData.getData('text/plain') } })
  expect(input()).toHaveValue('Panel count\t24')
  expect(screen.queryByRole('alert')).toBeNull()
})

it('S1-46 explains image-only catalog paste and clears the warning on input', () => {
  render(<Harness imageAttachmentsEnabled={false} />)
  const event = createEvent.paste(input(), { clipboardData: {
    items: [{ kind: 'file', type: 'image/png' }],
  } })
  fireEvent(input(), event)
  expect(event.defaultPrevented).toBe(true)
  expect(screen.getByRole('alert')).toHaveTextContent('Image paste is available in the assistant reply box.')
  fireEvent.change(input(), { target: { value: 'count panels' } })
  expect(screen.queryByRole('alert')).toBeNull()
})

it('S1-47 marks project results unavailable with a visible reason instead of inserting an id', async () => {
  searchIndex.mockResolvedValue({ results: [{ kind: 'project', id: 'project:private-id', label: 'Roof design' }] })
  render(<Harness initialValue="roof" />)
  chooseScope('find')
  const row = await screen.findByRole('option', { name: /Roof design/ })
  expect(row).toHaveAttribute('aria-disabled', 'true')
  expect(row).toHaveTextContent('Open this item from its project or history view.')
  fireEvent.click(row)
  fireEvent.keyDown(input(), { key: 'Enter' })
  expect(input()).toHaveValue('roof')
  expect(screen.getByRole('listbox', { name: 'Search results' })).not.toHaveTextContent('private-id')
  expect(screen.queryByText(/Enter jumps to one/)).toBeNull()
})

it('S1-47 completes a tool as a usable slash command rather than a tool id', async () => {
  searchIndex.mockResolvedValue({ results: [{ kind: 'tool', id: 'tool:panel-count', label: 'panel-count' }] })
  render(<Harness initialValue="panel" />)
  chooseScope('find')
  fireEvent.click(await screen.findByText('panel-count'))
  expect(input()).toHaveValue('/panel-count ')
})

it('S1-48 shows search progress while the next query is pending and keeps old rows unselectable', async () => {
  searchIndex.mockResolvedValueOnce({ results: [{ kind: 'tool', id: 'tool:pump', label: 'pump' }] })
  const next = deferred()
  searchIndex.mockReturnValueOnce(next.promise)
  render(<Harness initialValue="pump" />)
  chooseScope('find')
  await screen.findByRole('option', { name: /pump/ })
  fireEvent.change(input(), { target: { value: 'zzz' } })
  expect(screen.getByText('Searching…')).toBeTruthy()
  expect(screen.queryByRole('option', { name: /pump/ })).toBeNull()
  await waitFor(() => expect(searchIndex).toHaveBeenCalledTimes(2))
  await act(async () => next.resolve({ results: [] }))
  expect(screen.getByText('No results for “zzz”')).toBeTruthy()
})

it('S1-48 reports a search rejection safely and retries from the input', async () => {
  searchIndex.mockRejectedValueOnce(new Error('GET /api/search -> 503'))
  render(<Harness initialValue="roof" />)
  chooseScope('find')
  expect(await screen.findByRole('alert')).toHaveTextContent('Search could not be completed. Try again.')
  expect(screen.getByRole('alert')).not.toHaveTextContent('/api/')
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  expect(input()).toHaveFocus()
  await waitFor(() => expect(searchIndex).toHaveBeenCalledTimes(2))
  await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
})

it('S1-48 reports a real search fetch failure and re-requests on Retry', async () => {
  api.searchIndex.mockRestore()
  const fetch = vi.fn()
    .mockRejectedValueOnce(new TypeError('Failed to fetch'))
    .mockResolvedValue({ ok: true, json: async () => ({ results: [] }) })
  vi.stubGlobal('fetch', fetch)
  render(<Harness initialValue="roof" mock={false} />)
  chooseScope('find')
  expect(await screen.findByRole('alert')).toHaveTextContent('Search could not be completed. Try again.')
  expect(screen.getByRole('alert')).not.toHaveTextContent(/Failed to fetch|\/api\//)
  expect(fetch).toHaveBeenCalledTimes(1)
  expect(fetch.mock.calls[0][0]).toContain('/api/search?q=roof')
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  expect(input()).toHaveFocus()
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2))
  expect(fetch.mock.calls[1][0]).toBe(fetch.mock.calls[0][0])
  await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
  expect(screen.getByText('No results for “roof”')).toBeTruthy()
})

it('S1-48 shows saved-item loading and a retry after a failed versions read', async () => {
  const pending = deferred()
  getDrawingVersions.mockReturnValueOnce(pending.promise)
  render(<Harness drawingId="drawing-a" />)
  chooseScope('act')
  expect(screen.getByRole('status')).toHaveTextContent('Loading saved items…')
  await act(async () => pending.resolve(Promise.reject(new Error('GET /api/drawings -> 500'))))
  expect(await screen.findByRole('alert')).toHaveTextContent('Some saved items could not be loaded.')
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  expect(input()).toHaveFocus()
  await waitFor(() => expect(getDrawingVersions).toHaveBeenCalledTimes(2))
  await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
})

it('S1-49 describes available scopes and shows the selected scope on the chip', () => {
  render(<Harness />)
  fireEvent.click(document.querySelector('.bar-scope'))
  const menu = screen.getByRole('listbox', { name: 'Scope' })
  expect(menu).toHaveTextContent('search drawing objects and projects')
  expect(menu).toHaveTextContent('choose an action or tool')
  expect(menu).not.toHaveTextContent('not wired')
  expect(menu).not.toHaveTextContent('three lanes')
  fireEvent.click(within(menu).getByText('act'))
  expect(document.querySelector('.bar-scope')).toHaveTextContent('act ▾')
  expect(input()).toHaveFocus()
})

it('S1-50 leaves text drag and drop to the textarea', () => {
  render(<Harness />)
  for (const type of ['dragEnter', 'dragOver', 'drop']) {
    const event = createEvent[type](input(), { dataTransfer: { types: ['text/plain'], files: [], getData: () => 'roof' } })
    fireEvent(input(), event)
    expect(event.defaultPrevented).toBe(false)
  }
  expect(screen.queryByRole('alert')).toBeNull()
  expect(document.querySelector('.bar-drop-hint')).toBeNull()
})

it('S1-50 explains unsupported file drops without promising an upload and restores focus on dismiss', () => {
  render(<Harness />)
  fireEvent.dragEnter(bar(), { dataTransfer: { types: ['Files'] } })
  expect(document.querySelector('.bar-drop-hint')).toHaveTextContent('Use the drawing upload control')
  fireEvent.drop(bar(), { dataTransfer: { files: [new File(['drawing'], 'roof.dxf')] } })
  expect(screen.getByRole('alert')).toHaveTextContent('Files cannot be uploaded here.')
  expect(screen.getByRole('alert')).not.toHaveTextContent(/demo|ingest|—/)
  fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
  expect(input()).toHaveFocus()
})

it('S1-52 opens Build without leaving a drawing-sharing notice on later prompts', () => {
  const onOpenAuthor = vi.fn()
  render(<Harness drawingId="drawing-a" onOpenAuthor={onOpenAuthor} />)
  chooseScope('build')
  expect(onOpenAuthor).toHaveBeenCalledTimes(1)
  fireEvent.change(input(), { target: { value: 'count panels' } })
  expect(screen.queryByTestId('build-egress')).toBeNull()
  expect(document.querySelector('.bar-scope')).toHaveTextContent('scope ▾')
})

it('S1-53 keeps scope and add controls displayed despite the short-screen stylesheet rule', () => {
  render(<Harness />)
  // The existing max-height rule sets display:none without !important.
  // These inline declarations keep the real entry points in the layout.
  expect(document.querySelector('.bar-scope').style.display).toBe('inline-flex')
  expect(document.querySelector('.bar-add').style.display).toBe('inline-flex')
})

it('S1-54 includes linked services in the same list as broker services', async () => {
  render(<Harness initialValue="/mcp" mcpDiscoveryEnabled connectedMcpServers={[{ name: 'Drive', host: 'drive.example' }]} />)
  fireEvent.keyDown(input(), { key: 'Enter' })
  const panel = await screen.findByRole('status', { name: 'Mounted MCP servers' })
  expect(panel).toHaveTextContent('Drive')
  expect(panel).toHaveTextContent('drive.example')
  expect(panel).not.toHaveTextContent('No connected services.')
})

it('S1-54 closes the services panel with Escape and restores command-bar focus', async () => {
  render(<Harness initialValue="/mcp" />)
  fireEvent.keyDown(input(), { key: 'Enter' })
  const panel = await screen.findByRole('status', { name: 'Mounted MCP servers' })
  const close = within(panel).getByRole('button', { name: 'Close' })
  close.focus()
  fireEvent.keyDown(close, { key: 'Escape' })
  expect(screen.queryByRole('status', { name: 'Mounted MCP servers' })).toBeNull()
  expect(input()).toHaveFocus()
})
