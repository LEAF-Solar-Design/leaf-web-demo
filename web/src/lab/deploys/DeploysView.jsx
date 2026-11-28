import { useEffect, useRef, useState } from 'react'
import { labArtifactsClient } from '../history/labArtifactsClient.js'
import { authHeaders, config } from '../../api.js'
import { fetchWithBudget } from '../../fetchBudget.js'
import { humanizeError } from '../../errorHumanize.js'

const cardStyle = {
  background: 'var(--leaf-color-surface)', color: 'var(--leaf-color-foreground)',
  border: '1px solid var(--leaf-color-border)', padding: 'var(--leaf-spacing-panel-padding)',
  borderRadius: 'var(--leaf-radius-panel)', fontFamily: 'var(--leaf-font-sans)',
}

const activationFetch = (...args) => globalThis.fetch(...args)

function scheduleLabel(row) {
  return `Document ${row.doc_id} · Revision ${row.revision} · Every ${row.interval_s} seconds`
}

function ExecutionPlan({ plan }) {
  return <section aria-label="Execution plan">
    <p>Published r{plan.revision}. Proposed interval {plan.interval_s} seconds. Run limit {plan.max_run_wall_s} seconds.</p>
    <p>Graph SHA-256 <code style={{ overflowWrap: 'anywhere' }}>{plan.graph_sha256}</code></p>
    <details>
      <summary>Execution policy and graph connections</summary>
      <p>Worker policy {plan.ops_revision}. Registry SHA-256 <code style={{ overflowWrap: 'anywhere' }}>{plan.registry_digest}</code></p>
      <p>External inputs SHA-256 <code style={{ overflowWrap: 'anywhere' }}>{plan.input_digest}</code></p>
      {plan.pipes.map(({ from, to }) => <p key={JSON.stringify([from, to])}>
        {from.node}.{from.port} to {to.node}.{to.port}</p>)}
      {Object.entries(plan.outputs).map(([name, pin]) => <p key={name}>{name} from {pin.node}.{pin.port}</p>)}
    </details>
    <ul aria-label="Planned blocks">
      {plan.blocks.map(block => <li key={block.id}>
        <details>
          <summary>{block.id} / {block.registry.id} {block.registry.version} / {block.effect || 'Effect unavailable'}</summary>
          <p>Code SHA-256 <code style={{ overflowWrap: 'anywhere' }}>{block.registry.code_sha256}</code></p>
          <p>Settings SHA-256 <code style={{ overflowWrap: 'anywhere' }}>{block.settings_sha256}</code></p>
          {block.inputs && <p>Project access is unavailable to the scheduled worker.</p>}
          {Object.entries(block.inputs || {}).map(([name, pin]) => <p key={name}>
            {name === 'corpus' ? 'Corpus' : 'Suite'} material {pin.material_id}, {pin.byte_size} bytes.
            SHA-256 <code style={{ overflowWrap: 'anywhere' }}>{pin.sha256}</code>
          </p>)}
        </details>
      </li>)}
    </ul>
    {plan.blockers.map(({ code, block }) => <p role="status" key={JSON.stringify([code, block])}>
      {code === 'lab_activation_corpus_authority_unavailable'
        ? 'Scheduled workers cannot access project-scoped corpus inputs.'
        : `This revision cannot be scheduled (${code}).`}{block && ` Block ${block}.`}
    </p>)}
  </section>
}

function Activations({ docId, published, fetchImpl }) {
  const [rows, setRows] = useState([])
  const [interval, setInterval] = useState('60')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [preview, setPreview] = useState(null)
  const [previewError, setPreviewError] = useState(null)
  const [previewBusy, setPreviewBusy] = useState(false)
  const generation = useRef(0)
  const changing = useRef(false)
  useEffect(() => {
    let current = true
    let loading = false
    async function load() {
      if (changing.current || loading || document.visibilityState === 'hidden') return
      loading = true
      const token = ++generation.current
      try {
        const data = await request()
        if (current && token === generation.current) { setRows(data.activations); setError('') }
      } catch (failure) {
        if (current && token === generation.current) setError(humanizeError(failure))
      }
      finally { loading = false }
    }
    load()
    const timer = globalThis.setInterval(load, 15000)
    return () => { current = false; generation.current += 1; globalThis.clearInterval(timer) }
  }, [fetchImpl])
  async function request(path = '', body) {
    const options = {
      method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', redirect: 'error',
      headers: Object.assign({ accept: 'application/json', 'content-type': 'application/json', 'X-Tenant-Id': config.tenant }, authHeaders()),
    }
    if (body !== undefined) options.body = JSON.stringify(body)
    const response = await fetchWithBudget(fetchImpl, `${config.apiBase}/api/lab/activations${path}`, options)
    const data = await response.json()
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail))
    return data
  }
  async function change(path, body) {
    changing.current = true
    const token = ++generation.current
    setBusy(true); setError('')
    try {
      await request(path, body)
      if (token !== generation.current) return
      const data = await request()
      if (token === generation.current) setRows(data.activations)
    } catch (failure) {
      if (token === generation.current) setError(humanizeError(failure))
    }
    finally { changing.current = false; setBusy(false) }
  }
  async function changeReload() {
    const token = ++generation.current
    setBusy(true)
    try {
      const data = await request()
      if (token === generation.current) { setRows(data.activations); setError('') }
    } catch (failure) {
      if (token === generation.current) setError(humanizeError(failure))
    } finally { setBusy(false) }
  }
  const seconds = Number(interval)
  const previewKey = JSON.stringify([docId, published, seconds])
  const validInterval = Number.isInteger(seconds) && seconds >= 60 && seconds <= 86400
  const plan = preview?.key === previewKey ? preview.plan : null
  async function loadPreview() {
    setPreviewBusy(true)
    setPreviewError(null)
    setPreview(null)
    try {
      const query = new URLSearchParams({ preview_doc_id: docId, revision: published, interval_s: seconds })
      const data = await request('?' + query.toString())
      if (data.doc_id !== docId || data.revision !== published || data.interval_s !== seconds
          || !Array.isArray(data.blocks) || !Array.isArray(data.pipes) || !Array.isArray(data.blockers)
          || !data.outputs) throw new Error('Execution plan unavailable. Reload Deploys and try again.')
      setPreview({ key: previewKey, plan: data })
    } catch (failure) {
      setPreviewError({ key: previewKey, message: humanizeError(failure) })
    } finally {
      setPreviewBusy(false)
    }
  }
  const baseReason = busy ? 'Saving the scheduling change.' : published == null
    ? 'Publish a saved revision from History before activating it.'
    : !validInterval
      ? 'Choose a whole number of seconds from 60 to 86400.' : ''
  const activationReason = baseReason || (plan?.blockers.length
    ? 'Resolve the execution plan blockers before activating this revision.' : '')
  const labelCounts = new Map()
  for (const row of rows) {
    const label = scheduleLabel(row)
    labelCounts.set(label, (labelCounts.get(label) || 0) + 1)
  }
  return <section aria-label="Scheduled runs">
    <label>Interval (seconds) <input type="number" min="60" max="86400" step="1" value={interval}
      onChange={event => setInterval(event.target.value)} /></label>
    <button type="button" disabled={Boolean(baseReason) || previewBusy} onClick={loadPreview}>
      {previewBusy ? 'Loading execution plan...' : 'Preview execution plan'}
    </button>
    {plan && <ExecutionPlan plan={plan} />}
    {previewError?.key === previewKey && <p role="alert">{previewError.message}</p>}
    <button type="button" disabled={Boolean(activationReason) || previewBusy} title={activationReason || undefined}
      onClick={() => change('', { doc_id: docId, interval_s: seconds, expected_revision: published })}>
      {published == null ? 'Activate published revision' : `Activate published r${published}`}</button>
    {activationReason && <p role="status">{activationReason}</p>}
    {error && <p role="alert">{error}</p>}
    <button type="button" disabled={busy}
      title={busy ? 'Wait for the scheduling change to finish.' : undefined}
      onClick={() => changeReload()}>Reload schedules</button>
    {rows.length === 0 && <p>No scheduled runs.</p>}
    {rows.map(row => <fieldset key={row.activation_id} style={{ minInlineSize: 0 }}>
      <legend style={{ overflowWrap: 'anywhere' }}>
        {scheduleLabel(row)}
        {labelCounts.get(scheduleLabel(row)) > 1 && <> · Schedule {row.activation_id}</>}
      </legend>
      <p>{({ active: 'Scheduled', paused: 'Paused', expired: 'Expired', stopped: 'Stopped', off: 'Off' })[row.state] || 'Status unavailable'}</p>
      {['pause', 'resume', 'renew', 'off'].map(action => <button key={action} type="button"
        title={busy ? 'Wait for the scheduling change to finish.' : ({
          pause: 'Pause is available while a schedule is active.',
          resume: 'Resume is available while a schedule is paused.',
          renew: 'Renew is available for active, paused or expired schedules.',
          off: 'Turn off is available for active, paused, expired or stopped schedules.',
        })[action]}
        disabled={busy || !(action === 'pause' ? row.state === 'active'
          : action === 'resume' ? row.state === 'paused'
            : action === 'renew' ? ['active', 'paused', 'expired'].includes(row.state)
              : ['active', 'paused', 'expired', 'stopped'].includes(row.state))}
        onClick={() => change(`/${encodeURIComponent(row.activation_id)}/${action}`, {})}>
        {action[0].toUpperCase() + action.slice(1)}</button>)}
    </fieldset>)}
  </section>
}

export default function DeploysView({ hidden = false, client = labArtifactsClient, fetchImpl = activationFetch }) {
  const [, refresh] = useState(0)
  useEffect(() => {
    if (hidden) return undefined
    const unsubscribe = client.subscribe(() => refresh(value => value + 1))
    refresh(value => value + 1)
    return unsubscribe
  }, [client, hidden])
  if (hidden) return null

  const { head, published, lastSaved } = client.state
  const docId = client.currentDocId?.() || 'strategy'
  const publication = published == null ? 'Unpublished' : `Published r${published}`
  const receipt = published != null && lastSaved?.revision === published && lastSaved?.receipt_id
    ? 'Publication confirmed' : 'Publication confirmation unavailable'
  const cards = [
    { title: 'Revision publication', trigger: <><a href={`/app/lab/history${typeof window === 'undefined' ? '' : window.location.search}`}>Open revision history</a><p>Choose a saved revision in History to publish it.</p></>,
      rail: <>{head == null ? 'No saved revision' : `Saved revision ${head}`}. {publication}.</>,
      receipt: <>{receipt}. {lastSaved == null ? 'No saved revision' : `Last saved r${lastSaved.revision}`}.</>,
      rollback: 'Restore the prior revision as a new revision' },
    { title: 'Scheduled runs', trigger: <Activations docId={docId} published={published} fetchImpl={fetchImpl} />,
      rail: publication, receipt: 'Schedule status is shown above.',
      rollback: 'Pause scheduling or turn the schedule off.' },
    { title: 'Widget publication', trigger: <><button type="button" disabled title="Widget publication is not available in this workspace yet.">Publish widget</button><p>Widget publication is not available in this workspace yet.</p></>,
      rail: publication, receipt: 'Widget publication status is not available here.', rollback: 'Available after widget publication is supported.' },
  ]
  return <section aria-label="Deploys">
    <h2>Deploys</h2>
    <ol>
      {cards.map((card, index) => <li key={card.title}>
        <article data-kind={index + 1} data-live={index < 2 ? 'true' : 'false'} style={cardStyle}>
          <h3>{card.title}</h3>
          <dl>
            {['Action', 'Status', 'Confirmation', 'Undo'].map((label, line) => <div key={label}>
              <dt>{label}</dt><dd>{card[['trigger', 'rail', 'receipt', 'rollback'][line]]}</dd>
            </div>)}
          </dl>
        </article>
      </li>)}
    </ol>
    <section aria-label="Deploy state legend">
      <h3>State legend</h3>
      <dl>
        <dt>Published</dt><dd>A saved revision is available. Publishing does not start a run.</dd>
        <dt>Scheduled</dt><dd>The published revision is set to run at the chosen interval.</dd>
        <dt>Paused</dt><dd>Scheduled runs are on hold until you resume them.</dd>
      </dl>
    </section>
  </section>
}
