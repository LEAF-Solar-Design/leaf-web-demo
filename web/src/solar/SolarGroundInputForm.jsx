import { useEffect, useId, useMemo, useState } from 'react'
import { initialSolarParameters } from './solarProjectMaterializationClient.js'

const fields = [
  ['x', 'Site origin X (drawing units)', '0'],
  ['y', 'Site origin Y (drawing units)', '0'],
  ['width', 'Site width (drawing units)', ''],
  ['height', 'Site height (drawing units)', ''],
  ['name', 'Preset name', 'Default'],
  ['length', 'Module length (metres)', '2.384'],
  ['moduleWidth', 'Module width (metres)', '1.303'],
  ['thickness', 'Module thickness (metres)', '0.033'],
  ['power', 'Module power (watts)', '715'],
  ['horizontalGap', 'Horizontal module gap (metres)', '0.02'],
  ['verticalGap', 'Vertical module gap (metres)', '0.02'],
  ['orientation', 'Module orientation', 'Portrait'],
  ['rows', 'Module rows', '4'],
  ['columns', 'Module columns', '12'],
  ['tilt', 'Tilt (degrees)', '20'],
  ['azimuth', 'Axis azimuth (degrees)', '180'],
]
const defaultDraft = Object.freeze(Object.fromEntries(fields.map(([key, , value]) => [key, value])))
const siteKeys = ['x', 'y', 'width', 'height']
const metresPerFoot = 0.3048

function validateSite(values, addError) {
  for (const key of ['x', 'y']) {
    if (values[key] !== null && Math.abs(values[key]) > 1e9)
      addError([key], 'Site origin coordinates must be within ±1,000,000,000.')
  }
  for (const key of ['width', 'height']) {
    if (values[key] !== null && !(values[key] > 0 && values[key] <= 1e6))
      addError([key], 'Site width and height must be greater than 0 and at most 1,000,000 drawing units.')
  }
  for (const [origin, size] of [['x', 'width'], ['y', 'height']]) {
    if (values[origin] === null || values[size] === null) continue
    const corner = values[origin] + values[size]
    if (!Number.isFinite(corner) || Math.abs(corner) > 1e9)
      addError([origin, size], 'All site corners must be within ±1,000,000,000 drawing units.')
    if (values[size] > 0 && !(corner > values[origin]))
      addError([origin, size], 'Site origin and size must produce distinct corners; this size collapses at the entered origin.')
  }
}

function numberToken(token, integer = false) {
  if (token.length > 64) return null
  const text = token.trim()
  const match = /^[+-]?(\d+(?:\.\d*)?|\.\d+)(?:[eE]([+-]?\d+))?$/.exec(text)
  if (!match) return null
  const digits = match[1].replace('.', '')
  const nonzero = /[1-9]/.test(digits)
  if (integer && nonzero) {
    const decimalPlaces = match[1].includes('.') ? match[1].length - match[1].indexOf('.') - 1 : 0
    const trailingZeros = digits.length - digits.replace(/0+$/, '').length
    // The token bound also bounds this exponent parse. Never expand powers of ten.
    if (BigInt(match[2] || '0') + BigInt(trailingZeros) < BigInt(decimalPlaces)) return null
  }
  const value = Number(text)
  return Number.isFinite(value) && !(value === 0 && nonzero) ? value : null
}

function rectangleMeasurement(value) {
  if (value < 1e-6) return 'below display precision'
  return String(Number(value.toFixed(6)))
}

function invalidNameCharacters(name) {
  if (/[\u0000-\u001f\u007f-\u009f]/.test(name)) return true
  for (let index = 0; index < name.length; index++) {
    const code = name.charCodeAt(index)
    if (code >= 0xd800 && code <= 0xdbff) {
      const next = name.charCodeAt(++index)
      if (!(next >= 0xdc00 && next <= 0xdfff)) return true
    } else if (code >= 0xdc00 && code <= 0xdfff) return true
  }
  return false
}

export default function SolarGroundInputForm({ disabled, onParameters }) {
  const [draft, setDraft] = useState(() => ({ ...defaultDraft }))
  const [units, setUnits] = useState('m')
  const [edited, setEdited] = useState(false)
  const [conversionMessage, setConversionMessage] = useState('')
  const description = useId()
  const result = useMemo(() => {
    const errors = {}
    const addError = (keys, message) => {
      for (const key of keys) errors[key] = [errors[key], message].filter(Boolean).join(' ')
    }
    const values = {}
    for (const [key, label] of fields) {
      if (key === 'name' || key === 'orientation') continue
      const integer = ['power', 'rows', 'columns'].includes(key)
      values[key] = numberToken(draft[key], integer)
      if (values[key] === null) addError([key], `${label}: enter a finite ${integer ? 'whole ' : ''}number of at most 64 characters.`)
    }
    if (invalidNameCharacters(draft.name)) addError(['name'], 'Preset name: control characters and unpaired Unicode surrogates are not allowed.')
    const name = draft.name.trim()
    if (!name || name.length > 256) addError(['name'], 'Preset name: enter 1 to 256 characters after trimming.')
    if (!['Portrait', 'Landscape'].includes(draft.orientation)) addError(['orientation'], 'Module orientation must be Portrait or Landscape.')
    const check = (key, valid, message) => {
      if (values[key] !== null && !valid(values[key])) addError([key], message)
    }
    validateSite(values, addError)
    for (const key of ['length', 'moduleWidth']) check(key, n => n > 0 && n <= 100, 'Module dimensions must be greater than 0 and at most 100 metres.')
    check('thickness', n => n > 0 && n <= 1, 'Module thickness must be greater than 0 and at most 1 metre.')
    check('power', n => Number.isSafeInteger(n) && n >= 1 && n <= 1000000, 'Module power must be a safe whole number from 1 to 1,000,000 watts.')
    for (const key of ['horizontalGap', 'verticalGap']) check(key, n => n >= 0 && n <= 100, 'Module gaps must be from 0 to 100 metres.')
    for (const key of ['rows', 'columns']) check(key, n => Number.isInteger(n) && n >= 1 && n <= 1000, 'Module rows and columns must be whole numbers from 1 to 1000.')
    if (values.rows !== null && values.columns !== null && values.rows * values.columns > 10000)
      addError(['rows', 'columns'], 'Use at most 10,000 modules per frame.')
    check('tilt', n => n >= 0 && n <= 90, 'Tilt must be from 0 to 90 degrees.')
    check('azimuth', n => n >= 0 && n <= 360, 'Axis azimuth must be from 0 to 360 degrees.')
    const { x, y, width, height } = values
    if (Object.keys(errors).length) return { errors, parameters: null }
    // Native FramePreset decoder keys and BuildDefault values from solar_ground_frames.py.
    const preset = { Name: name, ModuleLengthM: values.length, ModuleWidthM: values.moduleWidth,
      ModuleThicknessM: values.thickness, ModulePowerWp: values.power, FramingType: 'FixedTilt', Orientation: draft.orientation,
      Rows: values.rows, Columns: values.columns, TiltDegrees: values.tilt,
      HorizontalGapM: values.horizontalGap, VerticalGapM: values.verticalGap, AxisAzimuthDeg: values.azimuth }
    return { errors, parameters: initialSolarParameters(
      [[x, y], [x + width, y], [x + width, y + height], [x, y + height]], preset, units) }
  }, [draft, units])
  useEffect(() => { onParameters(result.parameters) }, [onParameters, result.parameters])

  const convertUnits = () => {
    if (disabled) return
    const errors = []
    const addError = (keys, message) => errors.push(message)
    const values = Object.fromEntries(siteKeys.map(key => [key, numberToken(draft[key])]))
    if (siteKeys.some(key => values[key] === null)) errors.push('Enter finite site numbers of at most 64 characters before converting.')
    validateSite(values, addError)
    const convertedValues = Object.fromEntries(siteKeys.map(key => [key,
      units === 'm' ? values[key] / metresPerFoot : values[key] * metresPerFoot]))
    if (siteKeys.some(key => values[key] !== null && values[key] !== 0 && convertedValues[key] === 0))
      errors.push('A nonzero site value would become zero in the target units. Keep the current units to preserve exact geometry.')
    const converted = Object.fromEntries(siteKeys.map(key => [key, String(convertedValues[key])]))
    const target = Object.fromEntries(siteKeys.map(key => [key, numberToken(converted[key])]))
    if (siteKeys.some(key => target[key] === null)) errors.push('Converted site numbers must be finite and at most 64 characters.')
    validateSite(target, addError)
    if (errors.length) {
      setConversionMessage(`Site units were not converted. ${[...new Set(errors)].join(' ')}`)
      return
    }
    setDraft(current => ({ ...current, ...converted }))
    setUnits(units === 'm' ? 'ft' : 'm')
    setConversionMessage('Site units converted. Module metres and power are unchanged.')
  }
  const previewScale = Math.max(Number(draft.width), Number(draft.height))
  const previewWidth = result.parameters ? Math.max(2, 100 * (Number(draft.width) / previewScale)) : 0
  const previewHeight = result.parameters ? Math.max(2, 100 * (Number(draft.height) / previewScale)) : 0
  const displayFactor = units === 'ft' ? metresPerFoot : 1
  const rectangleWidthM = Number(draft.width) * displayFactor
  const rectangleHeightM = Number(draft.height) * displayFactor
  const boundary = result.parameters?.boundary
  const centre = boundary && boundary[0].map((value, axis) => value + (boundary[2][axis] - value) / 2)

  return <fieldset disabled={disabled} aria-describedby={description}>
    <legend>Ground layout inputs</legend>
    <p id={description}>Site coordinates and dimensions use drawing units. Module dimensions and gaps use metres; angles use degrees.</p>
    <label htmlFor={`${description}-units`}>Site drawing units</label><select id={`${description}-units`} value={units} onChange={event => { setEdited(true); setUnits(event.target.value); setConversionMessage('') }}>
      <option value="m">Metres</option><option value="ft">Feet</option>
    </select>
    <button type="button" onClick={convertUnits}>Convert site units to {units === 'm' ? 'feet' : 'metres'}</button>
    <button type="button" onClick={() => {
      if (disabled) return
      setDraft({ ...defaultDraft }); setUnits('m'); setEdited(false); setConversionMessage('')
    }}>Reset structured inputs</button>
    {conversionMessage && <p role="status">{conversionMessage}</p>}
    {fields.map(([key, label]) => {
      const id = `${description}-${key}`
      const error = edited && result.errors[key]
      const props = { id, 'aria-invalid': error ? 'true' : undefined,
        'aria-describedby': error ? `${description} ${id}-error` : description,
        value: draft[key], onChange: event => {
          setEdited(true)
          if (siteKeys.includes(key)) setConversionMessage('')
          setDraft(current => ({ ...current, [key]: event.target.value }))
        } }
      return <div key={key}><label htmlFor={id}>{label}</label>{key === 'orientation'
        ? <select {...props}><option value="Portrait">Portrait</option><option value="Landscape">Landscape</option></select>
        : <input type="text" {...props}
          inputMode={key === 'name' ? 'text' : 'decimal'}
          maxLength={key === 'name' ? 512 : 64} />}
      {error && <p id={`${id}-error`}>{error}</p>}</div>
    })}
    {edited && Object.keys(result.errors).length > 0 && <div role="alert">
      <p>{[...new Set(Object.values(result.errors))].join(' ')}</p>
      <ul>{fields.filter(([key]) => result.errors[key]).map(([key, label]) => <li key={key}>
        <a href={`#${description}-${key}`} aria-disabled={disabled || undefined} tabIndex={disabled ? -1 : undefined} onClick={event => {
          event.preventDefault()
          if (!disabled) document.getElementById(`${description}-${key}`)?.focus()
        }}>{label}</a>
      </li>)}</ul>
    </div>}
    {result.parameters && <p>Site {draft.width.trim()} × {draft.height.trim()} {units === 'm' ? 'metres' : 'feet'}
      {' · '}{result.parameters.preset.Rows * result.parameters.preset.Columns} modules per frame.</p>}
    {result.parameters && <p>Per-frame DC watts: {result.parameters.preset.Rows * result.parameters.preset.Columns * result.parameters.preset.ModulePowerWp}. This is the module power sum for one frame.</p>}
    {result.parameters && <p>Site rectangle area: {rectangleMeasurement(rectangleWidthM * rectangleHeightM)} square metres.
      {' '}Site rectangle perimeter: {rectangleMeasurement(2 * (rectangleWidthM + rectangleHeightM))} metres.</p>}
    {result.parameters && <p>Site rectangle schematic (not to scale). Narrow dimensions are enlarged for visibility.</p>}
    {result.parameters && <svg role="img" aria-labelledby={`${description}-preview-title`} aria-describedby={`${description}-preview-desc`} viewBox="0 0 120 120" width="240" height="240">
      <title id={`${description}-preview-title`}>Site rectangle schematic (not to scale)</title>
      <desc id={`${description}-preview-desc`}>Rectangle {draft.width.trim()} by {draft.height.trim()} {units === 'm' ? 'metres' : 'feet'}, origin X {draft.x.trim()}, Y {draft.y.trim()}. Schematic only, not to scale; narrow dimensions are enlarged for visibility. No module geometry or terrain assessment.</desc>
      <rect x="10" y="10" width={previewWidth} height={previewHeight} fill="none" stroke="currentColor" />
    </svg>}
    {result.parameters && units === 'ft' && <p>Converted site size: {rectangleMeasurement(rectangleWidthM)} × {rectangleMeasurement(rectangleHeightM)} metres. Module dimensions and gaps remain in metres.</p>}
    {result.parameters && <>
      <p>Site rectangle centre (drawing coordinates): X {String(centre[0])}, Y {String(centre[1])} {units}.</p>
      <table>
        <caption>Site boundary coordinates (drawing units: {units})</caption>
        <thead><tr><th scope="col">Corner</th><th scope="col">X ({units})</th><th scope="col">Y ({units})</th></tr></thead>
        <tbody>{boundary.map((point, index) => <tr key={index}>
          <th scope="row">Corner {index + 1}</th><td>{String(point[0])}</td><td>{String(point[1])}</td>
        </tr>)}</tbody>
      </table>
      <details>
        <summary>Generated source input parameters (JSON)</summary>
        <p>Read-only source inputs for generation, not a saved result.</p>
        <pre aria-label="Generated source input parameters JSON">{JSON.stringify(result.parameters, null, 2)}</pre>
      </details>
    </>}
    <p>Terrain-free layout preview. No terrain or engineering assessment is included.</p>
  </fieldset>
}
