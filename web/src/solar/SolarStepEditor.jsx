import { useEffect, useRef, useState } from 'react'
import SchemaForm, { defaultsOf } from '../components/SchemaForm.jsx'
import SolarConductorForm from './SolarConductorForm.jsx'
import { solarFormKeys, solarView } from './solarView.js'
import { solarFlowPrefill, solarFlowStepId } from './solarFlowModel.js'

function ownsKey(record, key) {
  return record !== null && typeof record === 'object' && Object.prototype.hasOwnProperty.call(record, key)
}

// One Solar step's inputs. Values start from the schema defaults, then the
// drawing's graph revision (only while expected_rev is untouched), then the
// inputs this step last submitted, so a failed run keeps what the drafter typed.
// Review & run arms the confirm path and leaves the editor open; a failed run
// offers Retry with the same values. Cancel returns focus to the step's rail button.
export default function SolarStepEditor({
  row, drawingId = null, drawingVersion = null, projectId = null, readIntake, retained = null,
  status = null, failureCode = null, onSubmit, onClose,
}) {
  const { view } = solarView(row)
  const properties = row?.params?.properties || {}
  const schema = {
    ...row.params,
    properties: Object.fromEntries(solarFormKeys(row, view).map((key) => [key, properties[key]])),
  }
  const retainedValues = retained !== null && typeof retained === 'object' && !Array.isArray(retained) ? retained : {}
  const [values, setValues] = useState(() => ({ ...defaultsOf(schema), ...retainedValues }))
  const revTouched = useRef(ownsKey(retainedValues, 'expected_rev'))
  const text = row.label || row.name
  const pending = status === 'pending'

  useEffect(() => {
    if (row.name === 'solar-string-conductors' || typeof readIntake !== 'function' || Object.keys(solarFlowPrefill(row, 0)).length === 0) return undefined
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

  function change(next) {
    if (ownsKey(next, 'expected_rev') && next.expected_rev !== values.expected_rev) revTouched.current = true
    setValues(next)
  }

  function close() {
    onClose()
    const button = typeof document !== 'undefined' ? document.getElementById(solarFlowStepId(row.name)) : null
    if (button && typeof button.focus === 'function') button.focus()
  }

  return (
    <section
      id="solar-step-editor"
      className="solar-step-editor tool-body"
      aria-label={`${text} parameters`}
      data-status={status ?? 'idle'}
      onKeyDown={(event) => {
        if (event.key === 'Escape') {
          event.stopPropagation()
          close()
        }
      }}
    >
      <h3>{text}</h3>
      {row.name === 'solar-string-conductors' ? (
        <SolarConductorForm row={row} drawingId={drawingId} drawingVersion={drawingVersion} projectId={projectId}
          readIntake={readIntake} status={status} failureCode={failureCode} onSubmit={onSubmit} />
      ) : <>
      <SchemaForm schema={schema} values={values} onChange={change} />
      {pending && <p role="status" className="solar-step-note">This step is running. Confirm or wait for it to finish.</p>}
      {status === 'failed' && (
        <p role="alert" className="solar-step-note">
          {failureCode ? `This run failed: ${failureCode}. Your inputs are kept.` : 'This run failed. Your inputs are kept.'}
        </p>
      )}
      <button type="button" className="chip-act" disabled={pending} onClick={() => onSubmit(row, values)}>
        Review & run
      </button>
      {status === 'failed' && (
        <button type="button" className="chip-act" onClick={() => onSubmit(row, values)}>Retry</button>
      )}
      </>}
      <button type="button" className="chip-act" onClick={close}>Cancel</button>
    </section>
  )
}
