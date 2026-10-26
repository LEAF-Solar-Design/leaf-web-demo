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

function Activations({ client, published, fetchImpl }) {
  const [rows, setRows] = useState([])
  const [interval, setInterval] = useState('60')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
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
  const activationReason = busy ? 'Saving the scheduling change.' : published == null
    ? 'Publish a saved revision from History before activating it.'
    : !Number.isInteger(seconds) || seconds < 60 || seconds > 86400
      ? 'Choose a whole number of seconds from 60 to 86400.' : ''
  return <section aria-label="Scheduled runs">
    <label>Interval (seconds) <input type="number" min="60" max="86400" step="1" value={interval}
      onChange={event => setInterval(event.target.value)} /></label>
    <button type="button" disabled={Boolean(activationReason)} title={activationReason || undefined}
      onClick={() => change('', { doc_id: client.currentDocId?.() || 'strategy', interval_s: seconds })}>
      {published == null ? 'Activate published revision' : `Activate published r${published}`}</button>
    {activationReason && <p role="status">{activationReason}</p>}
    {error && <p role="alert">{error}</p>}
    <button type="button" disabled={busy}
      title={busy ? 'Wait for the scheduling change to finish.' : undefined}
      onClick={() => changeReload()}>Reload schedules</button>
    {rows.length === 0 && <p>No scheduled runs.</p>}
    {rows.map(row => <div key={row.activation_id}>
      <p>Revision {row.revision}: {({ active: 'Scheduled', paused: 'Paused', expired: 'Expired', stopped: 'Stopped', off: 'Off' })[row.state] || 'Status unavailable'}. Runs every {row.interval_s} seconds.</p>
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
    </div>)}
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
  const publication = published == null ? 'Unpublished' : `Published r${published}`
  const receipt = published != null && lastSaved?.revision === published && lastSaved?.receipt_id
    ? 'Publication confirmed' : 'Publication confirmation unavailable'
  const cards = [
    { title: 'Revision publication', trigger: <><a href={`/app/lab/history${typeof window === 'undefined' ? '' : window.location.search}`}>Open revision history</a><p>Choose a saved revision in History to publish it.</p></>,
      rail: <>{head == null ? 'No saved revision' : `Saved revision ${head}`}. {publication}.</>,
      receipt: <>{receipt}. {lastSaved == null ? 'No saved revision' : `Last saved r${lastSaved.revision}`}.</>,
      rollback: 'Restore the prior revision as a new revision' },
    { title: 'Scheduled runs', trigger: <Activations client={client} published={published} fetchImpl={fetchImpl} />,
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
