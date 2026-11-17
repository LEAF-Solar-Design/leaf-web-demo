import { useLayoutEffect, useRef, useState } from 'react'
import { createSolarDrawingBinding, sameSolarDrawingBinding, solarDrawingBindingKey } from './solarDrawingBinding.js'

const current = (predicate) => { try { return predicate?.() === true } catch { return false } }
const DIGEST = /^sha256:[0-9a-f]{64}$/
const MAX_DECIMAL = 40

function decimal(token) {
  if (typeof token !== 'string' || token.length > MAX_DECIMAL || !/^\d+(\.\d+)?$/.test(token)) return null
  const [whole, fraction = ''] = token.split('.')
  const scale = 10n ** BigInt(fraction.length)
  const value = BigInt(whole + fraction)
  return value <= 1000000000000n * scale ? { value, scale } : null
}

export function estimateLineCents(line, rate) {
  if (line?.status !== 'computed' || line.priceable !== true) return null
  const quantity = decimal(line.quantity), price = decimal(rate)
  if (!quantity || !price) return null
  const numerator = quantity.value * price.value * 100n
  const denominator = quantity.scale * price.scale
  const cents = numerator / denominator, remainder = numerator % denominator
  return cents + (remainder * 2n > denominator || (remainder * 2n === denominator && cents % 2n === 1n) ? 1n : 0n)
}

const money = (cents) => `$${cents / 100n}.${String(cents % 100n).padStart(2, '0')}`

function describe(props) {
  const { tool, solarBinding, authorityKey, projectClient, projectCatalog, isCurrent } = props
  try {
    const binding = createSolarDrawingBinding(solarBinding)
    const snapshot = projectClient.getSnapshot()
    const rows = projectCatalog.families.flatMap((family) => family.capabilities)
    const valid = tool?.name === 'solar-takeoff' && DIGEST.test(tool.catalog_digest) &&
      rows.includes(tool) && !rows.some((row) => row.name === tool.name && row.catalog_digest !== tool.catalog_digest) &&
      current(isCurrent) && current(projectCatalog.isCurrent) &&
      typeof authorityKey === 'string' && !!authorityKey.trim() &&
      ['readSolar', 'subscribe', 'captureCatalogScope'].every((key) => typeof projectClient[key] === 'function') &&
      projectCatalog.client === projectClient && projectCatalog.authorityKey === authorityKey &&
      snapshot.authorityKey === authorityKey && sameSolarDrawingBinding(snapshot.binding, binding) &&
      sameSolarDrawingBinding(projectCatalog.binding, binding)
    return { ...props, binding, key: solarDrawingBindingKey(binding), digest: tool.catalog_digest,
      receiptCurrent: projectCatalog.isCurrent, catalogKey: JSON.stringify(projectCatalog.families), valid }
  } catch { return { ...props, valid: false } }
}

function same(left, right) {
  return !!left && ['key', 'authorityKey', 'projectClient', 'projectCatalog', 'tool', 'digest', 'isCurrent',
    'receiptCurrent', 'catalogKey', 'valid']
    .every((key) => left[key] === right[key])
}

function linesFrom(outcome, owner) {
  const result = outcome?.ok === true ? outcome.data?.result : null
  const summary = result?.output?.summary
  if (!result || result.tool !== 'solar-takeoff' || result.tool_manifest_sha256 !== owner.digest ||
      !['organization_id', 'project_id', 'drawing_id', 'input_version_id'].every((key) => result[key] === owner.binding[key]) ||
      summary?.schema !== 'leaf.solar-takeoff.v1' || !Array.isArray(summary.lines) || summary.lines.length > 128) {
    throw new TypeError('Invalid takeoff output')
  }
  const ids = new Set()
  return summary.lines.map((line) => {
    if (!line || typeof line.line_id !== 'string' || line.line_id.length > 96 || ids.has(line.line_id) ||
        typeof line.description !== 'string' || line.description.length > 200 ||
        typeof line.unit !== 'string' || line.unit.length > 40) throw new TypeError('Invalid takeoff line')
    ids.add(line.line_id)
    return { line_id: line.line_id, description: line.description, unit: line.unit,
      quantity: decimal(line.quantity) ? line.quantity : null, status: line.status, priceable: line.priceable }
  })
}

export default function ProjectGroundEstimatePanel(props) {
  const owner = describe(props)
  const latest = useRef(null), operation = useRef(null), presented = useRef(null), generation = useRef(0)
  const mounted = useRef(false), observe = useRef(null)
  const [state, setState] = useState({ phase: 'idle', generation: 0 })
  const [, refresh] = useState(0)
  const [rates, setRates] = useState({})
  const [rateVersion, setRateVersion] = useState('TEST-1')
  const invalidate = () => {
    generation.current += 1
    operation.current?.controller.abort()
    operation.current = null
    presented.current = null
  }
  if (!same(latest.current, owner) || (presented.current && !current(presented.current))) invalidate()
  latest.current = owner
  const visible = owner.valid && state.generation === generation.current ? state : { phase: 'idle' }
  observe.current = () => {
    if (!same(latest.current, describe(latest.current)) ||
        (operation.current && !operation.current.check()) || (presented.current && !current(presented.current))) invalidate()
    refresh((value) => value + 1)
  }
  useLayoutEffect(() => {
    mounted.current = true
    let unsubscribe
    try { unsubscribe = props.projectClient?.subscribe(() => observe.current()) } catch { /* unavailable */ }
    return () => { mounted.current = false; invalidate(); try { unsubscribe?.() } catch { /* observer isolation */ } }
  }, [props.projectClient])

  const read = async () => {
    const admitted = describe(latest.current)
    if (props.busy || !admitted.valid || !same(admitted, latest.current) || operation.current || !mounted.current) return
    invalidate()
    const token = generation.current, controller = new AbortController()
    const active = { controller, check: null }
    operation.current = active
    let captured
    const scopeCurrent = () => same(latest.current, admitted) && same(admitted, describe(admitted)) && current(captured)
    active.check = () => mounted.current && generation.current === token && operation.current === active &&
      !controller.signal.aborted && scopeCurrent()
    try {
      captured = admitted.projectClient.captureCatalogScope()
      if (!active.check()) return
      setRates({})
      setState({ phase: 'reading', generation: token })
      const outcome = await admitted.projectClient.readSolar({ tool: 'solar-takeoff',
        params: { drawing_id: admitted.binding.drawing_id }, catalogDigest: admitted.digest,
        current: true, signal: controller.signal })
      if (!active.check()) return
      const lines = linesFrom(outcome, admitted)
      if (!active.check()) return
      presented.current = scopeCurrent
      setState({ phase: 'presented', generation: token, lines })
    } catch {
      if (active.check()) setState({ phase: 'refused', generation: token })
    } finally {
      if (operation.current === active) operation.current = null
    }
  }
  const amounts = visible.lines?.map((line) => estimateLineCents(line, rates[line.line_id] ?? '')) || []
  const subtotal = amounts.reduce((total, cents) => total + (cents ?? 0n), 0n)
  return <section aria-label="Ground estimate preview">
    <p>Preview quantities and TEST RATES USD. This is an incomplete estimate preview. Full GroundPhysical flow is unavailable.</p>
    <button type="button" disabled={!owner.valid || props.busy || visible.phase === 'reading'} onClick={read}>Read project quantities</button>
    <p role="status">{!owner.valid ? 'The drawing or catalog changed. Reopen this preview.' :
      visible.phase === 'reading' ? 'Reading project quantities…' : visible.phase === 'refused' ? 'The project quantity read was refused.' : ''}</p>
    {visible.phase === 'presented' && <>
      <label>TEST rate card version<input maxLength={120} value={rateVersion} onChange={(event) => setRateVersion(event.target.value.slice(0, 120))} /></label>
      <table><thead><tr>{['Description', 'Unit', 'Quantity', 'TEST rate USD', 'Amount USD'].map((label) => <th key={label}>{label}</th>)}</tr></thead>
        <tbody>{visible.lines.map((line, index) => <tr key={line.line_id}>
          <td>{line.description}</td><td>{line.unit}</td><td>{line.quantity ?? 'Unavailable'}</td>
          <td><input aria-label={`TEST rate USD ${line.line_id}`} inputMode="decimal" maxLength={MAX_DECIMAL}
            value={rates[line.line_id] ?? ''} onChange={(event) => {
              const value = event.target.value.slice(0, MAX_DECIMAL)
              setRates((previous) => ({ ...previous, [line.line_id]: value }))
            }} /></td><td>{amounts[index] === null ? 'Unpriced' : money(amounts[index])}</td>
        </tr>)}</tbody></table>
      <p>Priced subtotal (preview): {money(subtotal)}</p>
      <p>{amounts.filter((amount) => amount === null).length} unpriced lines. TEST rates only; preview is not saved.</p>
    </>}
  </section>
}
