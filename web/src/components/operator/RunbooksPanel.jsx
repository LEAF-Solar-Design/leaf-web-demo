/**
 * RunbooksPanel — "fleet and tenant inspection (read-only)" + "approval
 * cards built from server truth" (Wave 2 Lane E plan). Every read is one of
 * the runbook `.../state` GETs or `/api/operator/external/destinations`; no
 * endpoint here is invented.
 *
 * A `propose` response is held in local state ONLY until its ApprovalCard
 * either executes it (removed on success) or the operator moves on — it is
 * never persisted, and a signed-out reset (acceptance #4) drops it entirely.
 */
import { useCallback, useState } from 'react'

import * as operatorClient from '../../operatorClient.js'
import { isRedactedField, renderFieldValue } from '../../projects/ReceiptPanel.jsx'
import ApprovalCard from './ApprovalCard.jsx'
import { fieldLabel, normalizeApproval } from './approvalDigest.js'

const TENANT_ID_RE = /^[a-z0-9][a-z0-9_-]{0,62}$/
const HANDLE_RE = /^[a-z0-9_.:-]{1,64}$/

const KEY_STYLE = { fontFamily: 'var(--font-mono)' }

// The destination's headline field: `destination` when present (the common
// case — every real payload from /api/operator/external/destinations names
// one), else the first string-valued key so an unfamiliar shape still shows
// something readable instead of silently falling through to raw JSON.
function primaryField(d) {
  if (typeof d?.destination === 'string' && d.destination) return 'destination'
  const entry = Object.entries(d || {}).find(([, v]) => typeof v === 'string')
  return entry ? entry[0] : null
}

function fieldText(fieldKey, value) {
  return isRedactedField(fieldKey, value) ? '[redacted]' : renderFieldValue(value)
}

// A destination row: its headline field inline, and every remaining field
// behind a `▸`/`▾` disclosure toggle (the house pattern ConversePanel's
// expandable tool chip uses) — never a raw JSON.stringify(d) dump.
function DestinationRow({ destination: d, index }) {
  const [open, setOpen] = useState(false)
  const primaryKey = primaryField(d)
  const rest = Object.entries(d || {}).filter(([k]) => k !== primaryKey)
  const rowKey = primaryKey ? fieldText(primaryKey, d[primaryKey]) : `destination-${index}`
  return (
    <li>
      <span>{primaryKey ? fieldText(primaryKey, d[primaryKey]) : renderFieldValue(d)}</span>
      {rest.length > 0 && (
        <>
          {' '}
          <button
            type="button"
            className="chip-act"
            aria-expanded={open}
            aria-label={open ? `Hide details for ${rowKey}` : `Show details for ${rowKey}`}
            onClick={() => setOpen((v) => !v)}
          >
            {open ? '▾ details' : '▸ details'}
          </button>
          {open && (
            <dl className="operator-readout">
              {rest.map(([k, v]) => (
                <div key={k} className="operator-field-row">
                  <dt style={KEY_STYLE}>{k}</dt>
                  <dd>{fieldText(k, v)}</dd>
                </div>
              ))}
            </dl>
          )}
        </>
      )}
    </li>
  )
}

export default function RunbooksPanel({ sessionEnvironment, onSignedOut }) {
  const [tenantId, setTenantId] = useState('')
  const [tenantState, setTenantState] = useState(null)
  const [overlayState, setOverlayState] = useState(null)
  const [handle, setHandle] = useState('')
  const [credState, setCredState] = useState(null)
  const [destinations, setDestinations] = useState(null)
  const [proposals, setProposals] = useState([]) // [{ key, kind, response }]
  const [error, setError] = useState(null)

  const guard = useCallback((e, fallback) => {
    setError(e?.body?.detail || e?.message || fallback)
    if (operatorClient.isOperatorDenied(e)) onSignedOut?.()
  }, [onSignedOut])

  const loadTenant = async () => {
    if (!TENANT_ID_RE.test(tenantId)) { setError('Enter a valid tenant id.'); return }
    setError(null)
    try {
      const [agent, overlay] = await Promise.all([
        operatorClient.tenantAgentState(tenantId),
        operatorClient.tenantOverlayState(tenantId),
      ])
      setTenantState(agent)
      setOverlayState(overlay)
    } catch (e) { guard(e, 'Could not load tenant state.') }
  }

  const proposeTenantVerb = async (verb) => {
    setError(null)
    try {
      const response = await operatorClient.tenantAgentPropose(verb, tenantId)
      setProposals((p) => [...p, { key: response.authority_id, kind: 'tenant-agent', verb, response }])
    } catch (e) { guard(e, `Could not propose ${verb}.`) }
  }

  const loadCredential = async () => {
    if (!HANDLE_RE.test(handle)) { setError('Enter a valid credential handle.'); return }
    setError(null)
    try {
      setCredState(await operatorClient.credentialState(handle))
    } catch (e) { guard(e, 'Could not load credential state.') }
  }

  const proposeCredentialRotate = async () => {
    setError(null)
    try {
      const response = await operatorClient.credentialPropose(handle)
      setProposals((p) => [...p, { key: response.authority_id, kind: 'credential', response }])
    } catch (e) { guard(e, 'Could not propose the rotation.') }
  }

  const loadDestinations = async () => {
    setError(null)
    try {
      setDestinations(await operatorClient.externalDestinations())
    } catch (e) { guard(e, 'Could not load external destinations.') }
  }

  const executeProposal = async (item) => {
    if (item.kind === 'tenant-agent') {
      await operatorClient.tenantAgentExecute(item.verb, tenantId, item.response.authority_id)
      await loadTenant()
    } else if (item.kind === 'credential') {
      await operatorClient.credentialExecute(handle, item.response.authority_id)
      await loadCredential()
    }
    setProposals((p) => p.filter((x) => x.key !== item.key))
  }

  return (
    <section className="operator-panel operator-runbooks-panel" aria-label="Fleet and tenant inspection">
      <h2>Fleet &amp; tenant inspection</h2>

      <div className="operator-field-row">
        <label htmlFor="operator-tenant-id">Tenant ID</label>
        <input id="operator-tenant-id" value={tenantId} onChange={(e) => setTenantId(e.target.value)} />
        <button type="button" className="chip-act" onClick={loadTenant}>Load tenant state</button>
      </div>
      {tenantState && (
        <dl className="operator-readout">
          <dt>agent_disabled</dt><dd>{String(tenantState.agent_disabled)}</dd>
          <dt>revision</dt><dd>{String(tenantState.revision)}</dd>
        </dl>
      )}
      {overlayState && (
        <dl className="operator-readout">
          <dt>overlay revision</dt><dd>{String(overlayState.revision)}</dd>
        </dl>
      )}
      {tenantState && (
        <div className="operator-field-row">
          <button type="button" className="chip-act" onClick={() => proposeTenantVerb('pause')}>Propose pause</button>
          <button type="button" className="chip-act" onClick={() => proposeTenantVerb('resume')}>Propose resume</button>
        </div>
      )}

      <div className="operator-field-row">
        <label htmlFor="operator-credential-handle">Credential handle</label>
        <input id="operator-credential-handle" value={handle} onChange={(e) => setHandle(e.target.value)} />
        <button type="button" className="chip-act" onClick={loadCredential}>Load credential state</button>
      </div>
      {credState && (
        <dl className="operator-readout">
          <dt>revision</dt><dd>{String(credState.revision)}</dd>
          <dt>rotated_at</dt><dd>{String(credState.rotated_at)}</dd>
        </dl>
      )}
      {credState && (
        <button type="button" className="chip-act" onClick={proposeCredentialRotate}>Propose rotate</button>
      )}

      <div className="operator-field-row">
        <button type="button" className="chip-act" onClick={loadDestinations}>Load external destinations</button>
      </div>
      {destinations && (
        <ul className="operator-readout-list">
          {destinations.map((d, i) => (
            <DestinationRow key={(typeof d?.destination === 'string' && d.destination) || i} destination={d} index={i} />
          ))}
          {destinations.length === 0 && <li className="dim">No destinations configured.</li>}
        </ul>
      )}

      {proposals.map((item) => {
        const { action, authorityId, digest, missing } = normalizeApproval(item.response, sessionEnvironment)
        return (
          <ApprovalCard
            key={item.key}
            action={action}
            authorityId={authorityId}
            digest={digest}
            missing={missing}
            fieldLabel={fieldLabel}
            onExecute={() => executeProposal(item)}
          />
        )
      })}

      {error && <p className="operator-panel-error" role="alert">{error}</p>}
    </section>
  )
}
