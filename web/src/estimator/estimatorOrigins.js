export const ESTIMATOR_ORIGINS = Object.freeze({
  'https://estimator-staging.leafdesign.ai': Object.freeze(['studio-staging', 'staging']),
  'https://estimator-studio-staging-973968415611.us-central1.run.app': Object.freeze(['studio-staging', 'staging']),
})

export function estimatorOriginAllowed(origin, studioOrigin = globalThis.window?.location?.origin) {
  if (typeof origin !== 'string') return false
  const local = studioOrigin === 'http://localhost:3000'
  if (!local && studioOrigin !== 'https://platform-staging.leafdesign.ai') return false
  if (Object.hasOwn(ESTIMATOR_ORIGINS, origin)) return ESTIMATOR_ORIGINS[origin][1] === 'staging'
  const match = /^http:\/\/localhost:([1-9][0-9]{0,4})$/.exec(origin)
  return local && Boolean(match && match[0] === origin && Number(match[1]) <= 65535)
}

export const canonicalProject = value => typeof value === 'string' && value.length === 36
  && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value)
