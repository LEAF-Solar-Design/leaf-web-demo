export const DEFAULT_PRODUCT_SURFACE = 'cad'

export const SHARED_WORKSPACE_CAPABILITIES = Object.freeze([
  'conversation',
  'annotations',
  'authoring',
  'approvals',
  'versions',
  'receipts',
  'marathons',
  'one-shot execution',
])

export const PRODUCT_SURFACES = Object.freeze([
  Object.freeze({
    id: 'browser',
    label: 'Browser',
    eyebrow: 'Blank slate',
    title: 'Build from an open project workspace',
    description: 'Shape files, conversations, annotations, tools, and automations without leaving the project.',
    additions: Object.freeze(['Files', 'General authoring', 'Browser artifacts']),
  }),
  Object.freeze({
    id: 'cad',
    label: 'CAD',
    eyebrow: 'Drawing workspace',
    title: 'Work directly with a project drawing',
    description: 'Use the live drawing, layers, tools, approvals, jobs, versions, and receipts in one scene.',
    additions: Object.freeze(['Drawing view', 'CAD tools', 'APS jobs']),
  }),
  Object.freeze({
    id: 'solar',
    label: 'Solar CAD',
    eyebrow: 'LEAF template',
    title: 'Apply the LEAF solar tool set',
    description: 'Start from a versioned solar template with standards, catalog tools, and project-owned versions.',
    additions: Object.freeze(['Solar template', 'Standards', 'Solar automations']),
  }),
  Object.freeze({
    id: 'ios',
    label: 'iOS',
    eyebrow: 'One-shot ship lane',
    title: 'Turn an approved project revision into a TestFlight build',
    description: 'Use mounted Apple readiness and resumable ship receipts. Credentials never enter this browser.',
    additions: Object.freeze(['App identity', 'Build stages', 'TestFlight receipt']),
  }),
])

const SURFACE_IDS = new Set(PRODUCT_SURFACES.map(({ id }) => id))

export function normalizeProductSurface(value) {
  return SURFACE_IDS.has(value) ? value : DEFAULT_PRODUCT_SURFACE
}

export function productSurfaceFromSearch(search = '') {
  try {
    return normalizeProductSurface(new URLSearchParams(search).get('surface'))
  } catch {
    return DEFAULT_PRODUCT_SURFACE
  }
}

export function searchForProductSurface(search, surfaceId) {
  const params = new URLSearchParams(search || '')
  params.set('surface', normalizeProductSurface(surfaceId))
  const encoded = params.toString()
  return encoded ? `?${encoded}` : ''
}

export function productSurfaceStates({ sessionActive, hasDrawing, apsLive, iosReady = false } = {}) {
  return {
    browser: sessionActive
      ? { state: 'available', label: 'Ready' }
      : { state: 'sign-in', label: 'Sign in' },
    cad: !sessionActive
      ? { state: 'sign-in', label: 'Sign in' }
      : !hasDrawing
        ? { state: 'setup', label: 'Add drawing' }
        : apsLive === false
          ? { state: 'unavailable', label: 'Execution paused' }
          : { state: 'available', label: 'Ready' },
    solar: !sessionActive
      ? { state: 'sign-in', label: 'Sign in' }
      : { state: 'beta', label: hasDrawing ? 'Beta' : 'Template pending' },
    ios: iosReady
      ? { state: 'available', label: 'Ready' }
      : { state: 'setup', label: 'Setup required' },
  }
}

export function productSurface(id) {
  const normalized = normalizeProductSurface(id)
  return PRODUCT_SURFACES.find((surface) => surface.id === normalized)
}
