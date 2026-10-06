import React, { useEffect, useId, useRef, useState } from 'react'
import {
  presetFormSpec, presetListing, presetDefaults, presetDrafts, parsePresetField,
  fieldMessage, buildPresetParams, SOLAR_PRESET_REASONS, SOLAR_PRESET_NOTES,
} from './solarPresetModel.js'
import './solarPresetForm.css'
import returnToolFocus from './returnToolFocus.js'
import useEscapeOwner from '../lib/useEscapeOwner.js'

export default function SolarPresetForm({ tool, onSubmit, onClose, listing, revision, autoRevision = null, reading = false }) {
  const spec = presetFormSpec(tool?.params)
  const view = spec.ok && listing !== undefined && listing !== null ? presetListing(listing, spec) : null
  const text = tool.label || tool.name
  const id = useId()
  const lock = useRef(false)
  const revisionTouched = useRef(false)
  const revisionAutomatic = useRef(null)
  const revisionExplicit = useRef(spec.ok && Number.isSafeInteger(revision)
    && revision >= spec.revision.min && revision <= spec.revision.max)
  const [revisionDraft, setRevisionDraft] = useState(() => spec.ok && Number.isSafeInteger(revision)
    && revision >= spec.revision.min && revision <= spec.revision.max ? String(revision) : '')
  const [subcommand, setSubcommand] = useState('Create')
  const [name, setName] = useState('')
  const [prefix, setPrefix] = useState('')
  const [mode, setMode] = useState('drawing')
  const [drafts, setDrafts] = useState(() => spec.ok
    ? presetDrafts(spec, view?.ok && view.current !== null ? view.current : presetDefaults(spec)) : {})
  const [blurred, setBlurred] = useState({})
  const [attempted, setAttempted] = useState(false)
  const result = buildPresetParams(spec, { revision: revisionDraft, subcommand, name, prefix, mode, drafts, listing: view })

  useEffect(() => {
    if (revisionExplicit.current || revisionTouched.current) return
    if (revisionAutomatic.current && (!autoRevision || revisionAutomatic.current.key !== autoRevision.key)) {
      setRevisionDraft('')
      revisionAutomatic.current = null
    }
    if (spec.ok && Number.isSafeInteger(autoRevision?.value)
      && autoRevision.value >= spec.revision.min && autoRevision.value <= spec.revision.max) {
      setRevisionDraft(String(autoRevision.value))
      revisionAutomatic.current = autoRevision
    }
  }, [autoRevision, spec.ok, spec.revision?.min, spec.revision?.max])

  function submit(event) {
    event.preventDefault()
    if (reading) return
    setAttempted(true)
    if (result.ok && !lock.current) {
      lock.current = true
      onSubmit(tool, result.params)
      onClose()
    }
  }

  function changeField(key, value) {
    setDrafts((previous) => ({ ...previous, [key]: value }))
  }

  function cancel() {
    onClose()
    returnToolFocus(tool.name)
  }

  // S27: Escape from inside the form cancels it through the one owner stack
  // (edit layer, scoped to the form as the old section handler was).
  const formRef = useRef(null)
  useEscapeOwner('solar-preset-form', true, cancel, { layer: 'edit', scope: formRef, scoped: true })

  return (
    <section ref={formRef} id="solar-tool-form" className="solar-tool-form tool-body solar-preset-form"
      aria-label={`${text} parameters`} data-testid="solar-preset-form">
      <h3>{text}</h3>
      {!spec.ok ? <>
        <p role="status" data-testid="solar-preset-reason">{SOLAR_PRESET_REASONS['preset_declaration_unsupported']}</p>
        <button type="button" className="chip-act" onClick={cancel}>Cancel</button>
      </> : <>
        {view && !view.ok && <p role="status" data-testid="solar-preset-listing">{SOLAR_PRESET_REASONS['listing_unreadable']}</p>}
        {view?.ok && <section aria-label="Presets on this drawing" data-testid="solar-preset-list">
          {view.presets.length > 0 ? <ul>
            {view.presets.map((preset) => <li key={preset.prefix}>{`${preset.prefix} ${preset.name}${preset.active ? ' (active)' : ''}`}</li>)}
          </ul> : <p>No presets on this drawing yet.</p>}
          {view.unfit.map((key) => {
            const field = spec.fields.find((candidate) => candidate.key === key)
            const value = view.current[key]
            return <p key={key} data-testid="solar-preset-unfit">
              {field.kind === 'text'
                ? `${field.label} on this drawing has ${Array.from(value).length} characters, and a preset stores at most ${field.maxLength}.`
                : `${field.label} on this drawing is ${value}, outside ${field.min} to ${field.max}.`}
              {' '}<code>{String(value)}</code>
            </p>
          })}
        </section>}
        <form noValidate onSubmit={submit}>
          <label className="param"><span>Graph revision</span>
            <input type="text" inputMode="numeric" value={revisionDraft} onChange={(event) => {
              revisionTouched.current = true
              setRevisionDraft(event.target.value)
            }} />
          </label>
          <label className="param"><span>Action</span>
            <select value={subcommand} onChange={(event) => setSubcommand(event.target.value)}>
              {spec.subcommands.map((command) => <option key={command} value={command}>{command}</option>)}
            </select>
          </label>
          {subcommand === 'Create' ? <label className="param"><span>Preset name</span>
            <input type="text" value={name} onChange={(event) => setName(event.target.value)} />
          </label> : <label className="param"><span>Preset prefix</span>
            <input type="text" value={prefix} onChange={(event) => setPrefix(event.target.value)} />
          </label>}
          <fieldset>
            <legend>Preset settings</legend>
            <label><input type="radio" name="solar-preset-mode" value="drawing" checked={mode === 'drawing'}
              onChange={() => setMode('drawing')} />{"Use the drawing's current settings"}</label>
            <label><input type="radio" name="solar-preset-mode" value="supply" checked={mode === 'supply'}
              onChange={() => setMode('supply')} />Supply the settings</label>
            {mode === 'supply' && <>
              <p className="solar-preset-note">{SOLAR_PRESET_NOTES['supply_sync']}</p>
              {['StringLayer', 'HomeRunLayer', 'PanelGroupLayer'].some((key) => Object.hasOwn(drafts, key) && drafts[key] === '')
                && <p className="solar-preset-note">{SOLAR_PRESET_NOTES['layer_blank']}</p>}
              <div className="solar-preset-fields">
                {spec.fields.map((field) => {
                  const parsed = parsePresetField(field, drafts[field.key])
                  const show = !parsed.ok && ((Object.hasOwn(blurred, field.key) && blurred[field.key]) || attempted)
                  const messageId = `${id}-${field.key}-message`
                  if (field.kind === 'boolean') return <label className="param" key={field.key}>
                    <input type="checkbox" checked={drafts[field.key]}
                      onChange={(event) => changeField(field.key, event.target.checked)} />{field.label}
                  </label>
                  return <div key={field.key}>
                    <label className="param"><span>{field.label}</span>
                      <input type="text" value={drafts[field.key]}
                        inputMode={field.kind === 'integer' ? 'numeric' : field.kind === 'number' ? 'decimal' : undefined}
                        onChange={(event) => changeField(field.key, event.target.value)}
                        onBlur={() => setBlurred((previous) => ({ ...previous, [field.key]: true }))}
                        aria-invalid={show ? 'true' : undefined} aria-describedby={show ? messageId : undefined} />
                    </label>
                    {show && <span className="solar-preset-field-message" id={messageId}>{fieldMessage(field, parsed.problem, drafts[field.key])}</span>}
                  </div>
                })}
              </div>
            </>}
          </fieldset>
          {!result.ok && <p role="status" data-testid="solar-preset-reason">{SOLAR_PRESET_REASONS[result.reason]}</p>}
          <button type="submit" className="chip-act" disabled={reading || !result.ok}
            title={reading ? "Reading this drawing's design revision." : undefined}>Review & run</button>
          <button type="button" className="chip-act" onClick={cancel}>Cancel</button>
        </form>
      </>}
    </section>
  )
}
