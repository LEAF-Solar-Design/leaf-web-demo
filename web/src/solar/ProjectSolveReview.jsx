import { useEffect, useRef, useState } from 'react'
import SchemaForm, { defaultsOf } from '../components/SchemaForm.jsx'
import { sameSolarDrawingBinding } from './solarDrawingBinding.js'
import { deriveProjectSolarGraphFormState } from './solarProjectGraphFormModel.js'
import { readSolarSolveProposal, readProjectSolarWorkerRefusal, projectSolarWorkerRefusal } from './solarProjectCompletion.js'
import { PROJECT_SOLAR_WORKSPACE_LABELS, PROJECT_SOLAR_WORKSPACE_REASONS } from './ProjectSolarReadPanel.jsx'

const code = (value) => typeof value === 'string' && /^[A-Za-z][A-Za-z0-9_]{0,127}$/.test(value) ? value : null
const current = (guard) => { try { return guard?.() === true } catch { return false } }
const solveSchema = { type: 'object', properties: {
  frame_ref: { type: 'string', title: PROJECT_SOLAR_WORKSPACE_LABELS.frame_label },
  phase: { type: 'string', title: PROJECT_SOLAR_WORKSPACE_LABELS.phase_label, enum: ['initial', 'background'], default: 'initial' },
}, required: ['frame_ref', 'phase'] }

// Both forms hand off to the project runtime. A local operation may be cancelled when its
// editor closes; an accepted job belongs to the captured project scope instead of the editor.
function useSubmission(props, tool) {
  const { solarBinding, authorityKey, projectClient, catalogDigest, newProjectIntentKey,
    onProjectSubmit, isCurrent, busy, submissionSignal, projectOutcome } = props
  const live = useRef(props)
  live.current = props
  const local = useRef(null)
  const [feedback, setFeedback] = useState(null)
  const [pending, setPending] = useState(false)
  const [accepted, setAccepted] = useState(null)
  useEffect(() => {
    const owner = { alive: true, pending: false, accepted: false, controllers: new Set() }
    local.current = owner
    setFeedback(null)
    setPending(false)
    setAccepted(null)
    return () => { owner.alive = false; for (const controller of owner.controllers) controller.abort() }
  }, [solarBinding, authorityKey, projectClient, tool, catalogDigest])
  useEffect(() => {
    const outcome = projectOutcome?.outcome, meta = projectOutcome?.meta
    if (!accepted || !outcome || outcome.jobId !== accepted.jobId || meta?.tool !== tool ||
        meta.authorityKey !== authorityKey || !sameSolarDrawingBinding(meta.binding, solarBinding) ||
        meta.signal?.aborted || !current(meta.isCurrent) || !current(accepted.current)) return
    setFeedback({ state: outcome.state, reason: code(outcome.reasonCode) })
    if (['failed', 'refused'].includes(outcome.state)) local.current.accepted = false
  }, [accepted, projectOutcome, solarBinding, authorityKey, tool])
  async function submit(params, extras = {}) {
    const owner = local.current
    if (!owner?.alive || owner.pending || owner.accepted || live.current.busy ||
        typeof onProjectSubmit !== 'function' || typeof newProjectIntentKey !== 'function' ||
        submissionSignal?.aborted || !current(isCurrent)) return
    const controller = new AbortController()
    owner.controllers.add(controller)
    owner.pending = true
    setPending(true)
    setFeedback(null)
    let handedOff = false
    let captured = null
    const guard = () => {
      try {
        const snapshot = projectClient.getSnapshot()
        return current(isCurrent) && current(captured) && !submissionSignal?.aborted &&
          sameSolarDrawingBinding(snapshot.binding, solarBinding) && snapshot.authorityKey === authorityKey
      } catch { return false }
    }
    try {
      captured = projectClient.captureCatalogScope()
      const response = await projectClient.readContext({ signal: controller.signal })
      if (!owner.alive || controller.signal.aborted || live.current.busy || !guard()) return
      const state = deriveProjectSolarGraphFormState({ binding: solarBinding,
        contextView: response?.ok === true ? response.data : null })
      if (state.mode !== 'edit') { setFeedback({ state: 'refused', reason: code(state.reason) }); return }
      const request = { tool, params: { ...params, drawing_id: solarBinding.drawing_id, expected_rev: state.rev },
        catalogDigest, idempotencyKey: newProjectIntentKey(), ...extras }
      if (tool === 'solar-solve-proposal') {
        // Graph revision is a transport binding for a proposal, never a solver parameter.
        delete request.params.expected_rev
        request.solveContext = { ...extras.solveContext, expected_rev: state.rev }
      }
      const intent = projectClient.prepareRun(request)
      if (!owner.alive || live.current.busy || !guard()) return
      handedOff = true
      owner.accepted = true
      const responseRun = await onProjectSubmit(intent, { binding: solarBinding, authorityKey,
        signal: submissionSignal, isCurrent: guard })
      if (!owner.alive || !guard()) return
      if (readProjectSolarWorkerRefusal(responseRun)) {
        const refusal = projectSolarWorkerRefusal(tool)
        owner.accepted = false
        setAccepted(null)
        setFeedback({ state: 'refused', reason: refusal.reasonCode, message: refusal.message })
      } else if (responseRun?.ok === true && responseRun.status === 202) {
        setAccepted({ jobId: responseRun.data.job_id, current: guard })
        setFeedback({ state: 'submitted', reason: null })
      } else {
        const reason = code(responseRun?.envelope?.error?.reason_code) ?? code(responseRun?.reason)
        const definite = responseRun?.ok === false && ((responseRun.status >= 400 && responseRun.status < 500) ||
          (responseRun.status == null && reason && !['SIP_R13B_OUTCOME_UNKNOWN', 'SIP_R7_TRANSPORT_FAILED',
            'SIP_R7_TIMEOUT', 'SIP_R7_RESPONSE_INVALID'].includes(reason)))
        owner.accepted = !definite
        setFeedback({ state: definite ? 'refused' : 'outcome_unknown', reason })
      }
    } catch {
      if (owner.alive && guard()) setFeedback({ state: handedOff ? 'outcome_unknown' : 'refused', reason: null })
    } finally {
      owner.controllers.delete(controller)
      owner.pending = false
      if (owner.alive) setPending(false)
    }
  }
  return { submit, feedback, locked: busy || pending || !!local.current?.accepted ||
    typeof onProjectSubmit !== 'function' || !current(isCurrent) }
}

function Feedback({ value }) {
  if (!value) return null
  const text = value.message ?? (value.state === 'submitted' || value.state === 'pending' ? PROJECT_SOLAR_WORKSPACE_REASONS.run_submitted
    : value.state === 'succeeded' ? PROJECT_SOLAR_WORKSPACE_REASONS.run_published
      : value.state === 'proposal' ? PROJECT_SOLAR_WORKSPACE_REASONS.proposal_ready
        : value.state === 'outcome_unknown' ? PROJECT_SOLAR_WORKSPACE_REASONS.run_outcome_unknown
          : PROJECT_SOLAR_WORKSPACE_REASONS.run_refused)
  return <p role="status">{text}{value.reason && <> ({value.reason})</>}</p>
}

export function ProjectSolarSchemaForm(props) {
  const { tool, onClose, draft, onDraftChange } = props
  const schema = tool.params_schema ?? tool.params
  const plain = (value) => value !== null && typeof value === 'object' && Object.getPrototypeOf(value) === Object.prototype
  const copy = (value) => Array.isArray(value) ? value.map(copy)
    : plain(value) ? Object.fromEntries(Object.entries(value).map(([key, item]) => [key, copy(item)])) : value
  const validDraft = plain(draft) && plain(draft.params) && plain(draft.context)
  const [params, setParams] = useState(() => validDraft ? copy(draft.params) : defaultsOf(schema))
  const [context, setContext] = useState(() => validDraft ? copy(draft.context) : { frame_ref: '', phase: 'initial' })
  const [valid, setValid] = useState(true)
  const run = useSubmission(props, tool.name)
  const solve = tool.name === 'solar-solve-proposal'
  const required = (schema?.required ?? []).filter((key) => !['drawing_id', 'expected_rev'].includes(key))
  const editable = { ...schema, properties: Object.fromEntries(Object.entries(schema?.properties ?? {})
    .filter(([key]) => !['drawing_id', 'expected_rev'].includes(key))) }
  const complete = required.every((key) => Object.hasOwn(params, key)) && (!solve ||
    (typeof context.frame_ref === 'string' && !!context.frame_ref.trim() && ['initial', 'background'].includes(context.phase)))
  return <form aria-label={solve ? PROJECT_SOLAR_WORKSPACE_LABELS.solve_action : PROJECT_SOLAR_WORKSPACE_LABELS.correct_strings_action} onSubmit={(event) => {
    event.preventDefault()
    if (complete && valid) void run.submit(params, solve ? { solveContext: context } : {})
  }}>
    <h3>{solve ? PROJECT_SOLAR_WORKSPACE_LABELS.solve_action : PROJECT_SOLAR_WORKSPACE_LABELS.correct_strings_action}</h3>
    <fieldset disabled={run.locked}>
      <SchemaForm schema={editable} values={params} onChange={(next) => {
        setParams(next)
        onDraftChange?.({ params: copy(next), context: copy(context) })
      }} onValidityChange={setValid} />
      {solve && <SchemaForm schema={solveSchema} values={context} onChange={(next) => {
        setContext(next)
        onDraftChange?.({ params: copy(params), context: copy(next) })
      }} />}
      <button type="submit" disabled={!complete || !valid}>{solve ? PROJECT_SOLAR_WORKSPACE_LABELS.run_solve_action : PROJECT_SOLAR_WORKSPACE_LABELS.correct_strings_action}</button>
    </fieldset>
    <Feedback value={run.feedback} />
    <button type="button" onClick={onClose}>{PROJECT_SOLAR_WORKSPACE_LABELS.close_action}</button>
  </form>
}

export default function ProjectSolveReview({ proposal, onDiscard, onSolveAgain, ...props }) {
  let candidate = null
  try {
    if (proposal?.state !== 'proposal') throw new TypeError('Invalid proposal outcome')
    candidate = readSolarSolveProposal({ schema: 'leaf.solar-solve-proposal.v1',
      candidate_sha256: proposal.candidateSha256, frame_id: proposal.frameId,
      phase: proposal.phase, candidate_summary: proposal.summary }, proposal.jobId)
  } catch { /* Invalid outcomes never enable a commit. */ }
  const run = useSubmission(props, 'solar-commit-solve')
  const failure = run.feedback && ['failed', 'refused'].includes(run.feedback.state)
  if (!candidate) return <p role="status">{PROJECT_SOLAR_WORKSPACE_REASONS.proposal_unreadable}</p>
  const rows = candidate.summary.slice(0, 200)
  return <section aria-label={PROJECT_SOLAR_WORKSPACE_LABELS.proposal_review_label}>
    <h3>{PROJECT_SOLAR_WORKSPACE_LABELS.proposal_review_title}</h3>
    <p>{PROJECT_SOLAR_WORKSPACE_REASONS.proposal_unpublished}</p>
    <dl><dt>{PROJECT_SOLAR_WORKSPACE_LABELS.frame_label}</dt><dd>{candidate.frameId}</dd><dt>{PROJECT_SOLAR_WORKSPACE_LABELS.phase_label}</dt><dd>{candidate.phase}</dd>
      <dt>{PROJECT_SOLAR_WORKSPACE_LABELS.string_count_label}</dt><dd>{candidate.summary.length}</dd></dl>
    <table><caption>{PROJECT_SOLAR_WORKSPACE_LABELS.proposed_strings_label}</caption><thead><tr><th>{PROJECT_SOLAR_WORKSPACE_LABELS.string_label}</th><th>{PROJECT_SOLAR_WORKSPACE_LABELS.panels_label}</th><th>{PROJECT_SOLAR_WORKSPACE_LABELS.start_label}</th><th>{PROJECT_SOLAR_WORKSPACE_LABELS.end_label}</th></tr></thead>
      <tbody>{rows.map((row, index) => <tr key={index}><td>{index + 1}</td><td>{row.panel_count}</td>
        <td>{row.endpoints[0]}</td><td>{row.endpoints[1]}</td></tr>)}</tbody></table>
    {candidate.summary.length > rows.length && <p>{PROJECT_SOLAR_WORKSPACE_REASONS.proposal_more_strings.replace('{count}', String(candidate.summary.length - rows.length))}</p>}
    <button type="button" disabled={run.locked || failure} onClick={() => void run.submit({}, { proposalJobId: candidate.jobId })}>{PROJECT_SOLAR_WORKSPACE_LABELS.commit_proposal_action}</button>
    <button type="button" disabled={props.busy} onClick={onDiscard}>{PROJECT_SOLAR_WORKSPACE_LABELS.discard_action}</button>
    <Feedback value={run.feedback} />
    {failure && <button type="button" disabled={props.busy} onClick={onSolveAgain}>{PROJECT_SOLAR_WORKSPACE_LABELS.solve_again_action}</button>}
  </section>
}
