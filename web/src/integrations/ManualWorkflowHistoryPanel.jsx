import { useLayoutEffect, useRef, useState } from 'react'
import { formatDatasetValue, resolveDisplayPreferences } from '../../../vendor/assistant-react/src/datasets/formatting'
import { ManualWorkflowHistoryPageV2 } from './client.js'

const timestamp = { id: 'created', label: 'Created', type: { kind: 'instant' }, nullable: false, sensitivity: 'public' }

export function ManualWorkflowHistoryPanel({ client, projectId, signal, guard, onForbidden, onSelect, disabled = false, displayPreferences, acquire, release }) {
  const [page, setPage] = useState(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const [retry, setRetry] = useState('Load')
  const [forbidden, setForbidden] = useState(false)
  const binding = useRef(null)
  const feedback = useRef(null)
  useLayoutEffect(() => {
    const ctx = { controller: new AbortController(), busy: false, cursors: new Set() }
    binding.current = ctx
    const clear = () => {
      ctx.controller.abort(); ctx.cursors.clear()
      setPage(null); setPending(false); setError(''); setRetry('Load'); setForbidden(true)
    }
    setPage(null); setPending(false); setError(''); setRetry('Load'); setForbidden(false)
    signal.addEventListener('abort', clear, { once: true })
    if (signal.aborted) clear()
    return () => { ctx.controller.abort(); ctx.cursors.clear(); signal.removeEventListener('abort', clear); if (binding.current === ctx) binding.current = null }
  }, [client, projectId, signal])
  useLayoutEffect(() => { if (!pending && (page || error)) feedback.current?.focus() }, [page, error, pending])

  async function read(kind) {
    const ctx = binding.current
    if (!ctx || ctx.busy || disabled || ctx.controller.signal.aborted) return
    const after = kind === 'Next' ? page?.next_cursor : undefined
    if (kind === 'Next' && (!after || ctx.cursors.has(after))) return
    const controller = new AbortController()
    if (acquire && !acquire(controller)) return
    ctx.busy = true; setPending(true); setError(''); setRetry(kind)
    const abort = () => controller.abort()
    ctx.controller.signal.addEventListener('abort', abort, { once: true })
    const current = () => binding.current === ctx && !ctx.controller.signal.aborted
    const assertCurrent = () => { if (!current() || controller.signal.aborted) throw new DOMException('Aborted', 'AbortError') }
    const guarded = async () => { await guard(controller.signal); assertCurrent() }
    let timer, cancel, expired = false
    try {
      const work = async () => {
        await guarded()
        const result = await client.listManualWorkflowRunsV2(projectId, { signal: controller.signal, limit: 25, after })
        assertCurrent()
        await guarded()
        const verified = ManualWorkflowHistoryPageV2.parse(result)
        if (verified.next_cursor && (verified.next_cursor === after || (kind === 'Next' && ctx.cursors.has(verified.next_cursor)))) throw new Error('invalid')
        if (kind !== 'Next') ctx.cursors.clear()
        if (after) ctx.cursors.add(after)
        setPage(verified)
      }
      await Promise.race([work(), new Promise((_, reject) => {
        cancel = () => reject(new DOMException('Aborted', 'AbortError'))
        controller.signal.addEventListener('abort', cancel, { once: true })
        timer = setTimeout(() => { expired = true; controller.abort(); reject(new Error('timeout')) }, 45000)
      })])
    } catch (failure) {
      if (!current() || (controller.signal.aborted && !expired)) return
      if (failure?.status === 401 || failure?.status === 403) {
        ctx.controller.abort(); ctx.cursors.clear()
        setPage(null); setPending(false); setError(''); setForbidden(true); onForbidden(); return
      }
      setError('Workflow history is unavailable. The read could not be verified. Retry history; the last verified page is retained.')
    } finally {
      clearTimeout(timer); controller.signal.removeEventListener('abort', cancel)
      ctx.controller.signal.removeEventListener('abort', abort)
      release?.(controller)
      if (current()) { ctx.busy = false; setPending(false) }
    }
  }

  let preferences
  try { preferences = resolveDisplayPreferences(displayPreferences) } catch { /* Invalid preferences cannot format evidence. */ }
  const locked = disabled || pending || signal.aborted || forbidden
  return <section aria-label="Workflow history" style={{ minWidth: 0, overflowWrap: 'anywhere' }}>
    <h3>Workflow history</h3>
    <p>These are stored run summaries. Opening details refreshes the selected run.</p>
    <button type="button" style={{ minHeight: 44 }} disabled={locked} onClick={() => read('Load')}>{page ? 'Refresh history' : 'Load history'}</button>
    {page?.next_cursor && <button type="button" style={{ minHeight: 44 }} disabled={locked || binding.current?.cursors.has(page.next_cursor)} onClick={() => read('Next')}>Next page</button>}
    {error && <button type="button" style={{ minHeight: 44 }} disabled={locked} onClick={() => read(retry)}>Retry history</button>}
    <div ref={feedback} tabIndex={-1} style={{ minWidth: 0 }}>
      <p role="status" aria-live="polite">{pending ? 'Loading workflow history…' : page ?
        page.items.length ? `${page.items.length} stored run summaries on this page.` : 'No stored runs on this page.' :
          forbidden ? 'Workflow history is unavailable.' : 'Workflow history has not been loaded.'}</p>
      {error && <p role="alert">{error}</p>}
    </div>
    {page && <ol aria-label="Stored workflow runs" style={{ minWidth: 0, paddingInlineStart: '1.5rem' }}>
      {page.items.map((run) => <li key={run.run_id} style={{ minWidth: 0, overflowWrap: 'anywhere' }}>
        <p>{run.workflow_ref.id} · Version {run.workflow_ref.version} · {run.state}</p>
        <p>Created <time dateTime={run.created_at}>{preferences ? formatDatasetValue(timestamp, run.created_at, preferences) : run.created_at}</time></p>
        <p>{run.run_id}</p>
        <button type="button" style={{ minHeight: 44, maxWidth: '100%', whiteSpace: 'normal' }} disabled={locked}
          aria-label={`Open run details ${run.run_id}`} onClick={() => {
            const ctx = binding.current
            if (ctx && !ctx.busy && !disabled && !ctx.controller.signal.aborted) onSelect(run)
          }}>Open run details</button>
      </li>)}
    </ol>}
  </section>
}
