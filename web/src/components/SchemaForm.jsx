import { useState } from 'react'

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

export function JsonParamInput({ value, expectedType, onChange }) {
  const emptyValue = expectedType === 'array' ? [] : {}
  const [draft, setDraft] = useState(() => JSON.stringify(value ?? emptyValue))
  const [valid, setValid] = useState(true)

  return (
    <input
      type="text"
      value={draft}
      aria-invalid={!valid}
      onChange={(event) => {
        const next = event.target.value
        setDraft(next)
        try {
          const parsed = JSON.parse(next)
          const matches = expectedType === 'array'
            ? Array.isArray(parsed)
            : parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed)
          if (!matches) throw new TypeError(`Expected a JSON ${expectedType}`)
          setValid(true)
          onChange(parsed)
        } catch {
          setValid(false)
        }
      }}
    />
  )
}

// Renders a JSON-Schema params object (CONTRACT §2 .params) as a small form.
export default function SchemaForm({ schema, values, onChange }) {
  const props = schema?.properties || {}
  const keys = Object.keys(props)
  if (keys.length === 0) return <p className="params-none">No parameters.</p>
  return (
    <div className="params">
      {keys.map((k) => {
        const p = props[k]
        const types = schemaTypes(p)
        const isArray = types.includes('array')
        const isObject = types.includes('object')
        const isNumber = types.includes('number') || types.includes('integer')
        const isBoolean = types.includes('boolean')
        const isNullable = types.includes('null')
        const label = sentence((p.title || k).replace(/_/g, ' '))
        const hasValidDefault = p.default !== undefined && valueMatchesSchemaType(p.default, p)
        const val = Object.prototype.hasOwnProperty.call(values, k)
          ? values[k]
          : hasValidDefault ? p.default : (isArray ? [] : isObject ? {} : isNumber && !isNullable ? 0 : isBoolean ? false : '')
        return (
          <label key={k} className="param">
            <span>{label}</span>
            {isArray || isObject ? (
              <JsonParamInput
                value={val}
                expectedType={isArray ? 'array' : 'object'}
                onChange={(next) => onChange({ ...values, [k]: next })}
              />
            ) : isBoolean ? (
              <input
                type="checkbox"
                checked={Boolean(val)}
                onChange={(e) => onChange({ ...values, [k]: e.target.checked })}
              />
            ) : (
              <input
                type={isNumber ? 'number' : 'text'}
                step={types.includes('integer') ? '1' : undefined}
                value={val ?? ''}
                onChange={(e) => onChange({
                  ...values,
                  [k]: isNumber
                    ? (isNullable && e.target.value === '' ? null : Number(e.target.value))
                    : e.target.value,
                })}
              />
            )}
          </label>
        )
      })}
    </div>
  )
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
