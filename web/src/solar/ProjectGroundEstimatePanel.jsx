import { useLayoutEffect, useRef, useState } from 'react'
import { createSolarDrawingBinding, sameSolarDrawingBinding, solarDrawingBindingKey } from './solarDrawingBinding.js'

const current = (predicate) => { try { return predicate?.() === true } catch { return false } }
const DIGEST = /^sha256:[0-9a-f]{64}$/
const GRAPH_SHA256 = /^[0-9a-f]{64}$/
const MAX_DECIMAL = 40
const READ_REFUSALS = new Map([
  ['GRAPH_NOT_EMBEDDED', 'This drawing version has no solar graph. Open a version with a solar graph or start a design in Solar settings.'],
  ['SIP_R6_STALE_CURRENT', 'This is an older drawing version. Open the latest saved version and read project quantities again.'],
])

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

const UNPRICED_LABELS = {
  RATE_MISSING: 'Missing rates',
  INVALID_RATE: 'Invalid rates',
  QUANTITY_MISSING: 'Unavailable quantities',
  NONCOMPUTED_LINE: 'Not computed',
  NONPRICEABLE_LINE: 'Nonpriceable',
  UNIT_MISMATCH: 'Rate unit mismatches',
  OTHER: 'Unspecified reason',
}

function draftReason(line, rate) {
  if (line.status !== 'computed') return 'NONCOMPUTED_LINE'
  if (line.priceable !== true) return 'NONPRICEABLE_LINE'
  if (!decimal(line.quantity)) return 'QUANTITY_MISSING'
  return rate ? 'INVALID_RATE' : 'RATE_MISSING'
}

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

function takeoffSource(summary, binding, graphSha256) {
  const source = summary?.source
  if (!source || source.project_id !== binding.project_id || source.drawing_id !== binding.drawing_id ||
      !Number.isSafeInteger(source.drawing_version) || source.drawing_version < 1 ||
      typeof graphSha256 !== 'string' || !GRAPH_SHA256.test(graphSha256) || source.graph_sha256 !== graphSha256) return null
  return { drawingVersion: source.drawing_version, graphSha256 }
}

function previewFrom(outcome, owner) {
  const result = outcome?.ok === true ? outcome.data?.result : null
  const summary = result?.output?.summary
  if (!result || result.tool !== 'solar-takeoff' || result.tool_manifest_sha256 !== owner.digest ||
      !['organization_id', 'project_id', 'drawing_id', 'input_version_id'].every((key) => result[key] === owner.binding[key]) ||
      summary?.schema !== 'leaf.solar-takeoff.v1' || !Array.isArray(summary.lines) || summary.lines.length > 128) {
    throw new TypeError('Invalid takeoff output')
  }
  const ids = new Set()
  const lines = summary.lines.map((line) => {
    if (!line || typeof line.line_id !== 'string' || line.line_id.length > 96 || ids.has(line.line_id) ||
        typeof line.description !== 'string' || line.description.length > 200 ||
        typeof line.unit !== 'string' || line.unit.length > 40) throw new TypeError('Invalid takeoff line')
    ids.add(line.line_id)
    return { line_id: line.line_id, description: line.description, unit: line.unit,
      quantity: decimal(line.quantity) ? line.quantity : null, status: line.status, priceable: line.priceable }
  })
  return { lines, takeoffSource: takeoffSource(summary, owner.binding, result.graph_sha256) }
}

export default function ProjectGroundEstimatePanel(props) {
  const owner = describe(props)
  const latest = useRef(null), operation = useRef(null), presented = useRef(null), generation = useRef(0)
  const mounted = useRef(false), observe = useRef(null)
  const [state, setState] = useState({ phase: 'idle', generation: 0 })
  const [, refresh] = useState(0)
  const [rates, setRates] = useState({})
  const [rateVersion, setRateVersion] = useState('TEST-1')
  const [checkpoint, setCheckpoint] = useState(null)
  const [notice, setNotice] = useState('')
  const [working, setWorking] = useState(false)
  const [dirty, setDirty] = useState(false)
  const custody = useRef(null), retry = useRef(null), draftSource = useRef(null), source = useRef(null), savedRead = useRef(null)
  if (!source.current || source.current.bindingKey !== owner.key || source.current.authorityKey !== owner.authorityKey ||
      source.current.client !== owner.projectClient) {
    source.current = { bindingKey: owner.key, authorityKey: owner.authorityKey, client: owner.projectClient }
  }
  const sourceKey = source.current
  const sourceChanged = draftSource.current !== sourceKey
  if (sourceChanged) {
    draftSource.current = sourceKey
    retry.current = null
    custody.current?.abort()
    savedRead.current?.abort()
  }
  const invalidate = () => {
    generation.current += 1
    operation.current?.controller.abort()
    operation.current = null
    presented.current = null
    custody.current?.abort()
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
    return () => { mounted.current = false; invalidate(); custody.current?.abort(); try { unsubscribe?.() } catch { /* observer isolation */ } }
  }, [props.projectClient])

  useLayoutEffect(() => {
    setRates({}); setRateVersion('TEST-1'); setCheckpoint(null); setDirty(false); setNotice(''); setWorking(false)
  }, [sourceKey])

  useLayoutEffect(() => {
    if (!owner.valid || typeof owner.projectClient.readEstimate !== 'function') return
    const controller = new AbortController()
    savedRead.current = controller
    const key = sourceKey
    const valid = () => mounted.current && !controller.signal.aborted && draftSource.current === key &&
      current(latest.current.isCurrent) && latest.current.valid
    void owner.projectClient.readEstimate({ signal: controller.signal }).then((outcome) => {
      if (!valid()) return
      if (outcome.ok && outcome.data.result.saved) setCheckpoint({ ...outcome.data.result, key })
      else setNotice(outcome.ok ? 'No saved estimate for this version.' : 'Saved estimate read was refused.')
    }).catch(() => { if (valid()) setNotice('Saved estimate read was refused.') })
    return () => controller.abort()
  }, [sourceKey, owner.valid])

  const saved = checkpoint?.key === sourceKey && owner.valid ? checkpoint : null
  const edit = () => { setDirty(true); setNotice('Unsaved changes.') }
  const act = async (kind) => {
    const admitted = describe(latest.current)
    if (!admitted.valid || working || props.busy || !mounted.current) return
    if (kind === 'reopen' && retry.current) return
    savedRead.current?.abort()
    if (kind === 'reopen') invalidate()
    const key = sourceKey, controller = new AbortController()
    custody.current?.abort(); custody.current = controller
    const valid = () => mounted.current && !controller.signal.aborted && draftSource.current === key &&
      same(latest.current, admitted) && same(admitted, describe(admitted))
    setWorking(true)
    try {
      if (kind === 'checkout') {
        const outcome = await admitted.projectClient.acquireCheckout({ signal: controller.signal })
        if (mounted.current && draftSource.current === key) setNotice(outcome.ok ? 'Drawing checked out. Read quantities to save.' : 'Drawing checkout was refused.')
      } else if (kind === 'reopen') {
        const outcome = await admitted.projectClient.readEstimate({ signal: controller.signal })
        if (!valid()) return
        if (!outcome.ok) throw new Error('Saved estimate read was refused.')
        const result = outcome.data.result
        if (!result.saved) { setCheckpoint(null); setNotice('No saved estimate for this version.'); return }
        const estimate = result.estimate
        setRates(Object.fromEntries(estimate.rate_card.rates.map((row) => [row.line_id, row.rate])))
        setRateVersion(estimate.rate_card.version)
        retry.current = null
        setDirty(false); setCheckpoint({ ...result, key })
        presented.current = () => mounted.current && draftSource.current === key &&
          same(latest.current, admitted) && same(admitted, describe(admitted))
        setState({ phase: 'presented', generation: generation.current, lines: estimate.takeoff.lines,
          takeoffSource: takeoffSource(estimate.takeoff, admitted.binding, estimate.source_binding?.graph_sha256),
          savedDigest: estimate.content_sha256 })
        setNotice('Saved estimate reopened.')
      } else if (kind === 'save') {
        if (!admitted.projectClient.getSnapshot().ownsCheckout) { setNotice('Check out drawing before saving an estimate.'); return }
        if (!visible.lines && !retry.current) return
        if (!retry.current) {
          const rateCard = { schema: 'leaf.solar-estimate-rate-card.v1', label: 'TEST RATES', currency: 'USD',
            version: rateVersion, rates: visible.lines.filter((line) => (rates[line.line_id] ?? '') !== '').map((line) =>
              ({ line_id: line.line_id, unit: line.unit, rate: rates[line.line_id] })) }
          const requestId = props.newProjectIntentKey ? props.newProjectIntentKey() : globalThis.crypto.randomUUID()
          rateCard.rates.forEach(Object.freeze)
          Object.freeze(rateCard.rates)
          retry.current = Object.freeze({ requestId, rateCard: Object.freeze(rateCard), catalogDigest: admitted.digest })
        }
        const outcome = await admitted.projectClient.saveEstimate({ ...retry.current, signal: controller.signal })
        if (!valid()) return
        if (!outcome.ok) {
          const ambiguous = outcome.status >= 500 || (outcome.status == null &&
            ['SIP_R7_TRANSPORT_FAILED', 'SIP_R7_TIMEOUT', 'SIP_R7_ABORTED', 'SIP_R7_RESPONSE_INVALID'].includes(outcome.reason))
          if (!ambiguous) retry.current = null
          setNotice(ambiguous ? 'Save outcome unknown. Retry Save estimate with the unchanged card.' : 'Estimate save was refused. Your rates are retained.')
          return
        }
        retry.current = null
        const result = outcome.data.result
        setCheckpoint({ ...result, key }); setDirty(false)
        const receipt = `Estimate saved as version ${result.publication.drawing_version.version_id}.`
        setNotice(receipt)
        try {
          const seated = await props.onEstimatePublished?.(result.publication, admitted.binding, valid)
          if (valid()) setNotice(seated === true ? receipt : `${receipt} Reopen that saved version to continue; selection was not changed.`)
        } catch { if (valid()) setNotice(`${receipt} Reopen that saved version to continue; selection was not changed.`) }
      } else if (kind === 'export' && saved && !dirty && !retry.current) {
        const outcome = await admitted.projectClient.exportEstimate({ contentSha256: saved.content_sha256,
          signal: controller.signal, isCurrent: valid })
        if (!valid()) return
        if (!outcome.ok) throw new Error('Saved estimate export was refused.')
        const url = URL.createObjectURL(new Blob([outcome.data.bytes], { type: 'application/json' }))
        try {
          if (!valid()) return
          const anchor = document.createElement('a')
          anchor.href = url; anchor.download = 'estimate.json'; anchor.click()
        } finally { URL.revokeObjectURL(url) }
      }
    } catch {
      if (valid()) setNotice(kind === 'save' && retry.current ?
        'Save outcome unknown. Retry Save estimate with the unchanged card.' : 'Estimate operation was refused. Your rates are retained.')
    }
    finally { if (mounted.current && draftSource.current === key && custody.current === controller) setWorking(false) }
  }

  const read = async () => {
    const admitted = describe(latest.current)
    if (props.busy || working || retry.current || !admitted.valid || !same(admitted, latest.current) || operation.current || !mounted.current) return
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
      if (saved) setDirty(true)
      setState({ phase: 'reading', generation: token })
      const outcome = await admitted.projectClient.readSolar({ tool: 'solar-takeoff',
        params: { drawing_id: admitted.binding.drawing_id }, catalogDigest: admitted.digest,
        current: true, signal: controller.signal })
      if (!active.check()) return
      if (outcome?.ok === false) {
        setState({ phase: 'refused', generation: token, message: READ_REFUSALS.get(outcome.reason) })
        return
      }
      const preview = previewFrom(outcome, admitted)
      if (!active.check()) return
      presented.current = scopeCurrent
      setState({ phase: 'presented', generation: token, ...preview })
    } catch {
      if (active.check()) setState({ phase: 'refused', generation: token })
    } finally {
      if (operation.current === active) operation.current = null
    }
  }
  const reasonCounts = new Map()
  const amounts = visible.lines?.map((line) => {
    const rate = rates[line.line_id] ?? ''
    let cents, reason
    if (saved && !dirty && visible.savedDigest === saved.content_sha256) {
      const row = saved.estimate.lines.find((entry) => entry.line_id === line.line_id)
      const amount = row?.amount
      cents = typeof amount === 'string' && /^\d+\.\d{2}$/.test(amount) ? BigInt(amount.replace('.', '')) : null
      reason = row?.pricing_reason === 'QUANTITY_INVALID' ? 'QUANTITY_MISSING' : row?.pricing_reason
    } else {
      cents = estimateLineCents(line, rate)
      if (cents === null) reason = draftReason(line, rate)
    }
    if (cents === null) {
      const key = Object.hasOwn(UNPRICED_LABELS, reason) ? reason : 'OTHER'
      reasonCounts.set(key, (reasonCounts.get(key) ?? 0) + 1)
    }
    return cents
  }) || []
  const subtotal = amounts.reduce((total, cents) => total + (cents ?? 0n), 0n)
  return <section aria-label="Ground estimate preview">
    <p>Preview quantities and TEST RATES USD. This is an incomplete estimate preview. Full GroundPhysical flow is unavailable.</p>
    <button type="button" disabled={!owner.valid || props.busy || working || !!retry.current || visible.phase === 'reading'} onClick={read}>Read project quantities</button>
    <button type="button" disabled={!owner.valid || working || props.busy} onClick={() => act('checkout')}>Check out drawing</button>
    <button type="button" disabled={!owner.valid || working || props.busy || (!visible.lines && !retry.current)} onClick={() => act('save')}>Save estimate</button>
    <button type="button" disabled={!owner.valid || working || props.busy || !!retry.current} onClick={() => act('reopen')}>Reopen saved estimate</button>
    <button type="button" disabled={!saved || dirty || working || props.busy || !!retry.current || !!saved.publication} onClick={() => act('export')}>Export saved estimate</button>
    {!owner.projectClient?.getSnapshot()?.ownsCheckout && <p>Check out drawing before saving. Saved estimates can be reopened and exported without checkout.</p>}
    {saved && <p>Saved content hash: {saved.content_sha256}. Saved subtotal: {saved.estimate.priced_subtotal ?? 'Unpriced'} USD.</p>}
    <p role="status">{!owner.valid ? 'The drawing or catalog changed. Reopen this preview.' :
      visible.phase === 'reading' ? 'Reading project quantities…' : visible.phase === 'refused' ?
        (visible.message ?? 'The project quantity read was refused.') :
        !sourceChanged ? notice : ''}</p>
    {visible.phase === 'presented' && <>
      {visible.takeoffSource && <p>
        Source drawing version {visible.takeoffSource.drawingVersion}. Graph SHA-256{' '}
        <code style={{ overflowWrap: 'anywhere' }}>{visible.takeoffSource.graphSha256}</code>
      </p>}
      <label>TEST rate card version<input disabled={working || !!retry.current} maxLength={120} value={rateVersion} onChange={(event) => { edit(); setRateVersion(event.target.value.slice(0, 120)) }} /></label>
      <table><thead><tr>{['Description', 'Unit', 'Quantity', 'TEST rate USD', 'Amount USD'].map((label) => <th key={label}>{label}</th>)}</tr></thead>
        <tbody>{visible.lines.map((line, index) => <tr key={line.line_id}>
          <td>{line.description}</td><td>{line.unit}</td><td>{line.quantity ?? 'Unavailable'}</td>
          <td><input disabled={working || !!retry.current} aria-label={`TEST rate USD ${line.line_id}`} inputMode="decimal" maxLength={MAX_DECIMAL}
            value={rates[line.line_id] ?? ''} onChange={(event) => {
              const value = event.target.value.slice(0, MAX_DECIMAL)
              edit()
              setRates((previous) => ({ ...previous, [line.line_id]: value }))
            }} /></td><td>{amounts[index] === null ? 'Unpriced' : money(amounts[index])}</td>
        </tr>)}</tbody></table>
      <p>Priced subtotal (preview): {money(subtotal)}</p>
      {reasonCounts.size > 0 && <ul aria-label="Unpriced line breakdown">
        {Object.entries(UNPRICED_LABELS).filter(([key]) =>
          key === 'RATE_MISSING' || key === 'INVALID_RATE' || reasonCounts.has(key)
        ).map(([key, label]) => <li key={key}>{label}: {reasonCounts.get(key) ?? 0}</li>)}
      </ul>}
      <p>{amounts.filter((amount) => amount === null).length} unpriced lines. {saved && !dirty && visible.savedDigest === saved.content_sha256 ?
        'Saved checkpoint; TEST rates only.' : 'TEST rates only; preview is not saved.'}</p>
    </>}
  </section>
}
