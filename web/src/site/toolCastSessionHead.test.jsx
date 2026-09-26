// @vitest-environment jsdom
// E05: /try seats the drawing's REAL head on load. /api/session serves the
// head intake with no version facts, so the load reads the versions summary
// (as App.jsx does) and seats it through sessionDrawingState; a failed or
// malformed read keeps the head-1 seat and never fails the load.
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { validateIosShipReadiness } from './iosShipReadiness.js'
import { notificationBus } from '../lib/notifications.js'

const fixture = vi.hoisted(() => {
  const noop = () => {}
  return {
    ship: null,
    session: { status: 'active', actions: { requireAuth: noop, checking: noop, activate: noop } },
    workspace: { openProjectId: 'p1', canonicalVersionId: 'r1', projects: [], workspace: null },
    controllers: {
      converse: { turns: [], activeRequests: [], startTurn: noop, clear: noop, resetCached: noop },
      bindConverseProject: noop,
      drawing: { actions: {}, shown: null, drawingState: null, visibleLayers: {} },
    },
    catalog: { state: { tools: [], catalog: { families: [] }, openFamilies: {} }, actions: {}, controller: {} },
    jobs: { jobs: [], reset: noop },
    platform: { isEntitled: () => true, actions: {} },
    checkout: { actions: {} },
    upload: { actions: {} },
    registry: { servers: [] },
  }
})

vi.mock('../api.js', async (importOriginal) => ({ ...(await importOriginal()), getSession: vi.fn(), getDrawingVersions: vi.fn() }))
vi.mock('../ios/useIosShipController.js', () => ({ useIosShipController: vi.fn(() => fixture.ship) }))
vi.mock('../controllers/WorkspaceControllerProvider.jsx', () => ({ useWorkspaceControllers: () => fixture.controllers }))
vi.mock('../controllers/session/useSessionController.js', () => ({ default: () => fixture.session }))
vi.mock('../controllers/workspace/useWorkspaceController.js', () => ({ default: () => fixture.workspace }))
vi.mock('../controllers/workspace/useSessionOrgAdoption.js', () => ({ default: () => {} }))
vi.mock('../drawing/DrawingIdentityProvider.jsx', () => ({ useDrawingScopeReset: () => {} }))
vi.mock('../controllers/catalog/useCatalogController.js', () => ({ default: () => fixture.catalog }))
vi.mock('../controllers/useJobController.js', () => ({ default: () => fixture.jobs }))
vi.mock('../controllers/useBuildQueue.js', () => ({ default: () => ({ builds: [] }) }))
vi.mock('../controllers/useAuthorStageController.js', () => ({ default: () => ({}) }))
vi.mock('../controllers/platform/usePlatformTrustController.js', () => ({ default: () => fixture.platform }))
vi.mock('../controllers/checkout/useCheckoutController.js', () => ({ default: () => fixture.checkout }))
vi.mock('../controllers/upload/useDrawingUploadController.js', () => ({ default: () => fixture.upload }))
vi.mock('../useTenantMcpRegistry.js', () => ({ default: () => fixture.registry }))
vi.mock('../ios/useIosSurface.js', () => ({ default: () => ({}) }))
vi.mock('./useSurfaceContract.js', () => ({ useSurfaceContract: () => ({
  chrome: { stageBranch: 'ios' }, authoring: false, versions: 'none',
}) }))
vi.mock('../components/ConversationList.jsx', () => ({ resumeHref: () => null }))
vi.mock('../components/LiveRegion.jsx', () => ({ default: () => null, HIDE_WITH_CLASS: 'hidden' }))
vi.mock('../components/ConversePanel.jsx', () => ({ default: () => null }))
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
vi.mock('../components/PromptBox.jsx', () => ({ default: () => null }))
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

// Keep ToolCast's actual slot placement and the real toast renderer.
vi.mock('./SurfaceFrame.jsx', async () => {
  const { createContext, useContext } = await import('react')
  const { default: Toast } = await import('../components/Toast.jsx')
  const Context = createContext(null)
  function Frame({ children, toast }) { return <Context.Provider value={toast}>{children}</Context.Provider> }
  Frame.Tabs = () => null
  // The iOS stage branch renders <SurfaceFrame.Frame /> (the declared frame slot).
  Frame.Frame = () => null
  Frame.Toast = () => {
    const value = useContext(Context)
    return <Toast {...value} />
  }
  return { default: Frame }
})

afterEach(() => { cleanup(); notificationBus.clearVisible(); vi.unstubAllGlobals() })

const HEAD_ONE = { drawing_id: 'd', version: 1, head: 1, latest: 1 }
const SESSION = { intake: { polylines: [] }, tenant: 't', tier: null, org: null }

// Load the real component only after jsdom's encoding constructors are aligned.
async function loadToolCast() {
  const { TextEncoder, TextDecoder } = await import('node:util')
  vi.stubGlobal('TextEncoder', TextEncoder)
  vi.stubGlobal('TextDecoder', TextDecoder)
  const module = await import('./ToolCast.jsx')
  const api = await import('../api.js')
  return { ...module, api }
}

// Fresh spies per row: the session effect lists seatIntake and the session
// actions as dependencies, so each row gets its own stable objects.
async function armRender() {
  const loaded = await loadToolCast()
  loaded.api.getSession.mockReset()
  loaded.api.getDrawingVersions.mockReset()
  const seatIntake = vi.fn()
  const activate = vi.fn()
  fixture.controllers.drawing = { actions: { seatIntake, seatVersion: () => {} }, shown: null, drawingState: null, visibleLayers: {} }
  fixture.session = { status: 'active', actions: { requireAuth: vi.fn(), checking: vi.fn(), activate } }
  fixture.ship = {
    readiness: validateIosShipReadiness({
      record_kind: 'leaf.ios-ship-readiness.v1', project_id: 'p1', healthy: true, launchable: true,
      grant_status: 'healthy', dispatch_available: true,
      approved_launch: { approval_id: 'a1', revision: 'r1', source_revision: 'source1',
        source_sha256: 'a'.repeat(64), bundle_identifier: 'com.leaf.test', marketing_version: '1.0', build_number: '12' },
    }, { projectId: 'p1', revision: 'r1' }),
    launch: vi.fn(), refresh: vi.fn(), phase: 'ready', busy: false, error: null, execution: null, receipt: null,
  }
  return { ...loaded, seatIntake, activate }
}

describe('sessionDrawingState', () => {
  it('E05 head row1 seats the summary head and latest', async () => {
    const { sessionDrawingState } = await loadToolCast()
    expect(sessionDrawingState('d', { head: 3, latest: 3 })).toEqual({ drawing_id: 'd', version: 3, head: 3, latest: 3 })
  })

  it('E05 head row2 keeps an undone head below latest', async () => {
    const { sessionDrawingState } = await loadToolCast()
    expect(sessionDrawingState('d', { version: 2, head: 2, latest: 3 })).toEqual({ drawing_id: 'd', version: 2, head: 2, latest: 3 })
  })

  it('E05 head row3 falls back to the head-1 seat on anything malformed', async () => {
    const { sessionDrawingState } = await loadToolCast()
    const malformed = [
      null, undefined, 'x', {}, { head: '3', latest: 3 }, { head: 0, latest: 0 },
      { head: 3, latest: 2 }, { head: NaN, latest: NaN }, { head: 1.5, latest: 2 },
    ]
    for (const summary of malformed) {
      expect(sessionDrawingState('d', summary)).toEqual(HEAD_ONE)
    }
  })
})

describe('ToolCast session load', () => {
  it('E05 head row4 seats the versions summary head after the session', async () => {
    const { default: ToolCast, api, seatIntake } = await armRender()
    api.getSession.mockResolvedValue(SESSION)
    api.getDrawingVersions.mockResolvedValue({ drawing_id: 'd-3', head: 3, latest: 3, versions: [] })
    render(<ToolCast active drawingId="d-3" />)
    await waitFor(() => expect(seatIntake).toHaveBeenCalledTimes(1))
    expect(api.getSession).toHaveBeenCalledWith(false, 'd-3')
    expect(api.getDrawingVersions).toHaveBeenCalledWith(false, 'd-3')
    expect(seatIntake).toHaveBeenCalledWith(SESSION.intake, {
      drawingId: 'd-3',
      drawingState: { drawing_id: 'd-3', version: 3, head: 3, latest: 3 },
      apply: true,
    })
  })

  it('E05 head row5 a failed versions read seats head 1 and does not fail the load', async () => {
    const { default: ToolCast, api, seatIntake } = await armRender()
    api.getSession.mockResolvedValue(SESSION)
    api.getDrawingVersions.mockRejectedValue(new Error('versions unavailable'))
    render(<ToolCast active drawingId="d-3" />)
    await waitFor(() => expect(seatIntake).toHaveBeenCalledTimes(1))
    expect(seatIntake.mock.calls[0][1].drawingState).toEqual({ ...HEAD_ONE, drawing_id: 'd-3' })
    expect(screen.queryByText(/The drawing backend is unavailable\./)).toBeNull()
  })

  it('E05 head row6 activates the session before the versions read resolves', async () => {
    const { default: ToolCast, api, seatIntake, activate } = await armRender()
    let resolveVersions
    api.getSession.mockResolvedValue(SESSION)
    api.getDrawingVersions.mockReturnValue(new Promise((resolve) => { resolveVersions = resolve }))
    render(<ToolCast active drawingId="d-3" />)
    await waitFor(() => expect(activate).toHaveBeenCalledWith(SESSION))
    await waitFor(() => expect(api.getDrawingVersions).toHaveBeenCalledWith(false, 'd-3'))
    expect(seatIntake).not.toHaveBeenCalled()
    resolveVersions({ drawing_id: 'd-3', head: 3, latest: 3, versions: [] })
    await waitFor(() => expect(seatIntake).toHaveBeenCalledTimes(1))
    expect(seatIntake.mock.calls[0][1].drawingState).toEqual({ drawing_id: 'd-3', version: 3, head: 3, latest: 3 })
  })
})
