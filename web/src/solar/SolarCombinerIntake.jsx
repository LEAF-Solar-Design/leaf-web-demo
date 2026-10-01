import { useId, useLayoutEffect, useRef, useState } from 'react'
import { COMBINER_INTAKE_MAX_BYTES, combinerIntakeReason } from './solarCombinerIntakeClient.js'
import { placementParams, validateHardware } from './solarCombinerIntakeModel.js'

const IMPORT_FIRST = 'Import the combiner intake for this drawing first'
const FIELDS = [
  ['model', 'Model'], ['max_dc_voltage', 'DC voltage'], ['max_ac_power_kw', 'AC power'],
]

export default function SolarCombinerIntake({ drawingId, projectId, client, onStored, onRunPlacement, disabled = false }) {
  const id = useId()
  const [file, setFile] = useState(null)
  const [raw, setRaw] = useState({ model: '', max_dc_voltage: '', max_ac_power_kw: '' })
  const [stored, setStored] = useState(null)
  const [pending, setPending] = useState(false)
  const [announcement, setAnnouncement] = useState('')
  const active = useRef(null)
  const token = useRef(0)
  const mounted = useRef(false)
  const currentDrawing = useRef(drawingId)
  const importButton = useRef(null)

  useLayoutEffect(() => {
    mounted.current = true
    currentDrawing.current = drawingId
    setStored(null)
    setPending(false)
    setAnnouncement('')
    return () => {
      mounted.current = false
      token.current += 1
      active.current?.controller.abort()
      active.current = null
    }
  }, [drawingId])

  const ready = stored !== null && stored.drawing_id === drawingId
  const validated = validateHardware(raw)
  const params = ready && validated.ok ? placementParams(stored.graph_rev, validated.hardware) : null
  // Validate each field independently so every invalid input has its own reason.
  const errors = Object.fromEntries(FIELDS.map(([field]) => {
    const result = validateHardware({ model: 'Combiner', max_dc_voltage: '480', max_ac_power_kw: '10', [field]: raw[field] })
    return [field, result.ok ? null : result.reason]
  }))

  async function importFile() {
    if (disabled || active.current || !file) return
    if (file.size > COMBINER_INTAKE_MAX_BYTES) {
      setAnnouncement(combinerIntakeReason('COMBINER_IMPORT_TOO_LARGE'))
      importButton.current?.focus()
      return
    }
    const request = { controller: new AbortController(), token: ++token.current, drawingId }
    active.current = request
    setPending(true)
    setAnnouncement('')
    const result = await client.importCombinerIntake({ drawingId, projectId, file, signal: request.controller.signal })
    if (!mounted.current || request.token !== token.current || request.drawingId !== currentDrawing.current) return
    active.current = null
    setPending(false)
    if (result.ok) {
      onStored?.(result.value)
      setStored(result.value)
      setAnnouncement('Combiner intake imported for this drawing.')
    } else {
      setAnnouncement(`${combinerIntakeReason(result.code)}${result.retryable ? '. This import can be retried.' : ''}`)
    }
    // The pending render enables the button before focus is restored.
  }

  useLayoutEffect(() => {
    if (!pending && announcement) importButton.current?.focus()
  }, [pending, announcement])

  return (
    <section aria-label="Combiner intake">
      <label htmlFor={`${id}-file`}>Combiner intake file</label>
      <input id={`${id}-file`} type="file" accept=".json,application/json" disabled={disabled || pending}
        onChange={(event) => setFile(event.target.files?.[0] ?? null)} />
      <button ref={importButton} type="button" disabled={disabled || pending || !file} onClick={importFile}>Import</button>
      <p role="status" aria-live="polite">{announcement}</p>
      <fieldset disabled={disabled}>
        <legend>Combiner hardware</legend>
        {FIELDS.map(([field, label]) => (
          <div key={field}>
            <label htmlFor={`${id}-${field}`}>{label}</label>
            <input id={`${id}-${field}`} type="text" value={raw[field]}
              aria-invalid={Boolean(errors[field])} aria-describedby={errors[field] ? `${id}-${field}-reason` : undefined}
              onChange={(event) => setRaw((previous) => ({ ...previous, [field]: event.target.value }))} />
            {errors[field] && <p id={`${id}-${field}-reason`}>{errors[field]}</p>}
          </div>
        ))}
        {!ready && <p id={`${id}-import-first`}>{IMPORT_FIRST}</p>}
        <button type="button" disabled={disabled || pending || !params}
          aria-describedby={!ready ? `${id}-import-first` : undefined}
          onClick={() => { if (!disabled && !pending && params) onRunPlacement?.(params) }}>Run placement</button>
      </fieldset>
    </section>
  )
}
