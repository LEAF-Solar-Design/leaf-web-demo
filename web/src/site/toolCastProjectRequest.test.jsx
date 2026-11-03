// @vitest-environment jsdom
// Studio18 O01: exercise the actual stage admission and shared conversation.
// Synthetic transport responses are source proof only, never live acceptance.
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { notificationBus } from '../lib/notifications.js'

const fixture = vi.hoisted(() => {
  const noop = () => {}
  return {
    ship: { readiness: {launchable: false}, busy: false }, signedIn: true, route: { lane: 'run', tool: 'count_panels', confidence: 0.99 },
    session: { status: 'active', actions: { requireAuth: noop, checking: noop, activate: noop } },
    workspace: { openProjectId: 'p1', canonicalVersionId: 'r1', projects: [], workspace: null },
    controllers: {
      converse: { turns: [], activeRequests: [], startTurn: noop, clear: noop, resetCached: noop },
      bindConverseProject: noop,
      drawing: { actions: {}, shown: null, drawingState: null, visibleLayers: {} },
    },
    catalog: { state: { tools: [], catalog: { families: [] }, openFamilies: {} }, actions: {}, controller: {} },
    jobs: { jobs: [], reset: noop },
    platform: { isEntitled: () => true, quota: {visible: null}, actions: {} },
    checkout: { actions: {} },
    upload: { actions: {cancel: noop} },
    registry: { servers: [] },
  }
})

vi.mock('../api.js', async (importOriginal) => ({ ...(await importOriginal()),
  getSession: vi.fn(async () => ({tenant: 'tenant'})), getDrawingVersions: vi.fn(),
  getTools: vi.fn(async () => []), getCapabilities: vi.fn(async () => ({families: [], source: 'test'})),
  nlPrompt: vi.fn(async (_mock, text, _tools, options) => {
    const {guardedText, SecretRefusedError} = await import('../lib/secretGuardTransport.js')
    const guarded = guardedText(text, options)
    if (!guarded.ok) throw new SecretRefusedError(guarded.refusal)
    return await fixture.route
  }),
}))
vi.mock('../auth.js', async (original) => ({...(await original()), isSignedIn: () => fixture.signedIn}))
vi.mock('../converse.js', async (original) => ({...(await original()),
  ensureSession: vi.fn(async (_drawing, project) => ({session_id: `session-${project}`})),
  postMessage: vi.fn(async (_session, payload) => {
    const {guardedText, SecretRefusedError} = await import('../lib/secretGuardTransport.js')
    const guarded = guardedText(payload.text, payload)
    if (!guarded.ok) throw new SecretRefusedError(guarded.refusal)
    return {turn_id: 'turn-1', status: 'running'}
  }),
  openStream: vi.fn(() => ({close: vi.fn()})),
  listPendingApprovals: vi.fn(async () => []),
}))
vi.mock('../engineChanges.js', () => ({listEngineChanges: vi.fn(async () => [])}))
vi.mock('../ios/useIosShipController.js', () => ({ useIosShipController: vi.fn(() => fixture.ship) }))
vi.mock('../controllers/WorkspaceControllerProvider.jsx', async () => {
  const {default: useConverse} = await import('../controllers/useConverseSessionController.js')
  return {useWorkspaceControllers: () => {
    const converse = useConverse({drawingId: null})
    return {...fixture.controllers, converse, bindConverseProject: converse.setProjectContext}
  }}
})
vi.mock('../controllers/session/useSessionController.js', () => ({ default: () => fixture.session }))
vi.mock('../controllers/workspace/useWorkspaceController.js', () => ({ default: () => fixture.workspace }))
vi.mock('../controllers/workspace/useSessionOrgAdoption.js', () => ({ default: () => {} }))
vi.mock('../drawing/DrawingIdentityProvider.jsx', () => ({ useDrawingScopeReset: () => {}, useDrawingIdentityOptional: () => null }))
vi.mock('../controllers/useJobController.js', () => ({ default: () => fixture.jobs }))
vi.mock('../controllers/useBuildQueue.js', () => ({ default: () => ({ builds: [] }) }))
vi.mock('../controllers/useAuthorStageController.js', () => ({ default: () => ({}) }))
vi.mock('../controllers/platform/usePlatformTrustController.js', () => ({ default: () => fixture.platform }))
vi.mock('../controllers/checkout/useCheckoutController.js', () => ({ default: () => fixture.checkout }))
vi.mock('../controllers/upload/useDrawingUploadController.js', () => ({ default: () => fixture.upload }))
vi.mock('../useTenantMcpRegistry.js', () => ({ default: () => fixture.registry }))
vi.mock('../ios/useIosSurface.js', () => ({ default: () => ({}) }))
vi.mock('./useSurfaceContract.js', async () => {
  const {surfaceContract} = await import('./productSurfaces.js')
  return {useSurfaceContract: id => surfaceContract(id)}
})
vi.mock('../components/ConversationList.jsx', () => ({ resumeHref: () => null }))
vi.mock('../components/LiveRegion.jsx', async () => {
  const {forwardRef} = await import('react')
  return {default: forwardRef(({children, role, label, className}, ref) => <div ref={ref} role={role} aria-label={label} className={className}>{children}</div>), HIDE_WITH_CLASS: 'hidden'}
})
vi.mock('../components/AuthorPanel.jsx', () => ({ default: () => null }))
vi.mock('../components/CapabilityCatalog.jsx', () => ({ default: () => null }))
vi.mock('../components/ClaudeAccountPanel.jsx', () => ({ default: () => null }))
vi.mock('../components/CheckoutControls.jsx', () => ({ default: () => null }))
vi.mock('../components/DetailsDrawer.jsx', () => ({ default: () => null }))
vi.mock('../components/DrawingUploadControl.jsx', () => ({ default: () => null }))
vi.mock('../components/Legend.jsx', () => ({ default: () => null }))
vi.mock('../components/ProjectSwitcher.jsx', () => ({ default: () => null }))
vi.mock('../components/SelectionReadout.jsx', () => ({ default: () => null }))
vi.mock('../components/RoutePanel.jsx', () => ({ default: () => null }))
vi.mock('../components/ResultPanel.jsx', () => ({ default: () => null }))
vi.mock('../components/QuotaCard.jsx', () => ({ default: () => null }))
vi.mock('../components/DegradedBanner.jsx', () => ({ default: () => null }))
vi.mock('../components/SessionGate.jsx', () => ({ default: () => null }))
vi.mock('../components/OpsDrawer.jsx', () => ({ default: () => null }))
vi.mock('../components/WorkspaceSummary.jsx', () => ({ default: () => null }))
vi.mock('../components/WorkspaceBootstrapGate.jsx', () => ({ default: () => null }))
vi.mock('../projects/ProjectLifecyclePanel.jsx', () => ({ default: () => null }))
vi.mock('../cadedit/CadEditSurface.jsx', () => ({ default: () => null }))
vi.mock('../ios/IosSurface.jsx', () => ({ default: () => null }))
vi.mock('../components/VersionList.jsx', () => ({ default: () => null, VersionPreviewStrip: () => null }))
vi.mock('../demo/DemoTour.jsx', () => ({ default: () => null }))
vi.mock('../demo/DemoConversationPanel.jsx', () => ({ default: () => null, demoReplyFor: () => '' }))
vi.mock('../demo/FirstRunCoach.jsx', () => ({ default: () => null }))

// Keep actual ToolCast, PromptBox, catalog, conversation hook and transcript.
// Frame chrome and unrelated controllers are presentation fixtures only.
vi.mock('./SurfaceFrame.jsx', async () => {
  const {createContext, useContext} = await import('react')
  const C = createContext(null)
  function Frame({children, commandBar}) {return <C.Provider value={commandBar}>{children}</C.Provider>}
  for (const key of ['Tabs','Frame','Toast','Conversations','JobRail','Inbox','Entitlement','Integrations','ContextMenu']) Frame[key] = () => null
  Frame.CommandBar = () => useContext(C)
  return {default: Frame}
})

afterEach(() => {cleanup(); notificationBus.clearVisible(); vi.unstubAllGlobals(); vi.clearAllMocks(); localStorage.clear()})

const PROJECT = '11111111-1111-4111-8111-111111111111'
const OTHER = '22222222-2222-4222-8222-222222222222'
const ORG = '33333333-3333-4333-8333-333333333333'
const selected = id => ({orgId: ORG, openProjectId: id, canonicalVersionId: null, projects: [], workspaceLoading: false,
  workspaceError: null, workspace: {project: {project_id: id, org_id: ORG}, drawing_versions: []}})
async function setup(surface = 'ops') {
  const {TextEncoder, TextDecoder} = await import('node:util')
  vi.stubGlobal('TextEncoder', TextEncoder); vi.stubGlobal('TextDecoder', TextDecoder)
  fixture.signedIn = true
  fixture.route = {lane: 'run', tool: 'count_panels', confidence: 0.99}
  fixture.session = {status: 'active', actions: {requireAuth: vi.fn(), checking: vi.fn(), activate: vi.fn()}}
  fixture.workspace = selected(PROJECT)
  fixture.platform = {isEntitled: () => true, quota: {visible: null}, actions: {}}
  fixture.controllers.drawing = {actions: {seatIntake: vi.fn()}, shown: null, drawingState: null, visibleLayers: {}}
  window.history.replaceState({}, '', `/try?surface=${surface}`)
  const {default: ToolCast} = await import('./ToolCast.jsx')
  const api = await import('../api.js')
  const converse = await import('../converse.js')
  const view = render(<ToolCast active />)
  await waitFor(() => expect(api.getSession).toHaveBeenCalled())
  const input = document.querySelector('.tc-bar textarea')
  expect(input).toBeTruthy()
  const type = text => fireEvent.change(input, {target: {value: text}})
  const run = () => screen.getByRole('button', {name: 'Run', exact: true})
  return {...view, api, converse, type, run, redraw: () => view.rerender(<ToolCast active />)}
}

describe('O01 actual mounted stage request admission (transport fixtures, no provider)', () => {
  it('submits once through the canonical project conversation and renders its reply without a drawing', async () => {
    const s = await setup()
    s.type('Summarize these six synthetic records by owner.')
    await waitFor(() => expect(s.run().disabled).toBe(false))
    const button = s.run(); fireEvent.click(button); fireEvent.click(button)
    await waitFor(() => expect(s.converse.postMessage).toHaveBeenCalledTimes(1))
    expect(s.converse.ensureSession).toHaveBeenCalledWith(null, PROJECT)
    expect(s.converse.postMessage.mock.calls[0][0]).toBe(`session-${PROJECT}`)
    expect(s.converse.postMessage.mock.calls[0][1]).toMatchObject({text: 'Summarize these six synthetic records by owner.', queue: true, request_id: expect.any(String)})
    await waitFor(() => expect(s.converse.openStream).toHaveBeenCalled())
    const [session, , handlers] = s.converse.openStream.mock.calls.at(-1)
    expect(session).toBe(`session-${PROJECT}`)
    await act(async () => {
      handlers.onEvent({type: 'text_delta', turn_id: 'turn-1', session_id: session, data: {text: 'Six records grouped by owner.'}})
      handlers.onEvent({type: 'turn_complete', turn_id: 'turn-1', session_id: session, data: {}})
    })
    expect(await screen.findByText('Six records grouped by owner.')).toBeTruthy()
  })

  it.each(['session','auth','entitlement','project','org','loading','mismatch'])('refuses unknown or missing %s before dispatch', async missing => {
    const s = await setup()
    if (missing === 'session') fixture.session = {...fixture.session, status: 'required'}
    if (missing === 'auth') fixture.signedIn = false
    if (missing === 'entitlement') fixture.platform = {...fixture.platform, isEntitled: () => false}
    if (missing === 'project') fixture.workspace = {...fixture.workspace, openProjectId: null}
    if (missing === 'org') fixture.workspace = {...fixture.workspace, orgId: null}
    if (missing === 'loading') fixture.workspace = {...fixture.workspace, workspaceLoading: true}
    if (missing === 'mismatch') fixture.workspace = {...fixture.workspace, workspace: selected(OTHER).workspace}
    s.redraw(); s.type('Summarize records')
    expect(s.run().disabled).toBe(true)
    fireEvent.click(s.run())
    expect(s.api.nlPrompt).not.toHaveBeenCalled()
    expect(s.converse.postMessage).not.toHaveBeenCalled()
  })

  it.each(['cad','solar'])('%s still requires a drawing', async surface => {
    const s = await setup(surface); s.type('Count panels')
    expect(s.run().disabled).toBe(true)
    expect(s.run().title).toContain('Upload a DWG or DXF')
    expect(s.api.nlPrompt).not.toHaveBeenCalled()
  })

  it('keeps slash drawing prerequisites in Ops', async () => {
    const s = await setup(); s.type('/count_panels')
    expect(s.run().disabled).toBe(true)
    expect(s.api.nlPrompt).not.toHaveBeenCalled()
  })

  it('renders the existing transport secret refusal without opening a conversation', async () => {
    const s = await setup(); s.type('token: abcdefghijklmnopqrstuvwxyz0123456789')
    fireEvent.click(s.run())
    expect(await screen.findByTestId('tc-secret-notice')).toBeTruthy()
    expect(s.converse.postMessage).not.toHaveBeenCalled()
    fixture.workspace = selected(OTHER); s.redraw()
    expect(screen.queryByTestId('tc-secret-notice')).toBeNull()
    expect(document.querySelector('.tc-bar textarea').value).toBe('')
  })

  it('drops a route when the selected project changes before it resolves', async () => {
    const s = await setup(); let resolve
    fixture.route = new Promise(r => {resolve = r})
    s.type('Summarize records'); fireEvent.click(s.run())
    await waitFor(() => expect(s.api.nlPrompt).toHaveBeenCalledTimes(1))
    fixture.workspace = selected(OTHER); s.redraw()
    expect(document.querySelector('.tc-bar textarea').value).toBe('')
    await act(async () => {resolve({lane: 'run', tool: 'count_panels', confidence: 0.99})})
    expect(s.converse.postMessage).not.toHaveBeenCalled()
    expect(s.converse.ensureSession).not.toHaveBeenCalled()
  })
})
