const DEFAULT_MAX_AGE_MS = 5 * 60 * 1000

function freeze(value) {
  if (!value || typeof value !== 'object' || Object.isFrozen(value)) return value
  Object.freeze(value)
  for (const child of Object.values(value)) freeze(child)
  return value
}

function normalize(value) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return value
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw new TypeError('run intent params must contain finite numbers')
    return Object.is(value, -0) ? 0 : value
  }
  if (Array.isArray(value)) return value.map(normalize)
  if (value && typeof value === 'object' && Object.getPrototypeOf(value) === Object.prototype) {
    const out = {}
    for (const key of Object.keys(value).sort()) {
      if (value[key] !== undefined) out[key] = normalize(value[key])
    }
    return out
  }
  throw new TypeError('run intent params must be JSON values')
}

export function normalizeRunParams(params) {
  return freeze(normalize(params || {}))
}

export function createCatalogRunContext({
  tenantId, orgId = null, projectId = null, workspace = null,
  selectedVersionId = null, drawingState = null, fallbackDrawingId,
}) {
  if (projectId) {
    const workspaceProjectId = workspace?.project?.project_id || workspace?.project?.id || null
    if (!orgId || workspaceProjectId !== projectId) return null
    const versions = (workspace?.drawing_versions || [])
      .filter((version) => typeof version?.version_id === 'string' && version.version_id)
    const selected = selectedVersionId
      ? versions.find((version) => version.version_id === selectedVersionId)
      : null
    const latest = selected || [...versions].sort((left, right) => {
      const seqDelta = Number(right.seq || 0) - Number(left.seq || 0)
      if (seqDelta) return seqDelta
      return String(right.version_id).localeCompare(String(left.version_id))
    })[0]
    if (!latest) return null
    return freeze(normalize({
      tenantId, orgId, projectId,
      drawingId: latest.version_id,
      drawingVersion: null,
    }))
  }
  return freeze(normalize({
    tenantId, orgId: null, projectId: null,
    drawingId: drawingState?.drawing_id || fallbackDrawingId,
    drawingVersion: drawingState?.version ?? null,
  }))
}

export function createRunSubmissionRequest(toolName, params, dwg, opts = {}) {
  const headers = {}
  if (opts.orgId) headers['X-Org-Id'] = opts.orgId
  if (opts.projectId) headers['X-Project-Id'] = opts.projectId
  if (opts.idempotencyKey) headers['Idempotency-Key'] = opts.idempotencyKey
  return {
    headers,
    body: {
      tool: toolName,
      params: params || {},
      dwg,
      ...(opts.dwgVersion != null ? { dwg_version: opts.dwgVersion } : {}),
    },
  }
}

function stableDigest(value) {
  const json = JSON.stringify(value)
  let hash = 0x811c9dc5
  for (let i = 0; i < json.length; i += 1) {
    hash ^= json.charCodeAt(i)
    hash = Math.imul(hash, 0x01000193) >>> 0
  }
  return `fnv1a32:${hash.toString(16).padStart(8, '0')}`
}

export function createCatalogToolSnapshot(tool) {
  if (!tool || typeof tool !== 'object' || typeof tool.name !== 'string' || !tool.name) {
    throw new TypeError('catalog tool is required')
  }
  const definition = normalizeRunParams(tool)
  const capabilities = [...new Set(Array.isArray(definition.capabilities) ? definition.capabilities : [])].sort()
  const revision = definition.catalog_revision
    || definition.catalog_digest
    || definition.version
    || definition.provenance?.commit
    || null
  return freeze({
    name: definition.name,
    revision,
    capabilities,
    digest: stableDigest(definition),
    definition,
  })
}

export function normalizeRunContext(context) {
  if (!context || typeof context !== 'object') throw new TypeError('run context is required')
  const normalized = normalizeRunParams({
    tenantId: context.tenantId ?? null,
    orgId: context.orgId ?? null,
    projectId: context.projectId ?? null,
    drawingId: context.drawingId ?? null,
    drawingVersion: context.drawingVersion ?? null,
  })
  if (typeof normalized.tenantId !== 'string' || !normalized.tenantId) {
    throw new TypeError('run context tenantId is required')
  }
  if (typeof normalized.drawingId !== 'string' || !normalized.drawingId) {
    throw new TypeError('run context drawingId is required')
  }
  return normalized
}

export function createRunIntentState(sessionId) {
  if (typeof sessionId !== 'string' || !sessionId) throw new TypeError('sessionId is required')
  return freeze({ sessionId, pending: null, consumedIds: [] })
}

export function stageRunIntent(state, {
  intentId, toolName, params, context, toolSnapshot, createdAt = Date.now(),
}) {
  if (!state?.sessionId) throw new TypeError('valid run intent state is required')
  if (typeof intentId !== 'string' || !intentId) throw new TypeError('intentId is required')
  if (typeof toolName !== 'string' || !toolName) throw new TypeError('toolName is required')
  if (toolSnapshot?.name !== toolName) throw new TypeError('tool snapshot must match toolName')
  if (!Number.isFinite(createdAt)) throw new TypeError('createdAt must be finite')
  if (state.pending?.intentId === intentId || state.consumedIds.includes(intentId)) {
    throw new Error('run intent id must be unique within its session')
  }
  const intent = freeze({
    intentId,
    sessionId: state.sessionId,
    toolName,
    params: normalizeRunParams(params),
    context: normalizeRunContext(context),
    toolSnapshot: freeze(normalize(toolSnapshot)),
    createdAt,
  })
  return freeze({
    state: { ...state, pending: intent },
    intent,
  })
}
export function dismissRunIntent(state, intentId = null) {
  if (!state?.pending) return state
  if (intentId && state.pending.intentId !== intentId) return state
  return freeze({ ...state, pending: null })
}

function denied(state, code) {
  return freeze({ ok: false, code, state: freeze({ ...state, pending: null }) })
}

export function confirmRunIntent(state, request, {
  now = Date.now(),
  maxAgeMs = DEFAULT_MAX_AGE_MS,
} = {}) {
  const id = request?.intentId
  if (id && state?.consumedIds?.includes(id)) return denied(state, 'replayed')
  const intent = state?.pending
  if (!intent) return denied(state, 'missing')
  if (request?.sessionId !== state.sessionId || intent.sessionId !== state.sessionId) {
    return denied(state, 'cross_session')
  }
  if (id !== intent.intentId) return denied(state, 'changed_intent')
  if (!Number.isFinite(now) || now < intent.createdAt || now - intent.createdAt > maxAgeMs) {
    return denied(state, 'stale')
  }
  if (request?.toolName !== intent.toolName) return denied(state, 'changed_tool')
  let params
  try {
    params = normalizeRunParams(request?.params)
  } catch {
    return denied(state, 'changed_params')
  }
  if (JSON.stringify(params) !== JSON.stringify(intent.params)) return denied(state, 'changed_params')

  let context
  try {
    context = normalizeRunContext(request?.context)
  } catch {
    return denied(state, 'changed_context')
  }
  if (JSON.stringify(context) !== JSON.stringify(intent.context)) return denied(state, 'changed_context')

  let toolSnapshot
  try {
    toolSnapshot = freeze(normalize(request?.toolSnapshot))
  } catch {
    return denied(state, 'changed_tool_snapshot')
  }
  if (JSON.stringify(toolSnapshot) !== JSON.stringify(intent.toolSnapshot)) {
    return denied(state, 'changed_tool_snapshot')
  }

  const execution = freeze({
    intentId: intent.intentId,
    toolName: intent.toolName,
    params: intent.params,
    context: intent.context,
    toolSnapshot: intent.toolSnapshot,
  })
  return freeze({
    ok: true,
    state: freeze({
      ...state,
      pending: null,
      consumedIds: [...state.consumedIds, intent.intentId],
    }),
    execution,
  })
}
