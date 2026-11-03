import { useContext } from 'react'
import { act, cleanup, fireEvent, render, screen, within, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { parse } from '@babel/parser'
import { createHash, webcrypto } from 'node:crypto'
import { composerDraftAccountScope } from '../lib/composerDraft.js'
import { readActiveScope } from '../auth/activeScope.js'
import { deliverProjectPageProposal } from './projectPageProposalDelivery.js'
import ConversePanel from '../components/ConversePanel.jsx'
import { BrowserArtifactContext, BrowserAssetGround } from './BrowserAssetGround.jsx'
import { loadProjectPage, saveProjectPage } from './projectPageClient.js'

vi.mock('./projectPageClient.js', async importOriginal => ({ ...await importOriginal(), loadProjectPage: vi.fn(), saveProjectPage: vi.fn() }))
import * as api from '../api.js'

vi.mock('./ProjectBoardGround.jsx', () => ({ ProjectBoardGround: ({ panel, active, mock }) => <div data-testid="project-board-ground" data-mock={String(mock)} hidden={!active}>{panel}</div> }))
const viewConverse = vi.hoisted(() => ({ streams: new Map(), postMessage: vi.fn() }))
vi.mock('../converse.js', async importOriginal => ({ ...await importOriginal(),
  openStream: (sessionId, _afterSeq, handlers) => {
    viewConverse.streams.set(sessionId, handlers)
    return { close: () => viewConverse.streams.delete(sessionId) }
  },
  postMessage: viewConverse.postMessage,
  listPendingApprovals: async () => [],
}))
vi.mock('../engineChanges.js', async importOriginal => ({ ...await importOriginal(),
  listEngineChanges: async () => ({ kind: 'ok', cards: [], unread_count: 0 }),
}))
vi.mock('../telemetry.js', async importOriginal => ({ ...await importOriginal(), track: vi.fn() }))
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
  viewConverse.streams.clear(); viewConverse.postMessage.mockReset()
})
afterEach(cleanup)

function authoredViewFixture() {
  const saved = new Map(['leaf.jwt', 'leaf.org_id', 'leaf.actor_binding_id'].map(key => [key, localStorage.getItem(key)]))
  const tenant = '11111111-1111-4111-8111-111111111111'
  const project = '22222222-2222-4222-8222-222222222222'
  localStorage.setItem('leaf.jwt', `header.${btoa(JSON.stringify({ iss: 'https://view.test/', sub: 'auth0|viewer', org_id: tenant }))}.signature`)
  localStorage.setItem('leaf.org_id', tenant)
  localStorage.setItem('leaf.actor_binding_id', '33333333-3333-4333-8333-333333333333')
  const descriptor = { contract: 'leaf.authored-view.v1', route: 'view', kind: 'view', tool: 'fixture-view',
    tenant_id: tenant, session_id: 'view-session', turn_id: 'view-turn', catalog_commit: 'a'.repeat(40),
    effective_catalog_digest: 'b'.repeat(64), catalog_digest: `sha256:${'c'.repeat(64)}`,
    tool_manifest_sha256: `sha256:${'c'.repeat(64)}` }
  const html = '<!doctype html><meta charset="utf-8"><h1>Arbitrary café view</h1><script>document.title="view"</script>\n'
  const source = { ...descriptor, project_id: project, entry: 'tools/fixture-view/nested/custom.htm',
    media_type: 'text/html', html, size_bytes: new TextEncoder().encode(html).length,
    sha256: createHash('sha256').update(html).digest('hex') }
  let live = true
  const request = { projectId: project, accountScope: composerDraftAccountScope(), activeScope: readActiveScope(),
    result: descriptor, key: 'view-session:view-turn:3', provenance: { sessionId: 'view-session', turnId: 'view-turn', seq: 3 },
    isCurrent: () => live }
  const mountedProps = { ...props, projectId: project, accountScope: request.accountScope }
  const response = (body = source) => new Response(JSON.stringify(body), {
    headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' } })
  const fetcher = vi.fn().mockImplementation(async () => response())
  vi.stubGlobal('fetch', fetcher)
  vi.stubGlobal('crypto', webcrypto)
  return { request, source, html, response, fetcher, mountedProps, expire: () => { live = false },
    restore: () => {
      cleanup(); vi.unstubAllGlobals()
      for (const [key, value] of saved) {
        if (value === null) localStorage.removeItem(key)
        else localStorage.setItem(key, value)
      }
    } }
}

it('authored_view fetches exact registered HTML into the existing sandbox', async () => {
  const fixture = authoredViewFixture()
  try {
    const { container } = render(<BrowserAssetGround {...fixture.mountedProps} />)
    act(() => expect(deliverProjectPageProposal(fixture.request)).toBe(true))
    await waitFor(() => expect(container.querySelector('iframe')).toBeTruthy())
    const iframe = container.querySelector('iframe')
    expect(iframe.getAttribute('srcdoc')).toBe(fixture.html)
    expect(iframe.getAttribute('sandbox')).toBe('allow-scripts allow-downloads')
    expect(fixture.fetcher).toHaveBeenCalledTimes(1)
    const [path, options] = fixture.fetcher.mock.calls[0]
    const url = new URL(path, window.location.origin)
    expect(url.origin).toBe(window.location.origin)
    expect(url.pathname).toBe('/api/tools/fixture-view/view-source')
    expect(url.searchParams.get('project_id')).toBe(fixture.mountedProps.projectId)
    expect(url.searchParams.get('session_id')).toBe('view-session')
    expect(url.searchParams.get('tool_manifest_sha256')).toBe(fixture.source.tool_manifest_sha256)
    expect(options).toMatchObject({ credentials: 'same-origin', redirect: 'error', cache: 'no-store',
      headers: { Authorization: expect.stringContaining('Bearer ') } })
    act(() => expect(deliverProjectPageProposal(fixture.request)).toBe(false))
  } finally { fixture.restore() }
})

it.each(['success', 'app-receipt', 'text-only', 'failed', 'foreign-session', 'replayed', 'session-change', 'project-change'])(
  'authored_view mounted conversation intake and source verification (%s)', async mode => {
    const fixture = authoredViewFixture()
    try {
      const sessionId = fixture.source.session_id
      const projectId = fixture.mountedProps.projectId
      const ui = (session = sessionId, project = projectId) => <>
        <ConversePanel sessionId={session} projectContext={{ project_id: project, input_version_id: 'version-one' }}
          userTurns={mode === 'app-receipt' ? [{ turnId: fixture.source.turn_id, text: 'Open my view', status: 'started' }] : []} />
        <BrowserAssetGround {...fixture.mountedProps} projectId={project} />
      </>
      viewConverse.postMessage.mockResolvedValue({ status: 'started', turn_id: fixture.source.turn_id })
      let resolveFetch
      if (mode.endsWith('-change')) fixture.fetcher.mockReturnValue(new Promise(resolve => { resolveFetch = resolve }))
      const { container, rerender } = render(ui())
      if (mode !== 'replayed' && mode !== 'app-receipt') {
        await act(async () => {
          fireEvent.change(screen.getByLabelText('Reply to the assistant'), { target: { value: 'Open my view' } })
          fireEvent.keyDown(screen.getByLabelText('Reply to the assistant'), { key: 'Enter' })
        })
        expect(viewConverse.postMessage).toHaveBeenCalledWith(sessionId,
          expect.objectContaining({ projectContext: { project_id: projectId, input_version_id: 'version-one' } }))
      }
      const handlers = viewConverse.streams.get(sessionId)
      const result = mode === 'foreign-session' ? { ...fixture.request.result, session_id: 'foreign-session' }
        : mode === 'text-only' ? JSON.stringify(fixture.request.result) : fixture.request.result
      act(() => {
        handlers.onEvent({ type: 'turn_started', session_id: sessionId, turn_id: fixture.source.turn_id, seq: 1, data: {} })
        handlers.onEvent({ type: 'tool_result', session_id: sessionId, turn_id: fixture.source.turn_id, seq: 3,
          data: { tool: 'run_capability', ok: mode !== 'failed', result } })
      })
      if (mode === 'success' || mode === 'app-receipt') {
        await waitFor(() => expect(container.querySelector('iframe')?.getAttribute('srcdoc')).toBe(fixture.html))
        expect(container.querySelector('iframe').getAttribute('sandbox')).toBe('allow-scripts allow-downloads')
        expect(fixture.fetcher).toHaveBeenCalledTimes(1)
      } else if (mode.endsWith('-change')) {
        await waitFor(() => expect(fixture.fetcher).toHaveBeenCalledTimes(1))
        rerender(mode === 'session-change' ? ui('new-session') : ui(sessionId, 'new-project'))
        await act(async () => { resolveFetch(fixture.response()) })
        expect(container.querySelector('iframe')).toBeNull()
      } else {
        expect(fixture.fetcher).not.toHaveBeenCalled()
        expect(container.querySelector('iframe')).toBeNull()
      }
    } finally { fixture.restore() }
  })

it.each(['hash', 'project', 'commit', 'session', 'tenant', 'kind', 'size', 'oversize', 'surrogate', 'redirect', 'request-project'])(
  'authored_view refuses unverified source (%s)', async failure => {
    const fixture = authoredViewFixture()
    try {
      const body = { ...fixture.source }
      if (failure === 'hash') body.sha256 = '0'.repeat(64)
      if (failure === 'project') body.project_id = 'foreign-project'
      if (failure === 'commit') body.catalog_commit = '0'.repeat(40)
      if (failure === 'session') body.session_id = 'foreign-session'
      if (failure === 'tenant') body.tenant_id = 'foreign-tenant'
      if (failure === 'kind') body.kind = 'script'
      if (failure === 'size') body.size_bytes++
      if (failure === 'oversize') body.size_bytes = 1024 * 1024 + 1
      if (failure === 'surrogate') {
        body.html = '\ud800'; body.size_bytes = 3
        body.sha256 = createHash('sha256').update(body.html).digest('hex')
      }
      if (failure === 'request-project') fixture.request.projectId = 'foreign-project'
      let consumed = false
      fixture.fetcher.mockImplementation(async () => {
        const response = fixture.response(body)
        if (failure === 'redirect') Object.defineProperty(response, 'redirected', { value: true })
        consumed = true
        return response
      })
      const { container } = render(<BrowserAssetGround {...fixture.mountedProps} />)
      await act(async () => { deliverProjectPageProposal(fixture.request) })
      if (failure !== 'request-project') await waitFor(() => expect(consumed).toBe(true))
      // Flush transport read + crypto continuations, including mismatched hashes.
      await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)) })
      expect(container.querySelector('iframe')).toBeNull()
      if (failure === 'request-project') expect(fixture.fetcher).not.toHaveBeenCalled()
    } finally { fixture.restore() }
  })

it.each(['project', 'logout', 'session', 'account', 'active-scope'])(
  'authored_view drops an in-flight source after %s lifetime changes', async change => {
    const fixture = authoredViewFixture()
    try {
      let resolveFetch
      fixture.fetcher.mockReturnValue(new Promise(resolve => { resolveFetch = resolve }))
      const { container, rerender } = render(<BrowserAssetGround {...fixture.mountedProps} />)
      act(() => deliverProjectPageProposal(fixture.request))
      await waitFor(() => expect(fixture.fetcher).toHaveBeenCalledTimes(1))
      act(() => {
        if (change === 'project') rerender(<BrowserAssetGround {...fixture.mountedProps} projectId="another-project" />)
        if (change === 'account') rerender(<BrowserAssetGround {...fixture.mountedProps} accountScope="account:another" />)
        if (change === 'session') fixture.expire()
        if (change === 'logout') {
          localStorage.removeItem('leaf.jwt')
          window.dispatchEvent(new StorageEvent('storage', { key: 'leaf.jwt' }))
        }
        if (change === 'active-scope') {
          localStorage.setItem('leaf.org_id', 'another-org')
          window.dispatchEvent(new StorageEvent('storage', { key: 'leaf.org_id' }))
        }
      })
      await act(async () => { resolveFetch(fixture.response()); await Promise.resolve() })
      expect(container.querySelector('iframe')).toBeNull()
      expect(fixture.fetcher).toHaveBeenCalledTimes(1)
    } finally { fixture.restore() }
  })

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
