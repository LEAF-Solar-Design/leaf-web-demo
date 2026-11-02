import { useContext } from 'react'
import { act, cleanup, fireEvent, render, screen, within, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { parse } from '@babel/parser'
import { BrowserArtifactContext, BrowserAssetGround } from './BrowserAssetGround.jsx'
import { loadProjectPage, saveProjectPage } from './projectPageClient.js'

vi.mock('./projectPageClient.js', async importOriginal => ({ ...await importOriginal(), loadProjectPage: vi.fn(), saveProjectPage: vi.fn() }))
import * as api from '../api.js'

vi.mock('./ProjectBoardGround.jsx', () => ({ ProjectBoardGround: ({ panel, active, mock }) => <div data-testid="project-board-ground" data-mock={String(mock)} hidden={!active}>{panel}</div> }))
const auth = vi.hoisted(() => ({ configured: true, signedIn: true, listeners: new Set(), login: vi.fn() }))
vi.mock('../auth.js', async importOriginal => ({
  ...await importOriginal(),
  get authConfigured() { return auth.configured },
  isSignedIn: () => auth.signedIn,
  login: auth.login,
  subscribeTokenStored: listener => {
    auth.listeners.add(listener)
    return () => auth.listeners.delete(listener)
  },
}))
beforeEach(() => {
  auth.configured = true
  auth.signedIn = true
  auth.listeners.clear()
  auth.login.mockReset()
  loadProjectPage.mockReset(); saveProjectPage.mockReset()
})
afterEach(cleanup)

it('switches personal editing styles around one dirty owner without loading or saving', async () => {
  const key = 'leaf.editingStyle.v1:' + encodeURIComponent(props.accountScope)
  localStorage.setItem(key, 'environment')
  const page = { label: 'Saved page', headline: 'Before editing', description: '', status: '', theme: 'cream', alignment: 'center' }
  loadProjectPage.mockResolvedValueOnce({ revision: 4, digest: 'a'.repeat(64), document: page })
  try {
    const { container } = render(<BrowserAssetGround {...props} />)
    const canvas = within(container.querySelector('.browser-asset-viewport'))
    fireEvent.click(canvas.getByRole('button', { name: 'Open page' }))
    await canvas.findByText('Before editing')
    fireEvent.click(canvas.getByRole('button', { name: 'Edit', exact: true }))
    fireEvent.change(canvas.getByLabelText('headline'), { target: { value: 'Unsaved in environment' } })
    fireEvent.click(canvas.getByRole('button', { name: 'Settings', exact: true }))
    fireEvent.change(canvas.getByLabelText('Editing style'), { target: { value: 'sidebar' } })
    expect(localStorage.getItem(key)).toBe('sidebar')
    await waitFor(() => expect(container.querySelector('.project-page-editor input[value="Unsaved in environment"]')).toBeTruthy())
    const owner = container.querySelector('.project-page-editor')
    fireEvent.change(canvas.getByLabelText('Editing style'), { target: { value: 'environment' } })
    expect(container.querySelector('.project-page-editor')).toBe(owner)
    expect(canvas.getByLabelText('headline').value).toBe('Unsaved in environment')
    fireEvent.click(canvas.getByRole('button', { name: 'Undo', exact: true }))
    expect(canvas.getByLabelText('headline').value).toBe('Before editing')
    fireEvent.click(canvas.getByRole('button', { name: 'Redo', exact: true }))
    expect(canvas.getByLabelText('headline').value).toBe('Unsaved in environment')
    expect(loadProjectPage).toHaveBeenCalledTimes(1)
    expect(saveProjectPage).not.toHaveBeenCalled()
  } finally { localStorage.removeItem(key) }
})


let handoff
function Output() {
  handoff = useContext(BrowserArtifactContext)
  return <label>Campaign input<input defaultValue="Saved request" /></label>
}
const props = { browserActive: true, signedIn: true, accountScope: 'account:one', projectId: 'project-one',
  workspaceProject: { kind: 'project', label: 'Pastry shop' }, panel: <Output /> }

// Exercise App's actual fenced loader without mounting unrelated App services.
function sampleLoader({ search = '?demo=1', source = 'rooftop_demo', profile = 'cad', current = true } = {}) {
  const app = readFileSync(`${process.cwd()}/src/App.jsx`, 'utf8').replace(/\r\n/g, '\n')
  const tree = parse(app, { sourceType: 'module', plugins: ['jsx'] })
  let loader, pending
  const walk = node => {
    if (!node || typeof node.type !== 'string') return
    if (node.type === 'VariableDeclarator' && node.id.name === 'sampleEntryPending') pending = node.init.arguments[0]
    if (node.type === 'CallExpression' && node.callee.name === 'useEffect'
      && app.slice(node.arguments[0]?.start, node.arguments[0]?.end).includes('getSession(mock, DRAWING_SOURCE)')) loader = node.arguments[0]
    for (const value of Object.values(node)) {
      if (Array.isArray(value)) value.forEach(walk)
      else if (value && typeof value.type === 'string') walk(value)
    }
  }
  walk(tree)
  expect(loader).toBeTruthy()
  expect(pending).toBeTruthy()
  const bindings = {
    REQUESTED_DRAWING_ID: 'demo', DRAWING_SOURCE: source,
    requestedDrawingIdRef: { current: 'demo' }, isScopeCurrent: () => current,
    sessionProof: { current: () => true, activate: vi.fn() }, knownGuest: false,
    resetDrawing: vi.fn(), setLoadErr: vi.fn(), setDrawingLoad: vi.fn(),
    hasDrawingSelection: () => true, resetCatalogTransient: vi.fn(), clearToast: vi.fn(),
    setDrawer: vi.fn(), clearAgentSession: vi.fn(), mockVersions: { reset: vi.fn(), seedBase: vi.fn() },
    seatIntake: vi.fn(), mock: true, isEditFixture: false,
    getSession: vi.fn(), getDrawingVersions: vi.fn(),
    session: { controller: { getSnapshot: () => ({ status: 'active' }) } },
    classifyDrawingLoadFailure: () => 'network', humanizeError: error => error.message,
    sampleEntryPending: { current: Function('window', `return (${app.slice(pending.start, pending.end)})`)({ location: { search } }) },
    setSampleEntry: vi.fn(), committedSurfaceRef: { current: profile },
  }
  const load = Function('bindings', `const { ${Object.keys(bindings).join(', ')} } = bindings; return (${app.slice(loader.start, loader.end)})()`)
  return { ...bindings, load: () => load(bindings) }
}

it('sends one profile-aware request only after App successfully seats the explicit sample', async () => {
  const loader = sampleLoader()
  const drawing = { dwg: '/samples/rooftop_demo.dwg' }
  loader.getSession.mockResolvedValue({ intake: drawing })
  await act(async () => { loader.load() })
  expect(loader.seatIntake).toHaveBeenCalledWith(drawing, { drawingId: 'demo' })
  expect(loader.setSampleEntry).toHaveBeenCalledExactlyOnceWith({ request: 1, profile: 'cad' })
  const request = loader.setSampleEntry.mock.calls[0][0]
  const { container, rerender } = render(<BrowserAssetGround {...props} profile="cad" browserActive={false} sampleEntry={request} drawing={drawing} />)
  expect(container.querySelector('.workspace-environment').dataset.entered).toBe('true')
  expect(container.querySelector('.environment-chooser').hasAttribute('inert')).toBe(true)
  expect(screen.getByRole('button', { name: 'Project' }).getAttribute('aria-expanded')).toBe('false')
  const field = screen.getByLabelText('Campaign input')
  await act(async () => { loader.load() })
  expect(loader.setSampleEntry).toHaveBeenCalledOnce()
  rerender(<BrowserAssetGround {...props} profile="solar" browserActive={false} sampleEntry={request} drawing={drawing} />)
  expect(container.querySelector('.workspace-environment').dataset.entered).toBe('false')
  expect(screen.getByLabelText('Campaign input')).toBe(field)
  rerender(<BrowserAssetGround {...props} profile="cad" browserActive={false} sampleEntry={request} drawing={drawing} />)
  expect(container.querySelector('.workspace-environment').dataset.entered).toBe('true')
})

it('keeps the chooser, error and retry available after a failed sample load and enters on successful retry', async () => {
  const loader = sampleLoader()
  loader.getSession.mockRejectedValue(new Error('Sample could not load'))
  await act(async () => { loader.load() })
  expect(loader.setSampleEntry).not.toHaveBeenCalled()
  expect(loader.sampleEntryPending.current).toBe(true)
  expect(loader.setDrawingLoad).toHaveBeenLastCalledWith({ drawingId: 'demo', state: 'failed', failure: 'network' })
  const error = loader.setLoadErr.mock.calls.at(-1)[0]
  const view = sampleEntry => <>
    <BrowserAssetGround {...props} profile="cad" browserActive={false} sampleEntry={sampleEntry} />
    {!sampleEntry && <div role="alert">{error}<button onClick={loader.load}>Retry</button></div>}
  </>
  const { container, rerender } = render(view())
  expect(screen.getByRole('alert').textContent).toContain('Sample could not load')
  expect(screen.getByRole('button', { name: 'Open project or material' })).toBeTruthy()
  expect(container.querySelector('.environment-chooser').hasAttribute('inert')).toBe(false)
  loader.getSession.mockResolvedValue({ intake: { dwg: '/samples/rooftop_demo.dwg' } })
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Retry' })) })
  expect(loader.setSampleEntry).toHaveBeenCalledOnce()
  rerender(view(loader.setSampleEntry.mock.calls[0][0]))
  expect(container.querySelector('.workspace-environment').dataset.entered).toBe('true')
})

it.each([
  { search: '' }, { search: '?demo=tour' }, { source: 'uploaded' }, { current: false },
])('does not request sample entry for an ordinary mount, other drawing or stale load: %j', async options => {
  const loader = sampleLoader(options)
  loader.getSession.mockResolvedValue({ intake: { dwg: '/samples/rooftop_demo.dwg' } })
  await act(async () => { loader.load() })
  expect(loader.setSampleEntry).not.toHaveBeenCalled()
})

it('does not request sample entry for an absent intake', async () => {
  const loader = sampleLoader()
  loader.getSession.mockResolvedValue({ intake: null })
  await act(async () => { loader.load() })
  expect(loader.setSampleEntry).not.toHaveBeenCalled()
})

it('forwards the sample request through the mounted App, surface and browser ground owners', () => {
  const source = file => readFileSync(`${process.cwd()}/src/${file}`, 'utf8').replace(/\r\n/g, '\n')
  expect(source('App.jsx')).toMatch(/<SurfaceGrounds\b[\s\S]*?sampleEntry=\{sampleEntry\}/)
  expect(source('site/SurfaceGrounds.jsx')).toMatch(/profile: surface, profiles: environmentProfiles, onSelect: onSelectSurface, onDescribe, onEnterWorkspace, sampleEntry/)
  expect(source('site/BrowserAssetGround.jsx')).toMatch(/<WorkspaceEnvironment\b[\s\S]*?sampleEntry=\{sampleEntry\}/)
})

it('wires the signed-out chooser sign-in click to live auth login', () => {
  auth.signedIn = false
  const { container } = render(<BrowserAssetGround {...props} signedIn={false} />)
  fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
  expect(auth.login).toHaveBeenCalledExactlyOnceWith()
  expect(container.querySelector('.workspace-environment').dataset.entered).toBe('false')
})

it.each([
  { configured: true, signedIn: true },
  { configured: false, signedIn: false },
])('omits chooser sign-in for auth state %j', state => {
  Object.assign(auth, state)
  render(<BrowserAssetGround {...props} signedIn={state.signedIn} />)
  expect(screen.queryByText('Sign in')).toBeNull()
})

it('drops chooser sign-in on token storage even when the account scope stays the same', () => {
  auth.signedIn = false
  render(<BrowserAssetGround {...props} signedIn={false} accountScope="guest" />)
  expect(screen.getByRole('button', { name: 'Sign in' })).toBeTruthy()
  act(() => {
    auth.signedIn = true
    for (const listener of auth.listeners) listener('stored-token')
  })
  expect(screen.queryByText('Sign in')).toBeNull()
})

it('starts empty with closed overlays and retains the existing panel input across toggles', () => {
  const { container } = render(<BrowserAssetGround {...props} />)
  expect(screen.getByText('No page open')).toBeTruthy()
  expect(screen.getByText('Open page loads your saved page or portfolio.')).toBeTruthy()
  expect(screen.getByText('Pastry shop')).toBeTruthy()
  expect(container.querySelector('iframe')).toBeNull()
  const toggle = screen.getByRole('button', { name: 'Project' })
  expect(toggle.getAttribute('aria-expanded')).toBe('false')
  expect(document.getElementById(toggle.getAttribute('aria-controls')).hasAttribute('inert')).toBe(true)
  fireEvent.click(toggle)
  const field = screen.getByLabelText('Campaign input')
  fireEvent.change(field, { target: { value: 'Keep this request' } })
  expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Close Project' }))
  fireEvent.keyDown(window, { key: 'Escape' })
  expect(toggle.getAttribute('aria-expanded')).toBe('false')
  expect(document.activeElement).toBe(toggle)
  fireEvent.click(toggle)
  expect(screen.getByLabelText('Campaign input')).toBe(field)
  expect(field.value).toBe('Keep this request')
})

it('hands HTML to a full viewport sandbox and retains it across toggles and profiles', () => {
  const { rerender } = render(<BrowserAssetGround {...props} />)
  fireEvent.click(screen.getByRole('button', { name: 'Project' }))
  act(() => expect(handoff({ html: '<h1>Verified output</h1>', name: 'pastry.html' })).toBe(true))
  const frame = screen.getByTitle('Verified preview: pastry.html')
  expect(frame.getAttribute('sandbox')).toBe('allow-scripts allow-downloads')
  expect(frame.getAttribute('srcdoc')).toBe('<h1>Verified output</h1>')
  expect(screen.getByRole('button', { name: 'Project' }).getAttribute('aria-expanded')).toBe('false')
  fireEvent.click(screen.getByRole('button', { name: 'Studio' }))
  expect(screen.getByTitle('Verified preview: pastry.html')).toBe(frame)
  fireEvent.keyDown(window, { key: 'Escape' })
  expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Studio' }))
  rerender(<BrowserAssetGround {...props} browserActive={false} />)
  rerender(<BrowserAssetGround {...props} />)
  expect(screen.getByTitle('Verified preview: pastry.html')).toBe(frame)
})

it('keeps one campaign input and hands off to Browser while another profile is selected', () => {
  const onSelect = vi.fn()
  const { container, rerender } = render(<BrowserAssetGround {...props} profile="cad" browserActive={false} onSelect={onSelect} />)
  fireEvent.click(screen.getByRole('button', { name: 'Project' }))
  const field = screen.getByLabelText('Campaign input')
  fireEvent.change(field, { target: { value: 'Retain across profiles' } })
  const receiver = handoff
  fireEvent.click(screen.getByRole('button', { name: 'Studio' }))
  rerender(<BrowserAssetGround {...props} profile="ios" browserActive={false} onSelect={onSelect} />)
  act(() => expect(receiver({ html: '<p>Exact verified bytes</p>', name: 'shop.html' })).toBe(true))
  expect(onSelect).toHaveBeenCalledWith('browser')
  rerender(<BrowserAssetGround {...props} profile="browser" onSelect={onSelect} />)
  expect(container.querySelector('.workspace-environment').dataset.entered).toBe('true')
  expect(screen.getByTitle('Verified preview: shop.html').getAttribute('srcdoc')).toBe('<p>Exact verified bytes</p>')
  fireEvent.click(screen.getByRole('button', { name: 'Project' }))
  expect(screen.getByLabelText('Campaign input')).toBe(field)
  expect(field.value).toBe('Retain across profiles')
  expect(screen.getAllByLabelText('Campaign input')).toHaveLength(1)
})

it('does not consume Escape when no environment panel is open', () => {
  render(<BrowserAssetGround {...props} />)
  const event = new KeyboardEvent('keydown', { key: 'Escape', cancelable: true, bubbles: true })
  act(() => window.dispatchEvent(event))
  expect(event.defaultPrevented).toBe(false)
})

it('resets entry on an auth notification even before any preview bytes exist', () => {
  const { container } = render(<BrowserAssetGround {...props} />)
  fireEvent.click(screen.getByRole('button', { name: 'Project' }))
  expect(container.querySelector('.workspace-environment').dataset.entered).toBe('true')
  act(() => window.dispatchEvent(new StorageEvent('storage', { key: 'leaf.jwt' })))
  expect(container.querySelector('.workspace-environment').dataset.entered).toBe('false')
  expect(screen.getByRole('button', { name: 'Project' }).getAttribute('aria-expanded')).toBe('false')
})

it.each([
  { projectId: 'project-two' },
  { accountScope: 'account:two' },
  { signedIn: false },
])('clears bytes and rejects an old handoff on scope change: %j', changed => {
  const { container, rerender } = render(<BrowserAssetGround {...props} />)
  const oldHandoff = handoff
  act(() => handoff({ html: '<p>Private</p>', name: 'private.html' }))
  rerender(<BrowserAssetGround {...props} {...changed} />)
  expect(container.querySelector('iframe')).toBeNull()
  expect(container.querySelector('.workspace-environment').dataset.entered).toBe('false')
  act(() => expect(oldHandoff({ html: '<p>Stale</p>', name: 'stale.html' })).toBe(false))
  expect(container.querySelector('iframe')).toBeNull()
  rerender(<BrowserAssetGround {...props} />)
  act(() => expect(oldHandoff({ html: '<p>Still stale</p>', name: 'stale.html' })).toBe(false))
  expect(container.querySelector('iframe')).toBeNull()
})

it('fences auth changes before updated scope props arrive and rejects an unmounted receiver', () => {
  const { container, unmount } = render(<BrowserAssetGround {...props} />)
  const oldHandoff = handoff
  act(() => handoff({ html: '<p>Private</p>', name: 'private.html' }))
  // A removed token is a different principal even before App updates its props.
  act(() => window.dispatchEvent(new StorageEvent('storage', { key: 'leaf.jwt' })))
  expect(container.querySelector('iframe')).toBeNull()
  act(() => expect(oldHandoff({ html: '<p>Stale</p>', name: 'stale.html' })).toBe(false))
  const receiver = handoff
  unmount()
  expect(receiver({ html: '<p>Late</p>', name: 'late.html' })).toBe(false)
})

it('keeps phone panels inside the viewport with an internal scroller and sticky close controls', () => {
  const css = readFileSync(`${process.cwd()}/src/site/browserAssetGround.css`, 'utf8')
  expect(css).toContain('calc(100vw - 24px)')
  expect(css).toContain('overscroll-behavior: contain')
  expect(css).toContain('position: sticky; top: 0')
  expect(css).toContain('.browser-asset-ground { position: fixed; inset: 0;')
})

it.each([
  { signedIn: true, canApplyLayout: true, mock: false, eligible: true },
  { signedIn: false, canApplyLayout: true, mock: false, eligible: false },
  { signedIn: true, canApplyLayout: false, mock: false, eligible: false },
  { signedIn: true, canApplyLayout: true, mock: true, eligible: false },
  { signedIn: true, mock: false, eligible: false },
])('forwards layout authority while preserving ProjectBoard mock mode: %j', ({ eligible, ...state }) => {
  vi.spyOn(api, 'getSurfaceConfig').mockResolvedValue({ surfaces: {} })
  render(<BrowserAssetGround {...props} profile="cad" {...state} />)
  fireEvent.click(screen.getByRole('button', { name: 'Layout', exact: true }))
  expect(screen.getByRole('button', { name: 'Apply layout' }).disabled).toBe(!eligible)
  expect(screen.getByTestId('project-board-ground').dataset.mock).toBe(String(state.mock))
})
