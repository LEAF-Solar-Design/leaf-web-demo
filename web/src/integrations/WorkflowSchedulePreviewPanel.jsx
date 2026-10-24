import { useLayoutEffect, useRef, useState } from 'react'
import { validateWorkflowSchedulePreview, validateWorkflowSchedulePreviewInput } from './client.js'

const emptyForm = () => ({ timezone: '', hour: '', minute: '', after: '' })
const inputStyle = { boxSizing: 'border-box', width: '100%', minWidth: 0, maxWidth: '100%' }

export function WorkflowSchedulePreviewPanel({ client, projectId, signal, guard, onForbidden }) {
  const [form, setForm] = useState(emptyForm)
  const [result, setResult] = useState(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const [forbidden, setForbidden] = useState(false)
  const binding = useRef(null)
  const feedback = useRef(null)
  useLayoutEffect(() => {
    const ctx = { controller: new AbortController(), busy: false }
    binding.current = ctx
    const clear = () => {
      ctx.controller.abort()
      setForm(emptyForm()); setResult(null); setPending(false); setError(''); setForbidden(true)
    }
    setForm(emptyForm()); setResult(null); setPending(false); setError(''); setForbidden(false)
    signal.addEventListener('abort', clear, { once: true })
    if (signal.aborted) clear()
    return () => { ctx.controller.abort(); signal.removeEventListener('abort', clear); if (binding.current === ctx) binding.current = null }
  }, [client, projectId, signal])
  useLayoutEffect(() => { if (!pending && (result || error)) feedback.current?.focus() }, [pending, result, error])

  function edit(key, value) {
    if (binding.current?.busy || signal.aborted || forbidden) return
    setForm((old) => ({ ...old, [key]: value })); setResult(null); setError('')
  }
  async function preview(event) {
    event.preventDefault()
    const ctx = binding.current
    if (!ctx || ctx.busy || ctx.controller.signal.aborted || forbidden) return
    setResult(null); setError('')
    let input
    try {
      if (!/^\d{1,2}$/.test(form.hour) || !/^\d{1,2}$/.test(form.minute)) throw new Error('invalid')
      input = validateWorkflowSchedulePreviewInput({ schema_version: 1,
        schedule: { kind: 'daily', timezone: form.timezone, hour: Number(form.hour), minute: Number(form.minute) }, after: form.after })
    } catch {
      setError('Enter a recognized IANA timezone, hour 0 to 23, minute 0 to 59, and a real UTC ISO instant in years 2000 to 2099, including seconds and Z.')
      feedback.current?.focus()
      return
    }
    ctx.busy = true; setPending(true)
    const controller = new AbortController()
    const abort = () => controller.abort()
    ctx.controller.signal.addEventListener('abort', abort, { once: true })
    const current = () => binding.current === ctx && !ctx.controller.signal.aborted
    const assertCurrent = () => { if (!current() || controller.signal.aborted) throw new DOMException('Aborted', 'AbortError') }
    const guarded = async () => { await guard(controller.signal); assertCurrent() }
    let timer, cancel
    try {
      const work = async () => {
        await guarded()
        const value = await client.previewWorkflowSchedule(projectId, input, { signal: controller.signal })
        assertCurrent()
        await guarded()
        setResult(validateWorkflowSchedulePreview(value, input).occurrence)
      }
      await Promise.race([work(), new Promise((_, reject) => {
        cancel = () => reject(new DOMException('Aborted', 'AbortError'))
        controller.signal.addEventListener('abort', cancel, { once: true })
        timer = setTimeout(() => { controller.abort(); reject(new Error('timeout')) }, 45000)
      })])
    } catch (failure) {
      if (!current()) return
      setResult(null)
      if (failure?.status === 401 || failure?.status === 403) {
        ctx.controller.abort(); setPending(false); setForbidden(true); onForbidden(); return
      }
      setError('Schedule preview is unavailable. Please retry using Preview next occurrence.')
    } finally {
      clearTimeout(timer); controller.signal.removeEventListener('abort', cancel)
      ctx.controller.signal.removeEventListener('abort', abort)
      if (current()) { ctx.busy = false; setPending(false) }
    }
  }

  const locked = pending || forbidden || signal.aborted
  return <section aria-label="Daily schedule preview" style={{ minWidth: 0, maxWidth: '100%', overflowWrap: 'anywhere' }}>
    <h2>Daily schedule preview</h2>
    <p>Preview only. No schedule is saved or activated.</p>
    <p>A missing local time moves to the next valid time. A repeated local time uses the first occurrence.</p>
    <form aria-label="Preview daily schedule" onSubmit={preview} noValidate>
      <fieldset disabled={locked} style={{ minWidth: 0, margin: 0 }}><legend>Daily schedule</legend>
        <label>IANA timezone<input style={inputStyle} value={form.timezone} maxLength={100} placeholder="America/New_York" onChange={(event) => edit('timezone', event.target.value)} /></label>
        <label>Daily hour<input style={inputStyle} inputMode="numeric" value={form.hour} placeholder="9 (0 to 23)" onChange={(event) => edit('hour', event.target.value)} /></label>
        <label>Daily minute<input style={inputStyle} inputMode="numeric" value={form.minute} placeholder="30 (0 to 59)" onChange={(event) => edit('minute', event.target.value)} /></label>
        <label>After (UTC)<input style={inputStyle} value={form.after} placeholder="2026-01-01T00:00:00Z" onChange={(event) => edit('after', event.target.value)} /></label>
        <button type="submit" disabled={locked} style={{ maxWidth: '100%', whiteSpace: 'normal' }}>Preview next occurrence</button>
      </fieldset>
    </form>
    <div ref={feedback} tabIndex={-1} style={{ minWidth: 0, overflowWrap: 'anywhere' }}>
      <p role="status" aria-live="polite">{pending ? 'Calculating schedule preview…' : result ? 'Schedule preview ready.' :
        forbidden ? 'Schedule preview is unavailable.' : 'No occurrence previewed.'}</p>
      {error && <p role="alert">{error}</p>}
      {result && <div role="region" aria-label="Next scheduled occurrence">
        <p>Scheduled local date: {result.scheduledLocalDate}</p>
        <p>Schedule timezone: {result.timezone}</p>
        <p>Daily time: {String(result.hour).padStart(2, '0')}:{String(result.minute).padStart(2, '0')}</p>
        <p>UTC occurrence: <time dateTime={result.instant}>{result.instant}</time></p>
      </div>}
    </div>
  </section>
}
