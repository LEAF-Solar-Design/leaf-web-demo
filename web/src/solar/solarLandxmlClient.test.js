// @vitest-environment node
// sf-w4-landxml-upload-client: the Studio side of POST /api/drawings/{drawing_id}/imports/landxml.
// Every fixture body below is the exact text the real route answered (planner measurement on Forge
// main 86573930 through server/tests/test_solar_landxml_route.py's client), and every digest is
// the canonical sha256 (sorted keys, JSON.stringify numbers) of the normalized value.
import { describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
import {
  LANDXML_DEFAULT_TARGET_CELLS,
  LANDXML_DRAWING_UNITS,
  LANDXML_IMPORT_FALLBACK,
  LANDXML_IMPORT_REASONS,
  LANDXML_MAX_BYTES,
  LANDXML_MAX_TARGET_CELLS,
  LANDXML_MIN_TARGET_CELLS,
  LANDXML_RESPONSE_MAX_BYTES,
  LANDXML_TIMEOUT_MS,
  createSolarLandxmlClient,
  landxmlReason,
  validateLandxmlImport,
} from './solarLandxmlClient.js'

const FIRST_TEXT = `{"schema":"leaf.solar-landxml-import.v1","created":true,"drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","source":{"artifact_id":"d6923bdcbbc5c7e02b2cfc486ff981d716adc4b7d05e344c04939bb4eef33cce","media_type":"application/xml","filename":"landxml-source.xml","byte_length":45305,"content_sha256":"9846f488c6780af2185fdf62741865fbc7a75077488449f6741f0aac77f370b5","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/d6923bdcbbc5c7e02b2cfc486ff981d716adc4b7d05e344c04939bb4eef33cce"},"interpretation":{"point_order":"northing-easting-elevation","drawing_x":"easting","drawing_y":"northing","linear_unit":"meter","meters_per_source_unit":1.0,"drawing_units":"m","meters_per_drawing_unit":1.0,"horizontal_scale":1.0,"elevation_scale":1.0,"crs":"none","crs_source":"declared","elevation_datum":"unrecorded"},"points":{"declared":441,"accepted":441,"skipped":0},"grid":{"rows":30,"cols":30,"target_cells":30,"x_min":-100.0,"x_max":100.0,"y_min":-100.0,"y_max":100.0},"head":{"schema":"leaf.solar-physical-head.v1","drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","index":0,"parent":null,"state":{"artifact_id":"56f3892ae8228c968f19b0a7f1a3fb65ae49be60d279382fb69e21ed4a4bcbc8","media_type":"application/json","filename":"physical-state.json","byte_length":18283,"content_sha256":"0de2afeb7ae692538ce33fd011b043438cd65cc426fd1cead1933eb4046d22f0","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/56f3892ae8228c968f19b0a7f1a3fb65ae49be60d279382fb69e21ed4a4bcbc8"}},"error":null,"degraded_mode":false}`
const AGAIN_TEXT = `{"schema":"leaf.solar-landxml-import.v1","created":false,"drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","source":{"artifact_id":"d6923bdcbbc5c7e02b2cfc486ff981d716adc4b7d05e344c04939bb4eef33cce","media_type":"application/xml","filename":"landxml-source.xml","byte_length":45305,"content_sha256":"9846f488c6780af2185fdf62741865fbc7a75077488449f6741f0aac77f370b5","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/d6923bdcbbc5c7e02b2cfc486ff981d716adc4b7d05e344c04939bb4eef33cce"},"interpretation":{"point_order":"northing-easting-elevation","drawing_x":"easting","drawing_y":"northing","linear_unit":"meter","meters_per_source_unit":1.0,"drawing_units":"m","meters_per_drawing_unit":1.0,"horizontal_scale":1.0,"elevation_scale":1.0,"crs":"none","crs_source":"declared","elevation_datum":"unrecorded"},"points":{"declared":441,"accepted":441,"skipped":0},"grid":{"rows":30,"cols":30,"target_cells":30,"x_min":-100.0,"x_max":100.0,"y_min":-100.0,"y_max":100.0},"head":{"schema":"leaf.solar-physical-head.v1","drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","index":0,"parent":null,"state":{"artifact_id":"56f3892ae8228c968f19b0a7f1a3fb65ae49be60d279382fb69e21ed4a4bcbc8","media_type":"application/json","filename":"physical-state.json","byte_length":18283,"content_sha256":"0de2afeb7ae692538ce33fd011b043438cd65cc426fd1cead1933eb4046d22f0","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/56f3892ae8228c968f19b0a7f1a3fb65ae49be60d279382fb69e21ed4a4bcbc8"}},"error":null,"degraded_mode":false}`
const CELLS_10_TEXT = `{"schema":"leaf.solar-landxml-import.v1","created":true,"drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","source":{"artifact_id":"d6923bdcbbc5c7e02b2cfc486ff981d716adc4b7d05e344c04939bb4eef33cce","media_type":"application/xml","filename":"landxml-source.xml","byte_length":45305,"content_sha256":"9846f488c6780af2185fdf62741865fbc7a75077488449f6741f0aac77f370b5","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/d6923bdcbbc5c7e02b2cfc486ff981d716adc4b7d05e344c04939bb4eef33cce"},"interpretation":{"point_order":"northing-easting-elevation","drawing_x":"easting","drawing_y":"northing","linear_unit":"meter","meters_per_source_unit":1.0,"drawing_units":"m","meters_per_drawing_unit":1.0,"horizontal_scale":1.0,"elevation_scale":1.0,"crs":"none","crs_source":"declared","elevation_datum":"unrecorded"},"points":{"declared":441,"accepted":441,"skipped":0},"grid":{"rows":10,"cols":10,"target_cells":10,"x_min":-100.0,"x_max":100.0,"y_min":-100.0,"y_max":100.0},"head":{"schema":"leaf.solar-physical-head.v1","drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","index":1,"parent":"56f3892ae8228c968f19b0a7f1a3fb65ae49be60d279382fb69e21ed4a4bcbc8","state":{"artifact_id":"3a16bacbf6d90a1f6d01345174a40f86f2a07165d85a98c61f53a791334e044f","media_type":"application/json","filename":"physical-state.json","byte_length":2453,"content_sha256":"d8e15891814e9ca10767dffce025e6dc1451f263badf7f323ea7f987cdf28965","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/3a16bacbf6d90a1f6d01345174a40f86f2a07165d85a98c61f53a791334e044f"}},"error":null,"degraded_mode":false}`
const WITH_PROJECT_TEXT = `{"schema":"leaf.solar-landxml-import.v1","created":true,"drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","source":{"artifact_id":"d6923bdcbbc5c7e02b2cfc486ff981d716adc4b7d05e344c04939bb4eef33cce","media_type":"application/xml","filename":"landxml-source.xml","byte_length":45305,"content_sha256":"9846f488c6780af2185fdf62741865fbc7a75077488449f6741f0aac77f370b5","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/d6923bdcbbc5c7e02b2cfc486ff981d716adc4b7d05e344c04939bb4eef33cce"},"interpretation":{"point_order":"northing-easting-elevation","drawing_x":"easting","drawing_y":"northing","linear_unit":"meter","meters_per_source_unit":1.0,"drawing_units":"m","meters_per_drawing_unit":1.0,"horizontal_scale":1.0,"elevation_scale":1.0,"crs":"none","crs_source":"declared","elevation_datum":"unrecorded"},"points":{"declared":441,"accepted":441,"skipped":0},"grid":{"rows":30,"cols":30,"target_cells":30,"x_min":-100.0,"x_max":100.0,"y_min":-100.0,"y_max":100.0},"head":{"schema":"leaf.solar-physical-head.v1","drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","index":2,"parent":"3a16bacbf6d90a1f6d01345174a40f86f2a07165d85a98c61f53a791334e044f","state":{"artifact_id":"0e63a1bceb0d338bd7a550bda337670d5af1d5dbec9f7242009249b425bc2eb4","media_type":"application/json","filename":"physical-state.json","byte_length":18345,"content_sha256":"1ca5790746b6b63f5eb5399fd75f37507361a1eb0e3479c3666ac526400bcaab","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/0e63a1bceb0d338bd7a550bda337670d5af1d5dbec9f7242009249b425bc2eb4"}},"error":null,"degraded_mode":false}`
const FEET_TEXT = `{"schema":"leaf.solar-landxml-import.v1","created":true,"drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","source":{"artifact_id":"a781e6aa8d03309fe71ccd5ef329f0822e9bf0fee99315dc3d756ec10f16e8c0","media_type":"application/xml","filename":"landxml-source.xml","byte_length":397,"content_sha256":"fe90106cab9c348d58dcaa53c53f658bcd1c9b702459631cca2fc8bba01ef534","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/a781e6aa8d03309fe71ccd5ef329f0822e9bf0fee99315dc3d756ec10f16e8c0"},"interpretation":{"point_order":"northing-easting-elevation","drawing_x":"easting","drawing_y":"northing","linear_unit":"foot","meters_per_source_unit":0.3048,"drawing_units":"ft","meters_per_drawing_unit":0.3048,"horizontal_scale":1.0,"elevation_scale":0.3048,"crs":"none","crs_source":"declared","elevation_datum":"unrecorded"},"points":{"declared":4,"accepted":4,"skipped":0},"grid":{"rows":30,"cols":30,"target_cells":30,"x_min":0.0,"x_max":10.0,"y_min":0.0,"y_max":10.0},"head":{"schema":"leaf.solar-physical-head.v1","drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","index":0,"parent":null,"state":{"artifact_id":"3d96036c26a7c4afc2cd2a61af308cc5111faf8fef6a44efb1280d97e1efae4c","media_type":"application/json","filename":"physical-state.json","byte_length":17120,"content_sha256":"fa51beec8c285e6a75fce557d1cdf3b267b8981e031f950a620167cc36b89bbc","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/3d96036c26a7c4afc2cd2a61af308cc5111faf8fef6a44efb1280d97e1efae4c"}},"error":null,"degraded_mode":false}`
const FILE_CRS_TEXT = `{"schema":"leaf.solar-landxml-import.v1","created":true,"drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","source":{"artifact_id":"e876a4a1e00bf0852d8999784a3c9a5eac1327b3b608e818d9b776ece620c622","media_type":"application/xml","filename":"landxml-source.xml","byte_length":432,"content_sha256":"9ec1f60c03bb7276faff4a874519ea3f3b45c5176184ddce5c43f7d24021be19","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/e876a4a1e00bf0852d8999784a3c9a5eac1327b3b608e818d9b776ece620c622"},"interpretation":{"point_order":"northing-easting-elevation","drawing_x":"easting","drawing_y":"northing","linear_unit":"foot","meters_per_source_unit":0.3048,"drawing_units":"ft","meters_per_drawing_unit":0.3048,"horizontal_scale":1.0,"elevation_scale":0.3048,"crs":"EPSG:2229","crs_source":"file","elevation_datum":"unrecorded"},"points":{"declared":4,"accepted":4,"skipped":0},"grid":{"rows":30,"cols":30,"target_cells":30,"x_min":0.0,"x_max":10.0,"y_min":0.0,"y_max":10.0},"head":{"schema":"leaf.solar-physical-head.v1","drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","index":0,"parent":null,"state":{"artifact_id":"4aa90b00d12ef0c30a72b0560675bf1de57b121a4f540d02ac662381c284e207","media_type":"application/json","filename":"physical-state.json","byte_length":17125,"content_sha256":"c4b95495892522847661a1915d1ffbe40bf6ba85689a2af2a516bc4125cdc901","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/4aa90b00d12ef0c30a72b0560675bf1de57b121a4f540d02ac662381c284e207"}},"error":null,"degraded_mode":false}`
const SURVEY_FOOT_TEXT = `{"schema":"leaf.solar-landxml-import.v1","created":true,"drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","source":{"artifact_id":"559c08ffa5f9d86594cdd420bab939c1dbc88cfeb90fc606e90969992e429ada","media_type":"application/xml","filename":"landxml-source.xml","byte_length":405,"content_sha256":"262715967fb861b3ac43def00f47c151e83b6128ea2cab45c2007233c209ad9c","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/559c08ffa5f9d86594cdd420bab939c1dbc88cfeb90fc606e90969992e429ada"},"interpretation":{"point_order":"northing-easting-elevation","drawing_x":"easting","drawing_y":"northing","linear_unit":"USSurveyFoot","meters_per_source_unit":0.3048006096012192,"drawing_units":"m","meters_per_drawing_unit":1.0,"horizontal_scale":0.3048006096012192,"elevation_scale":0.3048006096012192,"crs":"EPSG:2229","crs_source":"declared","elevation_datum":"unrecorded"},"points":{"declared":4,"accepted":4,"skipped":0},"grid":{"rows":7,"cols":7,"target_cells":7,"x_min":0.0,"x_max":3.048006096012192,"y_min":0.0,"y_max":3.048006096012192},"head":{"schema":"leaf.solar-physical-head.v1","drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","index":0,"parent":null,"state":{"artifact_id":"e25d727e48a25bcef3f5cc5d0b2da69253d666d587749001f08e5b9bc9144eec","media_type":"application/json","filename":"physical-state.json","byte_length":1416,"content_sha256":"09e0d26e0cd343d5c6c00e3a07b641b40202ee0b347d648aa2a8de196476a0cf","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/e25d727e48a25bcef3f5cc5d0b2da69253d666d587749001f08e5b9bc9144eec"}},"error":null,"degraded_mode":false}`
const UNSAFE_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"BAD_PARAMS","message":"LANDXML_UNSAFE","retryable":false,"retry_class":"after_action","actor":"user","next_action":"Review the inputs, correct them, and submit again.","reason_code":"LANDXML_UNSAFE"},"degraded_mode":false}`
const DRAINED_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"INTERNAL","message":"LANDXML_WRITES_DRAINED","retryable":true,"retry_class":"backoff","actor":"service","next_action":"Wait a short time, then retry the request.","reason_code":"LANDXML_WRITES_DRAINED"},"degraded_mode":false}`
const CONFLICT_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"BAD_PARAMS","message":"PHYSICAL_HEAD_CONFLICT","retryable":true,"retry_class":"backoff","actor":"service","next_action":"Wait a short time, then retry the request.","reason_code":"PHYSICAL_HEAD_CONFLICT"},"degraded_mode":false}`
const UNLISTED_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"INTERNAL","message":"LANDXML_IMPORT_FAILED","retryable":false,"retry_class":"never","actor":"operator","next_action":"Contact support with the displayed error identifier.","reason_code":"LANDXML_IMPORT_FAILED"},"degraded_mode":false}`
const MEDIA_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"BAD_PARAMS","message":"LANDXML_MEDIA_TYPE_REFUSED","retryable":false,"retry_class":"after_action","actor":"user","next_action":"Review the inputs, correct them, and submit again.","reason_code":"LANDXML_MEDIA_TYPE_REFUSED"},"degraded_mode":false}`
const UNITS_MISMATCH_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"BAD_PARAMS","message":"LANDXML_UNITS_MISMATCH","retryable":false,"retry_class":"after_action","actor":"user","next_action":"Review the inputs, correct them, and submit again.","reason_code":"LANDXML_UNITS_MISMATCH"},"degraded_mode":false}`
const CELLS_BAD_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"BAD_PARAMS","message":"LANDXML_TARGET_CELLS_INVALID","retryable":false,"retry_class":"after_action","actor":"user","next_action":"Review the inputs, correct them, and submit again.","reason_code":"LANDXML_TARGET_CELLS_INVALID"},"degraded_mode":false}`
const ENTITLEMENT_DENIED_TEXT = `{"entitlement_required":true,"required":"upload","tier":"demo","error":{"error_code":"ENTITLEMENT_REQUIRED","message":"the 'demo' plan does not include uploading drawings; upgrade the workspace plan to enable uploads.","retryable":false,"retry_class":"after_action","actor":"workspace_admin","next_action":"Enable this capability for the workspace, then retry."},"degraded_mode":false}`
const POLICY_UNAVAILABLE_TEXT = `{"entitlement_required":true,"required":"upload","tier":"demo","error":{"error_code":"INTERNAL","message":"entitlement policy is unavailable; request refused (fail closed).","retryable":true,"retry_class":"backoff","actor":"service","next_action":"Wait a short time, then retry the request."},"degraded_mode":false}`
const GUEST_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"FORBIDDEN","message":"guest sessions are upload-only: upload, upload-status, intake and versions reads; create an account for everything else","retryable":false,"retry_class":"after_action","actor":"workspace_admin","next_action":"Ask a workspace admin to grant the required access."},"degraded_mode":false}`
const UNAUTH_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"UNAUTHENTICATED","message":"missing bearer token (Authorization header)","retryable":false,"retry_class":"after_action","actor":"user","next_action":"Sign in, then repeat the request."},"degraded_mode":false}`
const MEASURED_FIRST_SHA = '3f1ec0ddad58f7cfee71d293213d2a0067fa6be64bc45f8a30eadced8dfdb91b'
const MEASURED_AGAIN_SHA = '6664d22058698ba3163c9562d8881dd2078cd035a1597caf4eb95f2c1aa8ba17'
const MEASURED_CELLS_10_SHA = '0d66095e4181dc8a9b00970c8119a7fa55afea32a539fbe43a4c745ba4e9d302'
const MEASURED_WITH_PROJECT_SHA = 'a2baf1ad540004fc474207799606856b1e384e7d4f45014648968c1f561bc7eb'
const MEASURED_FEET_SHA = '336ed2e47b5da14018ba1962af08c6f71745fb1df7dede398fa3afefdc83b9f1'
const MEASURED_FILE_CRS_SHA = '7b75cc284ca22c713a2e6a926f977253e731e83facd214f8deccdeb49aeace49'
const MEASURED_SURVEY_FOOT_SHA = '384d67fed3b93c3b26beb35e0b04b2e2f7be275f9a15b7270f3e1e026db09bf9'
const STATUS = Object.freeze({
  UNSAFE: 400, DRAINED: 503, CONFLICT: 409, UNLISTED: 500, MEDIA: 415, UNITS_MISMATCH: 409, CELLS_BAD: 400,
  ENTITLEMENT_DENIED: 403, POLICY_UNAVAILABLE: 503, GUEST: 403, UNAUTH: 401,
})
const FIRST = JSON.parse(FIRST_TEXT)
const PROJECT = 'leaf:project:00000000-0000-4000-8000-000000000001'
const CAPTURE_BYTES = 45_305
// The 42 keys of LANDXML_IMPORT_REFUSALS in server/routers/drawings.py at 86573930, sorted.
const SERVER_CODES = [
  'LANDXML_COORDINATE_OUT_OF_RANGE', 'LANDXML_CRS_INVALID', 'LANDXML_CRS_MISMATCH', 'LANDXML_CRS_UNSUPPORTED',
  'LANDXML_DRAWING_ID_INVALID', 'LANDXML_DRAWING_NOT_FOUND', 'LANDXML_DRAWING_UNITS_INVALID', 'LANDXML_EMPTY',
  'LANDXML_ENCODING_INVALID', 'LANDXML_GRAPH_REQUIRED', 'LANDXML_IMPORT_FAILED', 'LANDXML_MALFORMED',
  'LANDXML_MEDIA_TYPE_REFUSED', 'LANDXML_NOT_LANDXML', 'LANDXML_PROJECT_ID_INVALID', 'LANDXML_PROJECT_MISMATCH',
  'LANDXML_RESAMPLE_TOO_LARGE', 'LANDXML_SOURCE_CORRUPT', 'LANDXML_SOURCE_ID_INVALID', 'LANDXML_SOURCE_KIND_MISMATCH',
  'LANDXML_SOURCE_NOT_FOUND', 'LANDXML_STORE_UNAVAILABLE', 'LANDXML_TARGET_CELLS_INVALID', 'LANDXML_TOO_FEW_POINTS',
  'LANDXML_TOO_LARGE', 'LANDXML_TOO_MANY_POINTS', 'LANDXML_UNITS_MISMATCH', 'LANDXML_UNITS_MISSING',
  'LANDXML_UNITS_UNSUPPORTED', 'LANDXML_UNSAFE', 'LANDXML_WRITES_DRAINED',
  'PHYSICAL_HEAD_CONFLICT', 'PHYSICAL_HEAD_CORRUPT', 'PHYSICAL_HEAD_LOG_FULL', 'PHYSICAL_HEAD_PROJECT_MISMATCH',
  'PHYSICAL_HEAD_STORE_UNAVAILABLE', 'PHYSICAL_HEAD_STORE_UNSAFE', 'PHYSICAL_HEAD_WRITES_DRAINED',
  'PHYSICAL_STATE_CORRUPT', 'PHYSICAL_STATE_PROJECT_MISMATCH', 'PHYSICAL_STATE_STORE_UNAVAILABLE',
  'PHYSICAL_STATE_WRITES_DRAINED',
]
const CLIENT_AND_SESSION_CODES = [
  'ENTITLEMENT_POLICY_UNAVAILABLE', 'ENTITLEMENT_REQUIRED', 'FORBIDDEN', 'LANDXML_CLIENT_ABORTED',
  'LANDXML_CLIENT_NETWORK', 'LANDXML_CLIENT_REQUEST_INVALID', 'LANDXML_CLIENT_RESPONSE_INVALID',
  'LANDXML_CLIENT_TIMEOUT', 'UNAUTHENTICATED',
]

function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`
  if (value !== null && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`
  }
  return JSON.stringify(value)
}
const canonicalSha = (value) => createHash('sha256').update(canonical(value)).digest('hex')
const clone = (value) => JSON.parse(JSON.stringify(value))
const withoutEnvelope = (body) => {
  const copy = clone(body)
  delete copy.error
  delete copy.degraded_mode
  return copy
}
const textResponse = (text, status = 200, headers = {}) => new Response(
  text, { status, headers: { 'content-type': 'application/json', ...headers } },
)
const jsonResponse = (body, status = 200) => textResponse(JSON.stringify(body), status)
const answering = (makeResponse) => vi.fn(async () => makeResponse())
const neverAnswers = () => vi.fn((url, init) => new Promise((resolve, reject) => {
  init.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true })
}))
const tenantHeaders = () => ({ 'X-Tenant-Id': 'fixture-tenant', Authorization: 'Bearer t' })
function clientWith(fetchImpl, extra = {}) {
  const onResponse = vi.fn()
  const client = createSolarLandxmlClient({
    fetchImpl, apiBase: 'https://studio.test', headers: tenantHeaders, onResponse, ...extra,
  })
  return { client, onResponse }
}
const refused = (status, code, retryable) => ({ ok: false, status, code, retryable })
const xmlBlob = (bytes = CAPTURE_BYTES) => new Blob([new Uint8Array(bytes)], { type: 'application/xml' })
const args = (overrides = {}) => ({ drawingId: 'solar', file: xmlBlob(), drawingUnits: 'm', crs: 'none', ...overrides })
const URL_FIRST = 'https://studio.test/api/drawings/solar/imports/landxml?drawing_units=m&crs=none&target_cells=30'
function readServer(relative) {
  const buffer = readFileSync(new URL(`../../../server/${relative}`, import.meta.url))
  expect(buffer.length).toBeLessThanOrEqual(1024 * 1024)
  return buffer.toString('utf8')
}
const escapeRegExp = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
// A line that starts with `line`, then only spaces or a comment.
const hasLine = (text, line) => new RegExp(`^${escapeRegExp(line)}[ \\t]*(#.*)?\\r?$`, 'm').test(text)
// Every "CODE": (status, ErrorCode.X, bool) row of the route's LANDXML_IMPORT_REFUSALS literal.
function routeMap() {
  const text = readServer('routers/drawings.py')
  const start = text.indexOf('LANDXML_IMPORT_REFUSALS = {')
  expect(start).toBeGreaterThanOrEqual(0)
  const end = text.indexOf('\n}', start)
  expect(end).toBeGreaterThan(start)
  const rows = new Map()
  for (const match of text.slice(start, end).matchAll(
    /"([A-Z][A-Z0-9_]{0,63})":\s*\((\d{3}),\s*ErrorCode\.([A-Z_]+),\s*(True|False)\)/g)) {
    rows.set(match[1], { status: Number(match[2]), envelope: match[3], retryable: match[4] === 'True' })
  }
  return rows
}
// An envelope in the measured shape for any code.
const envelopeFor = (code, retryable, base = UNSAFE_TEXT) => {
  const copy = JSON.parse(base)
  copy.error.message = code
  copy.error.reason_code = code
  copy.error.retryable = retryable
  return copy
}
const HUNG = 'hung'
async function settleWithin(promise, ms = 1000) {
  let timer
  const bound = new Promise((resolve) => { timer = setTimeout(() => resolve(HUNG), ms) })
  try {
    return await Promise.race([promise, bound])
  } finally {
    clearTimeout(timer)
  }
}
const stalledResponse = (status = 200) => new Response(new ReadableStream({ start() {} }), {
  status, headers: { 'content-type': 'application/json' },
})
const oneBytePerPull = (bytes) => {
  let index = 0
  return new ReadableStream({
    pull(controller) {
      if (index < bytes.length) {
        controller.enqueue(bytes.subarray(index, index + 1))
        index += 1
      } else {
        controller.close()
      }
    },
  })
}

describe('solar LandXML client', () => {
  it('LX1 constants and the reason map', () => {
    expect(LANDXML_MAX_BYTES).toBe(16_777_216)
    expect(LANDXML_RESPONSE_MAX_BYTES).toBe(65_536)
    expect(LANDXML_DEFAULT_TARGET_CELLS).toBe(30)
    expect(LANDXML_MIN_TARGET_CELLS).toBe(2)
    expect(LANDXML_MAX_TARGET_CELLS).toBe(200)
    expect(LANDXML_DRAWING_UNITS).toEqual(['m', 'ft'])
    expect(LANDXML_TIMEOUT_MS).toBe(120_000)
    expect(LANDXML_IMPORT_FALLBACK).toBe('The LandXML import stopped')
    expect(Object.isFrozen(LANDXML_DRAWING_UNITS)).toBe(true)
    expect(Object.isFrozen(LANDXML_IMPORT_REASONS)).toBe(true)
    for (const list of [SERVER_CODES, CLIENT_AND_SESSION_CODES]) expect(list).toEqual([...list].sort())
    expect(SERVER_CODES).toHaveLength(42)
    const allCodes = [...SERVER_CODES, ...CLIENT_AND_SESSION_CODES].sort()
    expect(allCodes).toHaveLength(51)
    expect(Object.keys(LANDXML_IMPORT_REASONS).sort()).toEqual(allCodes)
    for (const sentence of Object.values(LANDXML_IMPORT_REASONS)) {
      expect(typeof sentence).toBe('string')
      expect(sentence.length).toBeGreaterThanOrEqual(12)
      expect(sentence.length).toBeLessThanOrEqual(120)
      expect(sentence).not.toMatch(/[;–—]/)
      expect(sentence.endsWith('.')).toBe(false)
    }
  })

  it('LX2 route codes follow the server refusal map', () => {
    const rows = routeMap()
    expect([...rows.keys()].sort()).toEqual(SERVER_CODES)
    const routeKeys = Object.keys(LANDXML_IMPORT_REASONS).filter((code) => !CLIENT_AND_SESSION_CODES.includes(code))
    expect(routeKeys.sort()).toEqual([...rows.keys()].sort())
    expect(hasLine(readServer('routers/drawings.py'),
      'LANDXML_MEDIA_TYPES = frozenset({"application/xml", "text/xml"})')).toBe(true)
  })

  it('LX3 limits and shapes follow the server constants', () => {
    const intake = readServer('solar_landxml_import.py')
    for (const line of [
      'RESULT_SCHEMA = "leaf.solar-landxml-import.v1"', 'SOURCE_MEDIA_TYPE = "application/xml"',
      'SOURCE_FILENAME = "landxml-source.xml"', 'POINT_ORDER = "northing-easting-elevation"',
      'MAX_LANDXML_BYTES = 16_777_216', 'MAX_LANDXML_POINTS = 250_000', 'MAX_ABS_COORDINATE = 1e9',
      'DEFAULT_TARGET_CELLS = 30', 'MIN_TARGET_CELLS = 2', 'MAX_TARGET_CELLS = 200', 'MAX_PROJECT_ID_CHARS = 100',
      '    ("Metric", "meter"): ("meter", 1.0),', '    ("Imperial", "foot"): ("foot", 0.3048),',
      '    ("Imperial", "USSurveyFoot"): ("USSurveyFoot", 1200.0 / 3937.0),',
    ]) {
      expect(hasLine(intake, line)).toBe(true)
    }
    const state = readServer('solar_physical_state.py')
    for (const line of ['UNITS = {"m": 1.0, "ft": 0.3048}', 'FILENAME = "physical-state.json"']) {
      expect(hasLine(state, line)).toBe(true)
    }
    const head = readServer('solar_physical_head.py')
    for (const line of ['HEAD_SCHEMA = "leaf.solar-physical-head.v1"', 'MAX_LOG_ENTRIES = 4096']) {
      expect(hasLine(head, line)).toBe(true)
    }
    expect(hasLine(readServer('solar_artifacts.py'), 'MAX_ARTIFACT_BYTES = 16_777_216')).toBe(true)
  })

  it('LX4 landxmlReason gives a sentence or a bounded fallback', () => {
    expect(landxmlReason('LANDXML_UNSAFE')).toBe('The file declares a document type or entities, which an import never reads')
    expect(landxmlReason('LANDXML_FUTURE_CODE')).toBe('The LandXML import stopped (LANDXML_FUTURE_CODE)')
    for (const value of ['bad code', 'A'.repeat(65), null, 7, 'constructor', 'toString', 'hasOwnProperty']) {
      expect(landxmlReason(value)).toBe('The LandXML import stopped')
    }
  })

  it('LX5 upload of the LEAFLANDXMLDEMO capture', async () => {
    const fetchImpl = answering(() => textResponse(FIRST_TEXT))
    const { client, onResponse } = clientWith(fetchImpl)
    const file = xmlBlob()
    const result = await client.uploadLandxml({ drawingId: 'solar', file, drawingUnits: 'm', crs: 'none' })
    expect(result).toEqual({ ok: true, status: 200, value: withoutEnvelope(FIRST) })
    expect(Object.keys(result.value)).toEqual([
      'schema', 'created', 'drawing_id', 'project_id', 'source', 'interpretation', 'points', 'grid', 'head',
    ])
    expect(canonicalSha(result.value)).toBe(MEASURED_FIRST_SHA)
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    const [url, init] = fetchImpl.mock.calls[0]
    expect(url).toBe(URL_FIRST)
    expect(init.method).toBe('POST')
    expect(init.body).toBe(file)
    expect(init.headers).toEqual({
      'X-Tenant-Id': 'fixture-tenant', Authorization: 'Bearer t', 'Content-Type': 'application/xml',
    })
    expect(onResponse).toHaveBeenCalledTimes(1)
    expect(onResponse.mock.calls[0][1]).toBe(URL_FIRST)
    expect(onResponse.mock.calls[0][2]).toBe('Bearer t')
    result.value.grid.rows = 1
    expect(FIRST.grid.rows).toBe(30)
    const body = JSON.parse(FIRST_TEXT)
    const request = {
      drawingId: 'solar', drawingUnits: 'm', crs: 'none', targetCells: 30, projectId: null, byteLength: file.size,
    }
    const value = validateLandxmlImport(body, request)
    expect(value).toEqual(withoutEnvelope(FIRST))
    expect(value.source).not.toBe(body.source)
    expect(value.interpretation).not.toBe(body.interpretation)
    expect(value.points).not.toBe(body.points)
    expect(value.grid).not.toBe(body.grid)
    expect(value.head).not.toBe(body.head)
    expect(value.head.state).not.toBe(body.head.state)
    value.grid.rows = 1
    expect(body.grid.rows).toBe(30)
  })

  it('LX6 every measured success reads back in its own request', async () => {
    const cases = [
      [AGAIN_TEXT, {}, URL_FIRST, MEASURED_AGAIN_SHA],
      [CELLS_10_TEXT, { targetCells: 10 }, URL_FIRST.replace('target_cells=30', 'target_cells=10'), MEASURED_CELLS_10_SHA],
      [WITH_PROJECT_TEXT, { projectId: PROJECT },
        `${URL_FIRST}&project_id=leaf%3Aproject%3A00000000-0000-4000-8000-000000000001`, MEASURED_WITH_PROJECT_SHA],
      [FEET_TEXT, { drawingUnits: 'ft', file: xmlBlob(397) }, URL_FIRST.replace('drawing_units=m', 'drawing_units=ft'),
        MEASURED_FEET_SHA],
      [FILE_CRS_TEXT, { drawingUnits: 'ft', crs: 'EPSG:2229', file: xmlBlob(432) },
        'https://studio.test/api/drawings/solar/imports/landxml?drawing_units=ft&crs=EPSG%3A2229&target_cells=30',
        MEASURED_FILE_CRS_SHA],
      [SURVEY_FOOT_TEXT, { crs: 'EPSG:2229', targetCells: 7, file: xmlBlob(405) },
        'https://studio.test/api/drawings/solar/imports/landxml?drawing_units=m&crs=EPSG%3A2229&target_cells=7',
        MEASURED_SURVEY_FOOT_SHA],
    ]
    for (const [text, overrides, url, sha] of cases) {
      const fetchImpl = answering(() => textResponse(text))
      const { client } = clientWith(fetchImpl)
      const result = await client.uploadLandxml(args(overrides))
      expect(result).toEqual({ ok: true, status: 200, value: withoutEnvelope(JSON.parse(text)) })
      expect(canonicalSha(result.value)).toBe(sha)
      expect(fetchImpl.mock.calls[0][0]).toBe(url)
    }
  })

  it('LX7 prechecks send nothing', async () => {
    const cases = [
      [{ drawingId: 'Bad!' }, 'LANDXML_DRAWING_ID_INVALID'],
      [{ drawingId: 'x'.repeat(64) }, 'LANDXML_DRAWING_ID_INVALID'],
      [{ drawingId: undefined }, 'LANDXML_DRAWING_ID_INVALID'],
      [{ projectId: '' }, 'LANDXML_PROJECT_ID_INVALID'],
      [{ projectId: 'p'.repeat(101) }, 'LANDXML_PROJECT_ID_INVALID'],
      [{ projectId: 7 }, 'LANDXML_PROJECT_ID_INVALID'],
      [{ projectId: '\ud800' }, 'LANDXML_PROJECT_ID_INVALID'],
      [{ drawingUnits: undefined }, 'LANDXML_DRAWING_UNITS_INVALID'],
      [{ drawingUnits: 'in' }, 'LANDXML_DRAWING_UNITS_INVALID'],
      [{ drawingUnits: 'M' }, 'LANDXML_DRAWING_UNITS_INVALID'],
      [{ crs: undefined }, 'LANDXML_CRS_INVALID'],
      [{ crs: 'epsg:4326' }, 'LANDXML_CRS_INVALID'],
      [{ crs: 'EPSG:0' }, 'LANDXML_CRS_INVALID'],
      [{ crs: 'EPSG:1234567' }, 'LANDXML_CRS_INVALID'],
      [{ crs: 'None' }, 'LANDXML_CRS_INVALID'],
      [{ targetCells: 1 }, 'LANDXML_TARGET_CELLS_INVALID'],
      [{ targetCells: 201 }, 'LANDXML_TARGET_CELLS_INVALID'],
      [{ targetCells: 2.5 }, 'LANDXML_TARGET_CELLS_INVALID'],
      [{ targetCells: '30' }, 'LANDXML_TARGET_CELLS_INVALID'],
      [{ targetCells: null }, 'LANDXML_TARGET_CELLS_INVALID'],
      [{ file: undefined }, 'LANDXML_CLIENT_REQUEST_INVALID'],
      [{ file: 'text' }, 'LANDXML_CLIENT_REQUEST_INVALID'],
      [{ file: xmlBlob(0) }, 'LANDXML_EMPTY'],
      [{ file: new Uint8Array(0) }, 'LANDXML_EMPTY'],
      [{ file: { size: 16_777_217 } }, 'LANDXML_CLIENT_REQUEST_INVALID'],
      [{ file: new ArrayBuffer(16_777_217) }, 'LANDXML_TOO_LARGE'],
      [{ signal: {} }, 'LANDXML_CLIENT_REQUEST_INVALID'],
    ]
    for (const [overrides, code] of cases) {
      const fetchImpl = vi.fn()
      const { client } = clientWith(fetchImpl)
      expect(await client.uploadLandxml(args(overrides))).toEqual(refused(null, code, false))
      expect(fetchImpl).not.toHaveBeenCalled()
    }
    const fetchImpl = vi.fn()
    const { client } = clientWith(fetchImpl)
    expect(await client.uploadLandxml()).toEqual(refused(null, 'LANDXML_DRAWING_ID_INVALID', false))
    expect(fetchImpl).not.toHaveBeenCalled()
  })

  it('LX8 bounds at the edge are sent', async () => {
    const cases = [
      [{ targetCells: 2 }, 'target_cells=2'],
      [{ targetCells: 200 }, 'target_cells=200'],
      [{ crs: 'EPSG:999999' }, 'crs=EPSG%3A999999'],
      [{ projectId: 'p'.repeat(100) }, `project_id=${'p'.repeat(100)}`],
      [{ projectId: '\u{1F600}'.repeat(100) }, `project_id=${'%F0%9F%98%80'.repeat(100)}`],
      [{ file: new Uint8Array(16_777_216) }, 'target_cells=30'],
      [{ drawingId: 'a'.repeat(63) }, `/api/drawings/${'a'.repeat(63)}/imports/landxml?`],
    ]
    for (const [overrides, fragment] of cases) {
      const fetchImpl = answering(() => textResponse(UNSAFE_TEXT, 400))
      const { client } = clientWith(fetchImpl)
      expect(await client.uploadLandxml(args(overrides))).toEqual(refused(400, 'LANDXML_UNSAFE', false))
      expect(fetchImpl).toHaveBeenCalledTimes(1)
      expect(fetchImpl.mock.calls[0][0]).toContain(fragment)
    }
  })

  it('LX9 refusals carry the server code and its retry flag', async () => {
    const measured = [
      [UNSAFE_TEXT, STATUS.UNSAFE, 'LANDXML_UNSAFE', false],
      [DRAINED_TEXT, STATUS.DRAINED, 'LANDXML_WRITES_DRAINED', true],
      [CONFLICT_TEXT, STATUS.CONFLICT, 'PHYSICAL_HEAD_CONFLICT', true],
      [UNLISTED_TEXT, STATUS.UNLISTED, 'LANDXML_IMPORT_FAILED', false],
      [MEDIA_TEXT, STATUS.MEDIA, 'LANDXML_MEDIA_TYPE_REFUSED', false],
      [UNITS_MISMATCH_TEXT, STATUS.UNITS_MISMATCH, 'LANDXML_UNITS_MISMATCH', false],
      [CELLS_BAD_TEXT, STATUS.CELLS_BAD, 'LANDXML_TARGET_CELLS_INVALID', false],
    ]
    for (const [text, status, code, retryable] of measured) {
      const { client } = clientWith(answering(() => textResponse(text, status)))
      expect(await client.uploadLandxml(args())).toEqual(refused(status, code, retryable))
    }
    const rows = routeMap()
    expect(rows.size).toBe(42)
    for (const [code, row] of rows) {
      const { client } = clientWith(answering(() => jsonResponse(envelopeFor(code, row.retryable), row.status)))
      expect(await client.uploadLandxml(args())).toEqual(refused(row.status, code, row.retryable))
    }
    const { client } = clientWith(answering(() => jsonResponse(envelopeFor('LANDXML_FUTURE_CODE', false), 400)))
    expect(await client.uploadLandxml(args())).toEqual(refused(400, 'LANDXML_FUTURE_CODE', false))
  })

  it('LX10 success bodies that break the shape or the request are refused', async () => {
    const edited = (edit) => {
      const body = JSON.parse(FIRST_TEXT)
      edit(body)
      return body
    }
    const edits = [
      (b) => { b.extra = 1 },
      (b) => { delete b.head },
      (b) => { b.schema = 'leaf.solar-landxml-import.v2' },
      (b) => { b.created = 'yes' },
      (b) => { b.drawing_id = 'other' },
      (b) => { b.project_id = null },
      (b) => { b.project_id = 'p'.repeat(101) },
      (b) => { b.error = { reason_code: 'X' } },
      (b) => { b.degraded_mode = 'no' },
      (b) => { b.source.media_type = 'application/pdf' },
      (b) => { b.source.filename = 'source.xml' },
      (b) => { b.source.byte_length = CAPTURE_BYTES - 1 },
      (b) => { b.source.download = '/api/drawings/other/artifacts/' + b.source.artifact_id },
      (b) => { b.source.artifact_id = 'A'.repeat(64) },
      (b) => { b.source.source_version = 0 },
      (b) => { b.source.extra = true },
      (b) => { b.interpretation.point_order = 'easting-northing-elevation' },
      (b) => { b.interpretation.drawing_x = 'northing' },
      (b) => { b.interpretation.linear_unit = 'inch' },
      (b) => { b.interpretation.meters_per_source_unit = 0.3048 },
      (b) => { b.interpretation.drawing_units = 'ft' },
      (b) => { b.interpretation.meters_per_drawing_unit = 0.3048 },
      (b) => { b.interpretation.horizontal_scale = 1 + Number.EPSILON },
      (b) => { b.interpretation.elevation_scale = 2 },
      (b) => { b.interpretation.crs = 'EPSG:2229' },
      (b) => { b.interpretation.crs_source = 'file' },
      (b) => { b.interpretation.crs_source = 'guessed' },
      (b) => { b.interpretation.elevation_datum = 'mean sea level' },
      (b) => { delete b.interpretation.elevation_datum },
      (b) => { b.points.skipped = 1 },
      (b) => { b.points.accepted = 442 },
      (b) => { b.points.declared = 2; b.points.accepted = 2 },
      (b) => { b.points.declared = 250_001; b.points.skipped = 249_560 },
      (b) => { b.grid.target_cells = 12 },
      (b) => { b.grid.rows = 1 },
      (b) => { b.grid.rows = 29; b.grid.cols = 29 },
      (b) => { b.grid.x_min = 100 },
      (b) => { b.grid.y_max = 1e11 },
      (b) => { b.grid.x_max = '100' },
      (b) => { b.head.schema = 'leaf.solar-physical-head.v2' },
      (b) => { b.head.drawing_id = 'other' },
      (b) => { b.head.project_id = 'leaf:project:other' },
      (b) => { b.head.index = 4096 },
      (b) => { b.head.parent = '0'.repeat(64) },
      (b) => { b.head.index = 1 },
      (b) => { b.head.state.filename = 'landxml-source.xml' },
      (b) => { b.head.state.media_type = 'application/xml' },
      (b) => { b.head.state.byte_length = 16_777_217 },
    ]
    for (const edit of edits) {
      const { client } = clientWith(answering(() => jsonResponse(edited(edit))))
      expect(await client.uploadLandxml(args())).toEqual(refused(200, 'LANDXML_CLIENT_RESPONSE_INVALID', false))
    }
    for (const text of ['[]', 'null', '"ok"', 'not json', '']) {
      const { client } = clientWith(answering(() => textResponse(text)))
      expect(await client.uploadLandxml(args())).toEqual(refused(200, 'LANDXML_CLIENT_RESPONSE_INVALID', false))
    }
    const { client } = clientWith(answering(() => textResponse(FIRST_TEXT)))
    expect(await client.uploadLandxml(args({ projectId: 'leaf:project:other' })))
      .toEqual(refused(200, 'LANDXML_CLIENT_RESPONSE_INVALID', false))
    expect(await client.uploadLandxml(args({ crs: 'EPSG:2229' })))
      .toEqual(refused(200, 'LANDXML_CLIENT_RESPONSE_INVALID', false))
    expect(await client.uploadLandxml(args({ targetCells: 31 })))
      .toEqual(refused(200, 'LANDXML_CLIENT_RESPONSE_INVALID', false))
    const created = clientWith(answering(() => textResponse(FIRST_TEXT, 201)))
    expect(await created.client.uploadLandxml(args())).toEqual(refused(201, 'LANDXML_CLIENT_RESPONSE_INVALID', false))
    expect(validateLandxmlImport(FIRST, null)).toBe(null)
    expect(validateLandxmlImport(FIRST, {
      drawingId: 'solar', drawingUnits: 'm', crs: 'none', targetCells: 30, byteLength: CAPTURE_BYTES,
    })).toEqual(withoutEnvelope(FIRST))
  })

  it('LX11 session, plan and guest refusals', async () => {
    const cases = [
      [UNAUTH_TEXT, STATUS.UNAUTH, refused(401, 'UNAUTHENTICATED', false)],
      [ENTITLEMENT_DENIED_TEXT, STATUS.ENTITLEMENT_DENIED, refused(403, 'ENTITLEMENT_REQUIRED', false)],
      [POLICY_UNAVAILABLE_TEXT, STATUS.POLICY_UNAVAILABLE, refused(503, 'ENTITLEMENT_POLICY_UNAVAILABLE', true)],
      [GUEST_TEXT, STATUS.GUEST, refused(403, 'FORBIDDEN', false)],
      [ENTITLEMENT_DENIED_TEXT, 400, refused(400, 'LANDXML_CLIENT_RESPONSE_INVALID', false)],
      [GUEST_TEXT, 400, refused(400, 'LANDXML_CLIENT_RESPONSE_INVALID', false)],
      ['<html>Bad gateway</html>', 502, refused(502, 'LANDXML_CLIENT_RESPONSE_INVALID', true)],
      ['{"error":{"reason_code":"bad code"}}', 400, refused(400, 'LANDXML_CLIENT_RESPONSE_INVALID', false)],
    ]
    for (const [text, status, expected] of cases) {
      const { client } = clientWith(answering(() => textResponse(text, status)))
      expect(await client.uploadLandxml(args())).toEqual(expected)
    }
  })

  it('LX12 transport failures resolve and never reject', async () => {
    const network = clientWith(vi.fn(async () => { throw new TypeError('Failed to fetch') }))
    expect(await network.client.uploadLandxml(args())).toEqual(refused(null, 'LANDXML_CLIENT_NETWORK', true))
    const noStatus = clientWith(answering(() => ({ status: '200', headers: new Headers() })))
    expect(await noStatus.client.uploadLandxml(args())).toEqual(refused(null, 'LANDXML_CLIENT_RESPONSE_INVALID', false))
    const aborted = new AbortController()
    aborted.abort()
    const early = clientWith(neverAnswers())
    expect(await settleWithin(early.client.uploadLandxml(args({ signal: aborted.signal }))))
      .toEqual(refused(null, 'LANDXML_CLIENT_ABORTED', false))
    const slow = clientWith(neverAnswers(), { timeoutMs: 30 })
    expect(await settleWithin(slow.client.uploadLandxml(args()))).toEqual(refused(null, 'LANDXML_CLIENT_TIMEOUT', true))
    const throwingHeaders = clientWith(vi.fn(), { headers: () => { throw new Error('no session') } })
    expect(await throwingHeaders.client.uploadLandxml(args())).toEqual(refused(null, 'LANDXML_CLIENT_REQUEST_INVALID', false))
    const badHeaders = clientWith(vi.fn(), { headers: () => ({ 'X-Tenant-Id': 7 }) })
    expect(await badHeaders.client.uploadLandxml(args())).toEqual(refused(null, 'LANDXML_CLIENT_REQUEST_INVALID', false))
    const throwingObserver = clientWith(answering(() => textResponse(FIRST_TEXT)), {
      onResponse: () => { throw new Error('observer') },
    })
    expect((await throwingObserver.client.uploadLandxml(args())).ok).toBe(true)
  })

  it('LX13 a body that never ends settles at the deadline', async () => {
    const { client } = clientWith(answering(() => stalledResponse()), { timeoutMs: 30 })
    expect(await settleWithin(client.uploadLandxml(args()))).toEqual(refused(200, 'LANDXML_CLIENT_TIMEOUT', true))
    const refusing = clientWith(answering(() => stalledResponse(409)), { timeoutMs: 30 })
    expect(await settleWithin(refusing.client.uploadLandxml(args()))).toEqual(refused(409, 'LANDXML_CLIENT_TIMEOUT', true))
  })

  it('LX14 a caller abort after the headers settles the call', async () => {
    const controller = new AbortController()
    const fetchImpl = answering(() => {
      setTimeout(() => controller.abort(), 5)
      return stalledResponse()
    })
    const { client } = clientWith(fetchImpl)
    expect(await settleWithin(client.uploadLandxml(args({ signal: controller.signal }))))
      .toEqual(refused(200, 'LANDXML_CLIENT_ABORTED', false))
  })

  it('LX15 the response bound counts bytes, not string length', async () => {
    const declared = clientWith(answering(() => textResponse(FIRST_TEXT, 200, { 'content-length': '65537' })))
    expect(await declared.client.uploadLandxml(args())).toEqual(refused(200, 'LANDXML_CLIENT_RESPONSE_INVALID', false))
    const padded = (spaces) => new TextEncoder().encode(`${' '.repeat(spaces)}${FIRST_TEXT}`)
    const fits = clientWith(answering(() => new Response(padded(65_536 - FIRST_TEXT.length), { status: 200 })))
    expect((await fits.client.uploadLandxml(args())).ok).toBe(true)
    const over = clientWith(answering(() => new Response(padded(65_537 - FIRST_TEXT.length), { status: 200 })))
    expect(await over.client.uploadLandxml(args())).toEqual(refused(200, 'LANDXML_CLIENT_RESPONSE_INVALID', false))
    // The bodiless text() path counts UTF-8 bytes too: a refusal whose message is 33,000 two-byte
    // characters is under 65,536 UTF-16 units but over 65,536 bytes.
    const bodiless = (text, status) => () => ({ status, headers: new Headers(), body: null, text: async () => text })
    const wideEnvelope = (count) => {
      const body = JSON.parse(UNSAFE_TEXT)
      body.error.message = 'é'.repeat(count)
      return JSON.stringify(body)
    }
    expect(wideEnvelope(33_000).length).toBeLessThan(65_536)
    expect(new TextEncoder().encode(wideEnvelope(33_000)).byteLength).toBeGreaterThan(65_536)
    const wideClient = clientWith(answering(bodiless(wideEnvelope(33_000), 400)))
    expect(await wideClient.client.uploadLandxml(args())).toEqual(refused(400, 'LANDXML_CLIENT_RESPONSE_INVALID', false))
    const narrowClient = clientWith(answering(bodiless(wideEnvelope(32_000), 400)))
    expect(await narrowClient.client.uploadLandxml(args())).toEqual(refused(400, 'LANDXML_UNSAFE', false))
    const wideStream = clientWith(answering(() => textResponse(wideEnvelope(33_000), 400)))
    expect(await wideStream.client.uploadLandxml(args())).toEqual(refused(400, 'LANDXML_CLIENT_RESPONSE_INVALID', false))
    const textClient = clientWith(answering(bodiless(FIRST_TEXT, 200)))
    expect((await textClient.client.uploadLandxml(args())).ok).toBe(true)
    const badUtf8 = clientWith(answering(() => new Response(new Uint8Array([0x7b, 0xff, 0x7d]), { status: 200 })))
    expect(await badUtf8.client.uploadLandxml(args())).toEqual(refused(200, 'LANDXML_CLIENT_RESPONSE_INVALID', false))
    const replacementBody = JSON.parse(UNSAFE_TEXT)
    replacementBody.error.message = '\uFFFD'
    const replacementText = JSON.stringify(replacementBody)
    const [prefix, suffix] = replacementText.split('\uFFFD')
    const before = new TextEncoder().encode(prefix)
    const after = new TextEncoder().encode(suffix)
    const invalidBytes = new Uint8Array(before.byteLength + 1 + after.byteLength)
    invalidBytes.set(before)
    invalidBytes[before.byteLength] = 0xff
    invalidBytes.set(after, before.byteLength + 1)
    const invalidMessage = clientWith(answering(() => new Response(oneBytePerPull(invalidBytes), { status: 400 })))
    expect(await invalidMessage.client.uploadLandxml(args())).toEqual(refused(400, 'LANDXML_CLIENT_RESPONSE_INVALID', false))
    const replacedMessage = clientWith(answering(bodiless(replacementText, 400)))
    expect(await replacedMessage.client.uploadLandxml(args())).toEqual(refused(400, 'LANDXML_CLIENT_RESPONSE_INVALID', false))
  })

  it('LX16 a body whose chunks are always ready settles at the deadline', async () => {
    const bytes = new TextEncoder().encode(FIRST_TEXT)
    let index = 0
    const stream = new ReadableStream({
      pull(controller) {
        if (index === 0) {
          const until = performance.now() + 60
          while (performance.now() < until) { /* spin */ }
        }
        if (index < bytes.length) {
          controller.enqueue(bytes.subarray(index, index + 1))
          index += 1
        } else {
          controller.close()
        }
      },
    }, { highWaterMark: 0 })
    const { client } = clientWith(answering(() => new Response(stream, { status: 200 })), { timeoutMs: 30 })
    expect(await settleWithin(client.uploadLandxml(args()), 5000)).toEqual(refused(200, 'LANDXML_CLIENT_TIMEOUT', true))
    expect(index).toBeLessThan(8)
  })

  it('LX17 creation guards', () => {
    const fetchImpl = vi.fn()
    expect(() => createSolarLandxmlClient()).toThrow(TypeError)
    expect(() => createSolarLandxmlClient({ fetchImpl })).toThrow(TypeError)
    expect(() => createSolarLandxmlClient({ fetchImpl, headers: tenantHeaders, apiBase: 'x'.repeat(2049) })).toThrow(TypeError)
    expect(() => createSolarLandxmlClient({ fetchImpl, headers: tenantHeaders, apiBase: 7 })).toThrow(TypeError)
    expect(() => createSolarLandxmlClient({ fetchImpl, headers: tenantHeaders, onResponse: 'x' })).toThrow(TypeError)
    for (const timeoutMs of [0, 600_001, 1.5, '30', null]) {
      expect(() => createSolarLandxmlClient({ fetchImpl, headers: tenantHeaders, timeoutMs })).toThrow(TypeError)
    }
    const client = createSolarLandxmlClient({ fetchImpl, headers: tenantHeaders })
    expect(Object.isFrozen(client)).toBe(true)
    expect(Object.keys(client)).toEqual(['uploadLandxml'])
  })

  it('LX18 the caller abort listener is removed on every settle', async () => {
    const controller = new AbortController()
    const add = vi.spyOn(controller.signal, 'addEventListener')
    const remove = vi.spyOn(controller.signal, 'removeEventListener')
    const outcomes = [
      () => textResponse(FIRST_TEXT),
      () => textResponse(UNSAFE_TEXT, 400),
      () => { throw new TypeError('Failed to fetch') },
    ]
    for (const outcome of outcomes) {
      const { client } = clientWith(vi.fn(async () => outcome()))
      await client.uploadLandxml(args({ signal: controller.signal }))
    }
    const added = add.mock.calls.filter(([type]) => type === 'abort').length
    const removed = remove.mock.calls.filter(([type]) => type === 'abort').length
    expect(added).toBeGreaterThanOrEqual(3)
    expect(removed).toBe(added)
  })

  it('LX19 arguments that throw or are not objects resolve', async () => {
    const fetchImpl = vi.fn()
    const { client } = clientWith(fetchImpl)
    const throwing = { get drawingId() { throw new Error('getter') } }
    const trapped = new Proxy({}, { get() { throw new Error('trap') } })
    for (const options of [null, 7, 'x', throwing, trapped]) {
      await expect(client.uploadLandxml(options)).resolves.toEqual(refused(null, 'LANDXML_CLIENT_REQUEST_INVALID', false))
    }
    await expect(client.uploadLandxml()).resolves.toEqual(refused(null, 'LANDXML_DRAWING_ID_INVALID', false))
    expect(fetchImpl).not.toHaveBeenCalled()
  })

  it('LX20 the deadline starts at the call and covers the last read', async () => {
    const bytes = new TextEncoder().encode(FIRST_TEXT)
    const fetchImpl = answering(() => {
      let first = true
      return new Response(new ReadableStream({
        pull(controller) {
          if (first) {
            first = false
            controller.enqueue(bytes)
          } else {
            const until = performance.now() + 60
            while (performance.now() < until) { /* spin */ }
            controller.close()
          }
        },
      }, { highWaterMark: 0 }), { status: 200 })
    })
    const { client } = clientWith(fetchImpl, { timeoutMs: 30 })
    expect(await settleWithin(client.uploadLandxml(args()), 5000)).toEqual(refused(200, 'LANDXML_CLIENT_TIMEOUT', true))
    const blob = new Blob([new Uint8Array(10)])
    Object.defineProperty(blob, 'size', {
      get() {
        const until = performance.now() + 60
        while (performance.now() < until) { /* spin */ }
        return 10
      },
    })
    const unusedFetch = vi.fn()
    const precheck = clientWith(unusedFetch, { timeoutMs: 30 })
    expect(await settleWithin(precheck.client.uploadLandxml(args({ file: blob })), 5000))
      .toEqual(refused(null, 'LANDXML_CLIENT_TIMEOUT', true))
    expect(unusedFetch).not.toHaveBeenCalled()
  })

  it('LX21 an injected content type in any casing is replaced', async () => {
    const cases = [
      [{ Authorization: 'Bearer t', 'content-type': 'text/plain' }, 'Authorization', 'Bearer t'],
      [{ 'CONTENT-TYPE': 'text/plain', 'X-Tenant-Id': 'acme' }, 'X-Tenant-Id', 'acme'],
    ]
    for (const [headers, otherKey, otherValue] of cases) {
      const fetchImpl = answering(() => textResponse(FIRST_TEXT))
      const { client } = clientWith(fetchImpl, { headers: () => headers })
      expect((await client.uploadLandxml(args())).ok).toBe(true)
      const sent = fetchImpl.mock.calls[0][1].headers
      expect(Object.keys(sent).filter((key) => /^content-type$/i.test(key))).toEqual(['Content-Type'])
      expect(sent['Content-Type']).toBe('application/xml')
      expect(sent[otherKey]).toBe(otherValue)
    }
  })

  it('LX22 a signal that throws while registering leaves no listener', async () => {
    vi.useFakeTimers()
    try {
      const added = []
      const removed = []
      const signal = {
        aborted: false,
        addEventListener(type, fn) { added.push(fn); throw new Error('boom') },
        removeEventListener(type, fn) { removed.push(fn) },
      }
      const fetchImpl = vi.fn()
      const { client } = clientWith(fetchImpl)
      await expect(client.uploadLandxml(args({ signal }))).resolves
        .toEqual(refused(null, 'LANDXML_CLIENT_REQUEST_INVALID', false))
      expect(fetchImpl).not.toHaveBeenCalled()
      expect(added).toHaveLength(1)
      expect(removed).toContain(added[0])
      expect(vi.getTimerCount()).toBe(0)
    } finally {
      vi.useRealTimers()
    }
  })

  it('LX23 a content length with leading zeros is read as its value', async () => {
    const length = new TextEncoder().encode(FIRST_TEXT).byteLength
    const padded = clientWith(answering(() => textResponse(FIRST_TEXT, 200, {
      'Content-Length': '0'.repeat(17) + String(length),
    })))
    expect(await padded.client.uploadLandxml(args())).toEqual({ ok: true, status: 200, value: withoutEnvelope(FIRST) })
    const oversized = clientWith(answering(() => textResponse(FIRST_TEXT, 200, {
      'Content-Length': '0'.repeat(17) + '65537',
    })))
    expect(await oversized.client.uploadLandxml(args())).toEqual(refused(200, 'LANDXML_CLIENT_RESPONSE_INVALID', false))
  })

  it('LX24 a stopped call leaves no timer behind', async () => {
    vi.useFakeTimers()
    try {
      const fetchImpl = vi.fn(() => new Promise(() => {}))
      const { client } = clientWith(fetchImpl, { timeoutMs: 1000 })
      const caller = new AbortController()
      const upload = client.uploadLandxml(args({ signal: caller.signal }))
      await vi.advanceTimersByTimeAsync(0)
      expect(fetchImpl).toHaveBeenCalledTimes(1)
      caller.abort()
      await expect(upload).resolves.toEqual(refused(null, 'LANDXML_CLIENT_ABORTED', false))
      await vi.advanceTimersByTimeAsync(0)
      expect(vi.getTimerCount()).toBe(0)
    } finally {
      vi.useRealTimers()
    }
  })

  it('LX25 an injected header named __proto__ is kept', async () => {
    const fetchImpl = answering(() => textResponse(FIRST_TEXT))
    const { client } = clientWith(fetchImpl, {
      headers: () => {
        const h = Object.create(null)
        for (const [name, value] of [
          ['__proto__', 'retained'],
          ['X-Tenant-Id', 'acme'],
          ['cOnTeNt-TyPe', 'text/plain'],
        ]) {
          Object.defineProperty(h, name, { value, enumerable: true, writable: true, configurable: true })
        }
        return h
      },
    })
    expect((await client.uploadLandxml(args())).ok).toBe(true)
    const init = fetchImpl.mock.calls[0][1]
    expect(Object.getOwnPropertyDescriptor(init.headers, '__proto__')?.value).toBe('retained')
    expect(init.headers['X-Tenant-Id']).toBe('acme')
    expect(Object.keys(init.headers).filter((key) => /^content-type$/i.test(key))).toEqual(['Content-Type'])
    expect(init.headers['Content-Type']).toBe('application/xml')
  })

  it('LX26 a signal whose removeEventListener throws does not change the result', async () => {
    const signal = {
      aborted: false,
      addEventListener() {},
      removeEventListener() { throw new Error('boom') },
    }
    const { client } = clientWith(answering(() => textResponse(FIRST_TEXT)))
    await expect(client.uploadLandxml(args({ signal }))).resolves
      .toEqual({ ok: true, status: 200, value: withoutEnvelope(FIRST) })
  })
})
