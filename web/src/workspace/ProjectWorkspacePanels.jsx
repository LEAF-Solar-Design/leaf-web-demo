import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import JobRail from '../components/JobRail.jsx'
import ReceiptPanel, { deepRedact } from '../projects/ReceiptPanel.jsx'
import ShipReceipts from '../ios/ShipReceipts.jsx'
import '../site/projectBoard.css'

export const BOARD_PANE_REASONS = Object.freeze({
  conversationDemo: 'Conversation is unavailable in this offline demo.',
  annotationsDemo: 'Annotations are unavailable in this offline demo.',
  authoringDemo: 'Tool authoring is unavailable in this offline demo.',
  signIn: 'Sign in to use this pane.',
  project: 'Open a project to use this pane.',
  drawing: 'Mount a drawing to view annotations.',
  session: "Open Conversation to start this project's session.",
  opening: 'Opening conversation.',
  failed: 'Conversation could not be opened.',
  loading: 'Loading annotations.',
  empty: 'No annotations yet.',
})

export function boardPaneReason(pane, { mock, signedIn, projectId, drawingId, sessionId }) {
  if (mock) return BOARD_PANE_REASONS[`${pane}Demo`]
  if (!signedIn) return BOARD_PANE_REASONS.signIn
  if (!projectId) return BOARD_PANE_REASONS.project
  if (pane === 'annotations' && !drawingId) return BOARD_PANE_REASONS.drawing
  if (pane === 'annotations' && !sessionId) return BOARD_PANE_REASONS.session
  return null
}

export function deriveBoardPaneSeats({
  mock, signedIn, sessionStatus, projectId, drawingId, sessionId,
  boardHostsProject, projectPane, canConverse, agentMode, authorOpen,
  conversationSource, conversationDestination, annotationSource,
  annotationDestination, authorSource, authorDestination, authorFallback,
}) {
  const live = !mock && signedIn && sessionStatus === 'active'
  const boardPaneContext = {
    mock, signedIn: signedIn && sessionStatus === 'active',
    projectId, drawingId, sessionId,
  }
  const boardConversation = boardHostsProject && projectPane === 'conversation'
  const boardAnnotations = boardHostsProject && projectPane === 'annotations'
  const boardAuthor = boardHostsProject && projectPane === 'authoring'
  const conversationEligible = !boardPaneReason('conversation', boardPaneContext) && canConverse
  const authorEligible = !boardPaneReason('authoring', boardPaneContext)
  return {
    boardPaneContext, boardConversation, boardAnnotations, boardAuthor,
    conversationEligible, authorEligible,
    annotationEnabled: Boolean(live && projectId && drawingId && sessionId),
    conversationMounted: Boolean(live && canConverse && agentMode && sessionId),
    // A signed-out or demo console mounts the form only where the rail renders its open section (authorSource), as
    // before #1874; a live session keeps the persistent seat, so a collapsed rail moves it to the fallback. The board
    // pane is gated by its own eligibility, which already requires a live signed-in session.
    authorMounted: Boolean((authorOpen && (live || authorSource)) || (boardAuthor && authorEligible)),
    conversationTarget: boardConversation && conversationEligible
      ? conversationDestination : conversationSource,
    annotationTarget: boardAnnotations ? annotationDestination : annotationSource,
    authorTarget: boardAuthor && authorEligible
      ? authorDestination : authorSource || authorFallback,
  }
}

export function BoardPaneState({ pane, context, onOpenConversation, children }) {
  const reason = boardPaneReason(pane, context)
  if (!reason) return children
  return <div><p>{reason}</p>{reason === BOARD_PANE_REASONS.session &&
    <button type="button" onClick={onOpenConversation}>Open Conversation</button>}</div>
}

// The portal target and React position never change. Only its DOM placement does.
export function PersistentSeat({ destination, children }) {
  const [container] = useState(() => document.createElement('div'))
  useLayoutEffect(() => {
    if (destination && container.parentNode !== destination) destination.appendChild(container)
    if (!destination) container.remove()
  }, [container, destination])
  useLayoutEffect(() => () => container.remove(), [container])
  return createPortal(children, container)
}

export function useBoardConversation({ active, eligible, sessionId, context, attach, onOpen }) {
  const [attempt, setAttempt] = useState(null)
  const pending = useRef(null)
  const current = useRef(context)
  current.current = context
  const alive = useRef(true)
  useEffect(() => {
    alive.current = true
    return () => { alive.current = false }
  }, [])
  const retry = () => setAttempt(null)
  useEffect(() => {
    if (sessionId) {
      if (attempt) setAttempt(null)
      return
    }
    if (!active || !eligible || sessionId || (attempt?.context === context)
        || pending.current?.context === context) return
    const request = { context }
    pending.current = request
    setAttempt({ context, status: 'opening' })
    const attachment = attach()
    onOpen()
    attachment.then(() => {
      if (alive.current && current.current === context) setAttempt({ context, status: 'ready' })
    }, () => {
      if (alive.current && current.current === context) setAttempt({ context, status: 'failed' })
    }).finally(() => {
      if (pending.current === request) pending.current = null
    })
  }, [active, eligible, sessionId, context, attach, onOpen, attempt])
  return { status: attempt?.context === context ? attempt.status : 'opening', retry }
}

export function ConversationOpening({ status, onRetry }) {
  return status === 'failed'
    ? <div><p>{BOARD_PANE_REASONS.failed}</p><button type="button" onClick={onRetry}>Retry</button></div>
    : <p>{BOARD_PANE_REASONS.opening}</p>
}

export function AnnotationPaneState({ annotations, children }) {
  if (annotations.annotation) return children
  if (annotations.error) return <div><p role="alert">{annotations.error}</p>
    <button type="button" onClick={annotations.reload}>Retry</button></div>
  return <p>{annotations.loaded ? BOARD_PANE_REASONS.empty : BOARD_PANE_REASONS.loading}</p>
}

export const PANE_NAMES = Object.freeze([
  'material', 'versions', 'conversation', 'tools', 'catalog', 'jobs', 'receipts',
  'annotations', 'authoring', 'settings',
])

const CAPABILITY_PANES = Object.freeze({
  conversation: 'conversation', annotations: 'annotations', authoring: 'authoring',
  approvals: 'annotations', versions: 'versions', receipts: 'receipts',
  marathons: 'jobs', 'one-shot execution': 'jobs',
})

export function paneForCapability(name) {
  return Object.hasOwn(CAPABILITY_PANES, name) ? CAPABILITY_PANES[name] : null
}

function pretty(value) {
  let text
  try {
    text = JSON.stringify(deepRedact(value), null, 2) ?? 'No result yet.'
  } catch {
    return 'Result could not be rendered.'  // a value JSON cannot serialize; never a crash, never [object Object]
  }
  return text.length > 4000 ? `${text.slice(0, 4000)}…` : text
}

function JobDetail({ job }) {
  return (
    <section className="ground-job-detail" aria-label="Job detail">
      <h3>{job.tool_name || job.kind || 'Job detail'}</h3>
      <dl>
        <dt>Status</dt><dd>{job.status || 'Unknown'}</dd>
        <dt>Created</dt><dd>{job.created_at || 'Not recorded'}</dd>
        <dt>Updated</dt><dd>{job.updated_at || 'Not recorded'}</dd>
        {job.cost_usd != null && <><dt>Cost (USD)</dt><dd>${job.cost_usd}</dd></>}
        <dt>Input version</dt><dd>{job.input_version_id || 'Not recorded'}</dd>
        <dt>Output version</dt><dd>{job.output_version_id || 'Not recorded'}</dd>
      </dl>
      <pre>{pretty(job.result)}</pre>
    </section>
  )
}

export default function ProjectWorkspacePanels({
  project, workspace, pane, onSelectPane, onBack, receipts, shipReceipts, slots,
  onOpenVersion, onSelectJob, currentJob, mock,
}) {
  if (!PANE_NAMES.includes(pane)) return null
  const title = pane[0].toUpperCase() + pane.slice(1)
  const material = workspace?.drawing_artifacts || []
  const versions = [...(workspace?.drawing_versions || [])].sort((a, b) =>
    (Date.parse(b.created_at) || 0) - (Date.parse(a.created_at) || 0) || (b.seq || 0) - (a.seq || 0))
  const tools = workspace?.built_tools || []
  let content
  switch (pane) {
    case 'material':
      content = material.length ? <ul>{material.map((row, index) => (
        <li key={row.artifact_id || row.drawing_id || index}>
          <strong>{row.name || row.drawing_id}</strong> · {row.status || 'Not recorded'} · {row.created_at || 'Not recorded'}
        </li>
      ))}</ul> : <p>No material attached yet. Upload a drawing to attach it.</p>
      break
    case 'versions':
      content = versions.length ? <ul>{versions.map((version) => (
        <li key={version.version_id}>
          {onOpenVersion ? <button type="button" className="ground-row-action" onClick={() => onOpenVersion(version)}>v{version.seq} · {version.version_id}</button>
            : <>v{version.seq} · {version.version_id}</>}
        </li>
      ))}</ul> : <p>No versions yet.</p>
      break
    case 'tools':
      content = tools.length ? <ul>{tools.map((tool, index) => (
        <li key={tool.tool_id || tool.name || index}>
          <strong>{tool.name || tool.tool_id}</strong> · {tool.version || 'Version not recorded'}
          {tool.provenance != null && <pre>{pretty(tool.provenance)}</pre>}
        </li>
      ))}</ul> : <p>No built tools yet. Author one from the Manage tab.</p>
      break
    case 'jobs':
      content = <>
        <JobRail jobs={[...(workspace?.jobs || [])].reverse()} currentJob={currentJob} onSelectJob={onSelectJob} mock={mock} />
        {currentJob && <JobDetail job={currentJob} />}
      </>
      break
    case 'receipts':
      content = receipts?.length || shipReceipts?.length ? <>
        {!!receipts?.length && <ReceiptPanel receipts={receipts} />}
        {!!shipReceipts?.length && <ShipReceipts receipts={shipReceipts} />}
      </> : <p>No receipts yet.</p>
      break
    default:
      content = slots?.[pane] ?? <p>{title} is not mounted on this surface.</p>
  }
  return (
    <section className="ground-pane" data-pane={pane} aria-label={title}>
      <header className="ground-pane-head">
        <div><h2>{title}</h2><p>{(project || workspace?.project)?.name || (project || workspace?.project)?.label || 'No project open'}</p></div>
        <button type="button" onClick={onBack}>Back to board</button>
      </header>
      {content}
    </section>
  )
}
