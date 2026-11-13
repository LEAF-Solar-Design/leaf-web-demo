// Canonical project publication only. No geometry, standalone version or fallback target.
const uuid = value => typeof value === 'string' && value.length === 36
  && /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(value)
const digest = value => typeof value === 'string' && value.length === 64 && /^[0-9a-f]+$/.test(value)
const fields = ['mode', 'organization_id', 'project_id', 'drawing_id', 'input_version_id',
  'physical_head', 'physical_content_sha256', 'physical_index', 'parameters_json', 'request_id']

export function initialSolarParameters(boundary, preset, drawing_units) {
  try {
    if (!['m', 'ft'].includes(drawing_units) || !Array.isArray(boundary) || boundary.length < 3
      || !boundary.every(point => Array.isArray(point) && point.length === 2 && point.every(Number.isFinite))
      || !preset || typeof preset !== 'object' || Array.isArray(preset)) return null
    const parameters = { boundary, preset, drawing_units }
    const finite = value => value === null || (typeof value === 'number' ? Number.isFinite(value)
      : typeof value === 'object' ? Object.values(value).every(finite) : ['string', 'boolean'].includes(typeof value))
    const text = JSON.stringify(parameters)
    return finite(parameters) && new TextEncoder().encode(text).length <= 2 * 1024 * 1024 ? JSON.parse(text) : null
  } catch { return null }
}

export function originalSolarParameters(original_intake, grid_long_axis, producer_semantics) {
  if (typeof original_intake !== 'string' || !Number.isInteger(grid_long_axis)
      || grid_long_axis < 2 || grid_long_axis > 300
      || (producer_semantics !== undefined && !['current-native', 'historical-list-reuse'].includes(producer_semantics))) return null
  try {
    const original = JSON.parse(original_intake.replace(/^\uFEFF/, ''))
    if (Object.keys(original).sort().join(',') !== 'active_preset,boundary,pile_template,units'
        || !initialSolarParameters(original.boundary, original.active_preset, original.units)
        || typeof original.pile_template?.Name !== 'string' || !original.pile_template.Name.trim()
        || original.pile_template.Name !== original.active_preset.PileTemplateName) return null
    const parameters = { original_intake, grid_long_axis }
    if (producer_semantics !== undefined) parameters.producer_semantics = producer_semantics
    return new TextEncoder().encode(JSON.stringify(parameters)).length <= 2 * 1024 * 1024 ? parameters : null
  } catch { return null }
}

function checkedInitialParameters(parameters) {
  if (!parameters) return null
  const keys = Object.keys(parameters).sort().join(',')
  if (keys === 'grid_long_axis,original_intake' || keys === 'grid_long_axis,original_intake,producer_semantics') {
    return originalSolarParameters(parameters.original_intake, parameters.grid_long_axis, parameters.producer_semantics)
  }
  return keys === 'boundary,drawing_units,preset'
    ? initialSolarParameters(parameters.boundary, parameters.preset, parameters.drawing_units) : null
}

function framePageMatches(value, scope, offset) {
  const poseFields = ['tilt_degrees', 'axis_azimuth_deg', 'height_at_low_pose_m', 'height_at_high_pose_m']
  const finite = number => typeof number === 'number' && Number.isFinite(number)
  return value?.schema === 'leaf.project-frame-inspection.v1'
    && value.organization_id === scope.organization_id && value.project_id === scope.project_id
    && value.drawing_id === scope.drawing_id && value.parent_version_id === scope.input_version_id
    && digest(value.physical_source?.physical_head) && digest(value.physical_source?.physical_content_sha256)
    && Number.isSafeInteger(value.physical_source?.physical_index) && value.physical_source.physical_index >= 0
    && typeof value.drawing_units === 'string' && value.drawing_units.length > 0 && value.drawing_units.length <= 32
    && value.offset === offset && value.page_size === 25 && Number.isSafeInteger(value.total) && value.total >= 0
    && (offset < value.total || offset === 0 && value.total === 0)
    && Array.isArray(value.frames) && value.frames.length === Math.min(25, value.total - offset)
    && new Set(value.frames.map(frame => frame?.handle)).size === value.frames.length
    && value.frames.every(frame => typeof frame?.handle === 'string' && /^[0-9a-f]{1,16}$/i.test(frame.handle)
      && Number.isSafeInteger(frame.row) && frame.row >= 0 && Number.isSafeInteger(frame.column) && frame.column >= 0
      && finite(frame.elevation) && Array.isArray(frame.vertices) && frame.vertices.length === 4
      && frame.vertices.every(point => Array.isArray(point) && point.length === 2 && point.every(finite))
      && [frame.preset, frame.pile_template].every(name => name === null || typeof name === 'string' && name.length <= 256)
      && poseFields.every(key => finite(frame.pose?.[key])))
}

function readbackMatches(scope, readback) {
  const initial = scope?.drawing_id === null && scope?.input_version_id === null
  if (!scope || !['organization_id', 'project_id'].every(key => uuid(scope[key]))
    || (!initial && (!uuid(scope.drawing_id) || !uuid(scope.input_version_id)))
    || readback?.schema !== 'leaf.project-physical-layout.v1'
    || readback.organization_id !== scope.organization_id || readback.project_id !== scope.project_id
    || readback.drawing_id !== scope.drawing_id || readback.parent_version_id !== scope.input_version_id) return false
  if (initial) return readback.mode === 'initial'
  const source = readback.physical_source
  return digest(source?.physical_head) && digest(source?.physical_content_sha256)
    && Number.isSafeInteger(source?.physical_index) && source.physical_index >= 0
}

async function boundedText(response) {
  if (Number(response.headers?.get?.('content-length')) > 65536) throw new Error('response too large')
  if (!response.body?.getReader) {
    const text = await response.text()
    if (new TextEncoder().encode(text).length > 65536) throw new Error('response too large')
    return text
  }
  const reader = response.body.getReader(), chunks = []; let size = 0
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      size += value.byteLength
      if (size > 65536) { await reader.cancel(); throw new Error('response too large') }
      chunks.push(value)
    }
  } finally { reader.releaseLock() }
  const bytes = new Uint8Array(size); let offset = 0
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength }
  return new TextDecoder().decode(bytes)
}

export function captureSolarPublication(scope, readback, requestId, parameters = null) {
  const initial = scope?.drawing_id === null && scope?.input_version_id === null
  const source = readback?.physical_source
  if (!readbackMatches(scope, readback) || !uuid(requestId)) return null
  const inputs = initial ? checkedInitialParameters(parameters) : null
  if (initial && !inputs) return null
  return Object.freeze({ mode: initial ? 'initial' : 'successor', organization_id: scope.organization_id, project_id: scope.project_id,
    drawing_id: scope.drawing_id, input_version_id: scope.input_version_id,
    physical_head: source?.physical_head ?? null, physical_content_sha256: source?.physical_content_sha256 ?? null,
    physical_index: source?.physical_index ?? null, parameters_json: initial ? JSON.stringify(inputs) : null, request_id: requestId })
}

export function solarPublicationMatches(captured, current) {
  return !!captured && !!current && fields.every(key => captured[key] === current[key])
}

export function solarPublicationReceiptMatches(receipt, captured) {
  const version = receipt?.drawing_version, physical = receipt?.physical_source
  return !!captured && uuid(version?.version_id) && version.version_id !== captured.input_version_id
    && receipt.organization_id === captured.organization_id && receipt.project_id === captured.project_id
    && receipt.drawing_id === version.drawing_id && uuid(version.drawing_id)
    && receipt.parent_version_id === captured.input_version_id && receipt.request_id === captured.request_id
    && version.org_id === captured.organization_id && version.project_id === captured.project_id
    && (captured.mode === 'initial' || version.drawing_id === captured.drawing_id)
    && (captured.mode === 'initial'
      ? receipt.mode === 'initial' && digest(receipt.generation_source?.parameters_sha256)
        && digest(receipt.generation_source?.physical_content_sha256)
        && digest(receipt.source_sha256) && digest(receipt.intake_sha256)
      : physical?.physical_head === captured.physical_head
        && physical?.physical_content_sha256 === captured.physical_content_sha256
        && physical?.physical_index === captured.physical_index)
}

export function createSolarProjectMaterializationClient({ fetchImpl = fetch, apiBase = '', headers,
  onResponse, timeoutMs = 120000 } = {}) {
  const route = scope => scope?.mode === 'initial' || scope?.drawing_id === null
    ? `${apiBase}/api/projects/${scope.project_id}/solar-layout`
    : `${apiBase}/api/projects/${scope.project_id}/drawings/${scope.drawing_id}/versions/${scope.input_version_id}/solar-layout`
  return { async inspectFrames(scope, { offset = 0, signal } = {}) {
    const refused = { ok: false, code: 'PROJECT_LAYOUT_FRAME_INSPECTION_UNAVAILABLE' }
    if (!scope || !['organization_id', 'project_id', 'drawing_id', 'input_version_id'].every(key => uuid(scope[key]))
      || !Number.isSafeInteger(offset) || offset < 0 || signal?.aborted) return refused
    const controller = new AbortController()
    const abort = () => controller.abort()
    signal?.addEventListener('abort', abort, { once: true })
    let timer
    const run = async () => {
      const base = headers?.(scope) ?? {}, url = route(scope) + '?frame_offset=' + offset
      const response = await fetchImpl(url, { cache: 'no-store', redirect: 'error', signal: controller.signal,
        headers: { ...base, 'X-Org-Id': scope.organization_id, 'X-Tenant-Id': scope.organization_id, 'X-Project-Id': scope.project_id } })
      onResponse?.(response, url, !!(base.Authorization || base.authorization))
      const text = await boundedText(response)
      if (!response.ok) return refused
      const value = JSON.parse(text)
      return framePageMatches(value, scope, offset) ? { ok: true, value } : refused
    }
    try { return await Promise.race([run(), new Promise(resolve => {
      timer = setTimeout(() => { controller.abort(); resolve(refused) }, timeoutMs)
    })]) } catch { return refused }
    finally { clearTimeout(timer); signal?.removeEventListener('abort', abort) }
  }, async read(scope) {
    if (!scope || !['organization_id', 'project_id'].every(key => uuid(scope[key])))
      return { ok: false, code: 'PROJECT_LAYOUT_SCOPE_INVALID' }
    if (!(scope.drawing_id === null && scope.input_version_id === null)
      && (!uuid(scope.drawing_id) || !uuid(scope.input_version_id))) return { ok: false, code: 'PROJECT_LAYOUT_SCOPE_INVALID' }
    const controller = new AbortController(); let timer
    const run = async () => {
      const base = headers?.(scope) ?? {}, url = route(scope)
      const response = await fetchImpl(url, { cache: 'no-store', redirect: 'error', signal: controller.signal,
        headers: { ...base, 'X-Org-Id': scope.organization_id, 'X-Tenant-Id': scope.organization_id, 'X-Project-Id': scope.project_id } })
      onResponse?.(response, url, !!(base.Authorization || base.authorization))
      const text = await boundedText(response)
      if (!response.ok) return { ok: false, code: 'PROJECT_LAYOUT_UNAVAILABLE' }
      const value = JSON.parse(text)
      if (!readbackMatches(scope, value))
        return { ok: false, code: 'PROJECT_LAYOUT_RESPONSE_INVALID' }
      return { ok: true, value }
    }
    try { return await Promise.race([run(), new Promise(resolve => {
      timer = setTimeout(() => { controller.abort(); resolve({ ok: false, code: 'PROJECT_LAYOUT_UNAVAILABLE' }) }, timeoutMs)
    })]) } catch { return { ok: false, code: 'PROJECT_LAYOUT_UNAVAILABLE' } }
    finally { clearTimeout(timer) }
  }, async publish(captured, { capability } = {}) {
    const initial = captured?.mode === 'initial'
    let inputs
    try { inputs = initial ? JSON.parse(captured.parameters_json) : null } catch { /* refused below */ }
    if (!captured || !['initial', 'successor'].includes(captured.mode) || !uuid(captured.request_id)
      || !['organization_id', 'project_id', 'drawing_id'].every(key => uuid(captured[key]))
        && !(initial && captured.drawing_id === null && uuid(captured.organization_id) && uuid(captured.project_id))
      || (initial ? captured.input_version_id !== null || !checkedInitialParameters(inputs)
        : !uuid(captured.input_version_id) || !digest(captured.physical_head) || !digest(captured.physical_content_sha256)
          || !Number.isSafeInteger(captured.physical_index) || captured.physical_index < 0))
      return { ok: false, code: 'PROJECT_LAYOUT_SCOPE_INVALID', outcomeUnknown: false }
    let sent = false, timer
    const controller = new AbortController()
    const fail = (code, outcomeUnknown = sent) => ({ ok: false, code, outcomeUnknown })
    const run = async () => {
      const base = { ...(headers?.(captured) ?? {}), ...(typeof capability === 'string' && capability ? { 'X-Checkout-Capability': capability } : {}) }
      if (initial) for (const key of Object.keys(base)) if (key.toLowerCase() === 'x-checkout-capability') delete base[key]
      if (!initial && (typeof base['X-Checkout-Capability'] !== 'string' || !base['X-Checkout-Capability']))
        return fail('PROJECT_LAYOUT_CHECKOUT_REQUIRED', false)
      const url = route(captured)
      sent = true
      const response = await fetchImpl(url, { method: 'POST', cache: 'no-store', redirect: 'error',
        signal: controller.signal, headers: { ...base, 'Content-Type': 'application/json',
          'X-Org-Id': captured.organization_id, 'X-Tenant-Id': captured.organization_id,
          'X-Project-Id': captured.project_id, 'Idempotency-Key': captured.request_id },
        body: initial ? captured.parameters_json : JSON.stringify({ physical_head: captured.physical_head,
          physical_content_sha256: captured.physical_content_sha256, physical_index: captured.physical_index }) })
      onResponse?.(response, url, !!(base.Authorization || base.authorization))
      const text = await boundedText(response)
      let body
      try { body = JSON.parse(text) } catch { return fail('PROJECT_LAYOUT_RESPONSE_INVALID') }
      if (!response.ok) {
        const code = body?.error?.reason_code ?? body?.error?.code ?? body?.detail?.reason_code
        return fail(typeof code === 'string' && /^[A-Z0-9_]{1,96}$/.test(code) ? code : 'PROJECT_LAYOUT_UNAVAILABLE',
          response.status >= 500 && code !== 'PROJECT_LAYOUT_LICENSED_WRITER_UNAVAILABLE')
      }
      if (!solarPublicationReceiptMatches(body, captured)) return fail('PROJECT_LAYOUT_RESPONSE_INVALID')
      return { ok: true, value: body }
    }
    try {
      return await Promise.race([run(), new Promise(resolve => {
        timer = setTimeout(() => { controller.abort(); resolve(fail('PROJECT_LAYOUT_OUTCOME_UNKNOWN')) }, timeoutMs)
      })])
    } catch { return fail('PROJECT_LAYOUT_OUTCOME_UNKNOWN') }
    finally { clearTimeout(timer) }
  } }
}
