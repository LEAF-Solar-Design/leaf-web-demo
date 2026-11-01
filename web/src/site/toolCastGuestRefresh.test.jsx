// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { notificationBus } from '../lib/notifications.js'

// Keep the stage, URL seating and DetailsDrawer real; isolate unrelated
// controllers and panels so any fetch in these tests belongs to Refresh.
const fixture = vi.hoisted(() => {
  const noop = () => {}
  return {
    session: { status: 'required', actions: { requireAuth: noop, checking: noop, activate: noop, signOut: vi.fn() } },
    workspace: { orgId: 'org-1', projects: [], workspace: null },
    controllers: {
      converse: { turns: [], activeRequests: [], startTurn: noop, clear: noop, resetCached: noop },
      bindConverseProject: noop,
      drawing: { actions: {
        loadHistory: vi.fn(),
        markRefreshFailure: vi.fn(),
        recordCommittedUnreadableHead: vi.fn(),
        recordRestore: vi.fn(),
        setVisibleLayers: vi.fn(),
      }, shown: null, drawingState: null, visibleLayers: {} },
    },
    catalog: { state: { tools: [], catalog: { families: [] }, openFamilies: {} }, actions: {
      commitDecision: vi.fn(),
      dismissRoute: vi.fn(),
      dispatch: vi.fn(),
      loadCatalog: vi.fn(),
      loadTools: vi.fn(),
      setFamilyOpen: vi.fn(),
      setPrompt: vi.fn(),
      upsertTool: vi.fn(),
    }, controller: {} },
    jobs: { jobs: [], reset: noop },
    platform: {
      entitlements: { tier: 'team' },
      // The CAD result rail reads quota even when there is no run result.
      // Match usePlatformTrustController's empty derived state.
      quota: { condition: null, freshUsage: null, cleared: false, visible: null },
      healthStatus: { status: 'unknown', degraded: false },
      isEntitled: () => true,
      actions: { refreshAll: vi.fn() },
    },
    checkout: { actions: {
      getCapability: vi.fn(() => null),
      refresh: vi.fn(),
      release: vi.fn(),
      take: vi.fn(),
    } },
    upload: { actions: {} },
    registry: { servers: [] },
    ship: { readiness: {}, launch: noop, refresh: noop },
  }
})

vi.mock('../api.js', async (importOriginal) => ({ ...(await importOriginal()), getSession: vi.fn() }))
vi.mock('../ios/useIosShipController.js', () => ({ useIosShipController: () => fixture.ship }))
vi.mock('../controllers/WorkspaceControllerProvider.jsx', () => ({ useWorkspaceControllers: () => fixture.controllers }))
vi.mock('../controllers/session/useSessionController.js', () => ({ default: () => fixture.session }))
vi.mock('../controllers/workspace/useWorkspaceController.js', () => ({ default: () => fixture.workspace }))
vi.mock('../controllers/workspace/useSessionOrgAdoption.js', () => ({ default: () => {} }))
vi.mock('../drawing/DrawingIdentityProvider.jsx', () => ({ useDrawingScopeReset: () => {}, useDrawingIdentityOptional: () => null }))
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
  chrome: { stageBranch: 'cad' }, authoring: true, versions: 'drawing',
}) }))
vi.mock('../components/ConversationList.jsx', () => ({ resumeHref: () => null }))
vi.mock('../components/LiveRegion.jsx', () => ({ default: () => null, HIDE_WITH_CLASS: 'hidden' }))
vi.mock('../components/ConversePanel.jsx', () => ({ default: () => null }))
vi.mock('../components/AuthorPanel.jsx', () => ({ default: () => null }))
vi.mock('../components/CapabilityCatalog.jsx', () => ({ default: () => null }))
vi.mock('../components/ClaudeAccountPanel.jsx', () => ({ default: () => null }))
vi.mock('../components/CheckoutControls.jsx', () => ({ default: () => null }))
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
vi.mock('./SurfaceFrame.jsx', () => {
  function Frame({ children }) { return <>{children}</> }
  for (const slot of ['Tabs', 'Frame', 'Toast', 'ContextMenu', 'CommandBar', 'Conversations', 'Entitlement', 'Inbox', 'Integrations', 'JobRail']) Frame[slot] = () => null
  return { default: Frame }
})

beforeEach(() => {
  window.history.replaceState({}, '', '/try')
  localStorage.removeItem('leaf.jwt')
  fixture.session.status = 'required'
  vi.clearAllMocks()
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ ok: true, source_revision: '6a18a82e' }) }))
})

afterEach(() => {
  cleanup()
  notificationBus.clearVisible()
  localStorage.removeItem('leaf.jwt')
  window.history.replaceState({}, '', '/')
  vi.unstubAllGlobals()
})

async function renderStage() {
  // Align encoding constructors before importing ToolCast's transport graph.
  const { TextEncoder, TextDecoder } = await import('node:util')
  vi.stubGlobal('TextEncoder', TextEncoder)
  vi.stubGlobal('TextDecoder', TextDecoder)
  const { default: ToolCast } = await import('./ToolCast.jsx')
  render(<ToolCast active />)
}

describe('stage Account details Refresh', () => {
  it('lets a guest open Account details and request health only', async () => {
    await renderStage()
    fireEvent.click(screen.getByRole('button', { name: 'Account details' }))
    const drawer = screen.getByRole('dialog', { name: 'Account details' })
    expect(within(drawer).getByText('authentication sign in required')).toBeVisible()
    expect(fetch).not.toHaveBeenCalled()
    const fetchCountBeforeRefresh = fetch.mock.calls.length
    fireEvent.click(within(drawer).getByRole('button', { name: 'Refresh', exact: true }))
    await waitFor(() => expect(within(drawer).getByRole('status')).toHaveTextContent('Up to date'))
    const refreshCalls = fetch.mock.calls.slice(fetchCountBeforeRefresh)
    expect(refreshCalls).toHaveLength(1)
    expect(refreshCalls[0]).toEqual([expect.stringMatching(/\/api\/health$/), { method: 'GET' }])
    const api = await import('../api.js')
    expect(api.getSession).not.toHaveBeenCalled()
    expect(fixture.platform.actions.refreshAll).not.toHaveBeenCalled()
  })

  it('restores drawer=details as a visible dialog with focus and Refresh', async () => {
    window.history.replaceState({}, '', '/try?drawer=details')
    await renderStage()
    const drawer = await screen.findByRole('dialog', { name: 'Account details' })
    expect(drawer).toBeVisible()
    expect(drawer).toHaveAttribute('open')
    expect(within(drawer).getByRole('button', { name: 'Refresh', exact: true })).toBeVisible()
    expect(within(drawer).getByRole('button', { name: 'Close details' })).toHaveFocus()
    expect(fetch).not.toHaveBeenCalled()
  })

  it('preserves signed-in account content and Sign out alongside Refresh', async () => {
    localStorage.setItem('leaf.jwt', 'signed-test-token')
    fixture.session.status = 'active'
    const api = await import('../api.js')
    api.getSession.mockResolvedValue({ org: 'org-1', tier: 'team' })
    await renderStage()
    fireEvent.click(screen.getByRole('button', { name: 'Account details' }))
    const drawer = screen.getByRole('dialog', { name: 'Account details' })
    for (const text of ['tenant try-surface', 'organization org-1', 'tier team', 'authentication active', 'Platform identity and Claude account credit are separate.']) {
      expect(within(drawer).getByText(text)).toBeVisible()
    }
    expect(within(drawer).getByRole('button', { name: 'Refresh', exact: true })).toBeVisible()
    fireEvent.click(within(drawer).getByRole('button', { name: 'Sign out' }))
    expect(fixture.session.actions.signOut).toHaveBeenCalledTimes(1)
  })
})
