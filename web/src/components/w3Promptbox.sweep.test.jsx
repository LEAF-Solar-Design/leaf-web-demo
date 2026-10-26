// @vitest-environment jsdom
import { useState } from 'react'
import { act, cleanup, createEvent, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import PromptBox from './PromptBox.jsx'
import { getDrawingVersions, listOperatorSessions, searchIndex } from '../api.js'

vi.mock('../api.js', async (importOriginal) => ({
  ...await importOriginal(),
  getStoredOrgId: () => null,
  getDrawingVersions: vi.fn(),
  listOperatorSessions: vi.fn(),
  searchIndex: vi.fn(),
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
  getDrawingVersions.mockResolvedValue({ versions: [] })
  listOperatorSessions.mockResolvedValue({ sessions: [] })
  searchIndex.mockResolvedValue({ results: [] })
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ servers: [] }) }))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

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
