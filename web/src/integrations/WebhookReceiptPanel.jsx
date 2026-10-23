import { useLayoutEffect, useRef, useState } from 'react'
import { validateWebhookReceipt, validateWebhookReceiptInput } from './client.js'

const emptyForm = () => ({ registrationId: '', deliveryId: '' })
const inputStyle = { boxSizing: 'border-box', width: '100%', minWidth: 0, maxWidth: '100%' }
const fields = [
  ['registrationId', 'Registration ID'], ['registrationRevision', 'Registration revision'],
  ['connectionId', 'Connection ID'], ['provider', 'Provider'], ['deliveryId', 'Delivery ID'],
  ['payloadSha256', 'Payload SHA-256'], ['byteLength', 'Byte length'],
  ['connectionRevision', 'Connection revision'], ['createdBy', 'Created by'], ['createdAt', 'Created at'],
]

export function WebhookReceiptPanel({ client, projectId, signal, guard, onForbidden }) {
  const [form, setForm] = useState(emptyForm)
  const [result, setResult] = useState(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const [forbidden, setForbidden] = useState(false)
  const binding = useRef(null)
  const feedback = useRef(null)
  useLayoutEffect(() => {
    const ctx = { controller: new AbortController(), request: null }
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
    const ctx = binding.current
    if (!ctx || ctx.controller.signal.aborted || forbidden) return
    ctx.request?.abort(); ctx.request = null
    setForm((old) => ({ ...old, [key]: value })); setResult(null); setError(''); setPending(false)
  }
  async function lookup(event) {
    event.preventDefault()
    const ctx = binding.current
    if (!ctx || ctx.request || ctx.controller.signal.aborted || forbidden) return
    setResult(null); setError('')
    let input
    try { input = validateWebhookReceiptInput(form) } catch {
      setError('Enter IDs of 1 to 128 ASCII characters, starting with a letter or number. Use letters, numbers, dots, underscores or hyphens; delivery IDs may also contain colons.')
      return
    }
    const controller = new AbortController()
    ctx.request = controller; setPending(true)
    const abort = () => controller.abort()
    ctx.controller.signal.addEventListener('abort', abort, { once: true })
    const current = () => binding.current === ctx && !ctx.controller.signal.aborted && ctx.request === controller
    const assertCurrent = () => { if (!current() || controller.signal.aborted) throw new DOMException('Aborted', 'AbortError') }
    const guarded = async () => { await guard(controller.signal); assertCurrent() }
    let timer, cancel
    try {
      const work = async () => {
        await guarded()
        const value = await client.getWebhookReceipt(projectId, input.registrationId, input.deliveryId, { signal: controller.signal })
        assertCurrent()
        await guarded()
        setResult(validateWebhookReceipt(value, input))
      }
      await Promise.race([work(), new Promise((_, reject) => {
        cancel = () => reject(new DOMException('Aborted', 'AbortError'))
        controller.signal.addEventListener('abort', cancel, { once: true })
        timer = setTimeout(() => { controller.abort(); reject(new Error('timeout')) }, 45000)
        if (controller.signal.aborted) cancel()
      })])
    } catch (failure) {
      if (!current()) return
      setResult(null)
      if (failure?.status === 401 || failure?.status === 403) {
        ctx.controller.abort(); setForm(emptyForm()); setPending(false); setForbidden(true); onForbidden(); return
      }
      setError(failure?.status === 404 ? 'No accessible receipt was returned for these IDs.' :
        'Receipt lookup is unavailable. Please retry using Look up receipt.')
    } finally {
      clearTimeout(timer); controller.signal.removeEventListener('abort', cancel)
      ctx.controller.signal.removeEventListener('abort', abort)
      if (current()) { ctx.request = null; setPending(false) }
    }
  }

  const locked = forbidden || signal.aborted
  return <section aria-label="Webhook admission receipt" aria-busy={pending} style={{ minWidth: 0, maxWidth: '100%', overflowWrap: 'anywhere' }}>
    <h2>Webhook admission receipt</h2>
    <p>Read stored admission evidence only. A receipt proves admission integrity and durable deduplication. It does not prove event identity, provider execution or exactly-once side effects.</p>
    <form aria-label="Look up webhook receipt" onSubmit={lookup} noValidate>
      <fieldset disabled={locked} style={{ minWidth: 0, margin: 0 }}><legend>Receipt IDs</legend>
        <label>Registration ID<input style={inputStyle} value={form.registrationId} maxLength={128} onChange={(event) => edit('registrationId', event.target.value)} /></label>
        <label>Delivery ID<input style={inputStyle} value={form.deliveryId} maxLength={128} onChange={(event) => edit('deliveryId', event.target.value)} /></label>
        <button type="submit" disabled={locked || pending} style={{ maxWidth: '100%', whiteSpace: 'normal' }}>Look up receipt</button>
      </fieldset>
    </form>
    <div ref={feedback} tabIndex={-1} style={{ minWidth: 0, overflowWrap: 'anywhere' }}>
      <p role="status" aria-live="polite">{pending ? 'Looking up receipt…' : result ? 'Admission receipt ready.' : forbidden ? 'Receipt lookup is unavailable.' : 'No receipt looked up.'}</p>
      {error && <p role="alert">{error}</p>}
      {result && <div role="region" aria-label="Stored admission evidence">
        <dl>{fields.map(([key, label]) => <div key={key}><dt>{label}</dt><dd style={{ marginInlineStart: 0, overflowWrap: 'anywhere' }}>{result[key]}</dd></div>)}</dl>
      </div>}
    </div>
  </section>
}
