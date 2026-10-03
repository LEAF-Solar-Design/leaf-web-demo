import { useEffect, useRef, useState } from 'react'
import SchemaForm, { defaultsOf } from '../components/SchemaForm.jsx'
import { solarFormKeys, solarView } from './solarView.js'
import { solarFlowPrefill } from './solarFlowModel.js'
import SolarPresetForm from './SolarPresetForm.jsx'
import { PRESET_TOOL } from './solarPresetModel.js'

export default function SolarToolForm({ tool, onSubmit, onClose, presetListing, presetRevision, readIntake, drawingId = null, drawingVersion = null }) {
  if (tool?.name === PRESET_TOOL) {
    return <PresetSolarToolForm tool={tool} onSubmit={onSubmit} onClose={onClose}
      presetListing={presetListing} presetRevision={presetRevision} readIntake={readIntake}
      drawingId={drawingId} drawingVersion={drawingVersion} />
  }
  return <GenericSolarToolForm tool={tool} onSubmit={onSubmit} onClose={onClose}
    readIntake={readIntake} drawingId={drawingId} drawingVersion={drawingVersion} />
}

function ownsKey(record, key) {
  return record !== null && typeof record === 'object' && Object.prototype.hasOwnProperty.call(record, key)
}

function PresetSolarToolForm({ tool, onSubmit, onClose, presetListing, presetRevision, readIntake, drawingId, drawingVersion }) {
  const binding = useRef(null)
  const counter = useRef(0)
  const [readState, setReadState] = useState(null)
  if (!binding.current || binding.current.tool !== tool || binding.current.drawingId !== drawingId
    || binding.current.drawingVersion !== drawingVersion || binding.current.readIntake !== readIntake) {
    binding.current = { tool, drawingId, drawingVersion, readIntake, key: `${drawingId}:${drawingVersion}:${++counter.current}` }
  }
  const bound = binding.current
  const matches = readState?.key === bound.key
  const reading = typeof readIntake === 'function' && (!matches || readState.pending)
  const autoRevision = typeof readIntake === 'function' && matches ? readState.autoRevision : null

  useEffect(() => {
    if (typeof readIntake !== 'function') return undefined
    let current = true
    setReadState({ key: bound.key, pending: true, autoRevision: null })
    Promise.resolve()
      .then(() => readIntake(drawingId, drawingVersion))
      .then((result) => {
        if (!current || binding.current !== bound || result === null || typeof result !== 'object'
          || ![Object.prototype, null].includes(Object.getPrototypeOf(result)) || result.version !== drawingVersion) return
        const prefill = solarFlowPrefill(tool, result.intake?.solar_design_graph?.rev)
        if (!ownsKey(prefill, 'expected_rev')) return
        setReadState({ key: bound.key, pending: true,
          autoRevision: Object.freeze({ key: bound.key, value: prefill.expected_rev }) })
      })
      .catch(() => {})
      .finally(() => {
        if (current && binding.current === bound) setReadState((previous) => ({ ...previous, pending: false }))
      })
    return () => { current = false }
  }, [tool, drawingId, drawingVersion, readIntake])

  return <SolarPresetForm tool={tool} onSubmit={onSubmit} onClose={onClose}
    listing={presetListing} revision={presetRevision} autoRevision={autoRevision} reading={reading} />
}

// expected_rev starts as the drawing's own graph revision, read from the intake
// of the version this form was opened on (the step editor's rule, solarFlowPrefill),
// and only while the drafter has not typed one. No loader, no prefill: the form is
// then exactly the schema defaults it always was.
function GenericSolarToolForm({ tool, onSubmit, onClose, readIntake, drawingId, drawingVersion }) {
  const { view } = solarView(tool)
  const schema = {
    ...tool.params,
    properties: Object.fromEntries(solarFormKeys(tool, view).map((key) => [key, tool.params.properties[key]])),
  }
  const [values, setValues] = useState(() => defaultsOf(schema))
  const revTouched = useRef(false)
  const revAutomatic = useRef(false)
  const revisionKey = useRef({ tool, drawingId, drawingVersion })
  const [readState, setReadState] = useState(null)
  const text = tool.label || tool.name
  const takesRev = ownsKey(schema.properties, 'expected_rev') && ownsKey(solarFlowPrefill(tool, 0), 'expected_rev')
  const reading = takesRev && typeof readIntake === 'function' && (!readState
    || readState.tool !== tool || readState.drawingId !== drawingId || readState.drawingVersion !== drawingVersion
    || readState.readIntake !== readIntake || readState.pending)

  useEffect(() => {
    const previousKey = revisionKey.current
    if (previousKey.tool !== tool || previousKey.drawingId !== drawingId || previousKey.drawingVersion !== drawingVersion) {
      if (revAutomatic.current) {
        const defaultRev = defaultsOf(schema).expected_rev
        setValues((previous) => {
          const next = { ...previous }
          if (defaultRev === undefined) delete next.expected_rev
          else next.expected_rev = defaultRev
          return next
        })
        revAutomatic.current = false
      }
      revisionKey.current = { tool, drawingId, drawingVersion }
    }
    if (!takesRev || typeof readIntake !== 'function') return undefined
    let current = true
    const key = { tool, drawingId, drawingVersion, readIntake }
    setReadState({ ...key, pending: true })
    Promise.resolve()
      .then(() => readIntake(drawingId, drawingVersion))
      .then((intake) => {
        if (intake === null || typeof intake !== 'object'
          || ![Object.prototype, null].includes(Object.getPrototypeOf(intake)) || intake.version !== drawingVersion) return
        const prefill = solarFlowPrefill(tool, intake.intake?.solar_design_graph?.rev)
        if (!current || revTouched.current || !ownsKey(prefill, 'expected_rev')) return
        revAutomatic.current = true
        setValues((previous) => ({ ...previous, expected_rev: prefill.expected_rev }))
      })
      .catch(() => {})
      .finally(() => { if (current) setReadState({ ...key, pending: false }) })
    return () => { current = false }
  }, [tool, takesRev, readIntake, drawingId, drawingVersion])

  function change(next) {
    if (ownsKey(next, 'expected_rev') && next.expected_rev !== values.expected_rev) {
      revTouched.current = true
      revAutomatic.current = false
    }
    setValues(next)
  }

  return (
    <section
      id="solar-tool-form"
      className="solar-tool-form tool-body"
      aria-label={`${text} parameters`}
      onKeyDown={(event) => {
        if (event.key === 'Escape') {
          event.stopPropagation()
          onClose()
        }
      }}
    >
      <h3>{text}</h3>
      <SchemaForm schema={schema} values={values} onChange={change} />
      <button type="button" className="chip-act" disabled={reading}
        title={reading ? "Reading this drawing's design revision." : undefined}
        onClick={() => { onSubmit(tool, values); onClose() }}>
        Review & run
      </button>
      <button type="button" className="chip-act" onClick={onClose}>Cancel</button>
    </section>
  )
}
