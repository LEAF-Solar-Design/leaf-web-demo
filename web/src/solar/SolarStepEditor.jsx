import { useEffect, useRef, useState } from 'react'
import SchemaForm, { defaultsOf } from '../components/SchemaForm.jsx'
import SolarConductorForm from './SolarConductorForm.jsx'
import SolarSizingForm from './SolarSizingForm.jsx'
import SolarStringComposer from './SolarStringComposer.jsx'
import { STRING_ADD_TOOL, STRING_MULTI_ADD_TOOL } from './solarStringComposerModel.js'
import { solarFormKeys, solarView } from './solarView.js'
import { solarFlowPrefill, solarFlowStepId, projectSolarRailReason } from './solarFlowModel.js'
import { ProjectSolarEquipmentForm } from './SolarToolForm.jsx'
import useEscapeOwner from '../lib/useEscapeOwner.js'
import { humanizeError } from '../errorHumanize.js'
import { SOLAR_REFUSAL_REASONS } from '../lib/ribbonClusters.js'
import { SOLAR_SIZING_RUN_REASONS } from './solarSizingRunReasons.js'

function ownsKey(record, key) {
  return record !== null && typeof record === 'object' && Object.prototype.hasOwnProperty.call(record, key)
}

// One Solar step's inputs. Values start from the schema defaults, then the
// drawing's graph revision (only while expected_rev is untouched), then the
// inputs this step last submitted, so a failed run keeps what the drafter typed.
// Review & run arms the confirm path and leaves the editor open; a failed run
// offers Retry with the same values. Cancel returns focus to the step's rail button.
export default function SolarStepEditor(props) {
  const project = Object.prototype.hasOwnProperty.call(props, 'solarBinding') || props.projectId != null
  const projectProps = project ? {
    solarBinding: props.solarBinding, authorityKey: props.authorityKey, projectClient: props.projectClient,
    catalogDigest: props.catalogDigest, newProjectIntentKey: props.newProjectIntentKey, busy: props.busy ?? false,
    onProjectSubmit: props.busy && ['solar-size-strings', 'solar-string-conductors'].includes(props.row?.name)
      ? undefined : props.onProjectSubmit,
  } : {}
  if (project) {
    return <fieldset disabled={props.busy ?? false} data-testid="project-solar-step-boundary"
      aria-describedby={props.busy ? 'project-solar-step-busy' : undefined}>
      {props.busy && <p id="project-solar-step-busy" role="status">{projectSolarRailReason('run_in_progress')}</p>}
      {!['solar-string-conductors', 'solar-size-strings', STRING_ADD_TOOL, STRING_MULTI_ADD_TOOL].includes(props.row?.name)
        ? <ProjectSolarEquipmentForm {...props} {...projectProps} tool={props.row} surface="step" />
        : <SolarStepInputs {...props} projectId={props.projectId ?? true} projectProps={projectProps} />}
    </fieldset>
  }
  // The existing specialised hosts still use projectId to refuse their legacy loaders.
  // Keep that guard active when the caller supplies only the canonical project props.
  return <SolarStepInputs {...props} projectId={project ? props.projectId ?? true : props.projectId} projectProps={projectProps} />
}

function solarStepFailureSentence(code) {
  if (typeof code !== 'string' || !code) return humanizeError('This run failed. Your inputs are kept.')
  const key = code.toLowerCase()
  const cloudReason = {
    cloud_auth_missing: 'grant_missing', cloud_tenant_unauthorized: 'grant_scope',
    cloud_upstream_failure: 'cloud_unavailable', cloud_response_invalid: 'cloud_response_invalid',
    cloud_request_invalid: 'request_invalid',
  }[key]
  const proposalRefused = ['solve_proposal_stale', 'solve_proposal_not_succeeded', 'solve_proposal_not_found',
    'solve_proposal_manifest_mismatch', 'solve_proposal_evidence_invalid', 'solve_proposal_required',
    'solve_proposal_invalid', 'invalid_solve_candidate', 'stale_solve_result'].includes(key)
  const message = key === 'stale_rev'
    ? 'The drawing changed. Review it before trying again.'
    : humanizeError((proposalRefused && 'The server refused this proposal. Run the solve again.')
      || (Object.hasOwn(SOLAR_SIZING_RUN_REASONS, cloudReason) && SOLAR_SIZING_RUN_REASONS[cloudReason])
      || (Object.hasOwn(SOLAR_REFUSAL_REASONS, key) && SOLAR_REFUSAL_REASONS[key]) || SOLAR_REFUSAL_REASONS.unlisted)
  return `${message.replace(/[.!?]+$/, '')}. Your inputs are kept.`
}

function SolarStepInputs({
  row, drawingId = null, drawingVersion = null, projectId = null, readIntake, retained = null,
  status = null, failureCode = null, onSubmit, onClose, projectProps,
}) {
  const { view } = solarView(row)
  const properties = row?.params?.properties || {}
  const schema = {
    ...row.params,
    properties: Object.fromEntries(solarFormKeys(row, view).map((key) => [key, properties[key]])),
  }
  const retainedValues = retained !== null && typeof retained === 'object' && !Array.isArray(retained) ? retained : {}
  const [values, setValues] = useState(() => ({ ...defaultsOf(schema), ...retainedValues }))
  const [formValid, setFormValid] = useState(true)
  const revTouched = useRef(ownsKey(retainedValues, 'expected_rev'))
  const text = row.label || row.name
  const pending = status === 'pending'

  useEffect(() => {
    if (['solar-string-conductors', 'solar-size-strings', STRING_ADD_TOOL, STRING_MULTI_ADD_TOOL].includes(row.name) || typeof readIntake !== 'function' || Object.keys(solarFlowPrefill(row, 0)).length === 0) return undefined
    let current = true
    Promise.resolve()
      .then(() => readIntake(drawingId, drawingVersion))
      .then((intake) => {
        if (intake === null || typeof intake !== 'object'
          || ![Object.prototype, null].includes(Object.getPrototypeOf(intake)) || intake.version !== drawingVersion) return
        const rev = intake.intake?.solar_design_graph?.rev
        const prefill = solarFlowPrefill(row, rev)
        if (!current || revTouched.current || !ownsKey(prefill, 'expected_rev')) return
        setValues((previous) => ({ ...previous, expected_rev: prefill.expected_rev }))
      })
      .catch(() => {})
    return () => { current = false }
  }, [row, drawingId, drawingVersion, readIntake])

  function change(next, { changedKey } = {}) {
    if (changedKey === 'expected_rev') revTouched.current = true
    setValues(next)
  }

  function close() {
    onClose()
    const button = typeof document !== 'undefined' ? document.getElementById(solarFlowStepId(row.name)) : null
    if (button && typeof button.focus === 'function') button.focus()
  }

  // S27: Escape from inside the editor closes it through the one owner stack
  // (edit layer, scoped to the editor as the old section handler was).
  const editorRef = useRef(null)
  useEscapeOwner('solar-step-editor', true, close, { layer: 'edit', scope: editorRef, scoped: true })

  return (
    <section
      ref={editorRef}
      id="solar-step-editor"
      className="solar-step-editor tool-body"
      aria-label={`${text} parameters`}
      data-status={status ?? 'idle'}
    >
      <h3>{text}</h3>
      {row.name === 'solar-string-conductors' ? (
        <SolarConductorForm row={row} drawingId={drawingId} drawingVersion={drawingVersion} projectId={projectId}
          readIntake={readIntake} status={status} failureCode={failureCode} onSubmit={onSubmit} {...projectProps} />
      ) : row.name === 'solar-size-strings' ? (
        <SolarSizingForm row={row} drawingId={drawingId} drawingVersion={drawingVersion} projectId={projectId}
          readIntake={readIntake} status={status} failureCode={failureCode} onSubmit={onSubmit} {...projectProps} />
      ) : [STRING_ADD_TOOL, STRING_MULTI_ADD_TOOL].includes(row.name) ? (
        <SolarStringComposer row={row} drawingId={drawingId} drawingVersion={drawingVersion} projectId={projectId}
          readIntake={readIntake} status={status} failureCode={failureCode} onSubmit={onSubmit} onClose={close} {...projectProps} />
      ) : <>
      <SchemaForm schema={schema} values={values} onChange={change} onValidityChange={setFormValid} />
      {pending && <p role="status" className="solar-step-note">This step is running. Confirm or wait for it to finish.</p>}
      {status === 'failed' && (
        <p role="alert" className="solar-step-note">
          {solarStepFailureSentence(failureCode)}
        </p>
      )}
      <button type="button" className="chip-act" disabled={pending || !formValid} onClick={() => { if (!pending && formValid) onSubmit(row, values) }}>
        Review & run
      </button>
      {status === 'failed' && (
        <button type="button" className="chip-act" disabled={pending || !formValid} onClick={() => { if (!pending && formValid) onSubmit(row, values) }}>Retry</button>
      )}
      </>}
      {![STRING_ADD_TOOL, STRING_MULTI_ADD_TOOL].includes(row.name) && <button type="button" className="chip-act" onClick={close}>Cancel</button>}
    </section>
  )
}

// Retained verbatim for the standalone source compatibility contract.
function StandaloneSolarStepEditor({
  row, drawingId = null, drawingVersion = null, projectId = null, readIntake, retained = null,
  status = null, failureCode = null, onSubmit, onClose, projectProps,
}) {
  const { view } = solarView(row)
  const properties = row?.params?.properties || {}
  const schema = {
    ...row.params,
    properties: Object.fromEntries(solarFormKeys(row, view).map((key) => [key, properties[key]])),
  }
  const retainedValues = retained !== null && typeof retained === 'object' && !Array.isArray(retained) ? retained : {}
  const [values, setValues] = useState(() => ({ ...defaultsOf(schema), ...retainedValues }))
  const [formValid, setFormValid] = useState(true)
  const revTouched = useRef(ownsKey(retainedValues, 'expected_rev'))
  const text = row.label || row.name
  const pending = status === 'pending'

  useEffect(() => {
    if (['solar-string-conductors', 'solar-size-strings', STRING_ADD_TOOL, STRING_MULTI_ADD_TOOL].includes(row.name) || typeof readIntake !== 'function' || Object.keys(solarFlowPrefill(row, 0)).length === 0) return undefined
    let current = true
    Promise.resolve()
      .then(() => readIntake(drawingId, drawingVersion))
      .then((intake) => {
        if (intake === null || typeof intake !== 'object'
          || ![Object.prototype, null].includes(Object.getPrototypeOf(intake)) || intake.version !== drawingVersion) return
        const rev = intake.intake?.solar_design_graph?.rev
        const prefill = solarFlowPrefill(row, rev)
        if (!current || revTouched.current || !ownsKey(prefill, 'expected_rev')) return
        setValues((previous) => ({ ...previous, expected_rev: prefill.expected_rev }))
      })
      .catch(() => {})
    return () => { current = false }
  }, [row, drawingId, drawingVersion, readIntake])

  function change(next, { changedKey } = {}) {
    if (changedKey === 'expected_rev') revTouched.current = true
    setValues(next)
  }

  function close() {
    onClose()
    const button = typeof document !== 'undefined' ? document.getElementById(solarFlowStepId(row.name)) : null
    if (button && typeof button.focus === 'function') button.focus()
  }

  // S27: Escape from inside the editor closes it through the one owner stack
  // (edit layer, scoped to the editor as the old section handler was).
  const editorRef = useRef(null)
  useEscapeOwner('solar-step-editor', true, close, { layer: 'edit', scope: editorRef, scoped: true })

  return (
    <section
      ref={editorRef}
      id="solar-step-editor"
      className="solar-step-editor tool-body"
      aria-label={`${text} parameters`}
      data-status={status ?? 'idle'}
    >
      <h3>{text}</h3>
      {row.name === 'solar-string-conductors' ? (
        <SolarConductorForm row={row} drawingId={drawingId} drawingVersion={drawingVersion} projectId={projectId}
          readIntake={readIntake} status={status} failureCode={failureCode} onSubmit={onSubmit} {...projectProps} />
      ) : row.name === 'solar-size-strings' ? (
        <SolarSizingForm row={row} drawingId={drawingId} drawingVersion={drawingVersion} projectId={projectId}
          readIntake={readIntake} status={status} failureCode={failureCode} onSubmit={onSubmit} {...projectProps} />
      ) : [STRING_ADD_TOOL, STRING_MULTI_ADD_TOOL].includes(row.name) ? (
        <SolarStringComposer row={row} drawingId={drawingId} drawingVersion={drawingVersion} projectId={projectId}
          readIntake={readIntake} status={status} failureCode={failureCode} onSubmit={onSubmit} onClose={close} {...projectProps} />
      ) : <>
      <SchemaForm schema={schema} values={values} onChange={change} onValidityChange={setFormValid} />
      {pending && <p role="status" className="solar-step-note">This step is running. Confirm or wait for it to finish.</p>}
      {status === 'failed' && (
        <p role="alert" className="solar-step-note">
          {failureCode ? `This run failed: ${failureCode}. Your inputs are kept.` : 'This run failed. Your inputs are kept.'}
        </p>
      )}
      <button type="button" className="chip-act" disabled={pending || !formValid} onClick={() => { if (!pending && formValid) onSubmit(row, values) }}>
        Review & run
      </button>
      {status === 'failed' && (
        <button type="button" className="chip-act" disabled={pending || !formValid} onClick={() => { if (!pending && formValid) onSubmit(row, values) }}>Retry</button>
      )}
      </>}
      {![STRING_ADD_TOOL, STRING_MULTI_ADD_TOOL].includes(row.name) && <button type="button" className="chip-act" onClick={close}>Cancel</button>}
    </section>
  )
}
