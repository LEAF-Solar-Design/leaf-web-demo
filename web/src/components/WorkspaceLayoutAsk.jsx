import { useEffect, useRef, useState } from 'react'
import { getSurfaceConfig, submitSurfaceConfig } from '../api.js'
import { refreshSurfaceConfigOverlay } from '../site/useSurfaceContract.js'
import { confirmedLayout, headerField, headerPosition, patchHeader, requestedHeaderPosition, sameHeaderField } from '../site/workspaceLayout.js'

export default function WorkspaceLayoutAsk({ profile, scopeKey, position, mock = true, canApplyLayout = false, onPreview, onClose, onBusyChange }) {
  const [request, setRequest] = useState('')
  const [choice, setChoice] = useState(headerPosition(position))
  const [status, setStatus] = useState('Choose a position or ask to move the workspace header.')
  const [busy, setBusy] = useState(false)
  const [undo, setUndo] = useState(null)
  const [uncertain, setUncertain] = useState(false)
  const identity = JSON.stringify([scopeKey, profile])
  const authority = useRef({ identity, eligible: !mock && canApplyLayout === true })
  authority.current = { identity, eligible: !mock && canApplyLayout === true }
  const alive = useRef(true)
  const inFlight = useRef(false)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const current = () => alive.current && authority.current.identity === identity && authority.current.eligible
  const preview = value => {
    setChoice(value)
    onPreview(value)
    setStatus(`Preview: workspace header ${value === 'above-tools' ? 'above' : 'below'} CAD tools.`)
  }
  async function write(reverting = false) {
    if (inFlight.current || !current() || uncertain || (reverting && !undo)) return
    inFlight.current = true
    setBusy(true)
    onBusyChange?.(true)
    let submitted = false
    try {
      const live = await getSurfaceConfig(false, { fresh: true })
      if (!current()) return
      const before = headerField(live?.surfaces, profile)
      if (before.present && !['above-tools', 'below-tools'].includes(before.value)) throw new Error('The current header setting is not supported. Nothing was changed.')
      if (reverting && !sameHeaderField(before, undo.applied)) throw new Error('The header changed since Apply. Undo was not submitted.')
      const field = reverting ? undo.before : { present: true, value: choice }
      const next = patchHeader(live?.surfaces, profile, field)
      if (sameHeaderField(before, field)) {
        setStatus('This header position is already saved.')
        onPreview(null)
        return
      }
      submitted = true
      const receipt = await submitSurfaceConfig(next)
      if (!current()) return
      const readback = await getSurfaceConfig(false, { fresh: true })
      if (!current()) return
      if (!confirmedLayout(receipt, readback, next)) throw new Error('The save was not confirmed. Reload workspace settings before another change.')
      await refreshSurfaceConfigOverlay()
      if (!current()) return
      setUndo(reverting ? null : { before, applied: field })
      setChoice(headerPosition(field.value))
      onPreview(null)
      setStatus(reverting ? 'Layout restored.' : 'Layout saved.')
    } catch (error) {
      if (alive.current && authority.current.identity === identity) {
        if (submitted) setUncertain(true)
        setStatus(submitted ? 'Save outcome is unconfirmed. Reload workspace settings before another change.' : error.message)
      }
    } finally {
      inFlight.current = false
      if (alive.current && authority.current.identity === identity) {
        setBusy(false)
        onBusyChange?.(false)
      }
    }
  }
  return <section className="workspace-layout-panel" aria-label="Workspace layout">
    <div className="workspace-layout-heading"><strong>Workspace header</strong><button type="button" disabled={busy} onClick={onClose} aria-label="Close layout">Close</button></div>
    <p>{!mock && canApplyLayout ? 'Preview here, then save this profile’s layout to the workspace.' : 'Preview only. Saving requires an eligible signed-in workspace.'}</p>
    <form onSubmit={event => {
      event.preventDefault()
      const value = requestedHeaderPosition(request)
      if (value) preview(value)
      else setStatus('Ask to put the workspace header above or below the CAD tools. Drawing changes are not supported here.')
    }}>
      <label>Layout request<input value={request} maxLength={300} disabled={busy} onChange={event => setRequest(event.target.value)} placeholder="Move the workspace header below the tools" /></label>
      <button type="submit" disabled={busy || !request.trim()}>Preview request</button>
    </form>
    <div role="group" aria-label="Header position">
      <button type="button" aria-pressed={choice === 'above-tools'} disabled={busy} onClick={() => preview('above-tools')}>Above tools</button>
      <button type="button" aria-pressed={choice === 'below-tools'} disabled={busy} onClick={() => preview('below-tools')}>Below tools</button>
    </div>
    <div className="workspace-layout-actions">
      <button type="button" disabled={busy} onClick={() => { onPreview(null); setChoice(headerPosition(position)); setStatus('Preview cancelled.') }}>Cancel preview</button>
      <button type="button" disabled={busy || mock || !canApplyLayout || uncertain} onClick={() => write()}>Apply layout</button>
      <button type="button" disabled={busy || mock || !canApplyLayout || uncertain || !undo} onClick={() => write(true)}>Undo layout</button>
    </div>
    <p role="status" aria-live="polite">{busy ? 'Saving workspace layout…' : status}</p>
  </section>
}
