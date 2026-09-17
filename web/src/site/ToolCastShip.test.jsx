// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { validateIosShipReadiness } from './iosShipReadiness.js'
import { notificationBus } from '../lib/notifications.js'

const fixture = vi.hoisted(() => {
  const noop = () => {}
  return {
    ship: null,
    session: { status: 'active', actions: { requireAuth: noop } },
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
  Frame.Toast = () => {
    const value = useContext(Context)
    return <Toast {...value} />
  }
  return { default: Frame }
})

afterEach(() => { cleanup(); notificationBus.clearVisible(); vi.unstubAllGlobals() })

it('I1 row10 renders controller launch delegation, acceptance toast, errors and request-only busy', async () => {
  // Load the real component only after jsdom's encoding constructors are aligned.
  const { TextEncoder, TextDecoder } = await import('node:util')
  vi.stubGlobal('TextEncoder', TextEncoder)
  vi.stubGlobal('TextDecoder', TextDecoder)
  const { default: ToolCast } = await import('./ToolCast.jsx')
  const launch = vi.fn().mockResolvedValue({ ok: true, execution: { execution_id: 'e1', status: 'queued' } })
  fixture.ship = {
    readiness: validateIosShipReadiness({
      record_kind: 'leaf.ios-ship-readiness.v1', project_id: 'p1', healthy: true, launchable: true,
      grant_status: 'healthy', dispatch_available: true,
      approved_launch: { approval_id: 'a1', revision: 'r1', source_revision: 'source1',
        source_sha256: 'a'.repeat(64), bundle_identifier: 'com.leaf.test', marketing_version: '1.0', build_number: '12' },
    }, { projectId: 'p1', revision: 'r1' }),
    launch, refresh: vi.fn(), phase: 'ready', busy: false, error: null, execution: null, receipt: null,
  }
  const view = render(<ToolCast active={false} />)
  await act(async () => { fireEvent.click(screen.getByTestId('ios-ship-launch')) })
  expect(launch).toHaveBeenCalledTimes(1)
  expect(screen.getByText('iOS ship launch accepted. Track it in the lane.')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'View' })).toBeInTheDocument()
  fixture.ship = { ...fixture.ship, error: 'Launch transport unavailable', busy: true, phase: 'launching' }
  view.rerender(<ToolCast active={false} />)
  expect(screen.getByTestId('ios-ship-error')).toHaveTextContent('Launch transport unavailable')
  expect(screen.getByTestId('ios-ship-launch')).toHaveTextContent(/^Launching$/)
  expect(screen.getByTestId('ios-ship-launch')).toBeDisabled()
  fixture.ship = { ...fixture.ship, busy: false, phase: 'running', execution: { execution_id: 'e1', status: 'running' } }
  view.rerender(<ToolCast active={false} />)
  expect(screen.getByTestId('ios-ship-launch')).toHaveTextContent(/^Launch TestFlight build$/)
  expect(screen.getByTestId('ios-ship-launch')).toBeEnabled()
})
