import { beforeEach, afterEach, expect, it, vi } from 'vitest'
import { useEffect, useLayoutEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import ProjectWorkspacePanels, { PANE_NAMES, paneForCapability, BoardPaneState, deriveBoardPaneSeats,
  PersistentSeat, useBoardConversation, ConversationOpening, AnnotationPaneState } from './ProjectWorkspacePanels.jsx'

const settingsContext = {
  lifecycleEnabled: true, mock: false, signedIn: true, sessionStatus: 'active',
  projectId: 'project-a', boardHostsProject: true, projectPane: 'settings',
  settingsDestination: 'settings-destination',
}

it('BI03-05 Settings eligibility fails closed with exact reasons', () => {
  const failures = [
    [{ lifecycleEnabled: false, mock: true, signedIn: false, sessionStatus: 'idle', projectId: null }, 'Project settings are unavailable in this build.'],
    [{ mock: true, signedIn: false, sessionStatus: 'idle', projectId: null }, 'Project settings are unavailable in this offline demo.'],
    [{ signedIn: false, sessionStatus: 'idle', projectId: null }, 'Sign in to use project settings.'],
    ...['idle', 'signed_out', 'required', 'activating', 'error', undefined].map((sessionStatus) =>
      [{ sessionStatus, projectId: null }, 'Start an active session to use project settings.']),
    [{ projectId: null }, 'Open a project to use project settings.'],
    // Single faults: every other input is eligible and the project is open, so each guard is the only one failing.
    [{ lifecycleEnabled: false }, 'Project settings are unavailable in this build.'],
    [{ mock: true }, 'Project settings are unavailable in this offline demo.'],
    [{ signedIn: false }, 'Sign in to use project settings.'],
    ...['idle', 'signed_out', 'required', 'activating', 'error', undefined].map((sessionStatus) =>
      [{ sessionStatus }, 'Start an active session to use project settings.']),
  ]
  for (const [patch, reason] of failures) {
    const result = deriveBoardPaneSeats({ ...settingsContext, ...patch })
    expect(result.lifecycleEligible).toBe(false)
    expect(result.settingsTarget).toBeNull()
    expect(result.settingsReason).toBe(reason)
  }
  const enabled = deriveBoardPaneSeats(settingsContext)
  expect(enabled.lifecycleEligible).toBe(true)
  expect(enabled.settingsReason).toBeNull()
})

it('BI03-25 Settings eligibility agrees with its reason over every input', () => {
  let cases = 0
  for (const lifecycleEnabled of [true, false]) {
    for (const mock of [true, false]) {
      for (const signedIn of [true, false]) {
        for (const sessionStatus of ['active', 'idle', 'signed_out', 'required', 'activating', 'error', undefined]) {
          for (const projectId of ['p1', null]) {
            const result = deriveBoardPaneSeats({ ...settingsContext, lifecycleEnabled, mock, signedIn, sessionStatus, projectId })
            const eligible = lifecycleEnabled && !mock && signedIn && sessionStatus === 'active' && projectId !== null
            expect([lifecycleEnabled, mock, signedIn, sessionStatus, projectId, result.lifecycleEligible])
              .toEqual([lifecycleEnabled, mock, signedIn, sessionStatus, projectId, eligible])
            expect(result.lifecycleEligible).toBe(result.settingsReason === null)
            if (!result.lifecycleEligible) expect(result.settingsTarget).toBeNull()
            cases += 1
          }
        }
      }
    }
  }
  expect(cases).toBe(112)
})

it('BI03-17 Generic Settings keeps its no-slot fallback', () => {
  const { rerender } = render(<ProjectWorkspacePanels pane="settings" />)
  expect(screen.getAllByText('Settings is not mounted on this surface.')).toHaveLength(1)
  rerender(<ProjectWorkspacePanels pane="settings" slots={{ settings: <p>Supplied settings</p> }} />)
  expect(screen.getByText('Supplied settings')).toBeTruthy()
  expect(screen.queryByText('Settings is not mounted on this surface.')).toBeNull()
  rerender(<ProjectWorkspacePanels pane="tools" />)
  expect(screen.getByText('No built tools yet. Author one from the Manage tab.')).toBeTruthy()
})

it('BI03-24 Only board Settings receives the lifecycle destination', () => {
  for (const patch of [
    {}, { drawingId: null, canonicalVersionId: null, sessionId: null, canConverse: false, agentMode: null },
    { drawingId: 'drawing-a', sessionId: 'conversation-a', canConverse: true, agentMode: 'primary' },
  ]) {
    expect(deriveBoardPaneSeats({ ...settingsContext, ...patch }).settingsTarget).toBe('settings-destination')
  }
  for (const patch of [
    { boardHostsProject: false }, { settingsDestination: null },
    ...PANE_NAMES.filter((pane) => pane !== 'settings').map((projectPane) => ({ projectPane })),
    { projectPane: null },
  ]) {
    const result = deriveBoardPaneSeats({ ...settingsContext, ...patch })
    expect(result.lifecycleEligible).toBe(true)
    expect(result.settingsTarget).toBeNull()
  }
})
import ConversePanel from '../components/ConversePanel.jsx'
import AuthorPanel from '../components/AuthorPanel.jsx'
import AnnotationDecisionCard from '../components/AnnotationDecisionCard.jsx'
import { EntitlementNotice } from '../components/EntitlementGate.jsx'
import NavRail from '../site/NavRail.jsx'
import useConverseSessionController from '../controllers/useConverseSessionController.js'
import useAuthorStageController from '../controllers/useAuthorStageController.js'
import { INFLIGHT_AUTHOR_KEY, authorAccountScope } from '../authorStagePointer.js'
import { config } from '../api.js'
import { useAnnotations } from '../useAnnotations.js'
import { ensureSession, postMessage, openStream, listPendingApprovals, cancelTurn } from '../converse.js'
import { listEngineChanges } from '../engineChanges.js'
import { ladderListener } from '../lib/actionRegistry.js'
import { fetchCurrentAnnotation, acceptAnnotation } from '../annotationClient.js'

vi.mock('../telemetry.js', () => ({ track: vi.fn(), TELEMETRY_BUILD_DISABLED: false }))
vi.mock('../converse.js', () => ({
  ensureSession: vi.fn(), postMessage: vi.fn(), openStream: vi.fn(),
  listPendingApprovals: vi.fn(), cancelTurn: vi.fn(), resolveApproval: vi.fn(),
  resetSession: vi.fn(), classifyAgentError: () => 'unreachable',
  projectActivityProjection: (value) => value,
  normalizeScope: (value) => value, sessionCacheKey: () => 'scope',
}))
vi.mock('../engineChanges.js', () => ({ listEngineChanges: vi.fn() }))
vi.mock('../annotationClient.js', () => ({
  fetchCurrentAnnotation: vi.fn(), acceptAnnotation: vi.fn(), rejectAnnotation: vi.fn(),
  retryAnnotation: vi.fn(), undoAnnotation: vi.fn(),
}))

beforeEach(() => {
  vi.clearAllMocks()
  ensureSession.mockResolvedValue({ session_id: 'session-a' })
  openStream.mockImplementation(() => ({ close: vi.fn() }))
  listPendingApprovals.mockResolvedValue([])
  listEngineChanges.mockResolvedValue({ kind: 'ok', cards: [], unread_count: 0 })
  fetchCurrentAnnotation.mockResolvedValue(null)
  cancelTurn.mockResolvedValue({})
})

afterEach(() => { cleanup(); vi.useRealTimers() })

const project = { project_id: 'p1', name: 'North Yard' }

it('B2 row4 renders the controlled pane for the current project and returns to its board', () => {
  const onBack = vi.fn()
  const { container, rerender } = render(<ProjectWorkspacePanels pane={null} project={project} />)
  expect(container.innerHTML).toBe('')
  expect(Object.isFrozen(PANE_NAMES)).toBe(true)
  for (const pane of PANE_NAMES) {
    rerender(<ProjectWorkspacePanels pane={pane} project={project} onBack={onBack} mock />)
    expect(container.querySelector('.ground-pane')).toHaveAttribute('data-pane', pane)
    expect(screen.getByRole('heading', { name: pane[0].toUpperCase() + pane.slice(1), exact: true })).toBeInTheDocument()
    expect(screen.getByText(project.name)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Back to board' }))
  }
  expect(onBack).toHaveBeenCalledTimes(PANE_NAMES.length)
  rerender(<ProjectWorkspacePanels pane="versions" project={{ name: 'South Yard' }} />)
  expect(screen.getByText('South Yard')).toBeInTheDocument()
  // onBack absent: React attaches no listener for an undefined event prop, so the click is inert, never a throw.
  expect(() => fireEvent.click(screen.getByRole('button', { name: 'Back to board' }))).not.toThrow()
  expect(onBack).toHaveBeenCalledTimes(PANE_NAMES.length)
  expect(screen.queryByText('North Yard')).toBeNull()
})

it('B2 row5 seats the job rail and shows bounded real job detail', () => {
  const job = { job_id: 'j1', tool_name: 'Measure', tool: 'Measure', status: 'complete', cost_usd: 0.25,
    input_version_id: 'v-in', output_version_id: 'v-out', created_at: '2026-09-01T12:00:00Z',
    updated_at: '2026-09-01T12:01:00Z', result: { count: 12 } }
  const onSelectJob = vi.fn()
  const props = { project, pane: 'jobs', workspace: { jobs: [job] }, currentJob: job, onSelectJob }
  const { container, rerender } = render(<ProjectWorkspacePanels {...props} />)
  const detail = within(container.querySelector('.ground-job-detail'))
  for (const value of ['complete', 'v-in', 'v-out', '$0.25', job.created_at, job.updated_at]) expect(detail.getByText(value)).toBeInTheDocument()
  expect(container.querySelector('.ground-job-detail pre').textContent).toBe(JSON.stringify(job.result, null, 2))
  fireEvent.click(within(container.querySelector('.rail')).getByRole('button', { name: /Measure/ }))
  expect(onSelectJob).toHaveBeenCalledWith(job)
  const result = { text: 'x'.repeat(5000) }
  rerender(<ProjectWorkspacePanels {...props} currentJob={{ ...job, result }} />)
  expect(container.querySelector('.ground-job-detail pre').textContent).toBe(`${JSON.stringify(result, null, 2).slice(0, 4000)}…`)
})

it('B2 row6 orders versions and preserves honest material and tool empties', () => {
  const old = { version_id: 'old', seq: 1, created_at: '2026-09-01' }
  const recent = { version_id: 'recent', seq: 2, created_at: '2026-09-02' }
  const onOpenVersion = vi.fn()
  const { rerender } = render(<ProjectWorkspacePanels project={project} pane="versions" workspace={{ drawing_versions: [old, recent] }} onOpenVersion={onOpenVersion} />)
  expect(screen.getAllByRole('listitem').map((row) => row.textContent)).toEqual(['v2 · recent', 'v1 · old'])
  fireEvent.click(screen.getByRole('button', { name: 'v2 · recent' }))
  expect(onOpenVersion).toHaveBeenCalledWith(recent)
  rerender(<ProjectWorkspacePanels project={project} pane="material" />)
  expect(screen.getByText('No material attached yet. Upload a drawing to attach it.')).toBeInTheDocument()
  rerender(<ProjectWorkspacePanels project={project} pane="tools" />)
  expect(screen.getByText('No built tools yet. Author one from the Manage tab.')).toBeInTheDocument()
  rerender(<ProjectWorkspacePanels project={project} pane="versions" />)
  expect(screen.getByText('No versions yet.')).toBeInTheDocument()
})

it('B2 row7 allows missing slots in the generic host', () => {
  const { rerender } = render(<ProjectWorkspacePanels project={project} pane="conversation" />)
  for (const pane of ['conversation', 'annotations', 'authoring', 'settings', 'catalog']) {
    rerender(<ProjectWorkspacePanels project={project} pane={pane} />)
    expect(screen.getByText(`${pane[0].toUpperCase() + pane.slice(1)} is not mounted on this surface.`)).toBeInTheDocument()
  }
})

it('B2 row7 seats supplied elements in the generic host', () => {
  const { rerender } = render(<ProjectWorkspacePanels pane={null} />)
  for (const pane of ['conversation', 'annotations', 'authoring', 'settings', 'catalog']) {
    rerender(<ProjectWorkspacePanels project={project} pane={pane} slots={{ [pane]: <div>Supplied work</div> }} />)
    expect(screen.getByText('Supplied work')).toBeInTheDocument()
    expect(screen.queryByText(/not mounted/)).toBeNull()
  }
})

it('B2 row8 maps all shared capabilities', () => {
  expect(['conversation', 'annotations', 'authoring', 'approvals', 'versions', 'receipts', 'marathons', 'one-shot execution'].map(paneForCapability))
    .toEqual(['conversation', 'annotations', 'authoring', 'annotations', 'versions', 'receipts', 'jobs', 'jobs'])
  expect(paneForCapability('unknown')).toBeNull()
  expect(paneForCapability('constructor')).toBeNull()
})

const liveContext = { mock: false, signedIn: true, projectId: 'p1', drawingId: 'd1', sessionId: 'session-a' }
const proposal = { batchId: 'batch-a', revision: 4, targetVersion: 'v4', state: 'pending', decisionCopy: 'Review the proposed labels.' }
const flush = () => act(async () => { await Promise.resolve(); await Promise.resolve() })
function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

// One owner for each product state. Navigation only changes the empty destinations.
function PaneFixture({ pane = 'conversation', board = true,
  context = { ...liveContext, drawingId: pane === 'annotations' ? 'd1' : null },
  onAuthor = vi.fn(), seed, seedSignal, seedAutoSubmit, stageActivity,
  canConverse = true, buildEntitled = true, onBack = vi.fn(), onOpenConversation = vi.fn() }) {
  const [source, setSource] = useState(null)
  const [destination, setDestination] = useState(null)
  const [agentMode, setAgentMode] = useState(
    !board && pane === 'conversation' ? 'primary' : null,
  )
  const seats = deriveBoardPaneSeats({
    ...context,
    sessionStatus: context.sessionStatus ?? 'active',
    boardHostsProject: board, projectPane: pane, canConverse, agentMode,
    authorOpen: !board && pane === 'authoring',
    conversationSource: source, conversationDestination: destination,
    annotationSource: source, annotationDestination: destination,
    authorSource: source, authorDestination: destination, authorFallback: source,
  })
  useEffect(() => {
    if (seats.boardConversation && seats.conversationEligible && context.sessionId) {
      setAgentMode('primary')
    }
  }, [seats.boardConversation, seats.conversationEligible, context.sessionId])
  const annotations = useAnnotations(context.sessionId, {
    enabled: seats.annotationEnabled,
  })
  const card = <AnnotationDecisionCard annotation={annotations.annotation} busy={annotations.busy}
    error={annotations.error} confirmation={annotations.confirmation} onPreview={annotations.preview}
    onAccept={annotations.accept} onReject={annotations.reject} onRetry={annotations.retry} onUndo={annotations.undo} />
  const slot = <BoardPaneState pane={pane} context={context} onOpenConversation={onOpenConversation}>
    {pane === 'annotations' ? <AnnotationPaneState annotations={annotations}><div ref={setDestination} /></AnnotationPaneState>
      : pane === 'conversation' && !canConverse ? <EntitlementNotice required="converse" tier="free" />
        : <div ref={setDestination} />}
  </BoardPaneState>
  return <>
    <div data-testid="source" ref={setSource} />
    <div hidden={!board}><ProjectWorkspacePanels project={project} pane={pane} onBack={onBack}
      slots={{ [pane]: slot }} /></div>
    {seats.conversationMounted &&
      <PersistentSeat destination={seats.conversationTarget}><ConversePanel sessionId={context.sessionId} /></PersistentSeat>}
    {seats.authorMounted && <PersistentSeat destination={seats.authorTarget}>
      <AuthorPanel onAuthor={onAuthor} seed={seed} seedSignal={seedSignal} seedAutoSubmit={seedAutoSubmit}
        stageActivity={stageActivity} buildEntitled={buildEntitled} />
    </PersistentSeat>}
    {seats.annotationEnabled && annotations.annotation && seats.annotationTarget &&
      createPortal(card, seats.annotationTarget)}
  </>
}

function OpeningFixture({ projectId = 'p1', signedIn = true, drawingId = null,
  active = true, canConverse = true }) {
  const controller = useConverseSessionController({ drawingId })
  useLayoutEffect(() => { controller.setProjectContext(projectId) }, [projectId, controller.setProjectContext])
  useLayoutEffect(() => { if (!signedIn) controller.clear() }, [signedIn, controller.clear])
  const context = { ...liveContext, projectId, signedIn, drawingId, sessionId: controller.sessionId }
  const seats = deriveBoardPaneSeats({
    ...context, sessionStatus: 'active', boardHostsProject: active,
    projectPane: 'conversation', canConverse, agentMode: 'primary',
    authorOpen: false,
  })
  const conversationContext = useMemo(() => ({}), [projectId, signedIn, drawingId])
  const opening = useBoardConversation({
    active: seats.boardConversation, eligible: seats.conversationEligible,
    sessionId: controller.sessionId, context: conversationContext,
    attach: controller.attach, onOpen: () => {},
  })
  return <ProjectWorkspacePanels pane="conversation" slots={{ conversation:
    <BoardPaneState pane="conversation" context={context}>
      {!canConverse ? <EntitlementNotice required="converse" tier="free" />
        : seats.conversationMounted ? <ConversePanel sessionId={controller.sessionId} />
        : <ConversationOpening status={opening.status} onRetry={opening.retry} />}
    </BoardPaneState> }} />
}

it('A2-01 shows the real transcript and composer in Conversation', async () => {
  render(<PaneFixture />)
  await flush()
  act(() => openStream.mock.calls[0][2].onEvent({ type: 'text_delta', turn_id: 't1', data: { text: 'Current server reply' } }))
  const pane = screen.getByRole('region', { name: 'Conversation' })
  expect(within(pane).getByLabelText('Reply to the assistant')).toBeInTheDocument()
  expect(within(pane).getByText('Current server reply')).toBeInTheDocument()
  expect(within(pane).queryByText(/not mounted/)).toBeNull()
  expect(openStream).toHaveBeenCalledTimes(1)
})

it('A2-02 shows the current annotation and its existing actions', async () => {
  fetchCurrentAnnotation.mockResolvedValue(proposal)
  render(<PaneFixture pane="annotations" />)
  await flush()
  const pane = within(screen.getByRole('region', { name: 'Annotations', exact: true }))
  expect(pane.getByText(proposal.decisionCopy)).toBeInTheDocument()
  for (const name of ['Accept', 'Reject', 'Preview current']) expect(pane.getByRole('button', { name })).toBeEnabled()
  expect(fetchCurrentAnnotation).toHaveBeenCalledWith('session-a')
})

it('A2-03 shows the real author form and current stage', async () => {
  render(<PaneFixture pane="authoring" seed="Count panels" seedSignal={1}
    stageActivity={{ active: true, phase: 'running', progress: 'Inspecting drawing', elapsedMs: 2000 }} />)
  await flush()
  const pane = within(screen.getByRole('region', { name: 'Authoring', exact: true }))
  expect(pane.getByLabelText('What should the tool do?')).toHaveValue('Count panels')
  expect(pane.getByText(/Inspecting drawing/)).toBeInTheDocument()
  expect(pane.getByRole('button', { name: 'Authoring…' })).toBeDisabled()
})

it.each([
  ['A2-04', 'conversation', { projectId: null }, 'Open a project to use this pane.'],
  ['A2-05', 'annotations', { projectId: null }, 'Open a project to use this pane.'],
  ['A2-06', 'authoring', { projectId: null }, 'Open a project to use this pane.'],
  ['A2-07', 'conversation', { signedIn: false, projectId: null }, 'Sign in to use this pane.'],
  ['A2-08', 'annotations', { signedIn: false, projectId: null }, 'Sign in to use this pane.'],
  ['A2-09', 'authoring', { signedIn: false, projectId: null }, 'Sign in to use this pane.'],
  ['A2-10', 'conversation', { mock: true, signedIn: false }, 'Conversation is unavailable in this offline demo.'],
  ['A2-11', 'annotations', { mock: true, signedIn: false }, 'Annotations are unavailable in this offline demo.'],
  ['A2-12', 'authoring', { mock: true, signedIn: false }, 'Tool authoring is unavailable in this offline demo.'],
  ['A2-14', 'annotations', { drawingId: null }, 'Mount a drawing to view annotations.'],
])('%s explains unavailable %s without activating an owner', async (id, pane, overrides, sentence) => {
  const onAuthor = vi.fn(), onBack = vi.fn()
  render(<PaneFixture pane={pane} context={{ ...liveContext, ...overrides }} onAuthor={onAuthor}
    seed="Count panels" seedSignal={1} seedAutoSubmit onBack={onBack} />)
  await flush()
  expect(screen.getByText(sentence)).toBeInTheDocument()
  expect(screen.queryByRole('textbox')).toBeNull()
  expect(screen.queryByRole('button', { name: /Generate|Accept|Resume|Request publication/ })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Back to board' }))
  expect(onBack).toHaveBeenCalledOnce()
  for (const transport of [onAuthor, ensureSession, postMessage, openStream, fetchCurrentAnnotation, listPendingApprovals, listEngineChanges]) {
    expect(transport).not.toHaveBeenCalled()
  }
})

it('A2-13 opens a conversation with project scope and no drawing', async () => {
  render(<OpeningFixture />)
  await flush()
  expect(ensureSession).toHaveBeenCalledWith(null, 'p1')
  expect(screen.getByLabelText('Reply to the assistant')).toBeInTheDocument()
  expect(postMessage).not.toHaveBeenCalled()
})

it('A2-15 keeps authoring available without a drawing', async () => {
  const onAuthor = vi.fn().mockResolvedValue(null)
  render(<PaneFixture pane="authoring" context={{ ...liveContext, drawingId: null }} onAuthor={onAuthor} />)
  fireEvent.change(screen.getByLabelText('What should the tool do?'), { target: { value: 'Count panels' } })
  await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Generate tool' })))
  expect(onAuthor).toHaveBeenCalledWith('Count panels', null, { allowSecretOnce: false })
  expect(ensureSession).not.toHaveBeenCalled()
})

it('A2-16 shows attachment progress and then the real composer without a message', async () => {
  const request = deferred()
  ensureSession.mockReturnValue(request.promise)
  const view = render(<OpeningFixture />)
  await flush()
  expect(screen.getByText('Opening conversation.')).toBeInTheDocument()
  view.rerender(<OpeningFixture />)
  expect(ensureSession).toHaveBeenCalledOnce()
  await act(async () => request.resolve({ session_id: 'session-a' }))
  expect(screen.getByLabelText('Reply to the assistant')).toBeInTheDocument()
  expect(postMessage).not.toHaveBeenCalled()
})

it('A2-17 retries failed attachment with one pending request', async () => {
  ensureSession.mockRejectedValueOnce(new Error('offline'))
  render(<OpeningFixture />)
  await flush()
  expect(screen.getByText('Conversation could not be opened.')).toBeInTheDocument()
  const request = deferred()
  ensureSession.mockReturnValueOnce(request.promise)
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  await flush()
  expect(ensureSession).toHaveBeenCalledTimes(2)
  expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
  await act(async () => request.resolve({ session_id: 'session-a' }))
  expect(screen.getByLabelText('Reply to the assistant')).toBeInTheDocument()
  expect(postMessage).not.toHaveBeenCalled()
})

it('A2-18 offers Conversation when annotations need a session', () => {
  const open = vi.fn()
  render(<PaneFixture pane="annotations" context={{ ...liveContext, sessionId: null }} onOpenConversation={open} />)
  expect(screen.getByText("Open Conversation to start this project's session.")).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Open Conversation' }))
  expect(open).toHaveBeenCalledOnce()
  expect(openStream).not.toHaveBeenCalled()
})

it('A2-19 distinguishes pending, failed, retried and empty annotation reads', async () => {
  const request = deferred()
  fetchCurrentAnnotation.mockReturnValueOnce(request.promise)
  render(<PaneFixture pane="annotations" />)
  expect(screen.getByText('Loading annotations.')).toBeInTheDocument()
  await act(async () => request.reject(new Error('offline')))
  expect(screen.getByRole('alert')).toHaveTextContent('Annotation status is unavailable.')
  await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Retry' })))
  expect(screen.getByText('No annotations yet.')).toBeInTheDocument()
  expect(fetchCurrentAnnotation).toHaveBeenCalledTimes(2)
  expect(openStream).toHaveBeenCalledOnce()
})

it('A2-20 preserves the conversation DOM and owner effects through relocation', async () => {
  vi.useFakeTimers()
  const view = render(<PaneFixture board={false} />)
  await flush()
  const input = screen.getByLabelText('Reply to the assistant')
  fireEvent.change(input, { target: { value: 'Unsent draft' } })
  const stream = openStream.mock.results[0].value
  view.rerender(<PaneFixture board />)
  expect(within(screen.getByRole('region', { name: 'Conversation' })).getByLabelText('Reply to the assistant')).toBe(input)
  view.rerender(<PaneFixture board={false} />)
  expect(within(screen.getByTestId('source')).getByLabelText('Reply to the assistant')).toBe(input)
  expect(input).toHaveValue('Unsent draft')
  expect(stream.close).not.toHaveBeenCalled()
  expect(openStream).toHaveBeenCalledOnce()
  expect(listPendingApprovals).toHaveBeenCalledOnce()
  expect(listEngineChanges).toHaveBeenCalledOnce()
  await act(async () => vi.advanceTimersByTime(30_000))
  expect(listPendingApprovals).toHaveBeenCalledTimes(7)
  expect(listEngineChanges).toHaveBeenCalledTimes(2)
  view.unmount()
  expect(stream.close).toHaveBeenCalledOnce()
})

it('A2-21 preserves the author draft and consumes its seed once during relocation', async () => {
  vi.useFakeTimers()
  const onAuthor = vi.fn().mockResolvedValue(null)
  const stageActivity = { pointer: null, draftOnly: false }
  const props = { pane: 'authoring', onAuthor, seed: 'Count panels', seedSignal: 1, seedAutoSubmit: true, stageActivity }
  const view = render(<PaneFixture {...props} board={false} />)
  await flush()
  expect(onAuthor).toHaveBeenCalledOnce()
  const input = screen.getByLabelText('What should the tool do?')
  fireEvent.change(input, { target: { value: 'Edited draft' } })
  view.rerender(<PaneFixture {...props} board />)
  expect(within(screen.getByRole('region', { name: 'Authoring', exact: true })).getByLabelText('What should the tool do?')).toBe(input)
  view.rerender(<PaneFixture {...props} board={false} />)
  expect(input).toHaveValue('Edited draft')
  expect(within(screen.getByTestId('source')).getByLabelText('What should the tool do?')).toBe(input)
  expect(onAuthor).toHaveBeenCalledOnce()
  expect(stageActivity).toEqual({ pointer: null, draftOnly: false })
  view.unmount()
  const pointer = { description: 'Recovered draft', idempotency_key: 'saved-key' }
  render(<PaneFixture {...props} stageActivity={{ pointer, draftOnly: true }} />)
  await flush()
  expect(onAuthor).toHaveBeenCalledOnce()
  expect(pointer.idempotency_key).toBe('saved-key')
})

it('A2-21 retains the real stage controller and recovery pointer across destinations', async () => {
  vi.useFakeTimers()
  const values = new Map()
  const storage = { getItem: (key) => values.get(key) || null,
    setItem: (key, value) => values.set(key, value), removeItem: (key) => values.delete(key) }
  const pointer = { idempotency_key: 'recovered-key', description: 'Recovered draft',
    account_scope: authorAccountScope(config.tenant, storage), created_at: Date.now(),
    expires_at: Date.now() + 60_000, draft_only: true,
    prior_staged: { idempotency_key: 'prior-key', receipt: { change_set_id: 'prior-change' } } }
  storage.setItem(INFLIGHT_AUTHOR_KEY, JSON.stringify(pointer))
  const stageRequest = deferred()
  const stageAuthorTool = vi.fn().mockReturnValue(stageRequest.promise)
  const authorityProvider = vi.fn().mockResolvedValue({ sessionId: 'session-a', turnId: 'turn-a' })
  let owner
  function StageOwner({ board }) {
    owner = useAuthorStageController({ storage, stageAuthorTool, authorityProvider })
    return <PaneFixture pane="authoring" board={board} onAuthor={owner.stage} stageActivity={owner}
      seed="Recovered draft" seedSignal={1} seedAutoSubmit />
  }
  const view = render(<StageOwner board={false} />)
  await flush()
  const stage = owner.stage
  const recovered = owner.pointer
  const input = screen.getByLabelText('What should the tool do?')
  fireEvent.change(input, { target: { value: 'Revised description' } })
  view.rerender(<StageOwner board />)
  expect(screen.getByLabelText('What should the tool do?')).toBe(input)
  expect(input).toHaveValue('Revised description')
  expect(owner.stage).toBe(stage)
  expect(owner.pointer).toBe(recovered)
  expect(JSON.parse(storage.getItem(INFLIGHT_AUTHOR_KEY))).toEqual(pointer)
  expect(authorityProvider).not.toHaveBeenCalled()
  expect(stageAuthorTool).not.toHaveBeenCalled()
  await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Generate tool' })))
  expect(authorityProvider).toHaveBeenCalledOnce()
  expect(stageAuthorTool).toHaveBeenCalledOnce()
  const inFlight = owner.pointer
  const signal = stageAuthorTool.mock.calls[0][3].signal
  view.rerender(<StageOwner board={false} />)
  expect(owner.pointer).toBe(inFlight)
  expect(signal.aborted).toBe(false)
  expect(stageAuthorTool).toHaveBeenCalledOnce()
  await act(async () => stageRequest.resolve({ tool: { name: 'count-panels' }, receipt: { change_set_id: 'new-change' } }))
  expect(owner.pointer.terminal_staged).toBe(true)
})

it('A2-22 moves one annotation card while retaining the decision owner', async () => {
  fetchCurrentAnnotation.mockResolvedValue(proposal)
  const request = deferred()
  acceptAnnotation.mockReturnValue(request.promise)
  const view = render(<PaneFixture pane="annotations" board={false} />)
  await flush()
  view.rerender(<PaneFixture pane="annotations" board />)
  expect(screen.getAllByRole('region', { name: 'Annotation decision', exact: true })).toHaveLength(1)
  const accept = screen.getByRole('button', { name: 'Accept' })
  fireEvent.click(accept)
  fireEvent.click(accept)
  expect(acceptAnnotation).toHaveBeenCalledOnce()
  expect(acceptAnnotation).toHaveBeenCalledWith('batch-a', expect.any(String))
  expect(openStream).toHaveBeenCalledOnce()
  expect(openStream.mock.results[0].value.close).not.toHaveBeenCalled()
  await act(async () => request.resolve({ revision: 5, targetCommit: 'head' }))
})

it.each(['project', 'sign out'])('A2-23 ignores an old attachment after %s', async (change) => {
  const old = deferred(), next = deferred()
  ensureSession.mockReturnValueOnce(old.promise).mockReturnValueOnce(next.promise)
  const view = render(<OpeningFixture />)
  await flush()
  view.rerender(<OpeningFixture projectId={change === 'project' ? 'p2' : 'p1'} signedIn={change !== 'sign out'} />)
  await act(async () => old.resolve({ session_id: 'old-session' }))
  expect(screen.queryByLabelText('Reply to the assistant')).toBeNull()
  expect(openStream).not.toHaveBeenCalled()
  if (change === 'project') {
    await act(async () => next.resolve({ session_id: 'new-session' }))
    expect(openStream.mock.calls[0][0]).toBe('new-session')
  } else expect(screen.getByText('Sign in to use this pane.')).toBeInTheDocument()
})

it('A2-24 maps approvals to the existing annotation surface', async () => {
  fetchCurrentAnnotation.mockResolvedValue(proposal)
  render(<PaneFixture pane={paneForCapability('approvals')} />)
  await flush()
  expect(screen.getByRole('region', { name: 'Annotations', exact: true })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Accept' })).toBeInTheDocument()
  expect(screen.queryByRole('region', { name: 'Pending approvals' })).toBeNull()
})

it('A2-25 retains settings fallback and the unaffected panes', () => {
  const view = render(<ProjectWorkspacePanels pane="settings" />)
  expect(screen.getByText('Settings is not mounted on this surface.')).toBeInTheDocument()
  for (const pane of ['material', 'versions', 'tools', 'jobs', 'receipts', 'catalog']) {
    view.rerender(<ProjectWorkspacePanels pane={pane} mock slots={{ catalog: <ul><li>Existing family</li></ul> }} />)
    expect(screen.getByRole('region', { name: pane[0].toUpperCase() + pane.slice(1), exact: true })).toBeInTheDocument()
    expect(screen.queryByText(/not mounted/)).toBeNull()
  }
  expect(screen.getByText('Existing family')).toBeInTheDocument()
})

it('A2-26 moves the live instance out of a hidden board and back', async () => {
  const view = render(<PaneFixture />)
  await flush()
  const input = screen.getByLabelText('Reply to the assistant')
  view.rerender(<PaneFixture board={false} />)
  expect(input.closest('[hidden]')).toBeNull()
  expect(screen.getByTestId('source')).toContainElement(input)
  view.rerender(<PaneFixture board />)
  expect(screen.getByRole('region', { name: 'Conversation' })).toContainElement(input)
  expect(openStream).toHaveBeenCalledOnce()
})

it('A2-27 retains one Escape action and one author retry shortcut', async () => {
  vi.useFakeTimers()
  const view = render(<PaneFixture board={false} />)
  await flush()
  act(() => openStream.mock.calls[0][2].onEvent({ type: 'turn_started', turn_id: 't1', data: {} }))
  view.rerender(<PaneFixture board />)
  const closeStart = vi.fn()
  const windowKey = vi.fn(ladderListener(
    { startOpen: true },
    (state) => ({ ...state, onCloseStart: closeStart }),
    vi.fn(),
  ))
  window.addEventListener('keydown', windowKey)
  try {
    await act(async () => fireEvent.keyDown(document, { key: 'Escape' }))
    expect(cancelTurn).toHaveBeenCalledOnce()
    expect(windowKey).not.toHaveBeenCalled()
    expect(closeStart).not.toHaveBeenCalled()

    fireEvent.keyDown(window, { key: 'Escape' })
    expect(windowKey).toHaveBeenCalledOnce()
    expect(closeStart).toHaveBeenCalledOnce()
  } finally {
    window.removeEventListener('keydown', windowKey)
  }
  view.unmount()
  const onAuthor = vi.fn().mockRejectedValue(new Error('Connection lost'))
  const author = render(<PaneFixture pane="authoring" onAuthor={onAuthor} seed="Count panels" seedSignal={1} />)
  await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Generate tool' })))
  author.rerender(<PaneFixture pane="authoring" board={false} onAuthor={onAuthor} seed="Count panels" seedSignal={1} />)
  fireEvent.keyDown(screen.getByLabelText('What should the tool do?'), { key: 'R' })
  expect(onAuthor).toHaveBeenCalledOnce()
  await act(async () => fireEvent.keyDown(document, { key: 'R' }))
  expect(onAuthor).toHaveBeenCalledTimes(2)
  author.unmount()
  fireEvent.keyDown(document, { key: 'R' })
  expect(onAuthor).toHaveBeenCalledTimes(2)
})

it('A2-30 denies attachment without entitlement and permits it when entitlement returns', async () => {
  const view = render(<OpeningFixture canConverse={false} />)
  await flush()
  expect(ensureSession).not.toHaveBeenCalled()
  expect(postMessage).not.toHaveBeenCalled()
  expect(openStream).not.toHaveBeenCalled()
  expect(screen.queryByLabelText('Reply to the assistant')).toBeNull()

  view.rerender(<OpeningFixture canConverse />)
  await flush()
  expect(ensureSession).toHaveBeenCalledTimes(1)
  expect(ensureSession).toHaveBeenCalledWith(null, 'p1')
  expect(screen.getByLabelText('Reply to the assistant')).toBeInTheDocument()
  expect(postMessage).not.toHaveBeenCalled()
})

it('A2-28 preserves conversation entitlement and author build gates', async () => {
  const view = render(<PaneFixture canConverse={false} />)
  expect(screen.queryByLabelText('Reply to the assistant')).toBeNull()
  expect(openStream).not.toHaveBeenCalled()
  const onAuthor = vi.fn()
  view.rerender(<PaneFixture pane="authoring" onAuthor={onAuthor} buildEntitled={false}
    seed="Count panels" seedSignal={1} seedAutoSubmit />)
  await flush()
  expect(screen.getByRole('button', { name: 'Generate tool' })).toBeDisabled()
  expect(screen.getByText(/Upgrade to build/)).toBeInTheDocument()
  expect(onAuthor).not.toHaveBeenCalled()
})

it.each([
  { ...liveContext, signedIn: false, sessionStatus: 'required' },
  { ...liveContext, mock: true, signedIn: false },
])("A2-33 the rail's author form mounts while its section is open, signed in or not", (context) => {
  const onAuthor = vi.fn()
  render(<PaneFixture board={false} pane="authoring" context={context} onAuthor={onAuthor} />)
  expect(screen.getByLabelText('What should the tool do?')).toBeInTheDocument()
  expect(onAuthor).not.toHaveBeenCalled()
})

it.each([
  { mock: false, signedIn: false, sessionStatus: 'required' },
  { mock: true, signedIn: false, sessionStatus: 'active' },
  { mock: false, signedIn: true, sessionStatus: 'signed_out' },
])('A2-35 a non-live open author section mounts only at its rail source: %j', (context) => {
  const seats = (authorSource) => deriveBoardPaneSeats({
    ...context, boardHostsProject: false, projectPane: null, authorOpen: true,
    authorDestination: 'board', authorFallback: 'fb', canConverse: true,
    agentMode: 'primary', projectId: 'p1', drawingId: 'd1', sessionId: 's1', authorSource,
  })
  expect(seats(null).authorMounted).toBe(false)
  const author = seats('src')
  expect(author.authorMounted).toBe(true)
  expect(author.authorTarget).toBe('src')
})

it.each([
  { mock: false, signedIn: true, sessionStatus: 'active' },
])('A2-35 a live open author section keeps its fallback seat with a collapsed rail: %j', (context) => {
  const author = deriveBoardPaneSeats({
    ...context, boardHostsProject: false, projectPane: null, authorOpen: true,
    authorDestination: 'board', authorFallback: 'fb', canConverse: true,
    agentMode: 'primary', projectId: 'p1', drawingId: 'd1', sessionId: 's1', authorSource: null,
  })
  expect(author.authorMounted).toBe(true)
  expect(author.authorTarget).toBe('fb')
})

it.each([
  { mock: false, signedIn: false, sessionStatus: 'required' },
  { mock: true, signedIn: false, sessionStatus: 'active' },
  { mock: false, signedIn: true, sessionStatus: 'signed_out' },
])('A2-35 a closed non-live author section stays unmounted even with a rail source: %j', (context) => {
  const author = deriveBoardPaneSeats({
    ...context, boardHostsProject: false, projectPane: null, authorOpen: false,
    authorDestination: 'board', authorFallback: 'fb', canConverse: true,
    agentMode: 'primary', projectId: 'p1', drawingId: 'd1', sessionId: 's1', authorSource: 'src',
  })
  expect(author.authorMounted).toBe(false)
})

it.each([
  [{ mock: false, signedIn: false, sessionStatus: 'active' }, true],
  [{ mock: false, signedIn: true, sessionStatus: 'active' }, true],
  [{ mock: false, signedIn: true, sessionStatus: 'required' }, false],
  [{ mock: false, signedIn: false, sessionStatus: 'required' }, false],
  [{ mock: false, signedIn: false, sessionStatus: 'checking' }, false],
  [{ mock: true, signedIn: false, sessionStatus: 'active' }, false],
  [{ mock: false, signedIn: true, sessionStatus: 'checking' }, false],
  [{ mock: true, signedIn: false, sessionStatus: 'required' }, false],
])('A2-36 the conversation seat follows the session status, not a stored token: %j', (context, mounted) => {
  const seats = (extra) => deriveBoardPaneSeats({
    ...context, boardHostsProject: false, projectPane: null, authorOpen: true,
    authorDestination: 'board', authorFallback: 'fb', canConverse: true,
    agentMode: 'primary', projectId: 'p1', drawingId: 'd1', sessionId: 's1', authorSource: null, ...extra,
  })
  expect(seats({}).conversationMounted).toBe(mounted)
  expect(seats({ canConverse: false }).conversationMounted).toBe(false)
  expect(seats({ agentMode: null }).conversationMounted).toBe(false)
  expect(seats({ sessionId: null }).conversationMounted).toBe(false)
  const live = !context.mock && context.signedIn && context.sessionStatus === 'active'
  expect(seats({}).annotationEnabled).toBe(live)
  expect(seats({}).authorMounted).toBe(live)
})

it('author content override distinguishes empty from omitted content', () => {
  const view = render(<NavRail activeSurface="browser" authorOpen authorContent={null} />)
  expect(screen.queryByLabelText('What should the tool do?')).toBeNull()
  view.rerender(<NavRail activeSurface="browser" authorOpen />)
  expect(screen.getByLabelText('What should the tool do?')).toBeInTheDocument()
})
