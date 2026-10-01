// @vitest-environment node
// sf-w4-landxml-upload-control: the pure model of the LandXML upload control. Every refusal the
// model decides before sending is checked against the real upload client (solarLandxmlClient.js)
// on the same input, and every summary is built from a body the real route answered (planner
// measurement on Forge main; FIRST to SURVEY_FOOT are the client test's own fixtures, SKIPPED,
// FRACTION and TOO_FEW were captured on 3293e8cd with server/tests/test_solar_landxml_route.py's
// client) passed through the client's own validateLandxmlImport.
import { describe, expect, it, vi } from 'vitest'
import {
  LANDXML_DRAWING_UNITS, LANDXML_IMPORT_REASONS, LANDXML_MAX_BYTES,
  createSolarLandxmlClient, landxmlReason, validateLandxmlImport,
} from './solarLandxmlClient.js'
import {
  LANDXML_UPLOAD_CELLS_HINT, LANDXML_UPLOAD_CRS_HINT, LANDXML_UPLOAD_DEFAULT_CELLS, LANDXML_UPLOAD_MAX_DRAFT_CHARS,
  LANDXML_UPLOAD_REASONS, LANDXML_UPLOAD_UNIT_LABELS,
  buildLandxmlUpload, landxmlUploadCancelled, landxmlUploadOutcome, landxmlUploadSentence, landxmlUploadSummary,
  parseLandxmlCells, parseLandxmlCrs,
} from './solarLandxmlUploadModel.js'

const FIRST_TEXT = `{"schema":"leaf.solar-landxml-import.v1","created":true,"drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","source":{"artifact_id":"d6923bdcbbc5c7e02b2cfc486ff981d716adc4b7d05e344c04939bb4eef33cce","media_type":"application/xml","filename":"landxml-source.xml","byte_length":45305,"content_sha256":"9846f488c6780af2185fdf62741865fbc7a75077488449f6741f0aac77f370b5","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/d6923bdcbbc5c7e02b2cfc486ff981d716adc4b7d05e344c04939bb4eef33cce"},"interpretation":{"point_order":"northing-easting-elevation","drawing_x":"easting","drawing_y":"northing","linear_unit":"meter","meters_per_source_unit":1.0,"drawing_units":"m","meters_per_drawing_unit":1.0,"horizontal_scale":1.0,"elevation_scale":1.0,"crs":"none","crs_source":"declared","elevation_datum":"unrecorded"},"points":{"declared":441,"accepted":441,"skipped":0},"grid":{"rows":30,"cols":30,"target_cells":30,"x_min":-100.0,"x_max":100.0,"y_min":-100.0,"y_max":100.0},"head":{"schema":"leaf.solar-physical-head.v1","drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","index":0,"parent":null,"state":{"artifact_id":"56f3892ae8228c968f19b0a7f1a3fb65ae49be60d279382fb69e21ed4a4bcbc8","media_type":"application/json","filename":"physical-state.json","byte_length":18283,"content_sha256":"0de2afeb7ae692538ce33fd011b043438cd65cc426fd1cead1933eb4046d22f0","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/56f3892ae8228c968f19b0a7f1a3fb65ae49be60d279382fb69e21ed4a4bcbc8"}},"error":null,"degraded_mode":false}`
const AGAIN_TEXT = `{"schema":"leaf.solar-landxml-import.v1","created":false,"drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","source":{"artifact_id":"d6923bdcbbc5c7e02b2cfc486ff981d716adc4b7d05e344c04939bb4eef33cce","media_type":"application/xml","filename":"landxml-source.xml","byte_length":45305,"content_sha256":"9846f488c6780af2185fdf62741865fbc7a75077488449f6741f0aac77f370b5","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/d6923bdcbbc5c7e02b2cfc486ff981d716adc4b7d05e344c04939bb4eef33cce"},"interpretation":{"point_order":"northing-easting-elevation","drawing_x":"easting","drawing_y":"northing","linear_unit":"meter","meters_per_source_unit":1.0,"drawing_units":"m","meters_per_drawing_unit":1.0,"horizontal_scale":1.0,"elevation_scale":1.0,"crs":"none","crs_source":"declared","elevation_datum":"unrecorded"},"points":{"declared":441,"accepted":441,"skipped":0},"grid":{"rows":30,"cols":30,"target_cells":30,"x_min":-100.0,"x_max":100.0,"y_min":-100.0,"y_max":100.0},"head":{"schema":"leaf.solar-physical-head.v1","drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","index":0,"parent":null,"state":{"artifact_id":"56f3892ae8228c968f19b0a7f1a3fb65ae49be60d279382fb69e21ed4a4bcbc8","media_type":"application/json","filename":"physical-state.json","byte_length":18283,"content_sha256":"0de2afeb7ae692538ce33fd011b043438cd65cc426fd1cead1933eb4046d22f0","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/56f3892ae8228c968f19b0a7f1a3fb65ae49be60d279382fb69e21ed4a4bcbc8"}},"error":null,"degraded_mode":false}`
const CELLS_10_TEXT = `{"schema":"leaf.solar-landxml-import.v1","created":true,"drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","source":{"artifact_id":"d6923bdcbbc5c7e02b2cfc486ff981d716adc4b7d05e344c04939bb4eef33cce","media_type":"application/xml","filename":"landxml-source.xml","byte_length":45305,"content_sha256":"9846f488c6780af2185fdf62741865fbc7a75077488449f6741f0aac77f370b5","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/d6923bdcbbc5c7e02b2cfc486ff981d716adc4b7d05e344c04939bb4eef33cce"},"interpretation":{"point_order":"northing-easting-elevation","drawing_x":"easting","drawing_y":"northing","linear_unit":"meter","meters_per_source_unit":1.0,"drawing_units":"m","meters_per_drawing_unit":1.0,"horizontal_scale":1.0,"elevation_scale":1.0,"crs":"none","crs_source":"declared","elevation_datum":"unrecorded"},"points":{"declared":441,"accepted":441,"skipped":0},"grid":{"rows":10,"cols":10,"target_cells":10,"x_min":-100.0,"x_max":100.0,"y_min":-100.0,"y_max":100.0},"head":{"schema":"leaf.solar-physical-head.v1","drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","index":1,"parent":"56f3892ae8228c968f19b0a7f1a3fb65ae49be60d279382fb69e21ed4a4bcbc8","state":{"artifact_id":"3a16bacbf6d90a1f6d01345174a40f86f2a07165d85a98c61f53a791334e044f","media_type":"application/json","filename":"physical-state.json","byte_length":2453,"content_sha256":"d8e15891814e9ca10767dffce025e6dc1451f263badf7f323ea7f987cdf28965","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/3a16bacbf6d90a1f6d01345174a40f86f2a07165d85a98c61f53a791334e044f"}},"error":null,"degraded_mode":false}`
const FEET_TEXT = `{"schema":"leaf.solar-landxml-import.v1","created":true,"drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","source":{"artifact_id":"a781e6aa8d03309fe71ccd5ef329f0822e9bf0fee99315dc3d756ec10f16e8c0","media_type":"application/xml","filename":"landxml-source.xml","byte_length":397,"content_sha256":"fe90106cab9c348d58dcaa53c53f658bcd1c9b702459631cca2fc8bba01ef534","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/a781e6aa8d03309fe71ccd5ef329f0822e9bf0fee99315dc3d756ec10f16e8c0"},"interpretation":{"point_order":"northing-easting-elevation","drawing_x":"easting","drawing_y":"northing","linear_unit":"foot","meters_per_source_unit":0.3048,"drawing_units":"ft","meters_per_drawing_unit":0.3048,"horizontal_scale":1.0,"elevation_scale":0.3048,"crs":"none","crs_source":"declared","elevation_datum":"unrecorded"},"points":{"declared":4,"accepted":4,"skipped":0},"grid":{"rows":30,"cols":30,"target_cells":30,"x_min":0.0,"x_max":10.0,"y_min":0.0,"y_max":10.0},"head":{"schema":"leaf.solar-physical-head.v1","drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","index":0,"parent":null,"state":{"artifact_id":"3d96036c26a7c4afc2cd2a61af308cc5111faf8fef6a44efb1280d97e1efae4c","media_type":"application/json","filename":"physical-state.json","byte_length":17120,"content_sha256":"fa51beec8c285e6a75fce557d1cdf3b267b8981e031f950a620167cc36b89bbc","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/3d96036c26a7c4afc2cd2a61af308cc5111faf8fef6a44efb1280d97e1efae4c"}},"error":null,"degraded_mode":false}`
const FILE_CRS_TEXT = `{"schema":"leaf.solar-landxml-import.v1","created":true,"drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","source":{"artifact_id":"e876a4a1e00bf0852d8999784a3c9a5eac1327b3b608e818d9b776ece620c622","media_type":"application/xml","filename":"landxml-source.xml","byte_length":432,"content_sha256":"9ec1f60c03bb7276faff4a874519ea3f3b45c5176184ddce5c43f7d24021be19","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/e876a4a1e00bf0852d8999784a3c9a5eac1327b3b608e818d9b776ece620c622"},"interpretation":{"point_order":"northing-easting-elevation","drawing_x":"easting","drawing_y":"northing","linear_unit":"foot","meters_per_source_unit":0.3048,"drawing_units":"ft","meters_per_drawing_unit":0.3048,"horizontal_scale":1.0,"elevation_scale":0.3048,"crs":"EPSG:2229","crs_source":"file","elevation_datum":"unrecorded"},"points":{"declared":4,"accepted":4,"skipped":0},"grid":{"rows":30,"cols":30,"target_cells":30,"x_min":0.0,"x_max":10.0,"y_min":0.0,"y_max":10.0},"head":{"schema":"leaf.solar-physical-head.v1","drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","index":0,"parent":null,"state":{"artifact_id":"4aa90b00d12ef0c30a72b0560675bf1de57b121a4f540d02ac662381c284e207","media_type":"application/json","filename":"physical-state.json","byte_length":17125,"content_sha256":"c4b95495892522847661a1915d1ffbe40bf6ba85689a2af2a516bc4125cdc901","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/4aa90b00d12ef0c30a72b0560675bf1de57b121a4f540d02ac662381c284e207"}},"error":null,"degraded_mode":false}`
const SURVEY_FOOT_TEXT = `{"schema":"leaf.solar-landxml-import.v1","created":true,"drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","source":{"artifact_id":"559c08ffa5f9d86594cdd420bab939c1dbc88cfeb90fc606e90969992e429ada","media_type":"application/xml","filename":"landxml-source.xml","byte_length":405,"content_sha256":"262715967fb861b3ac43def00f47c151e83b6128ea2cab45c2007233c209ad9c","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/559c08ffa5f9d86594cdd420bab939c1dbc88cfeb90fc606e90969992e429ada"},"interpretation":{"point_order":"northing-easting-elevation","drawing_x":"easting","drawing_y":"northing","linear_unit":"USSurveyFoot","meters_per_source_unit":0.3048006096012192,"drawing_units":"m","meters_per_drawing_unit":1.0,"horizontal_scale":0.3048006096012192,"elevation_scale":0.3048006096012192,"crs":"EPSG:2229","crs_source":"declared","elevation_datum":"unrecorded"},"points":{"declared":4,"accepted":4,"skipped":0},"grid":{"rows":7,"cols":7,"target_cells":7,"x_min":0.0,"x_max":3.048006096012192,"y_min":0.0,"y_max":3.048006096012192},"head":{"schema":"leaf.solar-physical-head.v1","drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","index":0,"parent":null,"state":{"artifact_id":"e25d727e48a25bcef3f5cc5d0b2da69253d666d587749001f08e5b9bc9144eec","media_type":"application/json","filename":"physical-state.json","byte_length":1416,"content_sha256":"09e0d26e0cd343d5c6c00e3a07b641b40202ee0b347d648aa2a8de196476a0cf","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/e25d727e48a25bcef3f5cc5d0b2da69253d666d587749001f08e5b9bc9144eec"}},"error":null,"degraded_mode":false}`
const SKIPPED_TEXT = `{"schema":"leaf.solar-landxml-import.v1","created":true,"drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","source":{"artifact_id":"ed363d6a2d1a86136cc1bf18a518d28f508765aff69fcad6bc004b8586fe57fe","media_type":"application/xml","filename":"landxml-source.xml","byte_length":434,"content_sha256":"7a3986206a8e1427db649b5792db40b6cb17ca381c09914369dd251f57dfdd03","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/ed363d6a2d1a86136cc1bf18a518d28f508765aff69fcad6bc004b8586fe57fe"},"interpretation":{"point_order":"northing-easting-elevation","drawing_x":"easting","drawing_y":"northing","linear_unit":"meter","meters_per_source_unit":1.0,"drawing_units":"m","meters_per_drawing_unit":1.0,"horizontal_scale":1.0,"elevation_scale":1.0,"crs":"none","crs_source":"declared","elevation_datum":"unrecorded"},"points":{"declared":6,"accepted":4,"skipped":2},"grid":{"rows":30,"cols":30,"target_cells":30,"x_min":0.0,"x_max":10.0,"y_min":0.0,"y_max":10.0},"head":{"schema":"leaf.solar-physical-head.v1","drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","index":0,"parent":null,"state":{"artifact_id":"29168ba1921708bcc40c8b4dfae7dba5df0bf1f0bc6c976bbca68fd32c188350","media_type":"application/json","filename":"physical-state.json","byte_length":17364,"content_sha256":"7ca45d101f2179fe7247924194c7878adb6b7d48baff70acc6457558c8dbc686","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/29168ba1921708bcc40c8b4dfae7dba5df0bf1f0bc6c976bbca68fd32c188350"}},"error":null,"degraded_mode":false}`
const FRACTION_TEXT = `{"schema":"leaf.solar-landxml-import.v1","created":true,"drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","source":{"artifact_id":"91b43c875022f70ac0ebcb689ba1149989a562e6d321825803209eea70745c9d","media_type":"application/xml","filename":"landxml-source.xml","byte_length":417,"content_sha256":"21151fa78b9be094f53e94e361c01f9cfda23847658040678ff8cf1a74c71e45","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/91b43c875022f70ac0ebcb689ba1149989a562e6d321825803209eea70745c9d"},"interpretation":{"point_order":"northing-easting-elevation","drawing_x":"easting","drawing_y":"northing","linear_unit":"meter","meters_per_source_unit":1.0,"drawing_units":"m","meters_per_drawing_unit":1.0,"horizontal_scale":1.0,"elevation_scale":1.0,"crs":"none","crs_source":"declared","elevation_datum":"unrecorded"},"points":{"declared":4,"accepted":4,"skipped":0},"grid":{"rows":2,"cols":2,"target_cells":2,"x_min":-12.34567,"x_max":8.0,"y_min":-0.0004,"y_max":100.25},"head":{"schema":"leaf.solar-physical-head.v1","drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","index":0,"parent":null,"state":{"artifact_id":"3bbcd384acd71e5f9edbfe9f23f1cff115e4db98649c07274438eafe03e85c2f","media_type":"application/json","filename":"physical-state.json","byte_length":546,"content_sha256":"dc1fdbe5628c5c09fe6a5043f778ce4c9029a7af5d163a6f01a6cf38055f39d8","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/3bbcd384acd71e5f9edbfe9f23f1cff115e4db98649c07274438eafe03e85c2f"}},"error":null,"degraded_mode":false}`
const UNSAFE_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"BAD_PARAMS","message":"LANDXML_UNSAFE","retryable":false,"retry_class":"after_action","actor":"user","next_action":"Review the inputs, correct them, and submit again.","reason_code":"LANDXML_UNSAFE"},"degraded_mode":false}`

const PROJECT = 'leaf:project:00000000-0000-4000-8000-000000000001'
const blob = (bytes) => new Blob([new Uint8Array(bytes)], { type: 'application/xml' })
const FILE = blob(10)
const base = (overrides = {}) => ({
  drawingId: 'solar', projectId: null, file: FILE, units: 'm', crs: '', cells: '30', ...overrides,
})
const refused = (reason, field) => ({ ok: false, reason, field })
const lines = (summary) => summary.map((line) => [line.key, line.label, line.text])
const withoutEnvelope = (text) => {
  const copy = JSON.parse(text)
  delete copy.error
  delete copy.degraded_mode
  return copy
}
// The value the real client returns for a measured body and the request it answers.
function validated(text, request) {
  const value = validateLandxmlImport(JSON.parse(text), {
    drawingId: 'solar', drawingUnits: 'm', crs: 'none', targetCells: 30, projectId: null, byteLength: 45_305, ...request,
  })
  expect(value).not.toBeNull()
  return value
}
// One real client whose fetch answers the measured LANDXML_UNSAFE refusal.
function realClient() {
  const fetchImpl = vi.fn(async () => new Response(UNSAFE_TEXT, { status: 400, headers: { 'content-type': 'application/json' } }))
  const client = createSolarLandxmlClient({ fetchImpl, apiBase: 'https://studio.test', headers: () => ({ Authorization: 'Bearer t' }) })
  return { client, fetchImpl }
}
// The client call the same drafts stand for: a blank coordinate system is 'none', a draft of
// digits is its number, anything else is passed unchanged for the client to refuse.
function clientArgs(input) {
  const crsText = typeof input.crs === 'string' ? input.crs.trim() : input.crs
  const cellsText = typeof input.cells === 'string' ? input.cells.trim() : input.cells
  return {
    drawingId: input.drawingId,
    projectId: input.projectId,
    file: input.file,
    drawingUnits: input.units,
    crs: crsText === '' ? 'none' : crsText,
    targetCells: typeof cellsText === 'string' && /^[0-9]+$/.test(cellsText) ? Number(cellsText) : cellsText,
  }
}

const ACCEPTED = [
  ['the defaults', base(), 'solar', 'drawing_units=m&crs=none&target_cells=30'],
  ['feet, a padded EPSG code and a padded grid', base({ units: 'ft', crs: ' EPSG:2229\t', cells: ' 7 ' }), 'solar',
    'drawing_units=ft&crs=EPSG%3A2229&target_cells=7'],
  ['the literal none', base({ crs: 'none' }), 'solar', 'drawing_units=m&crs=none&target_cells=30'],
  ['the smallest grid', base({ cells: '2' }), 'solar', 'drawing_units=m&crs=none&target_cells=2'],
  ['the largest grid', base({ cells: '200' }), 'solar', 'drawing_units=m&crs=none&target_cells=200'],
  ['a zero padded grid', base({ cells: '030' }), 'solar', 'drawing_units=m&crs=none&target_cells=30'],
  ['the largest EPSG code', base({ crs: 'EPSG:999999' }), 'solar', 'drawing_units=m&crs=EPSG%3A999999&target_cells=30'],
  ['a 100 character project', base({ projectId: 'p'.repeat(100) }), 'solar',
    `drawing_units=m&crs=none&target_cells=30&project_id=${'p'.repeat(100)}`],
  ['a project of 100 emoji', base({ projectId: '\u{1F600}'.repeat(100) }), 'solar',
    `drawing_units=m&crs=none&target_cells=30&project_id=${encodeURIComponent('\u{1F600}'.repeat(100))}`],
  ['the measured project', base({ projectId: PROJECT }), 'solar',
    `drawing_units=m&crs=none&target_cells=30&project_id=${encodeURIComponent(PROJECT)}`],
  ['a 63 character drawing id', base({ drawingId: 'a'.repeat(63) }), 'a'.repeat(63), 'drawing_units=m&crs=none&target_cells=30'],
  ['a one byte array', base({ file: new Uint8Array(1) }), 'solar', 'drawing_units=m&crs=none&target_cells=30'],
  ['a one byte buffer', base({ file: new ArrayBuffer(1) }), 'solar', 'drawing_units=m&crs=none&target_cells=30'],
  ['a file of exactly 16 MiB', base({ file: blob(LANDXML_MAX_BYTES) }), 'solar', 'drawing_units=m&crs=none&target_cells=30'],
]

// [name, input, model reason, field, the client's code when it differs from the model's reason]
const REFUSED = [
  ['a drawing id with a symbol', base({ drawingId: 'Bad!' }), 'LANDXML_DRAWING_ID_INVALID', null],
  ['a 64 character drawing id', base({ drawingId: 'a'.repeat(64) }), 'LANDXML_DRAWING_ID_INVALID', null],
  ['an empty drawing id', base({ drawingId: '' }), 'LANDXML_DRAWING_ID_INVALID', null],
  ['no drawing id', base({ drawingId: undefined }), 'LANDXML_DRAWING_ID_INVALID', null],
  ['a numeric drawing id', base({ drawingId: 7 }), 'LANDXML_DRAWING_ID_INVALID', null],
  ['an empty project', base({ projectId: '' }), 'LANDXML_PROJECT_ID_INVALID', null],
  ['a 101 character project', base({ projectId: 'p'.repeat(101) }), 'LANDXML_PROJECT_ID_INVALID', null],
  ['a project of 101 emoji', base({ projectId: '\u{1F600}'.repeat(101) }), 'LANDXML_PROJECT_ID_INVALID', null],
  ['a numeric project', base({ projectId: 7 }), 'LANDXML_PROJECT_ID_INVALID', null],
  ['a lone surrogate project', base({ projectId: '\uD800' }), 'LANDXML_PROJECT_ID_INVALID', null],
  ['no units chosen', base({ units: '' }), 'LANDXML_DRAWING_UNITS_INVALID', 'units'],
  ['upper case units', base({ units: 'M' }), 'LANDXML_DRAWING_UNITS_INVALID', 'units'],
  ['inches', base({ units: 'in' }), 'LANDXML_DRAWING_UNITS_INVALID', 'units'],
  ['missing units', base({ units: undefined }), 'LANDXML_DRAWING_UNITS_INVALID', 'units'],
  ['a lower case EPSG code', base({ crs: 'epsg:4326' }), 'LANDXML_CRS_INVALID', 'crs'],
  ['EPSG zero', base({ crs: 'EPSG:0' }), 'LANDXML_CRS_INVALID', 'crs'],
  ['a seven digit EPSG code', base({ crs: 'EPSG:1234567' }), 'LANDXML_CRS_INVALID', 'crs'],
  ['a zero padded EPSG code', base({ crs: 'EPSG:02229' }), 'LANDXML_CRS_INVALID', 'crs'],
  ['None', base({ crs: 'None' }), 'LANDXML_CRS_INVALID', 'crs'],
  ['a missing coordinate system', base({ crs: undefined }), 'LANDXML_CRS_INVALID', 'crs'],
  ['a grid of 1', base({ cells: '1' }), 'LANDXML_TARGET_CELLS_INVALID', 'cells'],
  ['a grid of 201', base({ cells: '201' }), 'LANDXML_TARGET_CELLS_INVALID', 'cells'],
  ['a grid of 0', base({ cells: '0' }), 'LANDXML_TARGET_CELLS_INVALID', 'cells'],
  ['a fractional grid', base({ cells: '2.5' }), 'LANDXML_TARGET_CELLS_INVALID', 'cells'],
  ['an exponent grid', base({ cells: '1e2' }), 'LANDXML_TARGET_CELLS_INVALID', 'cells'],
  ['a signed grid', base({ cells: '+5' }), 'LANDXML_TARGET_CELLS_INVALID', 'cells'],
  ['a negative grid', base({ cells: '-5' }), 'LANDXML_TARGET_CELLS_INVALID', 'cells'],
  ['a word for a grid', base({ cells: 'abc' }), 'LANDXML_TARGET_CELLS_INVALID', 'cells'],
  ['an empty grid', base({ cells: '' }), 'LANDXML_TARGET_CELLS_INVALID', 'cells'],
  ['no file', base({ file: null }), 'file_required', 'file', 'LANDXML_CLIENT_REQUEST_INVALID'],
  ['an undefined file', base({ file: undefined }), 'file_required', 'file', 'LANDXML_CLIENT_REQUEST_INVALID'],
  ['a string for a file', base({ file: '<LandXML/>' }), 'LANDXML_CLIENT_REQUEST_INVALID', 'file'],
  ['an object with a size', base({ file: { size: 5 } }), 'LANDXML_CLIENT_REQUEST_INVALID', 'file'],
  ['an array of bytes', base({ file: [1, 2, 3] }), 'LANDXML_CLIENT_REQUEST_INVALID', 'file'],
  ['an empty blob', base({ file: blob(0) }), 'LANDXML_EMPTY', 'file'],
  ['an empty array', base({ file: new Uint8Array(0) }), 'LANDXML_EMPTY', 'file'],
  ['an empty buffer', base({ file: new ArrayBuffer(0) }), 'LANDXML_EMPTY', 'file'],
  ['a blob one byte over 16 MiB', base({ file: blob(LANDXML_MAX_BYTES + 1) }), 'LANDXML_TOO_LARGE', 'file'],
  ['a buffer one byte over 16 MiB', base({ file: new ArrayBuffer(LANDXML_MAX_BYTES + 1) }), 'LANDXML_TOO_LARGE', 'file'],
  ['units before the file', base({ units: '', file: null }), 'LANDXML_DRAWING_UNITS_INVALID', 'units'],
  ['the coordinate system before the grid', base({ crs: 'x', cells: 'x' }), 'LANDXML_CRS_INVALID', 'crs'],
  ['units before a lone surrogate project', base({ units: '', projectId: '\uD800' }), 'LANDXML_DRAWING_UNITS_INVALID', 'units'],
  ['the file before a lone surrogate project', base({ file: blob(0), projectId: '\uD800' }), 'LANDXML_EMPTY', 'file'],
]

describe('solar LandXML upload model', () => {
  it('UM1 the control reasons, hints and labels', () => {
    expect(Object.isFrozen(LANDXML_UPLOAD_REASONS)).toBe(true)
    expect(LANDXML_UPLOAD_REASONS).toEqual({
      file_required: 'Choose a LandXML file to import',
      checkout_required: 'Take the drawing checkout before importing terrain',
      run_in_progress: 'A run is in progress, so wait for it to finish',
      upload_in_progress: 'The LandXML file is being imported',
      upload_cancelled: 'The import was cancelled here, but the drawing may already hold this terrain',
    })
    for (const [key, sentence] of Object.entries(LANDXML_UPLOAD_REASONS)) {
      expect(Object.hasOwn(LANDXML_IMPORT_REASONS, key)).toBe(false)
      expect(sentence.length).toBeGreaterThanOrEqual(12)
      expect(sentence.length).toBeLessThanOrEqual(120)
      expect(sentence).not.toMatch(/[;–—]|\.$/)
    }
    expect(LANDXML_UPLOAD_CRS_HINT).toBe('Leave blank for none, or enter an EPSG code such as EPSG:2229')
    expect(LANDXML_UPLOAD_CELLS_HINT).toBe('Nodes along the longer side, 2 to 200')
    expect(LANDXML_UPLOAD_DEFAULT_CELLS).toBe('30')
    expect(LANDXML_UPLOAD_MAX_DRAFT_CHARS).toBe(64)
    expect(Object.isFrozen(LANDXML_UPLOAD_UNIT_LABELS)).toBe(true)
    expect(LANDXML_UPLOAD_UNIT_LABELS).toEqual({ m: 'Meters', ft: 'Feet' })
    expect(Object.keys(LANDXML_UPLOAD_UNIT_LABELS)).toEqual([...LANDXML_DRAWING_UNITS])
  })

  it('UM2 a sentence for every reason the control shows', () => {
    for (const [key, sentence] of Object.entries(LANDXML_UPLOAD_REASONS)) expect(landxmlUploadSentence(key)).toBe(sentence)
    for (const [code, sentence] of Object.entries(LANDXML_IMPORT_REASONS)) expect(landxmlUploadSentence(code)).toBe(sentence)
    expect(landxmlUploadSentence('LANDXML_FUTURE_CODE')).toBe('The LandXML import stopped (LANDXML_FUTURE_CODE)')
    for (const value of ['constructor', 'toString', 'hasOwnProperty', 'bad code', null, undefined, 7, {}]) {
      expect(landxmlUploadSentence(value)).toBe(landxmlReason(value))
      expect(landxmlUploadSentence(value)).toBe('The LandXML import stopped')
    }
  })

  it.each([
    ['', 'none'], ['   ', 'none'], ['\t\n', 'none'], ['none', 'none'], [' none ', 'none'],
    ['EPSG:2229', 'EPSG:2229'], [' EPSG:2229\t', 'EPSG:2229'], ['EPSG:1', 'EPSG:1'], ['EPSG:999999', 'EPSG:999999'],
    [' '.repeat(64), 'none'], [`${' '.repeat(55)}EPSG:2229`, 'EPSG:2229'],
    [' '.repeat(65), null], [`${' '.repeat(56)}EPSG:2229`, null],
    ['EPSG:1234567', null], ['EPSG:0', null], ['EPSG:02229', null], ['epsg:2229', null], ['EPSG: 2229', null],
    ['None', null], ['NONE', null], ['2229', null], ['EPSG:2229;', null],
    [null, null], [undefined, null], [4326, null], [['EPSG:2229'], null],
  ])('UM3 coordinate system draft %j reads %j', (text, expected) => {
    expect(parseLandxmlCrs(text)).toBe(expected)
  })

  it.each([
    ['30', 30], ['2', 2], ['200', 200], [' 7 ', 7], ['030', 30], ['0200', 200], [`${'0'.repeat(61)}200`, 200],
    [`${' '.repeat(62)}30`, 30],
    [`${'0'.repeat(62)}200`, null], [`${' '.repeat(63)}30`, null],
    ['1', null], ['201', null], ['0', null], ['000', null], ['2.5', null], ['30.0', null], ['1e2', null], ['+5', null],
    ['-5', null], ['0x1F', null], ['abc', null], ['', null], ['   ', null], ['3 0', null], ['٣٠', null],
    [null, null], [undefined, null], [30, null], [['30'], null],
  ])('UM4 grid size draft %j reads %j', (text, expected) => {
    expect(parseLandxmlCells(text)).toBe(expected)
  })

  it.each(ACCEPTED)('UM5 the model builds and the client sends: %s', async (name, input, drawingId, query) => {
    const built = buildLandxmlUpload(input)
    expect(built.ok).toBe(true)
    expect(Object.keys(built.request)).toEqual(['drawingId', 'file', 'drawingUnits', 'crs', 'targetCells', 'projectId'])
    expect(built.request.file).toBe(input.file)
    expect(built.request).toEqual(clientArgs(input))
    const { client, fetchImpl } = realClient()
    const result = await client.uploadLandxml(built.request)
    expect(result).toEqual({ ok: false, status: 400, code: 'LANDXML_UNSAFE', retryable: false })
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    expect(fetchImpl.mock.calls[0][0]).toBe(`https://studio.test/api/drawings/${drawingId}/imports/landxml?${query}`)
    expect(fetchImpl.mock.calls[0][1].body).toBe(input.file)
  })

  it.each(REFUSED)('UM6 the model refuses as the client does: %s', async (name, input, reason, field, clientCode = reason) => {
    expect(buildLandxmlUpload(input)).toEqual(refused(reason, field))
    const { client, fetchImpl } = realClient()
    const result = await client.uploadLandxml(clientArgs(input))
    expect(result).toEqual({ ok: false, status: null, code: clientCode, retryable: false })
    expect(fetchImpl).not.toHaveBeenCalled()
  })

  it('UM7 a draft past the model bound is refused before the client would read it', () => {
    expect(buildLandxmlUpload(base({ crs: ' '.repeat(65) }))).toEqual(refused('LANDXML_CRS_INVALID', 'crs'))
    expect(buildLandxmlUpload(base({ cells: `${' '.repeat(63)}30` }))).toEqual(refused('LANDXML_TARGET_CELLS_INVALID', 'cells'))
    expect(buildLandxmlUpload(base({ crs: ' '.repeat(64) })).ok).toBe(true)
    expect(buildLandxmlUpload(base({ cells: `${' '.repeat(62)}30` })).ok).toBe(true)
  })

  it('UM8 a malformed input never throws and the client agrees on a throwing size', async () => {
    for (const input of [undefined, null, [], 'solar', 7, true, new Map()]) {
      expect(buildLandxmlUpload(input)).toEqual(refused('LANDXML_CLIENT_REQUEST_INVALID', null))
    }
    const getter = base()
    Object.defineProperty(getter, 'units', { get() { throw new Error('units') }, enumerable: true })
    expect(buildLandxmlUpload(getter)).toEqual(refused('LANDXML_CLIENT_REQUEST_INVALID', null))
    const file = blob(10)
    Object.defineProperty(file, 'size', { get() { throw new Error('size') } })
    expect(buildLandxmlUpload(base({ file }))).toEqual(refused('LANDXML_CLIENT_REQUEST_INVALID', null))
    const { client, fetchImpl } = realClient()
    expect(await client.uploadLandxml(clientArgs(base({ file })))).toEqual(
      { ok: false, status: null, code: 'LANDXML_CLIENT_REQUEST_INVALID', retryable: false })
    expect(fetchImpl).not.toHaveBeenCalled()
    const nullProto = Object.assign(Object.create(null), base())
    expect(buildLandxmlUpload(nullProto).ok).toBe(true)
  })

  it.each([
    ['FIRST', FIRST_TEXT, {}, [
      ['terrain', 'Terrain', 'Stored as terrain change 1 of this drawing'], ['points', 'Survey points', '441 read'],
      ['grid', 'Terrain grid', '30 by 30 nodes'], ['extent', 'Extent', 'X -100 to 100, Y -100 to 100 meters'],
      ['file_units', 'File units', 'Meters'], ['crs', 'Coordinate system', 'None'], ['datum', 'Elevation datum', 'Not recorded'],
    ]],
    ['AGAIN', AGAIN_TEXT, {}, [
      ['terrain', 'Terrain', 'Already the terrain of this drawing, so nothing changed'], ['points', 'Survey points', '441 read'],
      ['grid', 'Terrain grid', '30 by 30 nodes'], ['extent', 'Extent', 'X -100 to 100, Y -100 to 100 meters'],
      ['file_units', 'File units', 'Meters'], ['crs', 'Coordinate system', 'None'], ['datum', 'Elevation datum', 'Not recorded'],
    ]],
    ['CELLS_10', CELLS_10_TEXT, { targetCells: 10 }, [
      ['terrain', 'Terrain', 'Stored as terrain change 2 of this drawing'], ['points', 'Survey points', '441 read'],
      ['grid', 'Terrain grid', '10 by 10 nodes'], ['extent', 'Extent', 'X -100 to 100, Y -100 to 100 meters'],
      ['file_units', 'File units', 'Meters'], ['crs', 'Coordinate system', 'None'], ['datum', 'Elevation datum', 'Not recorded'],
    ]],
    ['FEET', FEET_TEXT, { drawingUnits: 'ft', byteLength: 397 }, [
      ['terrain', 'Terrain', 'Stored as terrain change 1 of this drawing'], ['points', 'Survey points', '4 read'],
      ['grid', 'Terrain grid', '30 by 30 nodes'], ['extent', 'Extent', 'X 0 to 10, Y 0 to 10 feet'],
      ['file_units', 'File units', 'Feet'], ['crs', 'Coordinate system', 'None'], ['datum', 'Elevation datum', 'Not recorded'],
    ]],
    ['FILE_CRS', FILE_CRS_TEXT, { drawingUnits: 'ft', crs: 'EPSG:2229', byteLength: 432 }, [
      ['terrain', 'Terrain', 'Stored as terrain change 1 of this drawing'], ['points', 'Survey points', '4 read'],
      ['grid', 'Terrain grid', '30 by 30 nodes'], ['extent', 'Extent', 'X 0 to 10, Y 0 to 10 feet'],
      ['file_units', 'File units', 'Feet'], ['crs', 'Coordinate system', 'EPSG:2229, named by the file'],
      ['datum', 'Elevation datum', 'Not recorded'],
    ]],
    ['SURVEY_FOOT', SURVEY_FOOT_TEXT, { crs: 'EPSG:2229', targetCells: 7, byteLength: 405 }, [
      ['terrain', 'Terrain', 'Stored as terrain change 1 of this drawing'], ['points', 'Survey points', '4 read'],
      ['grid', 'Terrain grid', '7 by 7 nodes'], ['extent', 'Extent', 'X 0 to 3.048, Y 0 to 3.048 meters'],
      ['file_units', 'File units', 'US survey feet'], ['crs', 'Coordinate system', 'EPSG:2229, as chosen'],
      ['datum', 'Elevation datum', 'Not recorded'],
    ]],
    ['SKIPPED', SKIPPED_TEXT, { byteLength: 434 }, [
      ['terrain', 'Terrain', 'Stored as terrain change 1 of this drawing'], ['points', 'Survey points', '4 of 6 read, 2 skipped'],
      ['grid', 'Terrain grid', '30 by 30 nodes'], ['extent', 'Extent', 'X 0 to 10, Y 0 to 10 meters'],
      ['file_units', 'File units', 'Meters'], ['crs', 'Coordinate system', 'None'], ['datum', 'Elevation datum', 'Not recorded'],
    ]],
    ['FRACTION', FRACTION_TEXT, { targetCells: 2, byteLength: 417 }, [
      ['terrain', 'Terrain', 'Stored as terrain change 1 of this drawing'], ['points', 'Survey points', '4 read'],
      ['grid', 'Terrain grid', '2 by 2 nodes'], ['extent', 'Extent', 'X -12.346 to 8, Y 0 to 100.25 meters'],
      ['file_units', 'File units', 'Meters'], ['crs', 'Coordinate system', 'None'], ['datum', 'Elevation datum', 'Not recorded'],
    ]],
  ])('UM9 the summary of the measured %s answer', (name, text, request, expected) => {
    const value = validated(text, request)
    const summary = landxmlUploadSummary(value)
    expect(Object.isFrozen(summary)).toBe(true)
    expect(lines(summary)).toEqual(expected)
    expect(summary.every((line) => Object.isFrozen(line))).toBe(true)
  })

  it('UM10 an elevation datum and a large extent read as recorded', () => {
    const value = withoutEnvelope(FIRST_TEXT)
    value.interpretation.elevation_datum = 'EPSG:5703'
    value.grid.x_min = -1e10
    value.grid.y_max = 1e10
    const summary = lines(landxmlUploadSummary(value))
    expect(summary[3]).toEqual(['extent', 'Extent', 'X -10000000000 to 100, Y -100 to 10000000000 meters'])
    expect(summary[6]).toEqual(['datum', 'Elevation datum', 'EPSG:5703'])
  })

  const edit = (change) => {
    const value = withoutEnvelope(FIRST_TEXT)
    change(value)
    return value
  }
  it.each([
    ['created is text', (v) => { v.created = 'yes' }],
    ['no interpretation', (v) => { delete v.interpretation }],
    ['points is an array', (v) => { v.points = [441, 441, 0] }],
    ['no grid', (v) => { v.grid = null }],
    ['interpretation an array carrying every field', (v) => { v.interpretation = Object.assign([], v.interpretation) }],
    ['the head is a string', (v) => { v.head = 'head' }],
    ['head index 4096', (v) => { v.head.index = 4096 }],
    ['head index -1', (v) => { v.head.index = -1 }],
    ['head index 1.5', (v) => { v.head.index = 1.5 }],
    ['declared 2', (v) => { v.points.declared = 2; v.points.accepted = 2 }],
    ['declared 250001', (v) => { v.points.declared = 250_001; v.points.skipped = 249_560 }],
    ['accepted over declared', (v) => { v.points.accepted = 442; v.points.skipped = -1 }],
    ['skipped not the difference', (v) => { v.points.skipped = 1 }],
    ['rows 1', (v) => { v.grid.rows = 1 }],
    ['cols 201', (v) => { v.grid.cols = 201 }],
    ['rows text', (v) => { v.grid.rows = '30' }],
    ['x_min infinite', (v) => { v.grid.x_min = -Infinity }],
    ['y_max past 1e10', (v) => { v.grid.y_max = 1e10 + 1 }],
    ['x_max NaN', (v) => { v.grid.x_max = NaN }],
    ['y_min text', (v) => { v.grid.y_min = '-100' }],
    ['an unknown file unit', (v) => { v.interpretation.linear_unit = 'inch' }],
    ['a file unit from the prototype', (v) => { v.interpretation.linear_unit = 'toString' }],
    ['inch drawing units', (v) => { v.interpretation.drawing_units = 'in' }],
    ['drawing units from the prototype', (v) => { v.interpretation.drawing_units = 'constructor' }],
    ['a lower case coordinate system', (v) => { v.interpretation.crs = 'epsg:2229' }],
    ['no coordinate system', (v) => { v.interpretation.crs = null }],
    ['an unknown crs source', (v) => { v.interpretation.crs_source = 'guessed' }],
    ['an elevation datum of none', (v) => { v.interpretation.elevation_datum = 'none' }],
    ['an elevation datum that is not EPSG', (v) => { v.interpretation.elevation_datum = 'NAVD88' }],
    ['a missing elevation datum', (v) => { delete v.interpretation.elevation_datum }],
  ])('UM11 a result with %s has no summary', (name, change) => {
    expect(landxmlUploadSummary(withoutEnvelope(FIRST_TEXT))).not.toBeNull()
    expect(landxmlUploadSummary(edit(change))).toBeNull()
  })

  it('UM12 a value that is not a result has no summary and never throws', () => {
    for (const value of [undefined, null, [], 'FIRST', 7, true]) expect(landxmlUploadSummary(value)).toBeNull()
    const throwing = withoutEnvelope(FIRST_TEXT)
    Object.defineProperty(throwing, 'grid', { get() { throw new Error('grid') }, enumerable: true })
    expect(landxmlUploadSummary(throwing)).toBeNull()
    const nullProto = Object.assign(Object.create(null), withoutEnvelope(FIRST_TEXT))
    expect(landxmlUploadSummary(nullProto)).not.toBeNull()
  })

  it('UM13 the outcome of a success keeps the value and its summary', () => {
    const value = validated(FIRST_TEXT, {})
    const outcome = landxmlUploadOutcome({ ok: true, status: 200, value })
    expect(Object.isFrozen(outcome)).toBe(true)
    expect(outcome.kind).toBe('imported')
    expect(outcome.value).toBe(value)
    expect(outcome.lines).toEqual(landxmlUploadSummary(value))
    expect(Object.keys(outcome)).toEqual(['kind', 'value', 'lines'])
  })

  it.each([
    [{ ok: false, status: 400, code: 'LANDXML_UNSAFE', retryable: false }, 'LANDXML_UNSAFE', false],
    [{ ok: false, status: 409, code: 'PHYSICAL_HEAD_CONFLICT', retryable: true }, 'PHYSICAL_HEAD_CONFLICT', true],
    [{ ok: false, status: null, code: 'LANDXML_CLIENT_TIMEOUT', retryable: true }, 'LANDXML_CLIENT_TIMEOUT', true],
    [{ ok: false, status: 400, code: 'LANDXML_FUTURE_CODE', retryable: 'true' }, 'LANDXML_FUTURE_CODE', false],
    [{ ok: false, status: 403, code: 'FORBIDDEN' }, 'FORBIDDEN', false],
  ])('UM14 a coded refusal %j shows its sentence', (result, code, retryable) => {
    const outcome = landxmlUploadOutcome(result)
    expect(outcome).toEqual({ kind: 'refused', code, retryable, text: landxmlReason(code) })
    expect(Object.isFrozen(outcome)).toBe(true)
  })

  it('UM15 anything else is RESPONSE_INVALID, never a blank success', () => {
    const value = validated(FIRST_TEXT, {})
    const broken = edit((v) => { v.grid.rows = 1 })
    const getter = { ok: true, status: 200 }
    Object.defineProperty(getter, 'value', { get() { throw new Error('value') }, enumerable: true })
    for (const result of [
      undefined, null, [], 'ok', { ok: true }, { ok: true, status: 200 }, { ok: true, status: 201, value },
      { ok: 'true', status: 200, value }, { ok: true, status: 200, value: broken }, { ok: false },
      { ok: false, code: 'bad code' }, { ok: false, code: 'A'.repeat(65) }, { ok: false, code: 7 },
      { ok: 'false', code: 'LANDXML_UNSAFE' }, getter,
    ]) {
      expect(landxmlUploadOutcome(result)).toEqual({
        kind: 'refused', code: 'LANDXML_CLIENT_RESPONSE_INVALID', retryable: false,
        text: 'The server answer could not be read, so the step stopped',
      })
    }
  })

  it('UM16 a cancel from the control says the drawing may already hold the terrain', () => {
    expect(landxmlUploadCancelled()).toEqual({
      kind: 'refused', code: 'upload_cancelled', retryable: false,
      text: 'The import was cancelled here, but the drawing may already hold this terrain',
    })
  })
})
