import { useLayoutEffect, useRef, useState } from 'react'
import { DatasetDerivationCapabilitiesV1, DatasetDerivationRequestV1, DatasetDerivationResultV1, ProjectMaterialBindingV1 } from '../../../vendor/assistant-contract/src/dataset-derivation'
import { humanizeError } from '../errorHumanize.js'

const refKey = (ref) => JSON.stringify([ref.id, ref.version, ref.digest])
const freshWindow = () => ({ id: '', filters: [] })
const freshCalculation = () => ({ as: '', label: '', reason_as: '', reason_label: '', numerator: '', denominator: '', multiplier: 1 })
const numeric = ['number', 'count', 'ratio', 'money', 'duration']
const scalarValue = (field, value) => numeric.includes(field?.type.kind) ? (value.trim() === '' ? NaN : Number(value))
  : field?.type.kind === 'boolean' ? (value === 'true' ? true : value === 'false' ? false : undefined) : value
const predicates = (rows, fields) => rows.map(row => row.op === 'is_null' ? { field: row.field, op: row.op }
  : { field: row.field, op: row.op, value: scalarValue(fields.find(field => field.id === row.field), row.value) })

export const isImportedMaterialDataset = dataset => !dataset?.derivation &&
  ProjectMaterialBindingV1.safeParse(dataset?.material_lineage).success

function FieldSelect({ label, fields, value, onChange, optional = false }) {
  return <label>{label}<select value={value} onChange={event => onChange(event.target.value)} required={!optional}>
    <option value="">{optional ? 'None' : 'Select field'}</option>
    {fields.map(field => <option key={field.id} value={field.id}>{field.label}</option>)}
  </select></label>
}
function FieldChecks({ label, fields, values, onChange }) {
  return <fieldset><legend>{label}</legend>{fields.map(field => <label key={field.id}>
    <input type="checkbox" checked={values.includes(field.id)} onChange={event => onChange(event.target.checked
      ? [...values, field.id] : values.filter(id => id !== field.id))} />{field.label}
  </label>)}</fieldset>
}
function Filters({ label, fields, rows, onChange }) {
  const update = (index, patch) => onChange(rows.map((row, i) => i === index ? { ...row, ...patch } : row))
  return <fieldset><legend>{label}</legend>{rows.map((row, index) => <fieldset key={index}>
    <legend>Condition {index + 1}</legend>
    <FieldSelect label="Condition field" fields={fields} value={row.field} onChange={field => update(index, { field, value: '' })} />
    <label>Comparison<select value={row.op} onChange={event => update(index, { op: event.target.value })}>
      {['eq', 'neq', 'gt', 'gte', 'lt', 'lte', 'is_null'].map(op => <option key={op} value={op}>{({ eq: 'Equals', neq: 'Does not equal', gt: 'After / greater than', gte: 'At or after / at least', lt: 'Before / less than', lte: 'At or before / at most', is_null: 'Is missing' })[op]}</option>)}
    </select></label>
    {row.op !== 'is_null' && <label>Comparison value<input required value={row.value} onChange={event => update(index, { value: event.target.value })} /></label>}
    <button type="button" onClick={() => onChange(rows.filter((_, i) => i !== index))}>Remove condition {index + 1}</button>
  </fieldset>)}<button type="button" disabled={rows.length >= 20} onClick={() => onChange([...rows, { field: '', op: 'eq', value: '' }])}>Add condition to {label.toLowerCase()}</button>
  <p>Conditions use original values. Enter full timestamps with an explicit timezone. An empty condition list includes all admitted rows.</p></fieldset>
}

export function DatasetDerivationEvidence({ dataset }) {
  if (!dataset?.derivation) return null
  const clocks = [['source_updated_at', 'Source updated'], ['synced_at', 'Successfully synchronized'], ['fetched_at', 'Fetched']]
  return <section aria-label="Derived dataset evidence"><h3>Derived dataset evidence</h3>
    <p>Computed <time dateTime={dataset.computed_at}>{dataset.computed_at}</time>. This result covers admitted imported rows. Provider history and missing observations may be incomplete.</p>
    <p>Operation: {dataset.derivation.plan.kind === 'group_count' ? 'Grouped counts' : 'Counts before each observation'}. Result rows: {dataset.lastAttempt.row_count}.</p>
    {dataset.derivation.inputs.map(input => {
      const evidence = dataset.input_freshness.find(item => item.alias === input.alias)
      return <section key={input.alias} aria-label={`${input.alias} input evidence`}><h4>{input.alias} input</h4>
        <p>{input.dataset_ref.id}, version {input.dataset_ref.version}. Original material {input.original_binding.material_id}.</p>
        <dl>{clocks.map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{evidence?.freshness[key]
          ? <time dateTime={evidence.freshness[key]}>{evidence.freshness[key]}</time> : 'Unknown'}</dd></div>)}</dl>
      </section>
    })}
    <p>Rows outside the declared scope: {dataset.diagnostics.excluded_domain_rows}. Events with missing timestamps: {dataset.diagnostics.null_event_time_rows}.</p>
    {dataset.derivation.computed?.map(calculation => <p key={calculation.as}>{calculation.label}: missing estimates retain a reason in “{calculation.reason_label}”. Include that column in a table to inspect each reason.</p>)}
    <details><summary>Derivation and original lineage</summary><pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{JSON.stringify(dataset.derivation, null, 2)}</pre></details>
  </section>
}

export function DatasetDerivationPanel(props) {
  // A changed authority context retires even clients that ignore AbortSignal.
  const binding = [props.client, props.projectId, props.signal, props.contextKey]
  const [context, setContext] = useState(() => ({ binding, epoch: 0 }))
  if (binding.some((item, index) => item !== context.binding[index])) {
    setContext({ binding, epoch: context.epoch + 1 })
    return null
  }
  return <ScopedDerivation key={context.epoch} {...props} />
}
function ScopedDerivation({ client, projectId, signal, datasets, guard, onCreated, onForbidden }) {
  const scope = useRef(null), busy = useRef(false)
  const [capabilities, setCapabilities] = useState(null), [error, setError] = useState(''), [notice, setNotice] = useState('')
  const [pending, setPending] = useState(false), [reload, setReload] = useState(0), [created, setCreated] = useState(null)
  const [primaryKey, setPrimaryKey] = useState(''), [eventsKey, setEventsKey] = useState(''), [domainKey, setDomainKey] = useState('')
  const [kind, setKind] = useState('group_count'), [id, setId] = useState(''), [title, setTitle] = useState(''), [version, setVersion] = useState(1)
  const [groupFields, setGroupFields] = useState([]), [dayFields, setDayFields] = useState([]), [timezone, setTimezone] = useState('UTC')
  const [distinct, setDistinct] = useState([]), [anchorFields, setAnchorFields] = useState([])
  const [countId, setCountId] = useState('row_count'), [countLabel, setCountLabel] = useState('Count')
  const [windows, setWindows] = useState([freshWindow()]), [filters, setFilters] = useState([]), [eventFilters, setEventFilters] = useState([])
  const [atField, setAtField] = useState(''), [eventTime, setEventTime] = useState(''), [seconds, setSeconds] = useState(604800)
  const [lowerInclusive, setLowerInclusive] = useState(false), [upperInclusive, setUpperInclusive] = useState(true)
  const [pairs, setPairs] = useState([]), [domainFields, setDomainFields] = useState({}), [orderField, setOrderField] = useState('')
  const [calculations, setCalculations] = useState([])
  const eligible = datasets.filter(isImportedMaterialDataset)
  const primary = eligible.find(dataset => refKey(dataset.reference) === primaryKey)
  const events = eligible.find(dataset => refKey(dataset.reference) === eventsKey)
  const domain = eligible.find(dataset => refKey(dataset.reference) === domainKey)
  const fields = primary?.fields ?? [], eventFields = events?.fields ?? [], domainOptions = domain?.fields ?? []
  const current = ctx => scope.current === ctx && ctx && !ctx.controller.signal.aborted && !signal.aborted
  useLayoutEffect(() => {
    const ctx = { controller: new AbortController() }; scope.current = ctx
    const abort = () => { ctx.controller.abort(); setCapabilities(null) }
    signal.addEventListener('abort', abort, { once: true }); if (signal.aborted) abort()
    const load = async () => {
      try {
        if (!current(ctx) || typeof client.getDatasetDerivationCapabilities !== 'function' || typeof client.createDatasetDerivation !== 'function') throw new Error('Dataset derivation is unavailable.')
        const result = DatasetDerivationCapabilitiesV1.parse(await client.getDatasetDerivationCapabilities(projectId, { signal: ctx.controller.signal }))
        if (current(ctx)) { setCapabilities(result); setError('') }
      } catch (failure) {
        if (!current(ctx)) return
        setError('Dataset derivation is unavailable. Check access and retry.')
        if ([401, 403].includes(failure?.status)) onForbidden()
      }
    }
    load()
    return () => { scope.current = null; ctx.controller.abort(); signal.removeEventListener('abort', abort) }
  }, [client, projectId, signal, reload])
  const choosePrimary = key => {
    setPrimaryKey(key); setGroupFields([]); setDayFields([]); setAnchorFields([]); setDistinct([]); setFilters([]); setPairs([]); setDomainFields({}); setAtField(''); setCalculations([])
    setWindows([freshWindow()]); setCreated(null)
  }
  const datasetSelect = (label, value, change, optional = false) => <label>{label}<select required={!optional} value={value} onChange={event => change(event.target.value)}>
    <option value="">{optional ? 'None' : 'Select imported dataset'}</option>{eligible.map(dataset => <option key={refKey(dataset.reference)} value={refKey(dataset.reference)}>{dataset.title} · version {dataset.reference.version}</option>)}
  </select></label>
  async function create(event) {
    event.preventDefault()
    if (busy.current || !capabilities || !primary) return
    const ctx = scope.current
    let request
    try {
      if (!distinct.length || distinct.some(key => !(kind === 'group_count' ? fields : eventFields).some(field => field.id === key && !field.nullable))) throw new Error()
      const count = { as: countId, label: countLabel, distinct_by: distinct }
      const plan = kind === 'group_count' ? { kind, filters: predicates(filters, fields), group_by: groupFields.map(field => ({ field, as: field,
        bucket: dayFields.includes(field) ? 'day' : 'value', ...(dayFields.includes(field) && fields.find(item => item.id === field)?.type.kind === 'instant' ? { time_zone: timezone } : {}) })), count,
        windows: windows.map(window => ({ id: window.id, filters: predicates(window.filters, fields) })),
        ...(domain ? { domain: { keys: groupFields.map(field => ({ group_field: field, domain_field: domainFields[field] || '' })), ...(orderField ? { order_field: orderField } : {}) } } : {}) }
        : { kind, anchor_fields: anchorFields, anchor_filters: predicates(filters, fields), at_field: atField, event_time_field: eventTime,
          event_filters: predicates(eventFilters, eventFields), partition_by: pairs, lookback_seconds: Number(seconds), lower_inclusive: lowerInclusive, upper_inclusive: upperInclusive, count }
      request = DatasetDerivationRequestV1.parse({ schema_version: 1, output: { id, version: Number(version), title },
        inputs: { primary: primary.reference, ...(kind === 'window_count' ? { events: events?.reference } : domain ? { domain: domain.reference } : {}) }, plan,
        ...(calculations.length ? { computed: calculations.map(calculation => ({ ...calculation, multiplier: Number(calculation.multiplier) })) } : {}) })
    } catch {
      setError('Review dataset choices, fields, conditions, output names and window limits. Count identities must be non-missing fields.'); return
    }
    let persisted = false
    busy.current = true; setPending(true); setError(''); setNotice('')
    try {
      await guard(ctx.controller.signal)
      if (!current(ctx)) return
      const result = DatasetDerivationResultV1.parse(await client.createDatasetDerivation(projectId, request, { signal: ctx.controller.signal }))
      if (result.reference.id !== request.output.id || result.reference.version !== request.output.version) throw new Error('The created dataset did not match the requested output.')
      await guard(ctx.controller.signal)
      if (!current(ctx)) return
      persisted = true
      setCreated(result); setNotice(`Created ${title}. ${result.rowCount} result rows. Refresh its native view choice below.`)
      await onCreated(result.reference, ctx.controller.signal)
      if (current(ctx)) setNotice(`Created ${title}. Available in the native view dataset choices. Your current view draft is retained.`)
    } catch (failure) {
      if (!current(ctx)) return
      if ([401, 403].includes(failure?.status)) onForbidden()
      else setError(persisted ? 'The dataset was created, but its view choice could not be loaded. Refresh the created dataset below.' : humanizeError(failure))
    } finally { if (current(ctx)) { busy.current = false; setPending(false) } }
  }
  const calculationFields = [...(kind === 'group_count' ? groupFields : anchorFields).map(id => fields.find(field => field.id === id)).filter(field => field && numeric.includes(field.type.kind)), { id: countId, label: countLabel }]
  return <section aria-label="Create derived dataset"><h2>Create derived dataset</h2>
    <p>Count imported records using explicit fields and time bounds. Missing history is not zero. This does not fetch provider data.</p>
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    {!capabilities ? <button type="button" onClick={() => setReload(value => value + 1)}>Retry derivation availability</button> : <form onSubmit={create}>
      <fieldset disabled={pending || signal.aborted}><legend>Derivation choices</legend>
        <label>Operation<select value={kind} onChange={event => { setKind(event.target.value); setDistinct([]); setCreated(null); setCalculations([]) }}>
          {capabilities.operations.map(operation => <option key={operation} value={operation}>{operation === 'group_count' ? 'Grouped counts' : 'Counts before each observation'}</option>)}
        </select></label>
        {datasetSelect(kind === 'group_count' ? 'Source dataset' : 'Observation dataset', primaryKey, choosePrimary)}
        <label>Output ID<input required value={id} onChange={event => setId(event.target.value)} /></label>
        <label>Output title<input required maxLength={120} value={title} onChange={event => setTitle(event.target.value)} /></label>
        <label>Output version<input required type="number" min="1" max="2147483647" step="1" value={version} onChange={event => setVersion(event.target.value)} /></label>
        <label>Count field ID<input required value={countId} onChange={event => setCountId(event.target.value)} /></label>
        <label>Count label<input required value={countLabel} onChange={event => setCountLabel(event.target.value)} /></label>
        <Filters label={kind === 'group_count' ? 'Source conditions' : 'Observation conditions'} fields={fields} rows={filters} onChange={setFilters} />
        {kind === 'group_count' ? <>
          <FieldChecks label="Group fields" fields={fields} values={groupFields} onChange={setGroupFields} />
          <FieldChecks label="Group these date or time fields by day" fields={fields.filter(field => groupFields.includes(field.id) && ['date', 'instant'].includes(field.type.kind))} values={dayFields} onChange={setDayFields} />
          {dayFields.some(id => fields.find(field => field.id === id)?.type.kind === 'instant') && <label>Grouping timezone<input required value={timezone} onChange={event => setTimezone(event.target.value)} /></label>}
          {datasetSelect('Ordered scope dataset', domainKey, key => { setDomainKey(key); setDomainFields({}); setOrderField('') }, true)}
          {domain && <><p>Only the declared scope tuples are included. Zero counts require complete admitted inputs.</p>{groupFields.map(field => <FieldSelect key={field} label={`Scope field for ${field}`} fields={domainOptions} value={domainFields[field] || ''} onChange={value => setDomainFields(old => ({ ...old, [field]: value }))} />)}
            <FieldSelect label="Scope order field" fields={domainOptions.filter(field => numeric.includes(field.type.kind) && !field.nullable)} value={orderField} onChange={setOrderField} optional /></>}
          {windows.map((window, index) => <fieldset key={index}><legend>Named window {index + 1}</legend>
            <label>Window ID<input required value={window.id} onChange={event => setWindows(old => old.map((item, i) => i === index ? { ...item, id: event.target.value } : item))} /></label>
            <Filters label={`Window ${index + 1} conditions`} fields={fields} rows={window.filters} onChange={filters => setWindows(old => old.map((item, i) => i === index ? { ...item, filters } : item))} />
            {windows.length > 1 && <button type="button" onClick={() => setWindows(old => old.filter((_, i) => i !== index))}>Remove window {index + 1}</button>}
          </fieldset>)}<button type="button" disabled={windows.length >= capabilities.max_windows} onClick={() => setWindows(old => [...old, freshWindow()])}>Add named window</button>
          <p>Choose your own names and explicit bounds. For an All-history window, leave its conditions empty.</p>
        </> : <>
          {datasetSelect('Event dataset', eventsKey, key => { setEventsKey(key); setDistinct([]); setEventFilters([]); setPairs([]); setEventTime('') })}
          <FieldChecks label="Retained observation fields" fields={fields} values={anchorFields} onChange={setAnchorFields} />
          <FieldSelect label="Observation timestamp" fields={fields.filter(field => field.type.kind === 'instant')} value={atField} onChange={setAtField} />
          <FieldSelect label="Event timestamp" fields={eventFields.filter(field => field.type.kind === 'instant')} value={eventTime} onChange={setEventTime} />
          <label>Lookback seconds<input required type="number" min="1" max="315360000" step="1" value={seconds} onChange={event => setSeconds(event.target.value)} /></label>
          <label><input type="checkbox" checked={lowerInclusive} onChange={event => setLowerInclusive(event.target.checked)} />Include lower boundary</label>
          <label><input type="checkbox" checked={upperInclusive} onChange={event => setUpperInclusive(event.target.checked)} />Include observation boundary</label>
          <Filters label="Event conditions" fields={eventFields} rows={eventFilters} onChange={setEventFilters} />
          {pairs.map((pair, index) => <fieldset key={index}><legend>Matching partition {index + 1}</legend>
            <FieldSelect label="Observation partition field" fields={fields.filter(field => !field.nullable)} value={pair.anchor_field} onChange={anchor_field => setPairs(old => old.map((item, i) => i === index ? { ...item, anchor_field } : item))} />
            <FieldSelect label="Event partition field" fields={eventFields.filter(field => !field.nullable)} value={pair.event_field} onChange={event_field => setPairs(old => old.map((item, i) => i === index ? { ...item, event_field } : item))} />
            <button type="button" onClick={() => setPairs(old => old.filter((_, i) => i !== index))}>Remove partition {index + 1}</button>
          </fieldset>)}<button type="button" disabled={pairs.length >= 8} onClick={() => setPairs(old => [...old, { anchor_field: '', event_field: '' }])}>Add matching partition</button>
        </>}
        <FieldChecks label="Count distinct identities by" fields={(kind === 'group_count' ? fields : eventFields).filter(field => !field.nullable)} values={distinct} onChange={setDistinct} />
        {calculations.map((calculation, index) => <fieldset key={index}><legend>Estimate {index + 1}</legend>
          {['as', 'label', 'reason_as', 'reason_label'].map(key => <label key={key}>{({ as: 'Estimate field ID', label: 'Estimate label', reason_as: 'Reason field ID', reason_label: 'Reason label' })[key]}<input required value={calculation[key]} onChange={event => setCalculations(old => old.map((item, i) => i === index ? { ...item, [key]: event.target.value } : item))} /></label>)}
          {['numerator', 'denominator'].map(key => <FieldSelect key={key} label={key === 'numerator' ? 'Numerator field' : 'Denominator field'} fields={calculationFields} value={calculation[key]} onChange={value => setCalculations(old => old.map((item, i) => i === index ? { ...item, [key]: value } : item))} />)}
          <label>Multiplier<input required type="number" min="0" step="any" value={calculation.multiplier} onChange={event => setCalculations(old => old.map((item, i) => i === index ? { ...item, multiplier: event.target.value } : item))} /></label>
          <button type="button" onClick={() => setCalculations(old => old.filter((_, i) => i !== index))}>Remove estimate {index + 1}</button>
        </fieldset>)}<button type="button" disabled={calculations.length >= 4} onClick={() => setCalculations(old => [...old, freshCalculation()])}>Add quotient estimate</button>
        <p>Each estimate divides its numerator by its denominator and applies the explicit multiplier. Missing or zero denominators produce a missing estimate with a reason.</p>
        <button type="submit">{pending ? 'Creating derived dataset…' : 'Create derived dataset'}</button>
      </fieldset>
    </form>}
    {created && <button type="button" disabled={pending || signal.aborted} onClick={async () => {
      const ctx = scope.current
      try { await guard(ctx.controller.signal); if (current(ctx)) await onCreated(created.reference, ctx.controller.signal) }
      catch (failure) { if (current(ctx)) { if ([401, 403].includes(failure?.status)) onForbidden(); else setError('The created dataset could not be loaded.') } }
    }}>Refresh created dataset choice</button>}
  </section>
}
