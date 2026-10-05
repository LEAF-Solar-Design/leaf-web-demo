import { useCallback, useEffect, useId, useRef, useState } from 'react'

export function schemaTypes(param) {
  return Array.isArray(param?.type) ? param.type : [param?.type]
}

export function valueMatchesSchemaType(value, param) {
  const types = schemaTypes(param)
  return (value === null && types.includes('null'))
    || (Array.isArray(value) && types.includes('array'))
    || (value !== null && !Array.isArray(value) && typeof value === 'object' && types.includes('object'))
    || (typeof value === 'boolean' && types.includes('boolean'))
    || (typeof value === 'string' && types.includes('string'))
    || (typeof value === 'number' && Number.isFinite(value) && types.includes('number'))
    || (typeof value === 'number' && Number.isInteger(value) && types.includes('integer'))
}

function numericRange(property, integer) {
  const lowers = [], uppers = []
  if (typeof property.minimum === 'number') lowers.push([property.minimum, property.exclusiveMinimum === true])
  if (typeof property.maximum === 'number') uppers.push([property.maximum, property.exclusiveMaximum === true])
  if (typeof property.exclusiveMinimum === 'number') lowers.push([property.exclusiveMinimum, true])
  if (typeof property.exclusiveMaximum === 'number') uppers.push([property.exclusiveMaximum, true])
  if (integer) {
    lowers.forEach((bound) => { bound[0] = bound[1] ? Math.floor(bound[0]) + 1 : Math.ceil(bound[0]); bound[1] = false })
    uppers.forEach((bound) => { bound[0] = bound[1] ? Math.ceil(bound[0]) - 1 : Math.floor(bound[0]); bound[1] = false })
  }
  lowers.sort((a, b) => b[0] - a[0] || Number(b[1]) - Number(a[1]))
  uppers.sort((a, b) => a[0] - b[0] || Number(b[1]) - Number(a[1]))
  const [min, lowerStrict] = lowers[0] || []
  const [max, upperStrict] = uppers[0] || []
  const parts = []
  if (lowerStrict && upperStrict) parts.push(`Must be greater than ${min} and less than ${max}.`)
  else {
    if (min !== undefined) parts.push(lowerStrict ? `Must be greater than ${min}.` : `Minimum: ${min}.`)
    if (max !== undefined) parts.push(upperStrict ? `Must be less than ${max}.` : `Maximum: ${max}.`)
  }
  return { min, max, text: parts.join(' '), invalid: (value) => typeof value === 'number'
    && ((integer && !Number.isInteger(value)) || !Number.isFinite(value)
      || (min !== undefined && (lowerStrict ? value <= min : value < min))
      || (max !== undefined && (upperStrict ? value >= max : value > max))) }
}

function enumValuesEqual(left, right, depth = 0) {
  if (depth > 64) return false
  if ((typeof left === 'number' && !Number.isFinite(left))
    || (typeof right === 'number' && !Number.isFinite(right))) return false
  if (left === null || right === null || typeof left !== 'object' || typeof right !== 'object') {
    return left === right
  }
  if (Array.isArray(left) || Array.isArray(right)) {
    if (!Array.isArray(left) || !Array.isArray(right) || left.length !== right.length) return false
    for (let index = 0; index < left.length; index += 1) {
      if (!enumValuesEqual(left[index], right[index], depth + 1)) return false
    }
    return true
  }
  const keys = Object.keys(left)
  return keys.length === Object.keys(right).length && keys.every((key) =>
    Object.prototype.hasOwnProperty.call(right, key) && enumValuesEqual(left[key], right[key], depth + 1))
}

function ParamField({ property, fieldKey, value, present, required, onChange, onValidityChange }) {
  const id = useId()
  const input = useRef(null)
  const types = schemaTypes(property)
  const label = sentence((property.title || fieldKey).replace(/_/g, ' '))
  const choices = Array.isArray(property.enum) ? property.enum : property.type === 'boolean' ? [false, true] : null
  const json = !choices && (types.includes('object') || types.includes('array'))
  const numeric = !choices && !json && (types.includes('number') || types.includes('integer'))
  const serialized = present ? JSON.stringify(value) : ''
  const [draft, setDraft] = useState(serialized)
  const [jsonError, setJsonError] = useState('')
  useEffect(() => { setDraft(serialized); setJsonError('') }, [serialized])
  const range = numericRange(property, types.includes('integer'))
  // A committed value the current schema refuses (the schema changed under it) blocks the run.
  const declared = types.some((type) => typeof type === 'string')
  const stale = present && (choices ? !choices.some((choice) => enumValuesEqual(choice, value))
    : declared && !valueMatchesSchemaType(value, property))
  const staleError = stale ? `${label} has a value this tool no longer accepts. Choose or enter a new one.` : ''
  const error = json ? jsonError || staleError
    : numeric && range.invalid(value) ? `${label} is outside its allowed range.` : staleError
  useEffect(() => {
    input.current?.setCustomValidity(error)
    onValidityChange(fieldKey, !error)
    return () => onValidityChange(fieldKey, true)
  }, [fieldKey, error, onValidityChange])
  const describedBy = [property.description && `${id}-description`, numeric && range.text && `${id}-range`,
    numeric && present && value === null && `${id}-null`, error && `${id}-error`].filter(Boolean).join(' ')
  const attributes = { ref: input, 'aria-label': label, 'aria-required': required ? 'true' : undefined,
    'aria-invalid': !!error, 'aria-describedby': describedBy || undefined }
  function changeJson(next) {
    setDraft(next)
    if (next.trim() === '') { setJsonError(''); onChange(undefined, true); return }
    let parsed
    try { parsed = JSON.parse(next) } catch { setJsonError(`${label} must contain valid JSON.`); return }
    if (!valueMatchesSchemaType(parsed, property)) {
      const allowed = types.map((type) => type === 'null' ? 'null' : `a JSON ${type}`).join(' or ')
      setJsonError(`${label} must contain ${allowed}.`)
      return
    }
    setJsonError('')
    onChange(parsed)
  }
  return <label className="param">
    <span>{label}{required && <span aria-hidden="true"> *</span>}</span>
    {choices ? <select {...attributes} value={present ? String(choices.findIndex((choice) => enumValuesEqual(choice, value))).replace('-1', '') : ''}
      onChange={(event) => onChange(choices[Number(event.target.value)], event.target.value === '')}>
      <option value="">Choose a value</option>
      {choices.map((choice, index) => <option key={index} value={String(index)}>{Array.isArray(property.enum)
        ? typeof choice === 'string' ? choice : JSON.stringify(choice)
        : choice ? 'True' : 'False'}</option>)}
    </select> : <input {...attributes} type={numeric ? 'number' : 'text'}
      step={numeric ? types.includes('integer') ? '1' : 'any' : undefined}
      min={numeric ? range.min : undefined} max={numeric ? range.max : undefined}
      value={json ? draft : value ?? ''}
      onChange={(event) => json ? changeJson(event.target.value)
        : onChange(numeric ? Number(event.target.value) : event.target.value, event.target.value === '')} />}
    {property.description && <span id={`${id}-description`}>{property.description}</span>}
    {numeric && range.text && <span id={`${id}-range`}>{range.text}</span>}
    {numeric && present && value === null && <span id={`${id}-null`}>Current value: null.</span>}
    {error && <span id={`${id}-error`} role="alert">{error}</span>}
  </label>
}

// Renders a JSON-Schema params object (CONTRACT §2 .params) as a small form.
export default function SchemaForm({ schema, values, onChange, onValidityChange }) {
  const props = schema?.properties || {}
  const keys = Object.keys(props)
  const validity = useRef({})
  const callback = useRef(onValidityChange)
  callback.current = onValidityChange
  const report = useCallback((key, valid) => {
    validity.current[key] = valid
    callback.current?.(Object.values(validity.current).every(Boolean))
  }, [])
  useEffect(() => { callback.current?.(Object.values(validity.current).every(Boolean)) }, [])
  if (keys.length === 0) return <p className="params-none">No parameters.</p>
  return <div className="params">{keys.map((key) => <ParamField key={`${key}:${JSON.stringify(props[key])}`}
    property={props[key]} fieldKey={key} value={values[key]}
    present={Object.prototype.hasOwnProperty.call(values, key)} required={schema?.required?.includes(key)}
    onValidityChange={report} onChange={(next, remove = false) => {
      const nextValues = { ...values }
      if (remove) delete nextValues[key]
      else nextValues[key] = next
      onChange(nextValues, { changedKey: key })
    }} />)}</div>
}

export function defaultsOf(schema) {
  const out = {}
  for (const [k, p] of Object.entries(schema?.properties || {})) {
    if (p.default !== undefined && valueMatchesSchemaType(p.default, p)) out[k] = p.default
  }
  return out
}

// Sentence-case a description: leading capital, rest untouched (calm rule).
export function sentence(s) {
  if (!s || typeof s !== 'string') return s
  return s.charAt(0).toUpperCase() + s.slice(1)
}
