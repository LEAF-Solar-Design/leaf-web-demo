import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
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
const rectangle = ({ x, y, width, height }) => [[x, y], [x + width, y], [x + width, y + height], [x, y + height]]

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
  const [conversionSource, setConversionSource] = useState(null)
  const [composing, setComposing] = useState(false)
  const compositionActive = useRef(false)
  const [copyMessage, setCopyMessage] = useState('')
  const [copyPending, setCopyPending] = useState(false)
  const copyScope = useRef(0)
  const copyInFlight = useRef(false)
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
    const count = ['rows', 'columns'].every(key => values[key] !== null && Number.isInteger(values[key]) && values[key] >= 1 && values[key] <= 1000)
      ? values.rows * values.columns : null
    if (Object.keys(errors).length) return { errors, count, parameters: null }
    // Native FramePreset decoder keys and BuildDefault values from solar_ground_frames.py.
    const preset = { Name: name, ModuleLengthM: values.length, ModuleWidthM: values.moduleWidth,
      ModuleThicknessM: values.thickness, ModulePowerWp: values.power, FramingType: 'FixedTilt', Orientation: draft.orientation,
      Rows: values.rows, Columns: values.columns, TiltDegrees: values.tilt,
      HorizontalGapM: values.horizontalGap, VerticalGapM: values.verticalGap, AxisAzimuthDeg: values.azimuth }
    return { errors, count, parameters: initialSolarParameters(rectangle({ x, y, width, height }), preset, units) }
  }, [draft, units])
  // The bounded producer JSON is both the semantic key and the snapshot source.
  // Callers own their copy, including any mutations they make after delivery.
  const parameters = composing ? null : result.parameters
  const parametersJSON = JSON.stringify(parameters)
  useEffect(() => { onParameters(JSON.parse(parametersJSON)) }, [onParameters, parametersJSON])
  const invalidateCopy = () => {
    copyScope.current += 1
    copyInFlight.current = false
    setCopyPending(false)
    setCopyMessage('')
  }
  useLayoutEffect(() => {
    // Retire outcomes at commit, including disable and unmount. A user-started
    // clipboard write itself cannot be undone.
    invalidateCopy()
    return () => { copyScope.current += 1; copyInFlight.current = false }
  }, [draft, units, disabled, composing])
  const clearConversion = () => { setConversionMessage(''); setConversionSource(null) }
  const copyJSON = async () => {
    if (disabled || compositionActive.current || !parameters || copyInFlight.current) return
    const scope = copyScope.current
    if (typeof navigator.clipboard?.writeText !== 'function') {
      setCopyMessage('Clipboard is unavailable. You can select the generated JSON to copy it.')
      return
    }
    copyInFlight.current = true
    setCopyPending(true)
    setCopyMessage('Copying generated JSON.')
    try {
      await navigator.clipboard.writeText(JSON.stringify(parameters, null, 2))
      if (scope === copyScope.current) setCopyMessage('Generated JSON copied.')
    } catch {
      if (scope === copyScope.current) setCopyMessage('Could not copy generated JSON. You can select the JSON to copy it.')
    } finally {
      if (scope === copyScope.current) { copyInFlight.current = false; setCopyPending(false) }
    }
  }

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
      setConversionSource(null)
      setConversionMessage(`Site units were not converted. ${[...new Set(errors)].join(' ')}`)
      return
    }
    const sourceBoundary = result.parameters?.boundary ?? rectangle(values)
    const targetBoundary = rectangle(target)
    const sourceFactor = units === 'ft' ? metresPerFoot : 1
    const targetFactor = units === 'm' ? metresPerFoot : 1
    // Reconstruct after converting the site values back, preserving the
    // producer's addition order. Compare both paths in common metres exactly.
    const roundtrip = rectangle(Object.fromEntries(siteKeys.map(key => [key,
      units === 'm' ? target[key] * metresPerFoot : target[key] / metresPerFoot])))
    const precisionLoss = sourceBoundary.some((point, corner) => point.some((value, axis) =>
      value * sourceFactor !== targetBoundary[corner][axis] * targetFactor
      || value * sourceFactor !== roundtrip[corner][axis] * sourceFactor))
    invalidateCopy()
    setConversionSource({ boundary: sourceBoundary, units, targetBoundary, targetUnits: units === 'm' ? 'ft' : 'm' })
    setDraft(current => ({ ...current, ...converted }))
    setUnits(units === 'm' ? 'ft' : 'm')
    setConversionMessage(`Site units converted. Module metres and power are unchanged.${precisionLoss ? ' Precision loss: unit conversion changed represented corners. Review the boundary coordinates.' : ''}`)
  }
  const boundary = parameters?.boundary
  const rectangleWidth = boundary ? boundary[1][0] - boundary[0][0] : 0
  const rectangleHeight = boundary ? boundary[3][1] - boundary[0][1] : 0
  const previewScale = Math.max(rectangleWidth, rectangleHeight)
  const previewWidth = boundary ? Math.max(2, 100 * (rectangleWidth / previewScale)) : 0
  const previewHeight = boundary ? Math.max(2, 100 * (rectangleHeight / previewScale)) : 0
  const displayFactor = units === 'ft' ? metresPerFoot : 1
  const rectangleWidthM = rectangleWidth * displayFactor
  const rectangleHeightM = rectangleHeight * displayFactor
  const centre = boundary && boundary[0].map((value, axis) => value + (boundary[2][axis] - value) / 2)
  const renderFields = keys => fields.filter(([key]) => keys.includes(key)).map(([key, label]) => {
    const id = `${description}-${key}`
    const error = edited && result.errors[key]
    const props = { id, 'aria-invalid': error ? 'true' : undefined,
      'aria-describedby': error ? `${description} ${id}-error` : description,
      value: draft[key], onChange: event => {
        if (disabled) return
        invalidateCopy()
        setEdited(true)
        if (siteKeys.includes(key)) clearConversion()
        setDraft(current => ({ ...current, [key]: event.target.value }))
      } }
    return <div key={key}><label htmlFor={id}>{label}</label>{key === 'orientation'
      ? <select {...props}><option value="Portrait">Portrait</option><option value="Landscape">Landscape</option></select>
      : <input type="text" {...props}
        onCompositionStart={key === 'name' ? () => {
          if (disabled || compositionActive.current) return
          invalidateCopy(); compositionActive.current = true; setComposing(true)
        } : undefined}
        onCompositionEnd={key === 'name' ? event => {
          if (disabled || !compositionActive.current) return
          invalidateCopy(); compositionActive.current = false
          setDraft(current => ({ ...current, name: event.target.value }))
          setEdited(true); setComposing(false)
        } : undefined}
        inputMode={key === 'name' ? 'text' : 'decimal'}
        maxLength={key === 'name' ? 512 : 64} />}
    {error && <p id={`${id}-error`}>{error}</p>}</div>
  })

  return <fieldset disabled={disabled} aria-describedby={description}>
    <legend>Ground layout inputs</legend>
    <p id={description}>Site coordinates and dimensions use drawing units. Module dimensions and gaps use metres; angles use degrees. The unit selector reinterprets current numbers in the selected drawing units. Convert site units converts site origins and sizes to preserve geometry; module metres are unchanged.</p>
    <fieldset aria-describedby={description}><legend>Site inputs</legend>
    <label htmlFor={`${description}-units`}>Site drawing units</label><select id={`${description}-units`} aria-describedby={description} value={units} onChange={event => { if (disabled) return; invalidateCopy(); setEdited(true); setUnits(event.target.value); clearConversion() }}>
      <option value="m">Metres</option><option value="ft">Feet</option>
    </select>
    <button type="button" aria-describedby={description} onClick={convertUnits}>Convert site units to {units === 'm' ? 'feet' : 'metres'}</button>
    <button type="button" onClick={() => {
      if (disabled) return
      invalidateCopy(); compositionActive.current = false; setComposing(false)
      setDraft({ ...defaultDraft }); setUnits('m'); setEdited(false); clearConversion()
    }}>Reset structured inputs</button>
    {conversionMessage && <p role="status">{conversionMessage}</p>}
    {renderFields(siteKeys)}
    </fieldset>
    <fieldset><legend>Module inputs</legend>{renderFields(fields.slice(4, 12).map(([key]) => key))}</fieldset>
    <fieldset><legend>Frame inputs</legend>{renderFields(fields.slice(12).map(([key]) => key))}</fieldset>
    {result.count !== null && <p>{result.count <= 10000
      ? `Remaining per-frame modules: ${10000 - result.count} (10,000 module limit).`
      : `Per-frame module count exceeds the 10,000 module limit by ${result.count - 10000}.`}</p>}
    {edited && Object.keys(result.errors).length > 0 && <div role="alert">
      <p>{[...new Set(Object.values(result.errors))].join(' ')}</p>
      <ul>{fields.filter(([key]) => result.errors[key]).map(([key, label]) => <li key={key}>
        <a href={`#${description}-${key}`} aria-disabled={disabled || undefined} tabIndex={disabled ? -1 : undefined} onClick={event => {
          event.preventDefault()
          if (!disabled) document.getElementById(`${description}-${key}`)?.focus()
        }}>{label}</a>
      </li>)}</ul>
    </div>}
    {parameters && <p>Site {String(rectangleWidth)} × {String(rectangleHeight)} {units === 'm' ? 'metres' : 'feet'}
      {' · '}{parameters.preset.Rows * parameters.preset.Columns} modules per frame.</p>}
    {parameters && <p>Per-frame DC watts: {parameters.preset.Rows * parameters.preset.Columns * parameters.preset.ModulePowerWp}. This is the module power sum for one frame.</p>}
    {parameters && <p>Site rectangle area: {rectangleMeasurement(rectangleWidthM * rectangleHeightM)} square metres.
      {' '}Site rectangle perimeter: {rectangleMeasurement(2 * (rectangleWidthM + rectangleHeightM))} metres.</p>}
    {parameters && <p>Site rectangle schematic (not to scale). Narrow dimensions are enlarged for visibility.</p>}
    {parameters && <svg role="img" aria-labelledby={`${description}-preview-title`} aria-describedby={`${description}-preview-desc`} viewBox="0 0 120 120" width="240" height="240">
      <title id={`${description}-preview-title`}>Site rectangle schematic (not to scale)</title>
      <desc id={`${description}-preview-desc`}>Rectangle {String(rectangleWidth)} by {String(rectangleHeight)} {units === 'm' ? 'metres' : 'feet'}, origin X {String(boundary[0][0])}, Y {String(boundary[0][1])}. Schematic only, not to scale; narrow dimensions are enlarged for visibility. No module geometry or terrain assessment.</desc>
      <rect x="10" y="10" width={previewWidth} height={previewHeight} fill="none" stroke="currentColor" />
    </svg>}
    {parameters && units === 'ft' && <p>Converted site size: {rectangleMeasurement(rectangleWidthM)} × {rectangleMeasurement(rectangleHeightM)} metres. Module dimensions and gaps remain in metres.</p>}
    {parameters && <>
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
        <pre aria-label="Generated source input parameters JSON">{JSON.stringify(parameters, null, 2)}</pre>
        <button type="button" disabled={disabled || copyPending} onClick={copyJSON}>Copy generated JSON</button>
        {copyMessage && <p role="status">{copyMessage}</p>}
      </details>
    </>}
    {!composing && conversionSource && <>
      <p>Conversion review compares JavaScript Number representation only. No terrain or engineering assessment.</p>
      <table>
        <caption>Source boundary before conversion (drawing units: {conversionSource.units})</caption>
        <thead><tr><th scope="col">Corner</th><th scope="col">X ({conversionSource.units})</th><th scope="col">Y ({conversionSource.units})</th></tr></thead>
        <tbody>{conversionSource.boundary.map((point, index) => <tr key={index}>
          <th scope="row">Corner {index + 1}</th><td>{String(point[0])}</td><td>{String(point[1])}</td>
        </tr>)}</tbody>
      </table>
      {!parameters && <table>
        <caption>Converted boundary after conversion (drawing units: {conversionSource.targetUnits})</caption>
        <thead><tr><th scope="col">Corner</th><th scope="col">X ({conversionSource.targetUnits})</th><th scope="col">Y ({conversionSource.targetUnits})</th></tr></thead>
        <tbody>{conversionSource.targetBoundary.map((point, index) => <tr key={index}>
          <th scope="row">Corner {index + 1}</th><td>{String(point[0])}</td><td>{String(point[1])}</td>
        </tr>)}</tbody>
      </table>}
    </>}
    <p>Terrain-free layout preview. No terrain or engineering assessment is included.</p>
  </fieldset>
}
