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
    const errors = []
    const values = {}
    for (const [key, label] of fields) {
      if (key === 'name') continue
      values[key] = numberToken(draft[key])
      if (values[key] === null) errors.push(`${label}: enter a finite number of at most 64 characters.`)
    }
    const name = draft.name.trim()
    if (!name || name.length > 256) errors.push('Preset name: enter 1 to 256 characters after trimming.')
    const check = (key, valid, message) => {
      if (values[key] !== null && !valid(values[key])) errors.push(message)
    }
    for (const key of ['x', 'y']) check(key, n => Math.abs(n) <= 1e9, 'Site origin coordinates must be within ±1,000,000,000.')
    for (const key of ['width', 'height']) check(key, n => n > 0 && n <= 1e6, 'Site width and height must be greater than 0 and at most 1,000,000 drawing units.')
    for (const key of ['length', 'moduleWidth']) check(key, n => n > 0 && n <= 100, 'Module dimensions must be greater than 0 and at most 100 metres.')
    for (const key of ['rows', 'columns']) check(key, n => Number.isInteger(n) && n >= 1 && n <= 1000, 'Module rows and columns must be whole numbers from 1 to 1000.')
    if (values.rows !== null && values.columns !== null && values.rows * values.columns > 10000)
      errors.push('Use at most 10,000 modules per frame.')
    check('tilt', n => n >= 0 && n <= 90, 'Tilt must be from 0 to 90 degrees.')
    check('azimuth', n => n >= 0 && n <= 360, 'Axis azimuth must be from 0 to 360 degrees.')
    const { x, y, width, height } = values
    if ([x, y, width, height].every(n => n !== null)
      && (Math.abs(x + width) > 1e9 || Math.abs(y + height) > 1e9))
      errors.push('All site corners must be within ±1,000,000,000 drawing units.')
    if (errors.length) return { errors, parameters: null }
    // Native FramePreset decoder keys and BuildDefault values from solar_ground_frames.py.
    const preset = { Name: name, ModuleLengthM: values.length, ModuleWidthM: values.moduleWidth,
      ModuleThicknessM: 0.033, ModulePowerWp: 715, FramingType: 'FixedTilt', Orientation: 'Portrait',
      Rows: values.rows, Columns: values.columns, TiltDegrees: values.tilt,
      HorizontalGapM: 0.02, VerticalGapM: 0.02, AxisAzimuthDeg: values.azimuth }
    return { errors, parameters: initialSolarParameters(
      [[x, y], [x + width, y], [x + width, y + height], [x, y + height]], preset, units) }
  }, [draft, units])
  useEffect(() => { onParameters(result.parameters) }, [onParameters, result.parameters])

  return <fieldset disabled={disabled} aria-describedby={description}>
    <legend>Ground layout inputs</legend>
    <p id={description}>Site coordinates and dimensions use drawing units. Module dimensions use metres; angles use degrees.</p>
    <label>Site drawing units<select value={units} onChange={event => { setEdited(true); setUnits(event.target.value) }}>
      <option value="m">Metres</option><option value="ft">Feet</option>
    </select></label>
    {fields.map(([key, label]) => <label key={key}>{label}<input type="text"
      inputMode={key === 'name' ? 'text' : 'decimal'} value={draft[key]}
      maxLength={key === 'name' ? 512 : 64} onChange={event => {
        setEdited(true)
        setDraft(current => ({ ...current, [key]: event.target.value }))
      }} /></label>)}
    {edited && result.errors.length > 0 && <p role="alert">{[...new Set(result.errors)].join(' ')}</p>}
    {result.parameters && <p>Site {draft.width.trim()} × {draft.height.trim()} {units === 'm' ? 'metres' : 'feet'}
      {' · '}{result.parameters.preset.Rows * result.parameters.preset.Columns} modules per frame.</p>}
    <p>Terrain-free layout preview. No terrain or engineering assessment is included.</p>
  </fieldset>
}
