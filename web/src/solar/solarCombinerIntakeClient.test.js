// @vitest-environment node
// sf-w2-combiners-intake-client: the Studio side of POST /api/drawings/{drawing_id}/imports/combiner-intake.
// Every fixture body below is the exact text the real route answered (planner measurement on Forge
// main 3293e8cd through server/tests/test_solar_combiner_intake_import.py's client, the committed i4
// drawing), and every digest is the canonical sha256 (sorted keys, JSON.stringify numbers) of the
// normalized value.
import { describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
import {
  COMBINER_INTAKE_FALLBACK,
  COMBINER_INTAKE_MAX_BYTES,
  COMBINER_INTAKE_REASONS,
  COMBINER_INTAKE_RESPONSE_MAX_BYTES,
  COMBINER_INTAKE_TIMEOUT_MS,
  combinerIntakeReason,
  createSolarCombinerIntakeClient,
  validateCombinerIntakeImport,
} from './solarCombinerIntakeClient.js'

const FIRST_TEXT = `{"schema_version":"leaf.solar-combiner-intake-import.v1","drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","created":true,"version":2,"parent_version":1,"graph_sha256":"fb2a70ff57244ec29292cbaa9ff1febba2233e670ae1e4e3f0e3b4426d02293b","graph_rev":0,"intake_sha256":"2b771741ec50542aef1ed113f1779005e5b041fc25612b7d945c6348f5ebd705","combiner_intake_sha256":"2f213a4625781c3fd6acbb7ae99f5455500e5d5b3802835188c153879feda7e5","panel_groups_sha256":"9f76cbfe1725256991476b829511a47e0c009fbff815ea05af3b242fb61553e1","bound":{"l2_inverters":8,"strings":173},"panel_groups":11,"outline_vertices":245,"error":null,"degraded_mode":false}`
const AGAIN_TEXT = `{"schema_version":"leaf.solar-combiner-intake-import.v1","drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","created":false,"version":2,"parent_version":1,"graph_sha256":"fb2a70ff57244ec29292cbaa9ff1febba2233e670ae1e4e3f0e3b4426d02293b","graph_rev":0,"intake_sha256":"2b771741ec50542aef1ed113f1779005e5b041fc25612b7d945c6348f5ebd705","combiner_intake_sha256":"2f213a4625781c3fd6acbb7ae99f5455500e5d5b3802835188c153879feda7e5","panel_groups_sha256":"9f76cbfe1725256991476b829511a47e0c009fbff815ea05af3b242fb61553e1","bound":{"l2_inverters":8,"strings":173},"panel_groups":11,"outline_vertices":245,"error":null,"degraded_mode":false}`
const AGAIN_PROJECT_TEXT = `{"schema_version":"leaf.solar-combiner-intake-import.v1","drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","created":false,"version":2,"parent_version":1,"graph_sha256":"fb2a70ff57244ec29292cbaa9ff1febba2233e670ae1e4e3f0e3b4426d02293b","graph_rev":0,"intake_sha256":"2b771741ec50542aef1ed113f1779005e5b041fc25612b7d945c6348f5ebd705","combiner_intake_sha256":"2f213a4625781c3fd6acbb7ae99f5455500e5d5b3802835188c153879feda7e5","panel_groups_sha256":"9f76cbfe1725256991476b829511a47e0c009fbff815ea05af3b242fb61553e1","bound":{"l2_inverters":8,"strings":173},"panel_groups":11,"outline_vertices":245,"error":null,"degraded_mode":false}`
const REVERSED_TEXT = `{"schema_version":"leaf.solar-combiner-intake-import.v1","drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","created":true,"version":3,"parent_version":2,"graph_sha256":"fb2a70ff57244ec29292cbaa9ff1febba2233e670ae1e4e3f0e3b4426d02293b","graph_rev":0,"intake_sha256":"025f0d7a95fd615a4b1732e0f5567e3aa2a9a466fa11c3afd32108dee7e7b2fb","combiner_intake_sha256":"2f213a4625781c3fd6acbb7ae99f5455500e5d5b3802835188c153879feda7e5","panel_groups_sha256":"35d7ece3f55518bcedb3d93cc85697ffa8a013d41c09a83260ab6397143cc692","bound":{"l2_inverters":8,"strings":173},"panel_groups":11,"outline_vertices":245,"error":null,"degraded_mode":false}`
const CHARSET_TEXT = `{"schema_version":"leaf.solar-combiner-intake-import.v1","drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","created":false,"version":3,"parent_version":2,"graph_sha256":"fb2a70ff57244ec29292cbaa9ff1febba2233e670ae1e4e3f0e3b4426d02293b","graph_rev":0,"intake_sha256":"025f0d7a95fd615a4b1732e0f5567e3aa2a9a466fa11c3afd32108dee7e7b2fb","combiner_intake_sha256":"2f213a4625781c3fd6acbb7ae99f5455500e5d5b3802835188c153879feda7e5","panel_groups_sha256":"35d7ece3f55518bcedb3d93cc85697ffa8a013d41c09a83260ab6397143cc692","bound":{"l2_inverters":8,"strings":173},"panel_groups":11,"outline_vertices":245,"error":null,"degraded_mode":false}`
const CARRIED_V1_TEXT = `{"schema_version":"leaf.solar-combiner-intake-import.v1","drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","created":false,"version":1,"parent_version":null,"graph_sha256":"fb2a70ff57244ec29292cbaa9ff1febba2233e670ae1e4e3f0e3b4426d02293b","graph_rev":0,"intake_sha256":"8e3334dd2ec4fa14bb87c73fa7b730d2dc083aa28ee98bc7261f770f61afb96f","combiner_intake_sha256":"2f213a4625781c3fd6acbb7ae99f5455500e5d5b3802835188c153879feda7e5","panel_groups_sha256":"9f76cbfe1725256991476b829511a47e0c009fbff815ea05af3b242fb61553e1","bound":{"l2_inverters":8,"strings":173},"panel_groups":11,"outline_vertices":245,"error":null,"degraded_mode":false}`
const ONE_GROUP_TEXT = `{"schema_version":"leaf.solar-combiner-intake-import.v1","drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","created":true,"version":2,"parent_version":1,"graph_sha256":"fb2a70ff57244ec29292cbaa9ff1febba2233e670ae1e4e3f0e3b4426d02293b","graph_rev":0,"intake_sha256":"65f8462526de1f2f57402cb26c048576869a2144d8b885ceeffbfd9bd500d4df","combiner_intake_sha256":"2f213a4625781c3fd6acbb7ae99f5455500e5d5b3802835188c153879feda7e5","panel_groups_sha256":"740b0ea4fa208e5243d1becc3c093ebb5d73fa55cc718c7ab548063cca7150e8","bound":{"l2_inverters":8,"strings":173},"panel_groups":1,"outline_vertices":31,"error":null,"degraded_mode":false}`
const ONE_GROUP_EMPTY_OUTLINES_TEXT = `{"schema_version":"leaf.solar-combiner-intake-import.v1","drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","created":true,"version":2,"parent_version":1,"graph_sha256":"fb2a70ff57244ec29292cbaa9ff1febba2233e670ae1e4e3f0e3b4426d02293b","graph_rev":0,"intake_sha256":"b8da808cb798af5dfb34beb82719ad787db00ff340850f02d485951be71e66f6","combiner_intake_sha256":"2f213a4625781c3fd6acbb7ae99f5455500e5d5b3802835188c153879feda7e5","panel_groups_sha256":"f64f5d5fbbb93592e4e01efbdeb66efe8620c0725ff7a4a5c5a87b212b7aa9fa","bound":{"l2_inverters":8,"strings":173},"panel_groups":1,"outline_vertices":0,"error":null,"degraded_mode":false}`
const EMPTY_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"BAD_PARAMS","message":"COMBINER_IMPORT_EMPTY","retryable":false,"retry_class":"after_action","actor":"user","next_action":"Review the inputs, correct them, and submit again.","reason_code":"COMBINER_IMPORT_EMPTY"},"degraded_mode":false}`
const NOT_JSON_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"BAD_PARAMS","message":"COMBINER_IMPORT_JSON_INVALID","retryable":false,"retry_class":"after_action","actor":"user","next_action":"Review the inputs, correct them, and submit again.","reason_code":"COMBINER_IMPORT_JSON_INVALID"},"degraded_mode":false}`
const BOM_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"BAD_PARAMS","message":"COMBINER_IMPORT_ENCODING_INVALID","retryable":false,"retry_class":"after_action","actor":"user","next_action":"Review the inputs, correct them, and submit again.","reason_code":"COMBINER_IMPORT_ENCODING_INVALID"},"degraded_mode":false}`
const BODY_INVALID_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"BAD_PARAMS","message":"COMBINER_IMPORT_BODY_INVALID","retryable":false,"retry_class":"after_action","actor":"user","next_action":"Review the inputs, correct them, and submit again.","reason_code":"COMBINER_IMPORT_BODY_INVALID"},"degraded_mode":false}`
const OUTLINES_INVALID_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"BAD_PARAMS","message":"COMBINER_OUTLINES_INVALID","retryable":false,"retry_class":"after_action","actor":"user","next_action":"Review the inputs, correct them, and submit again.","reason_code":"COMBINER_OUTLINES_INVALID"},"degraded_mode":false}`
const INTAKE_INVALID_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"BAD_PARAMS","message":"COMBINER_INTAKE_INVALID","retryable":false,"retry_class":"after_action","actor":"user","next_action":"Review the inputs, correct them, and submit again.","reason_code":"COMBINER_INTAKE_INVALID"},"degraded_mode":false}`
const EXISTING_L1_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"BAD_PARAMS","message":"COMBINER_EXISTING_L1","retryable":false,"retry_class":"after_action","actor":"user","next_action":"Review the inputs, correct them, and submit again.","reason_code":"COMBINER_EXISTING_L1"},"degraded_mode":false}`
const UNITS_MISMATCH_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"BAD_PARAMS","message":"COMBINER_INTAKE_UNITS_MISMATCH","retryable":false,"retry_class":"after_action","actor":"user","next_action":"Review the inputs, correct them, and submit again.","reason_code":"COMBINER_INTAKE_UNITS_MISMATCH"},"degraded_mode":false}`
const MEDIA_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"BAD_PARAMS","message":"COMBINER_IMPORT_MEDIA_TYPE_REFUSED","retryable":false,"retry_class":"after_action","actor":"user","next_action":"Review the inputs, correct them, and submit again.","reason_code":"COMBINER_IMPORT_MEDIA_TYPE_REFUSED"},"degraded_mode":false}`
const DRAWING_ID_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"BAD_PARAMS","message":"COMBINER_IMPORT_DRAWING_ID_INVALID","retryable":false,"retry_class":"after_action","actor":"user","next_action":"Review the inputs, correct them, and submit again.","reason_code":"COMBINER_IMPORT_DRAWING_ID_INVALID"},"degraded_mode":false}`
const PROJECT_LONG_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"BAD_PARAMS","message":"COMBINER_IMPORT_PROJECT_ID_INVALID","retryable":false,"retry_class":"after_action","actor":"user","next_action":"Review the inputs, correct them, and submit again.","reason_code":"COMBINER_IMPORT_PROJECT_ID_INVALID"},"degraded_mode":false}`
const PROJECT_OTHER_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"BAD_PARAMS","message":"COMBINER_IMPORT_PROJECT_MISMATCH","retryable":false,"retry_class":"after_action","actor":"user","next_action":"Review the inputs, correct them, and submit again.","reason_code":"COMBINER_IMPORT_PROJECT_MISMATCH"},"degraded_mode":false}`
const NO_DRAWING_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"BAD_PARAMS","message":"COMBINER_IMPORT_DRAWING_NOT_FOUND","retryable":false,"retry_class":"after_action","actor":"user","next_action":"Review the inputs, correct them, and submit again.","reason_code":"COMBINER_IMPORT_DRAWING_NOT_FOUND"},"degraded_mode":false}`
const CHECKOUT_DENIED_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"BAD_PARAMS","message":"COMBINER_IMPORT_CHECKOUT_DENIED","retryable":false,"retry_class":"after_action","actor":"user","next_action":"Review the inputs, correct them, and submit again.","reason_code":"COMBINER_IMPORT_CHECKOUT_DENIED"},"degraded_mode":false}`
const HEAD_MOVED_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"BAD_PARAMS","message":"COMBINER_IMPORT_HEAD_MOVED","retryable":true,"retry_class":"backoff","actor":"service","next_action":"Wait a short time, then retry the request.","reason_code":"COMBINER_IMPORT_HEAD_MOVED"},"degraded_mode":false}`
const DRAINED_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"INTERNAL","message":"COMBINER_IMPORT_WRITES_DRAINED","retryable":true,"retry_class":"backoff","actor":"service","next_action":"Wait a short time, then retry the request.","reason_code":"COMBINER_IMPORT_WRITES_DRAINED"},"degraded_mode":false}`
const STORE_DOWN_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"INTERNAL","message":"COMBINER_IMPORT_STORE_UNAVAILABLE","retryable":true,"retry_class":"backoff","actor":"service","next_action":"Wait a short time, then retry the request.","reason_code":"COMBINER_IMPORT_STORE_UNAVAILABLE"},"degraded_mode":false}`
const CORRUPT_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"INTERNAL","message":"COMBINER_IMPORT_SOURCE_CORRUPT","retryable":false,"retry_class":"never","actor":"operator","next_action":"Contact support with the displayed error identifier.","reason_code":"COMBINER_IMPORT_SOURCE_CORRUPT"},"degraded_mode":false}`
const INTAKE_TOO_LARGE_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"BAD_PARAMS","message":"COMBINER_IMPORT_INTAKE_TOO_LARGE","retryable":false,"retry_class":"after_action","actor":"user","next_action":"Review the inputs, correct them, and submit again.","reason_code":"COMBINER_IMPORT_INTAKE_TOO_LARGE"},"degraded_mode":false}`
const TOO_LARGE_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"BAD_PARAMS","message":"COMBINER_IMPORT_TOO_LARGE","retryable":false,"retry_class":"after_action","actor":"user","next_action":"Review the inputs, correct them, and submit again.","reason_code":"COMBINER_IMPORT_TOO_LARGE"},"degraded_mode":false}`
const UNLISTED_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"INTERNAL","message":"COMBINER_IMPORT_FAILED","retryable":false,"retry_class":"never","actor":"operator","next_action":"Contact support with the displayed error identifier.","reason_code":"COMBINER_IMPORT_FAILED"},"degraded_mode":false}`
const CHECKOUT_UNAVAILABLE_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"INTERNAL","message":"COMBINER_IMPORT_CHECKOUT_UNAVAILABLE","retryable":true,"retry_class":"backoff","actor":"service","next_action":"Wait a short time, then retry the request.","reason_code":"COMBINER_IMPORT_CHECKOUT_UNAVAILABLE"},"degraded_mode":false}`
const L2_MODE_OFF_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"BAD_PARAMS","message":"COMBINER_L2_MODE_REQUIRED","retryable":false,"retry_class":"after_action","actor":"user","next_action":"Review the inputs, correct them, and submit again.","reason_code":"COMBINER_L2_MODE_REQUIRED"},"degraded_mode":false}`
const GRAPHLESS_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"BAD_PARAMS","message":"COMBINER_IMPORT_GRAPH_REQUIRED","retryable":false,"retry_class":"after_action","actor":"user","next_action":"Review the inputs, correct them, and submit again.","reason_code":"COMBINER_IMPORT_GRAPH_REQUIRED"},"degraded_mode":false}`
const NOT_LOCAL_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"BAD_PARAMS","message":"COMBINER_IMPORT_GRAPH_NOT_LOCAL","retryable":false,"retry_class":"after_action","actor":"user","next_action":"Review the inputs, correct them, and submit again.","reason_code":"COMBINER_IMPORT_GRAPH_NOT_LOCAL"},"degraded_mode":false}`
const WRITE_REFUSED_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"INTERNAL","message":"COMBINER_IMPORT_WRITE_REFUSED","retryable":false,"retry_class":"never","actor":"operator","next_action":"Contact support with the displayed error identifier.","reason_code":"COMBINER_IMPORT_WRITE_REFUSED"},"degraded_mode":false}`
const ENTITLEMENT_TEXT = `{"entitlement_required":true,"required":"upload","tier":"demo","error":{"error_code":"ENTITLEMENT_REQUIRED","message":"the 'demo' plan does not include uploading drawings; upgrade the workspace plan to enable uploads.","retryable":false,"retry_class":"after_action","actor":"workspace_admin","next_action":"Enable this capability for the workspace, then retry."},"degraded_mode":false}`
const POLICY_TEXT = `{"entitlement_required":true,"required":"upload","tier":"demo","error":{"error_code":"INTERNAL","message":"entitlement policy is unavailable; request refused (fail closed).","retryable":true,"retry_class":"backoff","actor":"service","next_action":"Wait a short time, then retry the request."},"degraded_mode":false}`
const GUEST_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"FORBIDDEN","message":"guest sessions are upload-only: upload, upload-status, intake and versions reads; create an account for everything else","retryable":false,"retry_class":"after_action","actor":"workspace_admin","next_action":"Ask a workspace admin to grant the required access."},"degraded_mode":false}`
const UNAUTH_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"UNAUTHENTICATED","message":"missing bearer token (Authorization header)","retryable":false,"retry_class":"after_action","actor":"user","next_action":"Sign in, then repeat the request."},"degraded_mode":false}`
const MEASURED_FIRST_SHA = 'e77123bd3a8c520bf7659dd5c7043cf92ccff85c18f895a589d4a1dde035daaa'
const MEASURED_AGAIN_SHA = '29beb77c87101e0404ed24590b76dcfb989d2bc68acb423961caa6fa9f0eff43'
const MEASURED_AGAIN_PROJECT_SHA = '29beb77c87101e0404ed24590b76dcfb989d2bc68acb423961caa6fa9f0eff43'
const MEASURED_REVERSED_SHA = 'd5a266fdd9c621284449dd3f211c8c705e213f9756120831464c93a8237f6fbf'
const MEASURED_CHARSET_SHA = 'f22b4341bb5cbedcefadada48c1f3d17dd462c9184e88c5763ba023572b58774'
const MEASURED_CARRIED_V1_SHA = 'ffb6044a096ab3a7d32ae71667f9555f434b9f1386c9d56d5d07864b9bdbd1af'
const MEASURED_ONE_GROUP_SHA = 'e9f5e2169335c50d48e718b1fa4072ecb5e4169a1da519379dcf09dfd672c54d'
const MEASURED_ONE_GROUP_EMPTY_OUTLINES_SHA = 'c84942ae9950fb35f937ab8b2a678ad15cffbc395d561189687f24a4170d8679'
const STATUS = Object.freeze({
  EMPTY: 400, NOT_JSON: 400, BOM: 400, BODY_INVALID: 400, OUTLINES_INVALID: 400, INTAKE_INVALID: 400,
  EXISTING_L1: 409, UNITS_MISMATCH: 409, MEDIA: 415, DRAWING_ID: 400, PROJECT_ID: 400, PROJECT_MISMATCH: 409,
  NOT_FOUND: 404, CHECKOUT_DENIED: 403, HEAD_MOVED: 409, DRAINED: 503, STORE_DOWN: 503, CORRUPT: 500,
  INTAKE_TOO_LARGE: 413, TOO_LARGE: 413, UNLISTED: 500, CHECKOUT_UNAVAILABLE: 503, L2_MODE_OFF: 409,
  GRAPHLESS: 409, NOT_LOCAL: 409, WRITE_REFUSED: 500,
  ENTITLEMENT_DENIED: 403, POLICY_UNAVAILABLE: 503, GUEST: 403, UNAUTH: 401,
})
const FIRST = JSON.parse(FIRST_TEXT)
const PROJECT = 'leaf:project:00000000-0000-4000-8000-000000000001'
const SENT_BYTES = 1_667_305
// The 29 keys of COMBINER_INTAKE_IMPORT_REFUSALS in server/routers/drawings.py at 3293e8cd, sorted.
const SERVER_CODES = [
  'COMBINER_EXISTING_L1', 'COMBINER_IMPORT_BODY_INVALID', 'COMBINER_IMPORT_CHECKOUT_DENIED',
  'COMBINER_IMPORT_CHECKOUT_UNAVAILABLE', 'COMBINER_IMPORT_DRAWING_ID_INVALID', 'COMBINER_IMPORT_DRAWING_NOT_FOUND',
  'COMBINER_IMPORT_EMPTY', 'COMBINER_IMPORT_ENCODING_INVALID', 'COMBINER_IMPORT_FAILED',
  'COMBINER_IMPORT_GRAPH_NOT_LOCAL', 'COMBINER_IMPORT_GRAPH_REQUIRED', 'COMBINER_IMPORT_HEAD_MOVED',
  'COMBINER_IMPORT_INTAKE_TOO_LARGE', 'COMBINER_IMPORT_JSON_INVALID', 'COMBINER_IMPORT_MEDIA_TYPE_REFUSED',
  'COMBINER_IMPORT_PROJECT_ID_INVALID', 'COMBINER_IMPORT_PROJECT_MISMATCH', 'COMBINER_IMPORT_SOURCE_CORRUPT',
  'COMBINER_IMPORT_STORE_UNAVAILABLE', 'COMBINER_IMPORT_TOO_LARGE', 'COMBINER_IMPORT_WRITES_DRAINED',
  'COMBINER_IMPORT_WRITE_REFUSED', 'COMBINER_INTAKE_CONTEXT_MISMATCH', 'COMBINER_INTAKE_INVALID',
  'COMBINER_INTAKE_L2_MISMATCH', 'COMBINER_INTAKE_STRING_MISMATCH', 'COMBINER_INTAKE_UNITS_MISMATCH',
  'COMBINER_L2_MODE_REQUIRED', 'COMBINER_OUTLINES_INVALID',
]
const CLIENT_AND_SESSION_CODES = [
  'COMBINER_CLIENT_ABORTED', 'COMBINER_CLIENT_NETWORK', 'COMBINER_CLIENT_REQUEST_INVALID',
  'COMBINER_CLIENT_RESPONSE_INVALID', 'COMBINER_CLIENT_TIMEOUT', 'ENTITLEMENT_POLICY_UNAVAILABLE',
  'ENTITLEMENT_REQUIRED', 'FORBIDDEN', 'UNAUTHENTICATED',
]
const RESULT_KEY_ORDER = [
  'schema_version', 'drawing_id', 'project_id', 'created', 'version', 'parent_version', 'graph_sha256',
  'graph_rev', 'intake_sha256', 'combiner_intake_sha256', 'panel_groups_sha256', 'bound', 'panel_groups',
  'outline_vertices',
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
  const client = createSolarCombinerIntakeClient({
    fetchImpl, apiBase: 'https://studio.test', headers: tenantHeaders, onResponse, ...extra,
  })
  return { client, onResponse }
}
const refused = (status, code, retryable) => ({ ok: false, status, code, retryable })
const jsonBlob = (bytes = SENT_BYTES) => new Blob([new Uint8Array(bytes)], { type: 'application/json' })
const args = (overrides = {}) => ({ drawingId: 'solar', file: jsonBlob(), ...overrides })
const URL_FIRST = 'https://studio.test/api/drawings/solar/imports/combiner-intake'
const URL_PROJECT = `${URL_FIRST}?project_id=leaf%3Aproject%3A00000000-0000-4000-8000-000000000001`
const EMOJI = '\u{1F600}'
function readServer(relative) {
  const buffer = readFileSync(new URL(`../../../server/${relative}`, import.meta.url))
  expect(buffer.length).toBeLessThanOrEqual(1024 * 1024)
  return buffer.toString('utf8')
}
const escapeRegExp = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
// A line that starts with `line`, then only spaces or a comment.
const hasLine = (text, line) => new RegExp(`^${escapeRegExp(line)}[ \\t]*(#.*)?\\r?$`, 'm').test(text)
// Every "CODE": (status, ErrorCode.X, bool) row of the route's COMBINER_INTAKE_IMPORT_REFUSALS literal.
function routeMap() {
  const text = readServer('routers/drawings.py')
  const start = text.indexOf('COMBINER_INTAKE_IMPORT_REFUSALS = {')
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
const envelopeFor = (code, retryable, base = BODY_INVALID_TEXT) => {
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
const spin = (ms) => {
  const until = performance.now() + ms
  while (performance.now() < until) { /* a synchronous delay no timer can interrupt */ }
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
const bodiless = (text, status) => () => ({ status, headers: new Headers(), body: null, text: async () => text })

describe('solar combiner intake client', () => {
  it('CI1 constants and the reason map', () => {
    expect(COMBINER_INTAKE_MAX_BYTES).toBe(16_777_216)
    expect(COMBINER_INTAKE_RESPONSE_MAX_BYTES).toBe(65_536)
    expect(COMBINER_INTAKE_TIMEOUT_MS).toBe(120_000)
    expect(COMBINER_INTAKE_FALLBACK).toBe('The combiner intake import stopped')
    expect(Object.isFrozen(COMBINER_INTAKE_REASONS)).toBe(true)
    for (const list of [SERVER_CODES, CLIENT_AND_SESSION_CODES]) expect(list).toEqual([...list].sort())
    expect(SERVER_CODES).toHaveLength(29)
    const allCodes = [...SERVER_CODES, ...CLIENT_AND_SESSION_CODES].sort()
    expect(allCodes).toHaveLength(38)
    expect(Object.keys(COMBINER_INTAKE_REASONS).sort()).toEqual(allCodes)
    for (const sentence of Object.values(COMBINER_INTAKE_REASONS)) {
      expect(typeof sentence).toBe('string')
      expect(sentence.length).toBeGreaterThanOrEqual(12)
      expect(sentence.length).toBeLessThanOrEqual(120)
      expect(sentence).not.toMatch(/[;\u2013\u2014]/)
      expect(sentence.endsWith('.')).toBe(false)
    }
  })

  it('CI2 route codes follow the server refusal map (reads drawings.py as text)', () => {
    const rows = routeMap()
    expect([...rows.keys()].sort()).toEqual(SERVER_CODES)
    const routeKeys = Object.keys(COMBINER_INTAKE_REASONS).filter((code) => !CLIENT_AND_SESSION_CODES.includes(code))
    expect(routeKeys.sort()).toEqual([...rows.keys()].sort())
    const drawings = readServer('routers/drawings.py')
    expect(hasLine(drawings, 'COMBINER_INTAKE_MEDIA_TYPES = frozenset({"application/json"})')).toBe(true)
    expect(drawings).toContain('@router.post("/api/drawings/{drawing_id}/imports/combiner-intake")')
    expect(hasLine(drawings, '    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", drawing_id):')).toBe(true)
  })

  it('CI3 limits and shapes follow the server constants (reads server files as text)', () => {
    const intake = readServer('solar_combiner_intake_import.py')
    for (const line of [
      'RESULT_SCHEMA = "leaf.solar-combiner-intake-import.v1"', 'MAX_IMPORT_BYTES = 16_777_216',
      'MAX_PROJECT_ID_CHARS = 100', 'MAX_GROUPS = 10_000',
    ]) {
      expect(hasLine(intake, line)).toBe(true)
    }
    const combiner = readServer('solar_inverter_combiner.py')
    for (const line of ['MAX_STRINGS = 20000', 'MAX_L2 = 1000']) expect(hasLine(combiner, line)).toBe(true)
    expect(hasLine(readServer('solar_inverter_cabling.py'), 'MAX_OUTLINE_VERTICES_TOTAL = 1_000_000')).toBe(true)
    const schema = JSON.parse(readServer('../contract/solar-design-graph.v1.schema.json'))
    expect(schema.properties.rev).toEqual({ type: 'integer', minimum: 0, maximum: 1000000 })
  })

  it('CI4 combinerIntakeReason gives a sentence or a bounded fallback', () => {
    expect(combinerIntakeReason('COMBINER_EXISTING_L1')).toBe('This drawing already has combiners, so the intake was not saved')
    expect(combinerIntakeReason('FORBIDDEN')).toBe('A guest session cannot import a combiner intake, so sign in to an account')
    expect(combinerIntakeReason('COMBINER_FUTURE_CODE')).toBe('The combiner intake import stopped (COMBINER_FUTURE_CODE)')
    for (const value of ['bad code', 'A'.repeat(65), null, 7, 'constructor', 'toString', 'hasOwnProperty', 'X\n']) {
      expect(combinerIntakeReason(value)).toBe('The combiner intake import stopped')
    }
  })

  it('CI5 import of the committed i4 intake', async () => {
    const fetchImpl = answering(() => textResponse(FIRST_TEXT))
    const { client, onResponse } = clientWith(fetchImpl)
    const file = jsonBlob()
    const result = await client.importCombinerIntake({ drawingId: 'solar', file })
    expect(result).toEqual({ ok: true, status: 200, value: withoutEnvelope(FIRST) })
    expect(Object.keys(result.value)).toEqual(RESULT_KEY_ORDER)
    expect(canonicalSha(result.value)).toBe(MEASURED_FIRST_SHA)
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    const [url, init] = fetchImpl.mock.calls[0]
    expect(url).toBe(URL_FIRST)
    expect(init.method).toBe('POST')
    expect(init.body).toBe(file)
    expect(init.headers).toEqual({
      'X-Tenant-Id': 'fixture-tenant', Authorization: 'Bearer t', 'Content-Type': 'application/json',
    })
    expect(onResponse).toHaveBeenCalledTimes(1)
    expect(onResponse.mock.calls[0][1]).toBe(URL_FIRST)
    expect(onResponse.mock.calls[0][2]).toBe('Bearer t')
    for (const bytes of [new Uint8Array(7), new ArrayBuffer(7)]) {
      const sent = answering(() => textResponse(FIRST_TEXT))
      const other = clientWith(sent)
      expect((await other.client.importCombinerIntake(args({ file: bytes }))).ok).toBe(true)
      expect(sent.mock.calls[0][1].body).toBe(bytes)
    }
    const body = JSON.parse(FIRST_TEXT)
    const value = validateCombinerIntakeImport(body, { drawingId: 'solar', projectId: null })
    expect(value).toEqual(withoutEnvelope(FIRST))
    expect(value).not.toBe(body)
    expect(value.bound).not.toBe(body.bound)
    value.bound.strings = 1
    expect(body.bound.strings).toBe(173)
    expect(validateCombinerIntakeImport(body, { drawingId: 'solar' })).toEqual(withoutEnvelope(FIRST))
  })

  it('CI6 every measured success reads back in its own request', async () => {
    const cases = [
      [AGAIN_TEXT, {}, URL_FIRST, MEASURED_AGAIN_SHA, [false, 2, 1]],
      [AGAIN_PROJECT_TEXT, { projectId: PROJECT }, URL_PROJECT, MEASURED_AGAIN_PROJECT_SHA, [false, 2, 1]],
      [REVERSED_TEXT, {}, URL_FIRST, MEASURED_REVERSED_SHA, [true, 3, 2]],
      [CHARSET_TEXT, {}, URL_FIRST, MEASURED_CHARSET_SHA, [false, 3, 2]],
      [CARRIED_V1_TEXT, {}, URL_FIRST, MEASURED_CARRIED_V1_SHA, [false, 1, null]],
      [ONE_GROUP_TEXT, {}, URL_FIRST, MEASURED_ONE_GROUP_SHA, [true, 2, 1]],
      [ONE_GROUP_EMPTY_OUTLINES_TEXT, {}, URL_FIRST, MEASURED_ONE_GROUP_EMPTY_OUTLINES_SHA, [true, 2, 1]],
    ]
    for (const [text, overrides, url, sha, [created, version, parent]] of cases) {
      const fetchImpl = answering(() => textResponse(text))
      const { client } = clientWith(fetchImpl)
      const result = await client.importCombinerIntake(args(overrides))
      expect(result).toEqual({ ok: true, status: 200, value: withoutEnvelope(JSON.parse(text)) })
      expect(canonicalSha(result.value)).toBe(sha)
      expect([result.value.created, result.value.version, result.value.parent_version]).toEqual([created, version, parent])
      expect(fetchImpl.mock.calls[0][0]).toBe(url)
    }
    const empty = JSON.parse(ONE_GROUP_EMPTY_OUTLINES_TEXT)
    expect([empty.panel_groups, empty.outline_vertices]).toEqual([1, 0])
  })

  it('CI7 prechecks send nothing', async () => {
    const cases = [
      [{ drawingId: 'Bad!' }, 'COMBINER_IMPORT_DRAWING_ID_INVALID'],
      [{ drawingId: 'x'.repeat(64) }, 'COMBINER_IMPORT_DRAWING_ID_INVALID'],
      [{ drawingId: undefined }, 'COMBINER_IMPORT_DRAWING_ID_INVALID'],
      [{ drawingId: 'solar\n' }, 'COMBINER_IMPORT_DRAWING_ID_INVALID'],
      [{ drawingId: '-solar' }, 'COMBINER_IMPORT_DRAWING_ID_INVALID'],
      [{ projectId: '' }, 'COMBINER_IMPORT_PROJECT_ID_INVALID'],
      [{ projectId: 'p'.repeat(101) }, 'COMBINER_IMPORT_PROJECT_ID_INVALID'],
      [{ projectId: EMOJI.repeat(101) }, 'COMBINER_IMPORT_PROJECT_ID_INVALID'],
      [{ projectId: 'p'.repeat(201) }, 'COMBINER_IMPORT_PROJECT_ID_INVALID'],
      [{ projectId: 7 }, 'COMBINER_IMPORT_PROJECT_ID_INVALID'],
      [{ projectId: '\ud800' }, 'COMBINER_IMPORT_PROJECT_ID_INVALID'],
      [{ file: undefined }, 'COMBINER_CLIENT_REQUEST_INVALID'],
      [{ file: '{}' }, 'COMBINER_CLIENT_REQUEST_INVALID'],
      [{ file: new Float64Array(1) }, 'COMBINER_CLIENT_REQUEST_INVALID'],
      [{ file: { size: 7 } }, 'COMBINER_CLIENT_REQUEST_INVALID'],
      [{ file: jsonBlob(0) }, 'COMBINER_IMPORT_EMPTY'],
      [{ file: new Uint8Array(0) }, 'COMBINER_IMPORT_EMPTY'],
      [{ file: new ArrayBuffer(16_777_217) }, 'COMBINER_IMPORT_TOO_LARGE'],
      [{ file: new Uint8Array(16_777_217) }, 'COMBINER_IMPORT_TOO_LARGE'],
      [{ signal: {} }, 'COMBINER_CLIENT_REQUEST_INVALID'],
      [{ signal: { aborted: 'yes', addEventListener() {}, removeEventListener() {} } }, 'COMBINER_CLIENT_REQUEST_INVALID'],
      [{ signal: { aborted: false, addEventListener() {} } }, 'COMBINER_CLIENT_REQUEST_INVALID'],
    ]
    for (const [overrides, code] of cases) {
      const fetchImpl = vi.fn()
      const { client } = clientWith(fetchImpl)
      expect(await client.importCombinerIntake(args(overrides))).toEqual(refused(null, code, false))
      expect(fetchImpl).not.toHaveBeenCalled()
    }
    const fetchImpl = vi.fn()
    const { client } = clientWith(fetchImpl)
    expect(await client.importCombinerIntake()).toEqual(refused(null, 'COMBINER_IMPORT_DRAWING_ID_INVALID', false))
    expect(fetchImpl).not.toHaveBeenCalled()
  })

  it('CI8 bounds at the edge are sent', async () => {
    const cases = [
      [{ projectId: 'p'.repeat(100) }, `${URL_FIRST}?project_id=${'p'.repeat(100)}`],
      [{ projectId: EMOJI.repeat(100) }, `${URL_FIRST}?project_id=${'%F0%9F%98%80'.repeat(100)}`],
      [{ projectId: null }, URL_FIRST],
      [{ projectId: undefined }, URL_FIRST],
      [{ file: new Uint8Array(16_777_216) }, URL_FIRST],
      [{ file: new Uint8Array(1) }, URL_FIRST],
      [{ drawingId: 'a'.repeat(63) }, `https://studio.test/api/drawings/${'a'.repeat(63)}/imports/combiner-intake`],
      [{ drawingId: '0_-' }, 'https://studio.test/api/drawings/0_-/imports/combiner-intake'],
    ]
    for (const [overrides, url] of cases) {
      const fetchImpl = answering(() => textResponse(BODY_INVALID_TEXT, 400))
      const { client } = clientWith(fetchImpl)
      expect(await client.importCombinerIntake(args(overrides))).toEqual(refused(400, 'COMBINER_IMPORT_BODY_INVALID', false))
      expect(fetchImpl).toHaveBeenCalledTimes(1)
      expect(fetchImpl.mock.calls[0][0]).toBe(url)
    }
  })

  it('CI9 refusals carry the server code and its retry flag', async () => {
    const measured = [
      [EMPTY_TEXT, STATUS.EMPTY, 'COMBINER_IMPORT_EMPTY', false],
      [NOT_JSON_TEXT, STATUS.NOT_JSON, 'COMBINER_IMPORT_JSON_INVALID', false],
      [BOM_TEXT, STATUS.BOM, 'COMBINER_IMPORT_ENCODING_INVALID', false],
      [BODY_INVALID_TEXT, STATUS.BODY_INVALID, 'COMBINER_IMPORT_BODY_INVALID', false],
      [OUTLINES_INVALID_TEXT, STATUS.OUTLINES_INVALID, 'COMBINER_OUTLINES_INVALID', false],
      [INTAKE_INVALID_TEXT, STATUS.INTAKE_INVALID, 'COMBINER_INTAKE_INVALID', false],
      [EXISTING_L1_TEXT, STATUS.EXISTING_L1, 'COMBINER_EXISTING_L1', false],
      [UNITS_MISMATCH_TEXT, STATUS.UNITS_MISMATCH, 'COMBINER_INTAKE_UNITS_MISMATCH', false],
      [MEDIA_TEXT, STATUS.MEDIA, 'COMBINER_IMPORT_MEDIA_TYPE_REFUSED', false],
      [DRAWING_ID_TEXT, STATUS.DRAWING_ID, 'COMBINER_IMPORT_DRAWING_ID_INVALID', false],
      [PROJECT_LONG_TEXT, STATUS.PROJECT_ID, 'COMBINER_IMPORT_PROJECT_ID_INVALID', false],
      [PROJECT_OTHER_TEXT, STATUS.PROJECT_MISMATCH, 'COMBINER_IMPORT_PROJECT_MISMATCH', false],
      [NO_DRAWING_TEXT, STATUS.NOT_FOUND, 'COMBINER_IMPORT_DRAWING_NOT_FOUND', false],
      [CHECKOUT_DENIED_TEXT, STATUS.CHECKOUT_DENIED, 'COMBINER_IMPORT_CHECKOUT_DENIED', false],
      [HEAD_MOVED_TEXT, STATUS.HEAD_MOVED, 'COMBINER_IMPORT_HEAD_MOVED', true],
      [DRAINED_TEXT, STATUS.DRAINED, 'COMBINER_IMPORT_WRITES_DRAINED', true],
      [STORE_DOWN_TEXT, STATUS.STORE_DOWN, 'COMBINER_IMPORT_STORE_UNAVAILABLE', true],
      [CORRUPT_TEXT, STATUS.CORRUPT, 'COMBINER_IMPORT_SOURCE_CORRUPT', false],
      [INTAKE_TOO_LARGE_TEXT, STATUS.INTAKE_TOO_LARGE, 'COMBINER_IMPORT_INTAKE_TOO_LARGE', false],
      [TOO_LARGE_TEXT, STATUS.TOO_LARGE, 'COMBINER_IMPORT_TOO_LARGE', false],
      [UNLISTED_TEXT, STATUS.UNLISTED, 'COMBINER_IMPORT_FAILED', false],
      [CHECKOUT_UNAVAILABLE_TEXT, STATUS.CHECKOUT_UNAVAILABLE, 'COMBINER_IMPORT_CHECKOUT_UNAVAILABLE', true],
      [L2_MODE_OFF_TEXT, STATUS.L2_MODE_OFF, 'COMBINER_L2_MODE_REQUIRED', false],
      [GRAPHLESS_TEXT, STATUS.GRAPHLESS, 'COMBINER_IMPORT_GRAPH_REQUIRED', false],
      [NOT_LOCAL_TEXT, STATUS.NOT_LOCAL, 'COMBINER_IMPORT_GRAPH_NOT_LOCAL', false],
      [WRITE_REFUSED_TEXT, STATUS.WRITE_REFUSED, 'COMBINER_IMPORT_WRITE_REFUSED', false],
    ]
    for (const [text, status, code, retryable] of measured) {
      const { client } = clientWith(answering(() => textResponse(text, status)))
      expect(await client.importCombinerIntake(args())).toEqual(refused(status, code, retryable))
    }
    const rows = routeMap()
    expect(rows.size).toBe(29)
    for (const [code, row] of rows) {
      const { client } = clientWith(answering(() => jsonResponse(envelopeFor(code, row.retryable), row.status)))
      expect(await client.importCombinerIntake(args())).toEqual(refused(row.status, code, row.retryable))
    }
    const { client } = clientWith(answering(() => jsonResponse(envelopeFor('COMBINER_FUTURE_CODE', false), 400)))
    expect(await client.importCombinerIntake(args())).toEqual(refused(400, 'COMBINER_FUTURE_CODE', false))
  })

  it('CI10 success bodies that break the shape or the request are refused', async () => {
    const edited = (edit, text = FIRST_TEXT) => {
      const body = JSON.parse(text)
      edit(body)
      return body
    }
    const SHAS = ['graph_sha256', 'intake_sha256', 'combiner_intake_sha256', 'panel_groups_sha256']
    const edits = [
      (b) => { b.extra = 1 },
      (b) => { delete b.bound },
      (b) => { delete b.outline_vertices },
      (b) => { b.schema_version = 'leaf.solar-combiner-intake-import.v2' },
      (b) => { b.created = 'yes' },
      (b) => { b.created = 1 },
      (b) => { b.drawing_id = 'other' },
      (b) => { b.project_id = null },
      (b) => { b.project_id = '' },
      (b) => { b.project_id = 'p'.repeat(101) },
      (b) => { b.project_id = EMOJI.repeat(101) },
      (b) => { b.error = { reason_code: 'X' } },
      (b) => { b.degraded_mode = 'no' },
      (b) => { b.version = 0 },
      (b) => { b.version = 2.5 },
      (b) => { b.version = '2' },
      (b) => { b.version = 2 ** 53 },
      (b) => { b.parent_version = 2 },
      (b) => { b.parent_version = 3 },
      (b) => { b.parent_version = 0 },
      (b) => { b.parent_version = null },
      (b) => { b.parent_version = '1' },
      (b) => { delete b.parent_version },
      (b) => { b.graph_rev = -1 },
      (b) => { b.graph_rev = 1_000_001 },
      (b) => { b.graph_rev = 0.5 },
      (b) => { b.graph_rev = null },
      ...SHAS.flatMap((key) => [
        (b) => { b[key] = b[key].toUpperCase() },
        (b) => { b[key] = b[key].slice(1) },
        (b) => { b[key] = `${b[key]}0` },
        (b) => { b[key] = null },
      ]),
      (b) => { b.bound = null },
      (b) => { b.bound = [8, 173] },
      (b) => { b.bound.extra = 0 },
      (b) => { delete b.bound.strings },
      (b) => { b.bound.l2_inverters = -1 },
      (b) => { b.bound.l2_inverters = 1001 },
      (b) => { b.bound.l2_inverters = '8' },
      (b) => { b.bound.strings = -1 },
      (b) => { b.bound.strings = 20_001 },
      (b) => { b.bound.strings = 1.5 },
      (b) => { b.panel_groups = 0 },
      (b) => { b.panel_groups = 10_001 },
      (b) => { b.panel_groups = [] },
      (b) => { b.outline_vertices = -1 },
      (b) => { b.outline_vertices = 1_000_001 },
      (b) => { b.outline_vertices = '245' },
    ]
    for (const edit of edits) {
      const { client } = clientWith(answering(() => jsonResponse(edited(edit))))
      expect(await client.importCombinerIntake(args())).toEqual(refused(200, 'COMBINER_CLIENT_RESPONSE_INVALID', false))
    }
    // An unchanged import at version 1 names no parent, so a parent of 1 there is older than nothing.
    const carried = (edit) => clientWith(answering(() => jsonResponse(edited(edit, CARRIED_V1_TEXT))))
    expect(await carried((b) => { b.parent_version = 1 }).client.importCombinerIntake(args()))
      .toEqual(refused(200, 'COMBINER_CLIENT_RESPONSE_INVALID', false))
    expect(await carried((b) => { b.created = true }).client.importCombinerIntake(args()))
      .toEqual(refused(200, 'COMBINER_CLIENT_RESPONSE_INVALID', false))
    // The same bounds accept their edge values.
    const accepted = [
      (b) => { b.graph_rev = 1_000_000 },
      (b) => { b.bound.l2_inverters = 1000; b.bound.strings = 20_000 },
      (b) => { b.bound.l2_inverters = 0; b.bound.strings = 0 },
      (b) => { b.panel_groups = 10_000; b.outline_vertices = 1_000_000 },
      (b) => { b.panel_groups = 1; b.outline_vertices = 0 },
      (b) => { b.version = Number.MAX_SAFE_INTEGER; b.parent_version = Number.MAX_SAFE_INTEGER - 1 },
      (b) => { b.project_id = EMOJI.repeat(100) },
      (b) => { delete b.error; delete b.degraded_mode },
    ]
    for (const edit of accepted) {
      const body = edited(edit)
      const { client } = clientWith(answering(() => jsonResponse(body)))
      expect(await client.importCombinerIntake(args())).toEqual({ ok: true, status: 200, value: withoutEnvelope(body) })
    }
    for (const text of ['[]', 'null', '"ok"', 'not json', '']) {
      const { client } = clientWith(answering(() => textResponse(text)))
      expect(await client.importCombinerIntake(args())).toEqual(refused(200, 'COMBINER_CLIENT_RESPONSE_INVALID', false))
    }
    const { client } = clientWith(answering(() => textResponse(FIRST_TEXT)))
    expect(await client.importCombinerIntake(args({ projectId: 'leaf:project:other' })))
      .toEqual(refused(200, 'COMBINER_CLIENT_RESPONSE_INVALID', false))
    const otherDrawing = clientWith(answering(() => textResponse(FIRST_TEXT)))
    expect(await otherDrawing.client.importCombinerIntake(args({ drawingId: 'other' })))
      .toEqual(refused(200, 'COMBINER_CLIENT_RESPONSE_INVALID', false))
    const created = clientWith(answering(() => textResponse(FIRST_TEXT, 201)))
    expect(await created.client.importCombinerIntake(args())).toEqual(refused(201, 'COMBINER_CLIENT_RESPONSE_INVALID', false))
    expect(validateCombinerIntakeImport(FIRST, null)).toBe(null)
    expect(validateCombinerIntakeImport(FIRST, { drawingId: 'Bad!' })).toBe(null)
    // A request with an id the route refuses is refused even when the body repeats that id.
    expect(validateCombinerIntakeImport({ ...FIRST, drawing_id: 'Bad!' }, { drawingId: 'Bad!' })).toBe(null)
    expect(validateCombinerIntakeImport(FIRST, { drawingId: 'solar', projectId: '' })).toBe(null)
    expect(validateCombinerIntakeImport(FIRST, { drawingId: 'solar', projectId: PROJECT })).toEqual(withoutEnvelope(FIRST))
  })

  it('CI11 session, plan and guest refusals', async () => {
    const cases = [
      [UNAUTH_TEXT, STATUS.UNAUTH, refused(401, 'UNAUTHENTICATED', false)],
      [ENTITLEMENT_TEXT, STATUS.ENTITLEMENT_DENIED, refused(403, 'ENTITLEMENT_REQUIRED', false)],
      [POLICY_TEXT, STATUS.POLICY_UNAVAILABLE, refused(503, 'ENTITLEMENT_POLICY_UNAVAILABLE', true)],
      [GUEST_TEXT, STATUS.GUEST, refused(403, 'FORBIDDEN', false)],
      [CHECKOUT_DENIED_TEXT, 403, refused(403, 'COMBINER_IMPORT_CHECKOUT_DENIED', false)],
      [ENTITLEMENT_TEXT, 400, refused(400, 'COMBINER_CLIENT_RESPONSE_INVALID', false)],
      [GUEST_TEXT, 400, refused(400, 'COMBINER_CLIENT_RESPONSE_INVALID', false)],
      [GUEST_TEXT, 503, refused(503, 'COMBINER_CLIENT_RESPONSE_INVALID', true)],
      ['<html>Bad gateway</html>', 502, refused(502, 'COMBINER_CLIENT_RESPONSE_INVALID', true)],
      ['{"error":{"reason_code":"bad code"}}', 400, refused(400, 'COMBINER_CLIENT_RESPONSE_INVALID', false)],
    ]
    for (const [text, status, expected] of cases) {
      const { client } = clientWith(answering(() => textResponse(text, status)))
      expect(await client.importCombinerIntake(args())).toEqual(expected)
    }
    const guest = JSON.parse(GUEST_TEXT)
    guest.error.retryable = true
    const retryFlag = clientWith(answering(() => jsonResponse(guest, 403)))
    expect(await retryFlag.client.importCombinerIntake(args())).toEqual(refused(403, 'FORBIDDEN', false))
    // A 403 that carries a well-formed reason code keeps it, even with the guest gate's error code.
    guest.error.reason_code = 'COMBINER_IMPORT_CHECKOUT_DENIED'
    guest.error.retryable = false
    const coded = clientWith(answering(() => jsonResponse(guest, 403)))
    expect(await coded.client.importCombinerIntake(args())).toEqual(refused(403, 'COMBINER_IMPORT_CHECKOUT_DENIED', false))
    expect(GUEST_TEXT).toHaveLength(424)
  })

  it('CI12 transport failures resolve and never reject', async () => {
    const network = clientWith(vi.fn(async () => { throw new TypeError('Failed to fetch') }))
    expect(await network.client.importCombinerIntake(args())).toEqual(refused(null, 'COMBINER_CLIENT_NETWORK', true))
    const noStatus = clientWith(answering(() => ({ status: '200', headers: new Headers() })))
    expect(await noStatus.client.importCombinerIntake(args())).toEqual(refused(null, 'COMBINER_CLIENT_RESPONSE_INVALID', false))
    const aborted = new AbortController()
    aborted.abort()
    const early = clientWith(neverAnswers())
    expect(await settleWithin(early.client.importCombinerIntake(args({ signal: aborted.signal }))))
      .toEqual(refused(null, 'COMBINER_CLIENT_ABORTED', false))
    const slow = clientWith(neverAnswers(), { timeoutMs: 30 })
    expect(await settleWithin(slow.client.importCombinerIntake(args()))).toEqual(refused(null, 'COMBINER_CLIENT_TIMEOUT', true))
    const throwingHeaders = clientWith(vi.fn(), { headers: () => { throw new Error('no session') } })
    expect(await throwingHeaders.client.importCombinerIntake(args())).toEqual(refused(null, 'COMBINER_CLIENT_REQUEST_INVALID', false))
    const badHeaders = clientWith(vi.fn(), { headers: () => ({ 'X-Tenant-Id': 7 }) })
    expect(await badHeaders.client.importCombinerIntake(args())).toEqual(refused(null, 'COMBINER_CLIENT_REQUEST_INVALID', false))
    const throwingObserver = clientWith(answering(() => textResponse(FIRST_TEXT)), {
      onResponse: () => { throw new Error('observer') },
    })
    expect((await throwingObserver.client.importCombinerIntake(args())).ok).toBe(true)
  })

  it('CI13 a body that never ends settles at the deadline', async () => {
    const { client } = clientWith(answering(() => stalledResponse()), { timeoutMs: 30 })
    expect(await settleWithin(client.importCombinerIntake(args()))).toEqual(refused(200, 'COMBINER_CLIENT_TIMEOUT', true))
    const refusing = clientWith(answering(() => stalledResponse(409)), { timeoutMs: 30 })
    expect(await settleWithin(refusing.client.importCombinerIntake(args()))).toEqual(refused(409, 'COMBINER_CLIENT_TIMEOUT', true))
  })

  it('CI14 a caller abort after the headers settles the call', async () => {
    const controller = new AbortController()
    const fetchImpl = answering(() => {
      setTimeout(() => controller.abort(), 5)
      return stalledResponse()
    })
    const { client } = clientWith(fetchImpl)
    expect(await settleWithin(client.importCombinerIntake(args({ signal: controller.signal }))))
      .toEqual(refused(200, 'COMBINER_CLIENT_ABORTED', false))
  })

  it('CI15 the response bound counts bytes, not string length', async () => {
    const declared = clientWith(answering(() => textResponse(FIRST_TEXT, 200, { 'content-length': '65537' })))
    expect(await declared.client.importCombinerIntake(args())).toEqual(refused(200, 'COMBINER_CLIENT_RESPONSE_INVALID', false))
    const padded = (spaces) => new TextEncoder().encode(`${' '.repeat(spaces)}${FIRST_TEXT}`)
    const fits = clientWith(answering(() => new Response(padded(65_536 - FIRST_TEXT.length), { status: 200 })))
    expect((await fits.client.importCombinerIntake(args())).ok).toBe(true)
    const over = clientWith(answering(() => new Response(padded(65_537 - FIRST_TEXT.length), { status: 200 })))
    expect(await over.client.importCombinerIntake(args())).toEqual(refused(200, 'COMBINER_CLIENT_RESPONSE_INVALID', false))
    // The bodiless text() path counts UTF-8 bytes too: a refusal whose message is 33,000 two-byte
    // characters is under 65,536 UTF-16 units but over 65,536 bytes.
    const wideEnvelope = (count) => {
      const body = JSON.parse(BODY_INVALID_TEXT)
      body.error.message = '\u00e9'.repeat(count)
      return JSON.stringify(body)
    }
    expect(wideEnvelope(33_000).length).toBeLessThan(65_536)
    expect(new TextEncoder().encode(wideEnvelope(33_000)).byteLength).toBeGreaterThan(65_536)
    const wideClient = clientWith(answering(bodiless(wideEnvelope(33_000), 400)))
    expect(await wideClient.client.importCombinerIntake(args())).toEqual(refused(400, 'COMBINER_CLIENT_RESPONSE_INVALID', false))
    const narrowClient = clientWith(answering(bodiless(wideEnvelope(32_000), 400)))
    expect(await narrowClient.client.importCombinerIntake(args())).toEqual(refused(400, 'COMBINER_IMPORT_BODY_INVALID', false))
    const wideStream = clientWith(answering(() => textResponse(wideEnvelope(33_000), 400)))
    expect(await wideStream.client.importCombinerIntake(args())).toEqual(refused(400, 'COMBINER_CLIENT_RESPONSE_INVALID', false))
    const textClient = clientWith(answering(bodiless(FIRST_TEXT, 200)))
    expect((await textClient.client.importCombinerIntake(args())).ok).toBe(true)
    const badUtf8 = clientWith(answering(() => new Response(new Uint8Array([0x7b, 0xff, 0x7d]), { status: 200 })))
    expect(await badUtf8.client.importCombinerIntake(args())).toEqual(refused(200, 'COMBINER_CLIENT_RESPONSE_INVALID', false))
    // The invalid byte sits inside an otherwise valid JSON string, so only strict decoding refuses it.
    const replacementBody = JSON.parse(BODY_INVALID_TEXT)
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
    expect(await invalidMessage.client.importCombinerIntake(args())).toEqual(refused(400, 'COMBINER_CLIENT_RESPONSE_INVALID', false))
    const replacedMessage = clientWith(answering(bodiless(replacementText, 400)))
    expect(await replacedMessage.client.importCombinerIntake(args())).toEqual(refused(400, 'COMBINER_CLIENT_RESPONSE_INVALID', false))
    const literalStreamed = clientWith(answering(() => textResponse(replacementText, 400)))
    expect(await literalStreamed.client.importCombinerIntake(args())).toEqual(refused(400, 'COMBINER_IMPORT_BODY_INVALID', false))
  })

  it('CI16 a body whose chunks are always ready settles at the deadline, before the byte cap', async () => {
    // The stream never ends, so only the check before each read can stop it inside the budget.
    let index = 0
    const space = new Uint8Array([0x20])
    const stream = new ReadableStream({
      pull(controller) {
        if (index === 0) spin(60)
        index += 1
        controller.enqueue(space.slice())
      },
    }, { highWaterMark: 0 })
    const { client } = clientWith(answering(() => new Response(stream, { status: 200 })), { timeoutMs: 30 })
    expect(await settleWithin(client.importCombinerIntake(args()), 5000)).toEqual(refused(200, 'COMBINER_CLIENT_TIMEOUT', true))
    expect(index).toBeLessThan(8)
  })

  it('CI17 creation guards', () => {
    const fetchImpl = vi.fn()
    expect(() => createSolarCombinerIntakeClient()).toThrow(TypeError)
    expect(() => createSolarCombinerIntakeClient({ fetchImpl })).toThrow(TypeError)
    expect(() => createSolarCombinerIntakeClient({ headers: tenantHeaders })).toThrow(TypeError)
    expect(() => createSolarCombinerIntakeClient({ fetchImpl, headers: tenantHeaders, apiBase: 'x'.repeat(2049) })).toThrow(TypeError)
    expect(() => createSolarCombinerIntakeClient({ fetchImpl, headers: tenantHeaders, apiBase: 7 })).toThrow(TypeError)
    expect(() => createSolarCombinerIntakeClient({ fetchImpl, headers: tenantHeaders, onResponse: 'x' })).toThrow(TypeError)
    for (const timeoutMs of [0, 600_001, 1.5, '30', null]) {
      expect(() => createSolarCombinerIntakeClient({ fetchImpl, headers: tenantHeaders, timeoutMs })).toThrow(TypeError)
    }
    for (const timeoutMs of [1, 600_000]) {
      expect(() => createSolarCombinerIntakeClient({ fetchImpl, headers: tenantHeaders, timeoutMs })).not.toThrow()
    }
    expect(() => createSolarCombinerIntakeClient({ fetchImpl, headers: tenantHeaders, apiBase: 'x'.repeat(2048) })).not.toThrow()
    const client = createSolarCombinerIntakeClient({ fetchImpl, headers: tenantHeaders })
    expect(Object.isFrozen(client)).toBe(true)
    expect(Object.keys(client)).toEqual(['importCombinerIntake'])
  })

  it('CI18 the caller abort listener is removed on every settle', async () => {
    const controller = new AbortController()
    const add = vi.spyOn(controller.signal, 'addEventListener')
    const remove = vi.spyOn(controller.signal, 'removeEventListener')
    const outcomes = [
      () => textResponse(FIRST_TEXT),
      () => textResponse(BODY_INVALID_TEXT, 400),
      () => { throw new TypeError('Failed to fetch') },
    ]
    for (const outcome of outcomes) {
      const { client } = clientWith(vi.fn(async () => outcome()))
      await client.importCombinerIntake(args({ signal: controller.signal }))
    }
    const added = add.mock.calls.filter(([type]) => type === 'abort').length
    const removed = remove.mock.calls.filter(([type]) => type === 'abort').length
    expect(added).toBeGreaterThanOrEqual(3)
    expect(removed).toBe(added)
  })

  it('CI19 arguments that throw or are not objects resolve', async () => {
    const fetchImpl = vi.fn()
    const { client } = clientWith(fetchImpl)
    const throwing = { get drawingId() { throw new Error('getter') } }
    const trapped = new Proxy({}, { get() { throw new Error('trap') } })
    const sizeThrows = new Blob(['{}'])
    Object.defineProperty(sizeThrows, 'size', { get() { throw new Error('size') } })
    for (const options of [null, 7, 'x', true, throwing, trapped, args({ file: sizeThrows })]) {
      await expect(client.importCombinerIntake(options)).resolves
        .toEqual(refused(null, 'COMBINER_CLIENT_REQUEST_INVALID', false))
    }
    await expect(client.importCombinerIntake()).resolves.toEqual(refused(null, 'COMBINER_IMPORT_DRAWING_ID_INVALID', false))
    await expect(client.importCombinerIntake({})).resolves.toEqual(refused(null, 'COMBINER_IMPORT_DRAWING_ID_INVALID', false))
    expect(fetchImpl).not.toHaveBeenCalled()
  })

  it('CI20 the deadline starts at the call and covers the last read', async () => {
    const bytes = new TextEncoder().encode(FIRST_TEXT)
    const fetchImpl = answering(() => {
      let first = true
      return new Response(new ReadableStream({
        pull(controller) {
          if (first) {
            first = false
            controller.enqueue(bytes)
          } else {
            spin(60)
            controller.close()
          }
        },
      }, { highWaterMark: 0 }), { status: 200 })
    })
    const { client } = clientWith(fetchImpl, { timeoutMs: 30 })
    expect(await settleWithin(client.importCombinerIntake(args()), 5000)).toEqual(refused(200, 'COMBINER_CLIENT_TIMEOUT', true))
    // A refusal whose last read ends after the budget is a timeout too: only the read that ends the body can see it.
    const refusalBytes = new TextEncoder().encode(HEAD_MOVED_TEXT)
    const lateRefusal = clientWith(answering(() => {
      let first = true
      return new Response(new ReadableStream({
        pull(controller) {
          if (first) {
            first = false
            controller.enqueue(refusalBytes)
          } else {
            spin(60)
            controller.close()
          }
        },
      }, { highWaterMark: 0 }), { status: 409 })
    }), { timeoutMs: 30 })
    expect(await settleWithin(lateRefusal.client.importCombinerIntake(args()), 5000))
      .toEqual(refused(409, 'COMBINER_CLIENT_TIMEOUT', true))
    const blob = new Blob([new Uint8Array(10)])
    Object.defineProperty(blob, 'size', {
      get() {
        spin(60)
        return 10
      },
    })
    const unusedFetch = vi.fn()
    const precheck = clientWith(unusedFetch, { timeoutMs: 30 })
    expect(await settleWithin(precheck.client.importCombinerIntake(args({ file: blob })), 5000))
      .toEqual(refused(null, 'COMBINER_CLIENT_TIMEOUT', true))
    const slowOptions = { file: jsonBlob(), get drawingId() { spin(60); return 'solar' } }
    expect(await settleWithin(precheck.client.importCombinerIntake(slowOptions), 5000))
      .toEqual(refused(null, 'COMBINER_CLIENT_TIMEOUT', true))
    expect(unusedFetch).not.toHaveBeenCalled()
  })

  it('CI21 an injected content type in any casing is replaced', async () => {
    const cases = [
      [{ Authorization: 'Bearer t', 'content-type': 'text/plain' }, 'Authorization', 'Bearer t'],
      [{ 'CONTENT-TYPE': 'text/plain', 'X-Tenant-Id': 'acme' }, 'X-Tenant-Id', 'acme'],
      [{ 'Content-Type': 'application/xml', 'X-Tenant-Id': 'acme' }, 'X-Tenant-Id', 'acme'],
    ]
    for (const [headers, otherKey, otherValue] of cases) {
      const fetchImpl = answering(() => textResponse(FIRST_TEXT))
      const { client } = clientWith(fetchImpl, { headers: () => headers })
      expect((await client.importCombinerIntake(args())).ok).toBe(true)
      const sent = fetchImpl.mock.calls[0][1].headers
      expect(Object.keys(sent).filter((key) => /^content-type$/i.test(key))).toEqual(['Content-Type'])
      expect(sent['Content-Type']).toBe('application/json')
      expect(new Headers(sent).get('content-type')).toBe('application/json')
      expect(sent[otherKey]).toBe(otherValue)
    }
  })

  it('CI22 a signal that throws while registering leaves no listener and no timer', async () => {
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
      await expect(client.importCombinerIntake(args({ signal }))).resolves
        .toEqual(refused(null, 'COMBINER_CLIENT_REQUEST_INVALID', false))
      expect(fetchImpl).not.toHaveBeenCalled()
      expect(added).toHaveLength(1)
      expect(removed).toContain(added[0])
      expect(vi.getTimerCount()).toBe(0)
      const both = {
        aborted: false,
        addEventListener() { throw new Error('add') },
        removeEventListener() { throw new Error('remove') },
      }
      await expect(client.importCombinerIntake(args({ signal: both }))).resolves
        .toEqual(refused(null, 'COMBINER_CLIENT_REQUEST_INVALID', false))
      expect(vi.getTimerCount()).toBe(0)
    } finally {
      vi.useRealTimers()
    }
  })

  it('CI23 a content length with leading zeros is read as its value', async () => {
    const length = new TextEncoder().encode(FIRST_TEXT).byteLength
    expect(length).toBe(666)
    for (const value of ['0'.repeat(17) + String(length), ` ${'0'.repeat(17)}${length} `, '0'.repeat(17)]) {
      const padded = clientWith(answering(() => {
        const response = textResponse(FIRST_TEXT)
        const headers = new Headers({ 'content-type': 'application/json', 'content-length': value })
        Object.defineProperty(response, 'headers', { value: headers })
        return response
      }))
      expect(await padded.client.importCombinerIntake(args())).toEqual({ ok: true, status: 200, value: withoutEnvelope(FIRST) })
    }
    for (const value of ['0'.repeat(17) + '65537', '1'.repeat(17)]) {
      const oversized = clientWith(answering(() => textResponse(FIRST_TEXT, 200, { 'Content-Length': value })))
      expect(await oversized.client.importCombinerIntake(args())).toEqual(refused(200, 'COMBINER_CLIENT_RESPONSE_INVALID', false))
    }
  })

  it('CI24 a stopped call leaves no timer behind', async () => {
    vi.useFakeTimers()
    try {
      const fetchImpl = vi.fn(() => new Promise(() => {}))
      const { client } = clientWith(fetchImpl, { timeoutMs: 1000 })
      const caller = new AbortController()
      const pending = client.importCombinerIntake(args({ signal: caller.signal }))
      await vi.advanceTimersByTimeAsync(0)
      expect(fetchImpl).toHaveBeenCalledTimes(1)
      caller.abort()
      await expect(pending).resolves.toEqual(refused(null, 'COMBINER_CLIENT_ABORTED', false))
      await vi.advanceTimersByTimeAsync(0)
      expect(vi.getTimerCount()).toBe(0)
      const late = clientWith(vi.fn(() => new Promise(() => {})), { timeoutMs: 30 })
      const timed = late.client.importCombinerIntake(args())
      await vi.advanceTimersByTimeAsync(30)
      await expect(timed).resolves.toEqual(refused(null, 'COMBINER_CLIENT_TIMEOUT', true))
      await vi.advanceTimersByTimeAsync(0)
      expect(vi.getTimerCount()).toBe(0)
    } finally {
      vi.useRealTimers()
    }
  })

  it('CI25 an injected header named __proto__ is kept', async () => {
    const fetchImpl = answering(() => textResponse(FIRST_TEXT))
    const { client, onResponse } = clientWith(fetchImpl, {
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
    expect((await client.importCombinerIntake(args())).ok).toBe(true)
    const init = fetchImpl.mock.calls[0][1]
    expect(Object.getOwnPropertyDescriptor(init.headers, '__proto__')?.value).toBe('retained')
    expect(Object.keys(init.headers)).toEqual(['__proto__', 'X-Tenant-Id', 'Content-Type'])
    expect(init.headers['Content-Type']).toBe('application/json')
    expect(onResponse.mock.calls[0][2]).toBe(undefined)
    const parsed = answering(() => textResponse(FIRST_TEXT))
    const fromJson = clientWith(parsed, { headers: () => JSON.parse('{"__proto__":"retained","Authorization":"Bearer t"}') })
    expect((await fromJson.client.importCombinerIntake(args())).ok).toBe(true)
    expect(Object.keys(parsed.mock.calls[0][1].headers)).toEqual(['__proto__', 'Authorization', 'Content-Type'])
    expect(fromJson.onResponse.mock.calls[0][2]).toBe('Bearer t')
  })

  it('CI26 a signal whose removeEventListener throws does not change the result', async () => {
    const signal = {
      aborted: false,
      addEventListener() {},
      removeEventListener() { throw new Error('boom') },
    }
    const { client } = clientWith(answering(() => textResponse(FIRST_TEXT)))
    await expect(client.importCombinerIntake(args({ signal }))).resolves
      .toEqual({ ok: true, status: 200, value: withoutEnvelope(FIRST) })
    const refusing = clientWith(answering(() => textResponse(HEAD_MOVED_TEXT, 409)))
    await expect(refusing.client.importCombinerIntake(args({ signal }))).resolves
      .toEqual(refused(409, 'COMBINER_IMPORT_HEAD_MOVED', true))
  })

  it('CI27 a success never leaves a call whose budget has elapsed', async () => {
    // Every read ends inside the budget. The chunk's length turns slow once the body has ended, so the
    // time is spent assembling the chunks, after the last read. `afterEnd` runs once, inside that work.
    const slowAssembly = (text, status, afterEnd) => answering(() => {
      const chunk = new TextEncoder().encode(text)
      const length = chunk.byteLength
      let ended = false
      let ran = false
      Object.defineProperty(chunk, 'byteLength', {
        get() {
          if (ended && !ran) {
            ran = true
            afterEnd()
          }
          return length
        },
      })
      let sent = false
      const reader = {
        async read() {
          if (!sent) {
            sent = true
            return { done: false, value: chunk }
          }
          ended = true
          return { done: true, value: undefined }
        },
        async cancel() {},
      }
      return { status, headers: new Headers({ 'content-type': 'application/json' }), body: { getReader: () => reader } }
    })
    const FAST = { timeoutMs: 30 }
    const late = clientWith(slowAssembly(FIRST_TEXT, 200, () => spin(60)), FAST)
    expect(await settleWithin(late.client.importCombinerIntake(args()), 5000))
      .toEqual(refused(200, 'COMBINER_CLIENT_TIMEOUT', true))
    // A caller abort that lands in the same place, with the whole default budget left.
    const caller = new AbortController()
    const aborted = clientWith(slowAssembly(FIRST_TEXT, 200, () => caller.abort()))
    expect(await settleWithin(aborted.client.importCombinerIntake(args({ signal: caller.signal })), 5000))
      .toEqual(refused(200, 'COMBINER_CLIENT_ABORTED', false))
    // A refusal the server decided is returned unchanged, late or not.
    const refusal = clientWith(slowAssembly(HEAD_MOVED_TEXT, 409, () => spin(60)), FAST)
    expect(await settleWithin(refusal.client.importCombinerIntake(args()), 5000))
      .toEqual(refused(409, 'COMBINER_IMPORT_HEAD_MOVED', true))
    const unreadable = clientWith(slowAssembly('{', 200, () => spin(60)), FAST)
    expect(await settleWithin(unreadable.client.importCombinerIntake(args()), 5000))
      .toEqual(refused(200, 'COMBINER_CLIENT_RESPONSE_INVALID', false))
    // Positive control: the same answer through the same reader with nothing slow succeeds inside the same budget.
    const plain = clientWith(slowAssembly(FIRST_TEXT, 200, () => {}), FAST)
    expect(await plain.client.importCombinerIntake(args())).toEqual({ ok: true, status: 200, value: withoutEnvelope(FIRST) })
  })

  // A real caller signal whose cleanup runs a callback before or after the real removal.
  const wrappedCleanupSignal = (before, after = () => {}) => {
    const caller = new AbortController()
    const { signal } = caller
    const remove = signal.removeEventListener.bind(signal)
    signal.removeEventListener = (...listenerArgs) => {
      before(caller)
      remove(...listenerArgs)
      after(caller)
    }
    return signal
  }

  it('CI28 the caller signal cleanup is inside the deadline', async () => {
    const { client } = clientWith(answering(() => textResponse(FIRST_TEXT)), { timeoutMs: 30 })
    const signal = wrappedCleanupSignal(() => spin(60))
    expect(await settleWithin(client.importCombinerIntake(args({ signal })), 5000))
      .toEqual(refused(200, 'COMBINER_CLIENT_TIMEOUT', true))
    const plain = wrappedCleanupSignal(() => {})
    expect(await settleWithin(client.importCombinerIntake(args({ signal: plain })), 5000))
      .toEqual({ ok: true, status: 200, value: withoutEnvelope(FIRST) })
  })

  it('CI29 a caller abort made during cleanup counts', async () => {
    const { client } = clientWith(answering(() => textResponse(FIRST_TEXT)), { timeoutMs: 30 })
    for (const signal of [
      wrappedCleanupSignal((caller) => caller.abort()),
      wrappedCleanupSignal(() => {}, (caller) => caller.abort()),
    ]) {
      expect(await settleWithin(client.importCombinerIntake(args({ signal })), 5000))
        .toEqual(refused(200, 'COMBINER_CLIENT_ABORTED', false))
    }
    const plain = wrappedCleanupSignal(() => {})
    expect(await settleWithin(client.importCombinerIntake(args({ signal: plain })), 5000))
      .toEqual({ ok: true, status: 200, value: withoutEnvelope(FIRST) })
  })
})
