import { useLayoutEffect, useRef, useState } from 'react'
import { UUID, validateVercelDestinations } from './client.js'

// This preset is imported only. It is never executed by this account-linking flow.
// Pinned v10 contract: a top-level array of project objects, without query bindings.
export const vercelDocument = {
  openapi: '3.0.3', info: { title: 'Vercel account connection', version: '1.0.0' },
  servers: [{ url: 'https://api.vercel.com' }],
  components: { securitySchemes: { bearerAuth: { type: 'http', scheme: 'bearer' } } },
  security: [{ bearerAuth: [] }],
  paths: { '/v10/projects': { get: { operationId: 'list-projects', summary: 'List projects',
    responses: { 200: { description: 'Project records', content: { 'application/json': {
      schema: { type: 'array', items: { type: 'object', additionalProperties: true } },
    } } } },
  } } },
}
const sameRef = (a, b) => a && b && ['id', 'version', 'digest'].every((key) => a[key] === b[key])
const scopeKey = (scope, projectId) => JSON.stringify([projectId, Object.keys(scope).sort().map((key) => [key, scope[key]])])
const canonicalOwner = (scope, projectId) => scope.projectRole === 'owner' && scope.projectId === projectId &&
  ['tenantId', 'projectId', 'platformUserId', 'bindingId'].every((key) => UUID.test(scope[key] || '')) &&
  scope.externalAuthority === 'auth0' && typeof scope.externalSubject === 'string' && scope.externalSubject.length > 0
function validDefinition(resource, definition) {
  const op = definition?.operations?.[0]
  return resource.auth_kind === 'bearer' && resource.operation_count === 1 && definition?.protocol === 'https-rest' &&
    definition.origins.length === 1 && definition.origins[0] === 'https://api.vercel.com' && definition.auth.kind === 'bearer' &&
    definition.operations.length === 1 && op.method === 'GET' && op.path === '/v10/projects' && op.effect === 'read' &&
    op.pagination.kind === 'none' && op.pagination.records_pointer === '' && op.idempotency.kind === 'read' && !op.adapter_ref
}
const safeId = (value) => typeof value === 'string' && /^[A-Za-z0-9_-]{1,256}(?![\s\S])/.test(value)
const emptyView = { busy: false, phase: 'empty', unknown: false, message: '', error: '', linked: null }

export function VercelConnectionPanel({ client, projectId, scope, signal, guard, onForbidden }) {
  const key = scopeKey(scope, projectId)
  const latest = useRef(null)
  latest.current = { key, signal, client, scope, guard }
  const context = useRef(null)
  const [view, setView] = useState(emptyView)
  function clearPending(ctx) {
    const pending = ctx.pending
    ctx.pending = null
    if (pending) {
      clearTimeout(pending.timer)
      pending.channel.onmessage = null
      pending.channel.close()
      pending.state = ''; pending.expiry = 0
    }
    try { ctx.popup?.close() } catch { /* COOP may sever the handle. */ }
    ctx.popup = null
    if (ctx.callback) { ctx.callback.code = ''; ctx.callback.state = ''; ctx.callback = null }
  }
  useLayoutEffect(() => {
    const ctx = { key, signal, client, controller: new AbortController(), phase: 'empty', busy: false, unknown: false, pending: null, states: new Set() }
    context.current = ctx
    setView(emptyView)
    const dispose = () => { clearPending(ctx); ctx.controller.abort(); ctx.linked = null; ctx.states.clear() }
    if (signal.aborted) dispose()
    else signal.addEventListener('abort', dispose, { once: true })
    return () => { dispose(); signal.removeEventListener('abort', dispose); if (context.current === ctx) context.current = null }
  }, [key, signal, client])
  function current(ctx) {
    return context.current === ctx && latest.current.key === ctx.key && latest.current.signal === ctx.signal &&
      latest.current.client === ctx.client && !signal.aborted && !ctx.controller.signal.aborted && canonicalOwner(latest.current.scope, projectId)
  }
  function assertCurrent(ctx) { if (!current(ctx)) throw new DOMException('Aborted', 'AbortError') }
  async function guarded(ctx) { assertCurrent(ctx); await latest.current.guard(ctx.controller.signal); assertCurrent(ctx) }
  function update(ctx, patch = {}) {
    if (!current(ctx)) return
    Object.assign(ctx, patch)
    setView({ busy: ctx.busy, phase: ctx.phase, unknown: ctx.unknown, message: ctx.message || '', error: ctx.error || '', linked: ctx.linked || null })
  }
  const options = (ctx) => ({ signal: ctx.controller.signal })
  function matchesConnection(ctx, value) {
    return value.id === ctx.connectionId && value.title === 'Vercel account' && value.owner_id === scope.platformUserId &&
      sameRef(value.connector_ref, ctx.published.reference) && value.origin === 'https://api.vercel.com' &&
      value.auth_kind === 'bearer' && value.operation_ids.length === 1 && value.operation_ids[0] === ctx.operationId &&
      !['revoked', 'disabled'].includes(value.state)
  }
  function verifyLinked(ctx, value) {
    const result = ctx.completed
    if (!result || !matchesConnection(ctx, value) || value.revision !== result.revision || result.ownerId !== (result.teamId ?? result.userId) ||
        !value.credential_purposes.includes('access_token') || value.cleanup !== result.cleanup) throw new Error('readback')
    ctx.connection = value
    ctx.phase = 'linked'; ctx.unknown = false
    ctx.linked = { team: result.teamId !== null, cleanup: result.cleanup }
    ctx.message = 'Vercel account linked.'
  }
  async function reconcile(ctx) {
    await guarded(ctx)
    if (ctx.phase === 'import') {
      const value = await client.getConnector(projectId, ctx.connectorId, options(ctx))
      assertCurrent(ctx)
      if (!ctx.imported || !sameRef(value.reference, ctx.imported.reference) || value.revision !== ctx.imported.revision ||
          value.state !== 'draft' || value.title !== 'Vercel account connection' || !validDefinition(value, value.draft_definition)) throw new Error('readback')
      ctx.draft = value; ctx.operationId = value.draft_definition.operations[0].id; ctx.phase = 'publish'
    } else if (ctx.phase === 'publish') {
      const value = await client.getConnectorVersion(projectId, ctx.connectorId, ctx.draft.reference.version, options(ctx))
      assertCurrent(ctx)
      if (!ctx.publication || !sameRef(value.reference, ctx.publication.reference) || value.revision !== ctx.publication.revision ||
          !validDefinition(value, value.definition) || value.operationReferences.length !== 1 || value.operationReferences[0].id !== ctx.operationId ||
          JSON.stringify(value.definition.operations[0]) !== JSON.stringify(ctx.draft.draft_definition.operations[0])) throw new Error('readback')
      ctx.published = value; ctx.phase = 'connection'
    } else if (ctx.phase === 'connection') {
      const value = await client.getConnectionSummary(projectId, ctx.connectionId, options(ctx))
      assertCurrent(ctx)
      if (!ctx.created || value.revision !== ctx.created.revision || !matchesConnection(ctx, value) ||
          value.credential_purposes.length !== 0 || value.cleanup !== 'complete') throw new Error('readback')
      ctx.connection = value; ctx.phase = 'ready'
    } else if (ctx.phase === 'complete') {
      const value = await client.getConnectionSummary(projectId, ctx.connectionId, options(ctx))
      await guarded(ctx)
      verifyLinked(ctx, value)
    } else {
      // An issue response cannot be recovered from a connection summary. Never issue again on uncertainty.
      await client.getConnectionSummary(projectId, ctx.connectionId, options(ctx))
      assertCurrent(ctx)
      throw new Error('unconfirmed')
    }
    ctx.unknown = false
  }
  async function mutate(ctx, call) {
    await guarded(ctx)
    ctx.unknown = true
    try { const value = await call(); assertCurrent(ctx); return value }
    catch (failure) { if (failure?.dispatched === false) ctx.unknown = false; throw failure }
  }
  async function work(ctx, task) {
    if (!current(ctx) || ctx.busy) return
    ctx.busy = true; update(ctx, { error: '' })
    try { await task() }
    catch (failure) {
      clearPending(ctx)
      if (!current(ctx)) return
      if ([401, 403].includes(failure?.status)) { ctx.controller.abort(); onForbidden?.(); return }
      if (!ctx.unknown && ctx.phase === 'issue') ctx.phase = 'ready'
      if (!ctx.unknown && ctx.phase === 'complete' && !ctx.completed) ctx.phase = 'ready'
      update(ctx, { error: ctx.phase === 'unsupported' ?
        'This integration host could not prepare the Vercel connection. Ask your administrator to check setup.' :
        ctx.unknown ? 'The result is not confirmed. Check status before continuing. No request will be repeated.' :
        'Vercel setup could not continue. Check your access and try again.', message: '' })
    } finally { if (current(ctx)) { ctx.busy = false; update(ctx) } }
  }
  function prepare() {
    const ctx = context.current
    if (!ctx || ctx.busy || ctx.unknown || !current(ctx)) return
    if (ctx.phase === 'empty') {
      try {
        ctx.connectorId = `vercel-${crypto.randomUUID()}`
        ctx.connectionId = `vercel-${crypto.randomUUID()}`
      } catch { update(ctx, { error: 'This browser cannot securely create a connection.' }); return }
      ctx.phase = 'import'
    }
    work(ctx, async () => {
      update(ctx, { message: 'Preparing your Vercel connection…' })
      if (ctx.phase === 'import') {
        ctx.imported = await mutate(ctx, () => client.importConnector(projectId, {
          document: vercelDocument, options: { id: ctx.connectorId, title: 'Vercel account connection' },
        }, options(ctx)))
        if (!ctx.imported.reference) { ctx.unknown = false; ctx.phase = 'unsupported'; throw new Error('unsupported') }
        await reconcile(ctx)
      }
      if (ctx.phase === 'publish') {
        ctx.publication = await mutate(ctx, () => client.publishConnector(projectId, ctx.connectorId, {
          version: ctx.draft.reference.version, expectedRevision: ctx.draft.revision, readOperationIds: [ctx.operationId],
        }, options(ctx)))
        await reconcile(ctx)
      }
      if (ctx.phase === 'connection') {
        ctx.created = await mutate(ctx, () => client.createConnection(projectId, {
          id: ctx.connectionId, title: 'Vercel account', connectorRef: ctx.published.reference,
          origin: 'https://api.vercel.com', operationIds: [ctx.operationId],
        }, options(ctx)))
        await reconcile(ctx)
      }
      update(ctx, { message: 'Your connection is ready for authorization.' })
    })
  }
  async function consume(ctx, pending, data) {
    if (!current(ctx) || ctx.pending !== pending || ctx.busy || pending.consumed || Date.now() >= pending.expiry ||
        !data || data.state !== pending.state) return
    const fields = data.outcome === 'authorized' ? ['state', 'outcome', 'code', 'configurationId'] : ['state', 'outcome']
    if (Object.keys(data).length !== fields.length || !fields.every((field) => Object.hasOwn(data, field))) return
    if (data.outcome !== 'denied' && (data.outcome !== 'authorized' || !safeId(data.configurationId) ||
        typeof data.code !== 'string' || !/^[A-Za-z0-9_-]{1,2048}(?![\s\S])/.test(data.code))) return
    pending.consumed = true
    let payload = data.outcome === 'authorized' ? { expectedRevision: pending.revision, state: pending.state,
      code: data.code, configurationId: data.configurationId } : null
    clearPending(ctx)
    if (!payload) { update(ctx, { phase: 'ready', message: 'Authorization was declined. You can start a new attempt.' }); return }
    ctx.callback = payload
    await work(ctx, async () => {
      try {
        await guarded(ctx)
        const fresh = await client.getConnectionSummary(projectId, ctx.connectionId, options(ctx))
        assertCurrent(ctx)
        if (!matchesConnection(ctx, fresh) || fresh.revision !== pending.revision || Date.now() >= pending.expiresAt) throw new Error('changed')
        ctx.phase = 'complete'
        ctx.completed = await mutate(ctx, () => {
          if (Date.now() >= pending.expiresAt) throw Object.assign(new Error('expired'), { dispatched: false })
          return client.completeVercelConnection(projectId, ctx.connectionId, payload, options(ctx))
        })
        await reconcile(ctx)
      } finally {
        if (payload) { payload.code = ''; payload.state = ''; payload = null }
        ctx.callback = null
      }
    })
  }
  function authorize() {
    const ctx = context.current
    if (!ctx || !current(ctx) || ctx.busy || ctx.unknown || ctx.phase !== 'ready') return
    if (typeof BroadcastChannel !== 'function') { update(ctx, { error: 'This browser cannot receive authorization. Use a browser with BroadcastChannel support.' }); return }
    // This must stay in the click stack. A severed opener is not a denial signal.
    const popup = window.open('about:blank', '_blank')
    if (!popup) { update(ctx, { error: 'Allow popups for Studio, then start authorization again.' }); return }
    ctx.popup = popup
    work(ctx, async () => {
      const fresh = await client.getConnectionSummary(projectId, ctx.connectionId, options(ctx))
      assertCurrent(ctx)
      if (!matchesConnection(ctx, fresh) || fresh.revision !== ctx.connection.revision) throw new Error('changed')
      ctx.phase = 'issue'
      let issued
      try { issued = await mutate(ctx, () => client.issueVercelConnection(projectId, ctx.connectionId, { expectedRevision: fresh.revision }, options(ctx))) }
      catch (failure) {
        if (failure?.status === 503) {
          clearPending(ctx)
          update(ctx, { unknown: false, phase: 'ready', message: 'Vercel authorization is unavailable. Ask your administrator to finish setup.' })
          return
        }
        throw failure
      }
      ctx.unknown = false
      const valid = validateVercelDestinations(issued, window.location.origin)
      const stateBytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(valid.state))
      assertCurrent(ctx)
      const stateFingerprint = Array.from(new Uint8Array(stateBytes), (byte) => byte.toString(16).padStart(2, '0')).join('')
      if (ctx.states.has(stateFingerprint)) throw new Error('replayed')
      ctx.states.add(stateFingerprint)
      const expiry = Date.parse(valid.expiresAt)
      if (!Number.isFinite(expiry) || expiry <= Date.now() || expiry - Date.now() > 30 * 60 * 1000) throw new Error('expired')
      const exact = await client.getConnectionSummary(projectId, ctx.connectionId, options(ctx))
      await guarded(ctx)
      if (!matchesConnection(ctx, exact) || exact.revision !== fresh.revision || Date.now() >= expiry) throw new Error('changed')
      const channel = new BroadcastChannel(`studio-vercel-${valid.state}`)
      const pending = { channel, state: valid.state, revision: fresh.revision, expiry, expiresAt: expiry, consumed: false }
      ctx.pending = pending
      channel.onmessage = (event) => { void consume(ctx, pending, event.data) }
      pending.timer = setTimeout(() => {
        if (ctx.pending !== pending) return
        clearPending(ctx)
        update(ctx, { phase: 'ready', message: 'Authorization expired. Start a new attempt.' })
      }, expiry - Date.now())
      try { popup.opener = null } catch { /* No opener is needed. */ }
      popup.location.replace(valid.authorizationUrl)
      update(ctx, { phase: 'waiting', message: 'Finish authorization in Vercel, then return here.' })
    })
  }
  function cancel() {
    const ctx = context.current
    if (!ctx || !current(ctx) || ctx.busy) return
    clearPending(ctx)
    update(ctx, { phase: 'ready', message: 'Authorization cancelled. Start a new attempt when ready.' })
  }
  function recover() {
    const ctx = context.current
    if (!ctx || !ctx.unknown) return
    work(ctx, async () => { await reconcile(ctx); update(ctx, { message: 'Saved status confirmed. Continue when ready.' }) })
  }
  if (scope.projectRole !== 'owner' || signal.aborted) return null
  return <section className="integration-setup" aria-label="Connect Vercel">
    <h2>Connect Vercel</h2>
    <p>Link your Vercel account through a private connection. This does not synchronize data or enable publishing.</p>
    <p role="status" aria-live="polite">{view.message}</p>
    {view.error && <p role="alert">{view.error}</p>}
    {!['ready', 'waiting', 'issue', 'complete', 'linked', 'unsupported'].includes(view.phase) &&
      <button type="button" disabled={view.busy || view.unknown} onClick={prepare}>{view.phase === 'empty' ? 'Create Vercel connection' : 'Continue Vercel setup'}</button>}
    {view.phase === 'ready' && <button type="button" disabled={view.busy || view.unknown} onClick={authorize}>Authorize Vercel</button>}
    {view.phase === 'waiting' && <button type="button" disabled={view.busy} onClick={cancel}>Cancel authorization</button>}
    {view.unknown && <button type="button" disabled={view.busy} onClick={recover}>Check Vercel status</button>}
    {view.linked && <p>{view.linked.team ? 'Verified Vercel team account.' : 'Verified personal Vercel account.'} {view.linked.cleanup === 'complete' ? 'Cleanup complete.' : 'Cleanup needs attention.'}</p>}
  </section>
}
