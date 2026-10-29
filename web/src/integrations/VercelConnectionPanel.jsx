import { useLayoutEffect, useRef, useState } from 'react'
import { UUID, validateVercelDestinations } from './client.js'
import { loadProjectPage } from '../site/projectPageClient.js'

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
const emptyView = { busy: false, phase: 'empty', unknown: false, message: '', error: '', linked: null, destination: null }
const emptyDestination = () => ({ search: '', projects: null, selectedId: '', revision: null, truncated: false,
  pending: null, saved: null, needsRefresh: false })

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
    const dispose = () => {
      clearPending(ctx); ctx.controller.abort(); ctx.linked = null; ctx.destination = null; ctx.preview = null; ctx.states.clear()
      if (context.current === ctx) setView(emptyView)
    }
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
    setView({ busy: ctx.busy, phase: ctx.phase, unknown: ctx.unknown, message: ctx.message || '', error: ctx.error || '', linked: ctx.linked || null,
      destination: ctx.destination ? { ...ctx.destination } : null, preview: ctx.preview ? { ...ctx.preview } : null })
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
        !value.identity_verified || value.provider_account_id !== result.ownerId ||
        !value.credential_purposes.includes('access_token') || value.cleanup !== result.cleanup) throw new Error('readback')
    ctx.connection = value
    ctx.phase = 'linked'; ctx.unknown = false
    ctx.linked = { team: result.teamId !== null, cleanup: result.cleanup }
    ctx.destination = { ...emptyDestination(), revision: value.revision }
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
  function matchesAccount(ctx, account) {
    return account?.ownerId === ctx.completed.ownerId && account.teamId === ctx.completed.teamId
  }
  async function destinationConnection(ctx, expectedRevision) {
    await guarded(ctx)
    const fresh = await client.getConnectionSummary(projectId, ctx.connectionId, options(ctx))
    await guarded(ctx)
    if (!matchesConnection(ctx, fresh) || !fresh.identity_verified || fresh.provider_account_id !== ctx.completed.ownerId ||
        !fresh.credential_purposes.includes('access_token') || fresh.cleanup !== ctx.completed.cleanup) throw new Error('identity')
    if (fresh.revision !== expectedRevision) throw Object.assign(new Error('revision'), { status: 409 })
    ctx.connection = fresh
  }
  function destinationWork(task) {
    const ctx = context.current
    if (!ctx || !current(ctx) || ctx.busy || ctx.phase !== 'linked' || !ctx.destination || ctx.preview?.frozen) return
    work(ctx, async () => {
      try { await task(ctx, ctx.destination) }
      catch (failure) {
        if (!current(ctx) || [401, 403].includes(failure?.status)) throw failure
        const dest = ctx.destination
        if (failure?.status === 409) {
          Object.assign(dest, { projects: null, selectedId: '', pending: null, saved: null, needsRefresh: true })
          update(ctx, { error: 'The connection changed. Refresh destination status before choosing again.', message: '' })
        } else {
          // Keep the intended ID after an uncertain PUT or failed confirmation GET. Never replay the PUT.
          if (!dest.pending) Object.assign(dest, { projects: null, selectedId: '', saved: null, needsRefresh: true })
          update(ctx, { error: dest.pending ? 'The destination is not confirmed. Check destination status before continuing. No save will be repeated.' :
            'Vercel destinations could not be verified. Refresh destination status before choosing again.', message: '' })
        }
      }
    })
  }
  function loadProjects() {
    destinationWork(async (ctx, dest) => {
      if (dest.pending || dest.needsRefresh) return
      ctx.preview = null
      Object.assign(dest, { projects: null, selectedId: '', saved: null })
      await destinationConnection(ctx, dest.revision)
      const result = await client.listVercelProjects(projectId, ctx.connectionId,
        { expectedRevision: dest.revision, search: dest.search }, options(ctx))
      await guarded(ctx)
      if (!matchesAccount(ctx, result.account) || result.revision !== dest.revision) throw new Error('identity')
      Object.assign(dest, { projects: result.projects, truncated: result.truncated, loadedSearch: dest.search })
      update(ctx, { message: result.projects.length ? 'Choose a Vercel project for this destination.' : 'No Vercel projects found.' })
    })
  }
  async function readDestination(ctx, dest) {
    await guarded(ctx)
    const result = await client.getVercelDestination(projectId, ctx.connectionId, options(ctx))
    await guarded(ctx)
    if (!matchesAccount(ctx, result.account) || (result.destination && result.destination.accountId !== ctx.completed.ownerId) ||
        result.revision < dest.revision) throw new Error('identity')
    await destinationConnection(ctx, result.revision)
    const pending = dest.pending
    const ack = pending?.acknowledged
    const matches = pending && result.destination?.projectId === pending.projectId &&
      (ack ? result.revision === ack.revision && result.destination.name === ack.destination.name &&
        result.destination.accountId === ack.destination.accountId && matchesAccount(ctx, ack.account) :
        result.revision > pending.revision && result.destination.name === pending.name)
    Object.assign(dest, { revision: result.revision, needsRefresh: false, pending: null, projects: null, selectedId: '', saved: matches ? result.destination : null })
    update(ctx, { error: '', message: matches ? 'Vercel destination saved and confirmed.' :
      pending ? 'The intended destination is unconfirmed. Load projects and make a new choice.' : 'Destination status refreshed. Load projects to make a new choice.' })
  }
  function saveDestination() {
    destinationWork(async (ctx, dest) => {
      if (dest.pending || dest.needsRefresh || !dest.selectedId || dest.revision >= 2147483647) return
      const selected = dest.projects?.find((project) => project.id === dest.selectedId)
      if (!selected) return
      await destinationConnection(ctx, dest.revision)
      await guarded(ctx)
      dest.pending = { projectId: selected.id, name: selected.name, revision: dest.revision, acknowledged: null }
      dest.saved = null
      update(ctx, { message: 'Saving destination; confirmation is pending…' })
      let ack
      try {
        ack = await client.selectVercelProject(projectId, ctx.connectionId,
          { expectedRevision: dest.revision, projectId: selected.id, search: dest.loadedSearch }, options(ctx))
      } catch (failure) {
        if (failure?.dispatched === false || (failure?.status >= 400 && failure.status < 500 && ![408, 429].includes(failure.status))) dest.pending = null
        throw failure
      }
      await guarded(ctx)
      if (!matchesAccount(ctx, ack.account) || ack.destination?.accountId !== ctx.completed.ownerId ||
          ack.destination.projectId !== selected.id || ack.revision <= dest.revision) throw new Error('identity')
      dest.pending.acknowledged = ack
      await readDestination(ctx, dest)
    })
  }
  function editDestination(field, value) {
    const ctx = context.current
    if (!ctx || !current(ctx) || ctx.busy || !ctx.destination || ctx.destination.pending || ctx.destination.needsRefresh || ctx.preview?.frozen) return
    ctx.preview = null
    ctx.destination[field] = value
    ctx.destination.saved = null
    if (field === 'search') Object.assign(ctx.destination, { projects: null, selectedId: '', truncated: false })
    update(ctx, { message: '', error: '' })
  }
  const sameDestination = (a, b) => a && b && ['projectId', 'accountId', 'name'].every((field) => a[field] === b[field])
  function samePermission(a, b) {
    return a.enabled === b.enabled && a.revision === b.revision && a.grantRevision === b.grantRevision &&
      sameRef(a.connectorRef, b.connectorRef) && a.account.ownerId === b.account.ownerId && a.account.teamId === b.account.teamId &&
      sameDestination(a.destination, b.destination)
  }
  async function readPreviewPermission(ctx, expected) {
    expected ||= ctx.preview.binding
    await guarded(ctx)
    const value = await client.getVercelPreviewPermission(projectId, ctx.connectionId, options(ctx))
    await guarded(ctx)
    if (!matchesAccount(ctx, value.account) || !sameDestination(value.destination, ctx.destination.saved)) throw new Error('preview destination changed')
    if (expected) {
      if (!samePermission(value, expected)) throw new Error('preview permission changed')
    } else if (ctx.preview.enabling) {
      const before = ctx.preview.enabling
      if (!value.enabled || value.revision <= before.revision || value.grantRevision <= before.grantRevision) throw new Error('enable unconfirmed')
    } else if (value.revision !== ctx.destination.revision || value.grantRevision !== ctx.connection.grant_revision ||
        !sameRef(value.connectorRef, ctx.connection.connector_ref)) throw new Error('preview binding changed')
    ctx.preview.permission = value
    ctx.preview.binding = value
    ctx.preview.enabling = null
    if (value.enabled) ctx.preview.frozen = true
    return value
  }
  function previewWork(task) {
    const ctx = context.current
    if (!ctx || !current(ctx) || ctx.busy || ctx.phase !== 'linked' || !ctx.destination?.saved) return
    ctx.preview ||= { permission: null, enabling: null, frozen: false, run: null, result: null, message: '' }
    work(ctx, async () => {
      try { await task(ctx, ctx.preview) }
      catch (failure) {
        if (!current(ctx) || [401, 403].includes(failure?.status)) throw failure
        // A failed read never proves that a potentially dispatched write was safe to repeat.
        ctx.preview.permission = null
        ctx.preview.message = ''
        update(ctx, { error: ctx.preview.run ? 'The preview result is unconfirmed. Check status. Creation will not be repeated.' :
          'Preview access could not be confirmed. Check preview access before continuing.', message: '' })
      }
    })
  }
  function checkPreviewAccess() {
    previewWork(async (ctx, preview) => {
      const value = await readPreviewPermission(ctx, preview.enableAck || preview.permission)
      preview.message = value.enabled ? 'Private previews are enabled for this destination.' : 'Enable previews to allow private preview creation.'
      update(ctx)
    })
  }
  function enablePreviews() {
    previewWork(async (ctx, preview) => {
      if (!preview.permission || preview.permission.enabled || preview.enabling || preview.run) return
      const before = await readPreviewPermission(ctx, preview.permission)
      await guarded(ctx)
      preview.enabling = before; preview.permission = null; preview.binding = null; preview.frozen = true
      let ack
      try {
        ack = await client.enableVercelPreviews(projectId, ctx.connectionId,
          { expectedConnectionRevision: before.revision, expectedGrantRevision: before.grantRevision }, options(ctx))
      } catch (failure) {
        if (failure?.dispatched === false) { preview.enabling = null; preview.frozen = false }
        throw failure
      }
      await guarded(ctx)
      if (!ack.enabled || !matchesAccount(ctx, ack.account) || !sameDestination(ack.destination, ctx.destination.saved) ||
          ack.revision <= before.revision || ack.grantRevision <= before.grantRevision) throw new Error('enable unconfirmed')
      preview.enableAck = ack
      await readPreviewPermission(ctx, ack)
      preview.message = 'Private previews are enabled for this destination.'
      update(ctx)
    })
  }
  function previewSavedPage() {
    previewWork(async (ctx, preview) => {
      if (!preview.permission?.enabled || preview.enabling || (preview.run &&
          !['ready', 'failed', 'failed_not_sent'].includes(preview.result?.status))) return
      const permission = await readPreviewPermission(ctx, preview.permission)
      await guarded(ctx)
      const saved = await loadProjectPage(projectId, options(ctx))
      await guarded(ctx)
      if (!saved) { preview.message = 'Save a page before previewing it.'; update(ctx); return }
      await readPreviewPermission(ctx, permission)
      await guarded(ctx)
      // Retain this identity before dispatch; recovery only reads this same run.
      const run = { runId: crypto.randomUUID(), savedRevision: saved.revision }
      preview.run = run; preview.result = null; preview.message = 'Uploading and creating the saved page preview…'
      update(ctx)
      try {
        const result = await client.createVercelPreviewRun(projectId, { ...run, connectionId: ctx.connectionId,
          expectedConnectionRevision: permission.revision, expectedGrantRevision: permission.grantRevision }, options(ctx))
        await guarded(ctx)
        preview.result = result; preview.message = ''
      } catch (failure) {
        if (failure?.dispatched === false) preview.run = null
        throw failure
      }
      update(ctx)
    })
  }
  function checkPreviewStatus() {
    previewWork(async (ctx, preview) => {
      if (!preview.run) return
      await readPreviewPermission(ctx, preview.permission)
      const { runId, savedRevision } = preview.run
      const observe = preview.result?.status === 'submitted' && preview.result.deploymentId
      const result = await (observe ? client.observeVercelPreviewRun(projectId, runId, savedRevision, options(ctx)) :
        client.getVercelPreviewRun(projectId, runId, savedRevision, options(ctx)))
      await guarded(ctx)
      preview.result = result.observation === 'unavailable' && preview.result ?
        { ...preview.result, observation: 'unavailable' } : result
      preview.message = ''
      update(ctx)
    })
  }
  const previewStatus = { uploading: 'Uploading the saved page.', creating: 'Creating a private preview.',
    uncertain: 'Preview creation is unconfirmed. Check status; creation will not be repeated.', submitted: 'Preview submitted. The build is not ready yet.',
    ready: 'Your private preview is ready.', failed: 'The preview build failed or was canceled.', failed_not_sent: 'The preview was not sent.' }
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
    {view.linked && view.destination && <div>
      <h3>Vercel publishing destination</h3>
      <p>Choose an existing project. Saving a destination does not publish your site.</p>
      <fieldset disabled={Boolean(view.preview?.frozen)} style={{ border: 0, padding: 0, minWidth: 0 }}>
      <label>Search Vercel projects
        <input type="search" maxLength={100} value={view.destination.search}
          style={{ boxSizing: 'border-box', width: '100%', minHeight: 44 }}
          disabled={view.busy || Boolean(view.destination.pending) || view.destination.needsRefresh}
          onChange={(event) => editDestination('search', event.target.value)} />
      </label>
      <button type="button" disabled={view.busy || Boolean(view.destination.pending) || view.destination.needsRefresh} onClick={loadProjects}>Load Vercel projects</button>
      {view.destination.projects && <>
        {view.destination.truncated && <p>Showing at most 20 projects. Narrow your search to find another project.</p>}
        {view.destination.projects.length > 0 && <>
          <label>Vercel project
            <select value={view.destination.selectedId} style={{ boxSizing: 'border-box', width: '100%', minHeight: 44 }}
              disabled={view.busy || Boolean(view.destination.pending) || view.destination.needsRefresh}
              onChange={(event) => editDestination('selectedId', event.target.value)}>
              <option value="">Choose a project</option>
              {view.destination.projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
            </select>
          </label>
          <button type="button" disabled={view.busy || Boolean(view.destination.pending) || view.destination.needsRefresh ||
            !view.destination.selectedId || view.destination.revision >= 2147483647} onClick={saveDestination}>Save destination</button>
        </>}
      </>}
      {view.destination.pending && <button type="button" disabled={view.busy}
        onClick={() => destinationWork(readDestination)}>Check destination status</button>}
      {view.destination.needsRefresh && <button type="button" disabled={view.busy}
        onClick={() => destinationWork(readDestination)}>Refresh destination status</button>}
      </fieldset>
      {view.destination.saved && <p style={{ overflowWrap: 'anywhere' }}>Confirmed destination: {view.destination.saved.name}</p>}
      {view.destination.saved && <section aria-label="Preview" style={{ overflowWrap: 'anywhere', minWidth: 0 }}>
        <h3>Preview</h3>
        <p>Create a private preview of your saved page in {view.destination.saved.name}. This does not publish publicly.</p>
        <p role="status">{view.preview?.message}</p>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
          <button type="button" style={{ minHeight: 44 }} disabled={view.busy} onClick={checkPreviewAccess}>Check preview access</button>
          {view.preview?.permission && !view.preview.permission.enabled && !view.preview.enabling &&
            <button type="button" style={{ minHeight: 44 }} disabled={view.busy} onClick={enablePreviews}>Enable previews</button>}
          {view.preview?.permission?.enabled && !view.preview.enabling && (!view.preview.run ||
            ['ready', 'failed', 'failed_not_sent'].includes(view.preview.result?.status)) &&
            <button type="button" style={{ minHeight: 44 }} disabled={view.busy} onClick={previewSavedPage}>Preview saved page</button>}
          {view.preview?.run && <button type="button" style={{ minHeight: 44 }} disabled={view.busy} onClick={checkPreviewStatus}>
            {view.preview.result?.status === 'submitted' && view.preview.result.deploymentId ? 'Check build status' : 'Check status'}</button>}
        </div>
        {view.preview?.run && <p>Saved revision: {view.preview.run.savedRevision}</p>}
        {view.preview?.result && <p>{previewStatus[view.preview.result.status]}</p>}
        {view.preview?.result?.reason === 'upload_unavailable' && <p>The saved page upload is unavailable.</p>}
        {view.preview?.result?.observation === 'unavailable' && <p>Build observation is unavailable. The last known state is retained; check again.</p>}
        {view.preview?.result?.status === 'ready' && view.preview.permission?.enabled && !view.error && <a style={{ display: 'inline-block', minHeight: 44 }}
          href={view.preview.result.url} target="_blank" rel="noopener noreferrer">Open preview</a>}
      </section>}
    </div>}
  </section>
}
