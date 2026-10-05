import { createContext, useContext, useEffect, useRef, useState } from 'react'
import { getToolRemovalAuthority, stageToolRemoval, publishStagedAuthor, restoreToolCatalog } from '../api.js'

const HistoryContext = createContext(null)

function definiteRefusal(step, error) {
  const body = error?.body
  if (!body || typeof body !== 'object') return false
  const status = error.status
  const reason = body.reason_code
  if (step === 'authority') return [401, 403, 404, 409, 422].includes(status)
  if (step !== 'stage') return false
  if (status === 401) return true
  if (status === 403) return ['tenant_identity_invalid', 'tenant_role_denied'].includes(reason)
    || (!reason && typeof body.detail === 'string' && (
      ['Not authenticated', 'Invalid authentication credentials'].includes(body.detail)
      || body.detail.startsWith('token verified but missing tenant claim ')))
  if (status === 404) return reason === 'customization_publish_disabled'
  if (status === 409) return reason === 'effective_catalog_digest_mismatch'
  return status === 422 && (reason === 'invalid_removal_request' || (!reason && body.detail != null))
}

export function ToolHistoryProvider({ enabled = false, children }) {
  const [operation, setOperation] = useState(null)
  const [history, setHistory] = useState(null)
  const [pending, setPending] = useState(false)
  const [refresh, setRefresh] = useState(null)
  const active = useRef(null)
  const busy = useRef(false)
  const mounted = useRef(true)
  const live = useRef(enabled)
  live.current = enabled
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  function update(op) {
    active.current = op
    if (mounted.current) setOperation(op ? { ...op } : null)
  }

  async function refreshList(callback) {
    if (!mounted.current) return
    if (!callback) {
      setRefresh({ message: 'Catalog changed. Refresh the catalog to update this list.' })
      return
    }
    try {
      await callback()
      if (mounted.current) setRefresh(null)
    } catch {
      if (mounted.current) setRefresh({ callback, message: 'Catalog changed, but the list could not refresh.' })
    }
  }

  function begin(tool, callback) {
    if (!live.current || busy.current || active.current) return
    update({ kind: 'remove', toolName: tool.name, callback, phase: 'confirm' })
  }

  function beginRestore() {
    if (!live.current || busy.current || active.current || !history || history.restored) return
    update({ kind: 'restore', changeSetId: history.staged.predecessor_change_set_id,
      callback: history.callback, phase: 'confirm' })
  }

  async function perform() {
    const op = active.current
    if (!op || !live.current || busy.current) return
    busy.current = true
    setPending(true)
    update({ ...op, phase: 'pending' })
    let step
    try {
      op.key ||= globalThis.crypto.randomUUID()
      if (op.kind === 'remove') {
        if (!op.authority) {
          step = 'authority'
          op.authority = await getToolRemovalAuthority(false, op.toolName)
          if (!mounted.current) return
        }
        if (!op.staged) {
          step = 'stage'
          op.staged = await stageToolRemoval(false, { toolName: op.toolName,
            catalogDigest: op.authority.effective_catalog_digest, idempotencyKey: op.key })
          if (!mounted.current) return
        }
        step = 'publication'
        const result = await publishStagedAuthor(false, op.staged)
        if (!mounted.current) return
        if (result.publication_status === 'awaiting_approval') {
          update({ ...op, phase: 'approval' })
          return
        }
        if (result.publication_status === 'denied') {
          update({ ...op, phase: 'denied' })
          return
        }
        if (result.publication_status !== 'published') throw new Error('Unverified publication')
        setHistory({ staged: op.staged, callback: op.callback, restored: false })
      } else {
        step = 'restore'
        await restoreToolCatalog(false, { changeSetId: op.changeSetId, idempotencyKey: op.key })
        if (!mounted.current) return
        setHistory((entry) => ({ ...entry, restored: true }))
      }
      update(null)
      await refreshList(op.callback)
    } catch (error) {
      const definite = definiteRefusal(step, error)
      if (step === 'stage' && !definite) op.stageOutcomeUnknown = true
      if (mounted.current) update({ ...op, failedStep: step,
        phase: op.kind === 'remove' && !op.staged && !op.stageOutcomeUnknown && definite ? 'refused' : 'error' })
    } finally {
      busy.current = false
      if (mounted.current) setPending(false)
    }
  }

  function retryRefused() {
    const op = active.current
    if (busy.current || !live.current || op?.phase !== 'refused') return
    update({ kind: op.kind, toolName: op.toolName, callback: op.callback })
    perform()
  }

  function dismissRefused() {
    if (busy.current || active.current?.phase !== 'refused') return
    update(null)
  }

  async function retryRefresh() {
    if (busy.current || active.current || !refresh?.callback) return
    busy.current = true
    setPending(true)
    try { await refreshList(refresh.callback) }
    finally {
      busy.current = false
      if (mounted.current) setPending(false)
    }
  }

  const controls = (
    <>
      {operation?.phase === 'confirm' && (
        <div>
          <p>{operation.kind === 'restore'
            ? 'Restore the catalog snapshot from before this removal. This can also change other tools.'
            : 'Remove this authored tool from the catalog after publication?'}</p>
          <button type="button" disabled={pending || !enabled} onClick={perform}>
            {operation.kind === 'restore' ? 'Confirm Restore' : 'Confirm Remove'}
          </button>
          <button type="button" disabled={pending} onClick={() => update(null)}>Cancel</button>
        </div>
      )}
      {pending && <p role="status">Updating the catalog. Please wait.</p>}
      {operation?.phase === 'approval' && (
        <div>
          <p role="status">Removal is awaiting approval. The catalog has not changed.</p>
          <button type="button" disabled={pending || !enabled} onClick={perform}>Check approval</button>
        </div>
      )}
      {operation?.phase === 'denied' && (
        <div>
          <p role="status">Removal was denied. The catalog has not changed.</p>
          <button type="button" onClick={() => update(null)}>Dismiss</button>
        </div>
      )}
      {operation?.phase === 'error' && (
        <div>
          <p role="alert">{operation.kind === 'restore' ? 'The catalog could not be restored. Try again.' : 'The tool removal could not be completed. Try again.'}</p>
          <button type="button" disabled={pending || !enabled} onClick={perform}>
            {operation.kind === 'restore' ? 'Retry restore' : 'Retry removal'}
          </button>
        </div>
      )}
      {operation?.phase === 'refused' && (
        <div>
          <p role="alert">The removal request was refused. Retry to check the current catalog, or dismiss it.</p>
          <button type="button" disabled={pending || !enabled} onClick={retryRefused}>Retry removal</button>
          <button type="button" disabled={pending} onClick={dismissRefused}>Dismiss</button>
        </div>
      )}
      {history && (
        <div aria-label="Tool removal history">
          {history.restored ? <p role="status">Catalog snapshot restored.</p> : (
            <button type="button" disabled={pending || !!operation || !enabled} onClick={beginRestore}>Restore</button>
          )}
        </div>
      )}
      {refresh && (
        <div>
          <p role={refresh.callback ? 'alert' : 'status'}>{refresh.message}</p>
          {refresh.callback && <button type="button" disabled={pending || !!operation} onClick={retryRefresh}>Retry refresh</button>}
        </div>
      )}
    </>
  )
  return (
    <HistoryContext.Provider value={{ enabled, blocked: pending || !!operation, begin }}>
      {typeof children === 'function' ? children(controls) : <>{children}{controls}</>}
    </HistoryContext.Provider>
  )
}

export default function ToolHistory({ tool, onCatalogChanged }) {
  const context = useContext(HistoryContext)
  if (!context) return null
  return (
    <span>
      <button type="button" className="chip-act" disabled={!context.enabled || context.blocked}
        onClick={() => context.begin(tool, onCatalogChanged)}>Remove</button>
      {!context.enabled && <span role="status">Tool management requires a live catalog.</span>}
      {context.enabled && context.blocked && <span role="status">Another catalog action is in progress.</span>}
    </span>
  )
}
