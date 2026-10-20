import { useLayoutEffect, useRef, useState } from 'react'
import { createSolarDrawingBinding, sameSolarDrawingBinding, solarDrawingBindingKey } from './solarDrawingBinding.js'
import SolarSettingsForm from './SolarSettingsForm.jsx'
import ProjectSolarReadPanel, { PROJECT_SOLAR_WORKSPACE_LABELS, projectSolarWorkspaceReason } from './ProjectSolarReadPanel.jsx'
import ProjectSolveReview, { ProjectSolarSchemaForm } from './ProjectSolveReview.jsx'

const STEP_TOOLS = ['solar-size-strings', 'solar-string-add', 'solar-string-multi-add',
  'solar-string-conductors', 'solar-assign-equipment']
const GRAPH_TOOLS = ['solar-panels-from-drawing', 'solar-homeruns', 'solar-schedule', 'solar-feeders']
const READ_TOOLS = ['solar-string-data', 'solar-electrical-schedules', 'solar-cable-export']
const SOLVE_TOOLS = ['solar-solve-proposal', 'solar-commit-solve', 'solar-correct-string']
const DIGEST = new RegExp('^sha256:[0-9a-f]{64}$')
const TOOL_NAME = new RegExp('^[a-z0-9][a-z0-9-]{0,127}$')
const plain = (value) => value !== null && typeof value === 'object' && Object.getPrototypeOf(value) === Object.prototype
const current = (predicate) => {
  try { return typeof predicate === 'function' && predicate() === true } catch { return false }
}
// A dense array: only `length` beside the indices, and every index an own data property.
function denseArray(value) {
  if (!Array.isArray(value) || Reflect.ownKeys(value).length !== value.length + 1) return false
  for (let index = 0; index < value.length; index += 1) {
    const descriptor = Object.getOwnPropertyDescriptor(value, String(index))
    if (!descriptor || !Object.hasOwn(descriptor, 'value')) return false
  }
  return true
}

// Compare JSON declarations independent of property order, refusing accessors and sparse arrays.
function declaration(value, seen = new Set(), depth = 0) {
  if (depth > 64) throw new TypeError('Invalid catalog declaration')
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return value
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if ((!Array.isArray(value) && !plain(value)) || seen.has(value)) throw new TypeError('Invalid catalog declaration')
  seen.add(value)
  const result = Array.isArray(value) ? [] : {}
  const keys = Array.isArray(value) ? Array.from({ length: value.length }, (_, index) => String(index))
    : Reflect.ownKeys(value).sort()
  if (Array.isArray(value) && Reflect.ownKeys(value).length !== value.length + 1) throw new TypeError('Invalid catalog array')
  for (const key of keys) {
    const descriptor = Object.getOwnPropertyDescriptor(value, key)
    if (typeof key !== 'string' || !descriptor?.enumerable || !Object.hasOwn(descriptor, 'value')) {
      throw new TypeError('Invalid catalog declaration')
    }
    Object.defineProperty(result, key, { value: declaration(descriptor.value, seen, depth + 1), enumerable: true })
  }
  seen.delete(value)
  return result
}

function rowDeclaration(row) {
  if (!plain(row) || typeof row.name !== 'string' || !TOOL_NAME.test(row.name) || typeof row.catalog_digest !== 'string' ||
      !DIGEST.test(row.catalog_digest)) throw new TypeError('Invalid catalog row')
  // Validate the entire row as JSON before reading its schema or admission claims.
  const copied = declaration(row)
  const schema = copied.params_schema === undefined ? copied.params : copied.params_schema
  if (!plain(schema) || schema.type !== 'object' || !plain(schema.properties) ||
      Object.values(schema.properties).some((property) => !plain(property) ||
        (property.title !== undefined && typeof property.title !== 'string') ||
        (property.description !== undefined && typeof property.description !== 'string')) ||
      (schema.required !== undefined && (!Array.isArray(schema.required) || schema.required.some((key) => typeof key !== 'string'))) ||
      !plain(copied.availability)) throw new TypeError('Invalid catalog schema')
  if (copied.params !== undefined && copied.params_schema !== undefined &&
      JSON.stringify(copied.params) !== JSON.stringify(copied.params_schema)) throw new TypeError('Conflicting catalog schemas')
  const claims = { ...copied }
  for (const key of ['name', 'catalog_digest', 'params', 'params_schema', 'label', 'title', 'description']) delete claims[key]
  return JSON.stringify([copied.catalog_digest, schema, claims])
}

function describeScope(props) {
  const { solarBinding, authorityKey, projectClient, projectCatalog, isCurrent, submissionSignal } = props
  let binding = null, bindingKey = null, receiptCurrent = null, rows = [], catalogKey = null
  // Validate the caller's binding, then keep its identity so every host receives the object it was given.
  try { createSolarDrawingBinding(solarBinding); binding = solarBinding; bindingKey = solarDrawingBindingKey(solarBinding) } catch { /* refusal */ }
  let reason = binding ? null : 'binding_invalid'
  if (!reason && !current(isCurrent)) reason = 'scope_stale'
  let available = false
  try {
    available = typeof authorityKey === 'string' && !!authorityKey.trim() && new TextEncoder().encode(authorityKey).length <= 256 &&
      !!projectClient && ['getSnapshot', 'subscribe', 'captureCatalogScope'].every((key) => typeof projectClient[key] === 'function')
  } catch { /* refusal */ }
  if (!reason && !available) reason = 'client_unavailable'
  if (!reason) {
    try {
      const snapshot = projectClient.getSnapshot()
      if (!sameSolarDrawingBinding(snapshot?.binding, binding) || snapshot?.authorityKey !== authorityKey) reason = 'scope_stale'
    } catch { reason = 'scope_stale' }
  }
  if (!reason) {
    try {
      receiptCurrent = projectCatalog?.isCurrent
      if (!projectCatalog || !sameSolarDrawingBinding(projectCatalog.binding, binding) ||
          projectCatalog.authorityKey !== authorityKey || projectCatalog.client !== projectClient ||
          !current(receiptCurrent)) throw new TypeError('Invalid catalog receipt')
      // Read each list once and require it dense before any element is read.
      const families = projectCatalog.families
      if (!denseArray(families)) throw new TypeError('Invalid catalog families')
      const collected = []
      for (let index = 0; index < families.length; index += 1) {
        const family = families[index]
        if (!plain(family)) throw new TypeError('Invalid catalog family')
        const capabilities = family.capabilities
        if (!denseArray(capabilities)) throw new TypeError('Invalid catalog family')
        for (let position = 0; position < capabilities.length; position += 1) collected.push(capabilities[position])
      }
      rows = collected
      const declarations = new Map()
      const keys = rows.map((row) => {
        const key = rowDeclaration(row)
        if (declarations.has(row.name) && declarations.get(row.name) !== key) throw new TypeError('Conflicting catalog rows')
        declarations.set(row.name, key)
        return [row.name, key]
      })
      catalogKey = JSON.stringify(keys)
    } catch { reason = 'catalog_unavailable'; rows = [] }
  }
  return { binding, bindingKey, authorityKey, projectClient, projectCatalog, isCurrent, submissionSignal,
    receiptCurrent, rows, catalogKey, reason }
}

function sameScope(left, right) {
  return !!left && ['bindingKey', 'authorityKey', 'projectClient', 'projectCatalog', 'isCurrent',
    'submissionSignal', 'receiptCurrent', 'catalogKey', 'reason'].every((key) => left[key] === right[key]) &&
    left.rows.length === right.rows.length && left.rows.every((row, index) => row === right.rows[index])
}

function hostFor(row, props) {
  if (row?.name === 'solar-settings') return 'settings'
  if (SOLVE_TOOLS.includes(row?.name)) return 'solve'
  if (STEP_TOOLS.includes(row?.name)) return props.StepEditor ? 'step' : 'host_unavailable'
  if (GRAPH_TOOLS.includes(row?.name)) return props.GraphForm ? 'graph' : 'host_unavailable'
  if (READ_TOOLS.includes(row?.name)) return 'read'
  return 'unsupported'
}

// A scope token answers for the props of the render that made it; a later committed scope
// retires it. Render writes nothing a later commit depends on: a render React abandons (a
// suspended transition) leaves the committed token, its accepted runs, the props handlers read
// and the presentation exactly as they were.
function scopeToken(owner, renderProps) {
  const token = { owner, alive: true, isCurrent: null }
  token.isCurrent = () => {
    if (!token.alive || owner.reason || !sameScope(owner, describeScope(renderProps))) {
      token.alive = false
      return false
    }
    return true
  }
  return token
}

export default function ProjectSolarWorkspace(props) {
  const { FlowRail, StepEditor, GraphForm, openTool = null, busy = false, newProjectIntentKey,
    projectRunMemory = null, projectPending = null, projectOutcome = null, submissionSignal } = props
  const latest = useRef(props)
  const owner = describeScope(props)
  const scopeRef = useRef(null)
  const presentation = useRef({ row: openTool, retired: false, generation: 0 })
  const [, repaint] = useState(0)
  const notifications = useRef([])
  const retainedProposal = useRef(null)
  const [discardedProposal, discardProposal] = useState(null)
  const committed = scopeRef.current
  const replacing = !committed || !committed.alive || !sameScope(committed.owner, owner)
  const scope = replacing ? scopeToken(owner, props) : committed
  function validProposal(receipt) {
    try {
      const meta = receipt?.meta
      return receipt?.outcome?.state === 'proposal' && meta?.tool === 'solar-solve-proposal' &&
        meta.authorityKey === owner.authorityKey && sameSolarDrawingBinding(meta.binding, owner.binding) &&
        !meta.signal?.aborted && current(meta.isCurrent) && scope.isCurrent()
    } catch { return false }
  }
  const incoming = validProposal(projectOutcome) ? projectOutcome : null
  const retained = retainedProposal.current?.scope === scope ? retainedProposal.current.receipt : null
  const proposalReceipt = projectPending?.tool === 'solar-solve-proposal' ? null
    : incoming || (validProposal(retained) ? retained : null)
  const proposal = proposalReceipt?.outcome?.jobId === discardedProposal ? null : proposalReceipt?.outcome
  const base = presentation.current
  let view = base
  if (replacing && committed) view = { ...view, retired: true, generation: view.generation + 1 }
  if (view.row !== openTool) view = { row: openTool, retired: false, generation: view.generation + 1 }
  const generation = view.generation
  useLayoutEffect(() => {
    latest.current = props
    retainedProposal.current = proposalReceipt ? { scope, receipt: proposalReceipt } : null
    if (scopeRef.current !== scope) {
      if (scopeRef.current) scopeRef.current.alive = false
      scopeRef.current = scope
    }
    // A close requested while this commit mounted its editor (a child's layout effect runs first)
    // keeps the row this commit shows, so the next render does not reopen it, and the generation
    // never goes back, so a callback the closed editor kept cannot pass for a later editor.
    if (presentation.current === base) presentation.current = view
    else presentation.current = { ...presentation.current, row: view.row,
      generation: Math.max(presentation.current.generation, view.generation) }
    const queued = notifications.current.splice(0)
    for (const notify of queued) notify()
  })
  useLayoutEffect(() => {
    let unsubscribe
    try { unsubscribe = props.projectClient?.subscribe(() => {
      if (!scopeRef.current.isCurrent()) scopeRef.current.alive = false
      repaint((value) => value + 1)
    }) } catch { /* admission renders the refusal */ }
    return () => { try { unsubscribe?.() } catch { /* observer isolation */ } }
  }, [props.projectClient])

  function retire(flow, changingFlow = false) {
    presentation.current = { ...presentation.current, retired: true, generation: presentation.current.generation + 1 }
    const close = latest.current.onClose, change = latest.current.onFlowChange
    notifications.current.push(() => {
      close?.()
      if (changingFlow) change?.(flow)
    })
    repaint((value) => value + 1)
  }
  function admitted(row) {
    const next = describeScope(latest.current)
    return scope.alive && sameScope(owner, next) && !next.reason && !!latest.current.FlowRail &&
      next.rows.includes(row) && !['unsupported', 'host_unavailable'].includes(hostFor(row, latest.current))
  }
  function open(row) {
    if (latest.current.busy || typeof latest.current.onOpenTool !== 'function' || !admitted(row)) return
    presentation.current = { ...presentation.current, retired: false, generation: presentation.current.generation + 1 }
    latest.current.onOpenTool(row)
    repaint((value) => value + 1)
  }
  const selected = view.retired ? null : openTool
  let reason = owner.reason || (!FlowRail ? 'host_unavailable' : null)
  let host = null
  if (!reason && selected) {
    if (!owner.rows.includes(selected)) reason = 'catalog_unavailable'
    else {
      host = hostFor(selected, props)
      if (host === 'unsupported' || host === 'host_unavailable') { reason = host; host = null }
    }
  }
  const canWrite = !busy && typeof newProjectIntentKey === 'function' && typeof props.onProjectSubmit === 'function'
  // A retained callback must still belong to this presentation at handoff. After handoff,
  // observation belongs only to the captured scope, never to this editor or its busy state.
  function submit(prepared, options, runRow = selected) {
    const live = latest.current
    if (!canWrite || live.busy || typeof live.newProjectIntentKey !== 'function' || typeof live.onProjectSubmit !== 'function' ||
        presentation.current.retired || presentation.current.generation !== generation || live.openTool !== selected || !admitted(selected)) return undefined
    let handoff
    try {
      if (!owner.rows.includes(runRow) || !admitted(runRow)) return undefined
      if (runRow !== selected && !(runRow.name === 'solar-commit-solve' && selected?.name === 'solar-solve-proposal')) return undefined
      if (runRow.name === 'solar-commit-solve' && (!proposal || !validProposal(proposalReceipt) ||
          prepared?.proposalJobId !== proposal.jobId)) return undefined
      const fields = ['binding', 'authorityKey', 'tool', 'catalogDigest']
      if (!plain(prepared) || fields.some((key) => {
        const descriptor = Object.getOwnPropertyDescriptor(prepared, key)
        return !descriptor || !Object.hasOwn(descriptor, 'value')
      }) || !current(options?.isCurrent) || options?.signal?.aborted ||
          (options?.signal !== undefined && !(options.signal instanceof AbortSignal)) ||
          !sameSolarDrawingBinding(options?.binding, owner.binding) || options?.authorityKey !== owner.authorityKey ||
          !sameSolarDrawingBinding(prepared?.binding, owner.binding) || prepared?.authorityKey !== owner.authorityKey ||
          prepared?.tool !== runRow.name || prepared?.catalogDigest !== runRow.catalog_digest ||
          live.submissionSignal?.aborted || (live.submissionSignal !== undefined && !(live.submissionSignal instanceof AbortSignal))) return undefined
      const captured = owner.projectClient.captureCatalogScope()
      const guard = () => scope.isCurrent() && current(captured) && !submissionSignal?.aborted
      if (!guard()) return undefined
      handoff = { binding: owner.binding, authorityKey: owner.authorityKey, signal: submissionSignal, isCurrent: guard }
    } catch { return undefined }
    return live.onProjectSubmit(prepared, handoff)
  }
  const shared = { solarBinding: owner.binding, authorityKey: owner.authorityKey, projectClient: owner.projectClient,
    catalogDigest: selected?.catalog_digest, newProjectIntentKey, onProjectSubmit: canWrite ? submit : undefined,
    busy, onClose: () => retire() }
  let editor = null
  if (!reason && host === 'settings') editor = <SolarSettingsForm {...shared} />
  if (!reason && host === 'step') editor = <StepEditor {...shared} row={selected} />
  if (!reason && host === 'graph') editor = <GraphForm {...shared} tool={selected} isCurrent={scope.isCurrent}
    projectOutcome={projectOutcome} submissionSignal={submissionSignal} />
  if (!reason && host === 'read') editor = <ProjectSolarReadPanel tool={selected} solarBinding={owner.binding}
    authorityKey={owner.authorityKey} projectClient={owner.projectClient} projectCatalog={owner.projectCatalog}
    isCurrent={scope.isCurrent} busy={busy} />
  const solveRow = owner.rows.find((row) => row.name === 'solar-solve-proposal')
  const commitRow = owner.rows.find((row) => row.name === 'solar-commit-solve')
  if (!reason && host === 'solve') {
    if (selected.name === 'solar-correct-string' || (selected.name === 'solar-solve-proposal' && !proposal)) {
      editor = <ProjectSolarSchemaForm key={selected.name} {...shared} tool={selected} isCurrent={scope.isCurrent}
        projectOutcome={projectOutcome} submissionSignal={submissionSignal} />
    } else if (proposal) {
      editor = <ProjectSolveReview {...shared} proposal={proposal} catalogDigest={commitRow?.catalog_digest}
        isCurrent={scope.isCurrent} projectOutcome={projectOutcome} submissionSignal={submissionSignal}
        onProjectSubmit={canWrite && commitRow ? (prepared, options) => submit(prepared, options, commitRow) : undefined}
        onDiscard={() => discardProposal(proposal.jobId)} onSolveAgain={() => {
          discardProposal(proposal.jobId)
          if (solveRow) open(solveRow)
        }} />
    } else editor = <div><p role="status">{projectSolarWorkspaceReason('proposal_required')}</p>
      {solveRow && <button type="button" disabled={busy} onClick={() => open(solveRow)}>{PROJECT_SOLAR_WORKSPACE_LABELS.run_solve_action}</button>}</div>
  }
  const opening = !owner.reason && !!FlowRail && typeof props.onOpenTool === 'function'
  const inspectionRows = owner.rows.filter((row, index, rows) =>
    (GRAPH_TOOLS.includes(row.name) || READ_TOOLS.includes(row.name)) && rows.findIndex((item) => item.name === row.name) === index)
  return <section className="project-solar-workspace">
    {FlowRail && <FlowRail solarBinding={owner.binding} authorityKey={owner.authorityKey} projectClient={owner.projectClient}
      projectCatalog={owner.reason ? null : owner.projectCatalog} projectRunMemory={projectRunMemory}
      projectPending={projectPending} busy={busy} openName={editor ? selected.name : null}
      onOpenStep={opening && !busy ? open : undefined} onFlowChange={(flow) => retire(flow, true)} />}
    {inspectionRows.map((row) => <button type="button" key={row.name}
      disabled={!opening || busy || hostFor(row, props) === 'host_unavailable'} onClick={() => open(row)}>
      {typeof row.label === 'string' && row.label ? row.label : row.name}
    </button>)}
    {(reason || busy) && <p role="status">{projectSolarWorkspaceReason(reason || 'busy')}</p>}
    {editor && <div key={generation} className="project-solar-workspace-editor">
      {host === 'read' ? editor : <fieldset disabled={busy || !canWrite}>{editor}</fieldset>}
      {(host === 'read' || busy || !canWrite) && <button type="button" onClick={() => retire()}>Close</button>}
    </div>}
  </section>
}
