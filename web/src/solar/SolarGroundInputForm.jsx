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
  ['power', 'Module power (watts)', '715'],
  ['horizontalGap', 'Horizontal module gap (metres)', '0.02'],
  ['verticalGap', 'Vertical module gap (metres)', '0.02'],
  ['orientation', 'Module orientation', 'Portrait'],
  ['rows', 'Module rows', '4'],
  ['columns', 'Module columns', '12'],
  ['tilt', 'Tilt (degrees)', '20'],
  ['azimuth', 'Axis azimuth (degrees)', '180'],
]

function numberToken(token) {
  const text = token.trim()
  return token.length <= 64 && /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(text)
    && Number.isFinite(Number(text)) ? Number(text) : null
}

export default function SolarGroundInputForm({ disabled, onParameters }) {
  const [draft, setDraft] = useState(() => Object.fromEntries(fields.map(([key, , value]) => [key, value])))
  const [units, setUnits] = useState('m')
  const [edited, setEdited] = useState(false)
  const description = useId()
  const result = useMemo(() => {
    const errors = {}
    const addError = (keys, message) => {
      for (const key of keys) errors[key] = [errors[key], message].filter(Boolean).join(' ')
    }
    const values = {}
    for (const [key, label] of fields) {
      if (key === 'name' || key === 'orientation') continue
      values[key] = numberToken(draft[key])
      if (values[key] === null) addError([key], `${label}: enter a finite number of at most 64 characters.`)
    }
    const name = draft.name.trim()
    if (!name || name.length > 256) addError(['name'], 'Preset name: enter 1 to 256 characters after trimming.')
    if (!['Portrait', 'Landscape'].includes(draft.orientation)) addError(['orientation'], 'Module orientation must be Portrait or Landscape.')
    const check = (key, valid, message) => {
      if (values[key] !== null && !valid(values[key])) addError([key], message)
    }
    for (const key of ['x', 'y']) check(key, n => Math.abs(n) <= 1e9, 'Site origin coordinates must be within ±1,000,000,000.')
    for (const key of ['width', 'height']) check(key, n => n > 0 && n <= 1e6, 'Site width and height must be greater than 0 and at most 1,000,000 drawing units.')
    for (const key of ['length', 'moduleWidth']) check(key, n => n > 0 && n <= 100, 'Module dimensions must be greater than 0 and at most 100 metres.')
    check('power', n => Number.isSafeInteger(n) && n >= 1 && n <= 1000000, 'Module power must be a safe whole number from 1 to 1,000,000 watts.')
    for (const key of ['horizontalGap', 'verticalGap']) check(key, n => n >= 0 && n <= 100, 'Module gaps must be from 0 to 100 metres.')
    for (const key of ['rows', 'columns']) check(key, n => Number.isInteger(n) && n >= 1 && n <= 1000, 'Module rows and columns must be whole numbers from 1 to 1000.')
    if (values.rows !== null && values.columns !== null && values.rows * values.columns > 10000)
      addError(['rows', 'columns'], 'Use at most 10,000 modules per frame.')
    check('tilt', n => n >= 0 && n <= 90, 'Tilt must be from 0 to 90 degrees.')
    check('azimuth', n => n >= 0 && n <= 360, 'Axis azimuth must be from 0 to 360 degrees.')
    const { x, y, width, height } = values
    for (const [origin, size] of [['x', 'width'], ['y', 'height']]) {
      if (values[origin] !== null && values[size] !== null && Math.abs(values[origin] + values[size]) > 1e9)
        addError([origin, size], 'All site corners must be within ±1,000,000,000 drawing units.')
    }
    if (Object.keys(errors).length) return { errors, parameters: null }
    // Native FramePreset decoder keys and BuildDefault values from solar_ground_frames.py.
    const preset = { Name: name, ModuleLengthM: values.length, ModuleWidthM: values.moduleWidth,
      ModuleThicknessM: 0.033, ModulePowerWp: values.power, FramingType: 'FixedTilt', Orientation: draft.orientation,
      Rows: values.rows, Columns: values.columns, TiltDegrees: values.tilt,
      HorizontalGapM: values.horizontalGap, VerticalGapM: values.verticalGap, AxisAzimuthDeg: values.azimuth }
    return { errors, parameters: initialSolarParameters(
      [[x, y], [x + width, y], [x + width, y + height], [x, y + height]], preset, units) }
  }, [draft, units])
  useEffect(() => { onParameters(result.parameters) }, [onParameters, result.parameters])

  return <fieldset disabled={disabled} aria-describedby={description}>
    <legend>Ground layout inputs</legend>
    <p id={description}>Site coordinates and dimensions use drawing units. Module dimensions and gaps use metres; angles use degrees.</p>
    <label htmlFor={`${description}-units`}>Site drawing units</label><select id={`${description}-units`} value={units} onChange={event => { setEdited(true); setUnits(event.target.value) }}>
      <option value="m">Metres</option><option value="ft">Feet</option>
    </select>
    {fields.map(([key, label]) => {
      const id = `${description}-${key}`
      const error = edited && result.errors[key]
      const props = { id, 'aria-invalid': error ? 'true' : undefined,
        'aria-describedby': error ? `${description} ${id}-error` : description,
        value: draft[key], onChange: event => {
          setEdited(true)
          setDraft(current => ({ ...current, [key]: event.target.value }))
        } }
      return <div key={key}><label htmlFor={id}>{label}</label>{key === 'orientation'
        ? <select {...props}><option value="Portrait">Portrait</option><option value="Landscape">Landscape</option></select>
        : <input type="text" {...props}
          inputMode={key === 'name' ? 'text' : 'decimal'}
          maxLength={key === 'name' ? 512 : 64} />}
      {error && <p id={`${id}-error`}>{error}</p>}</div>
    })}
    {edited && Object.keys(result.errors).length > 0 && <p role="alert">{[...new Set(Object.values(result.errors))].join(' ')}</p>}
    {result.parameters && <p>Site {draft.width.trim()} × {draft.height.trim()} {units === 'm' ? 'metres' : 'feet'}
      {' · '}{result.parameters.preset.Rows * result.parameters.preset.Columns} modules per frame.</p>}
    {result.parameters && units === 'ft' && <p>Converted site size: {Number((Number(draft.width) * 0.3048).toFixed(6))} × {Number((Number(draft.height) * 0.3048).toFixed(6))} metres. Module dimensions and gaps remain in metres.</p>}
    <p>Terrain-free layout preview. No terrain or engineering assessment is included.</p>
  </fieldset>
}
