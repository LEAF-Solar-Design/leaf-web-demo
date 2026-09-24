// Pure request builders and one assertion helper for the E05 managed-stack row
// (local/solar-local-graph-commit.spec.mjs). Every request field and its exact
// shape is copied from the in-process contract,
// server/tests/test_w1_seed_product_path.py (seed_params, change,
// test_correction_still_needs_strings, run), whose sources are
// server/write_tools.json, server/solar_graph_seed.py (validate_seed_request,
// seed_units) and server/builtins/solar_settings.py. No field is invented here.
// Fails closed: a malformed version record or versions view throws.

// server/solar_local_graph.py SEED_RESULT_SCHEMA and RESULT_SCHEMA.
export const SEED_RESULT_SCHEMA = 'leaf.solar-graph-seed.v1'
export const COMMIT_RESULT_SCHEMA = 'leaf.solar-graph-commit.v1'

// test_w1_seed_product_path.py UNITS: the explicit units a seed must carry
// (solar_graph_seed.seed_units requires exactly these four keys).
export const SEED_UNITS = Object.freeze({
  drawing_units: 'ft',
  wcs_to_ucs: Object.freeze([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]),
  elevation_datum: 'unknown',
  crs: null,
})

const SHA256 = /^[0-9a-f]{64}$/

function positiveInteger(value) {
  return Number.isInteger(value) && value >= 1
}

// One row of GET /api/drawings/{id}/versions: `{v, sha256, ...}`.
function intakeDigest(version) {
  if (!version || typeof version !== 'object' || !positiveInteger(version.v)) {
    throw new Error('solar graph proof: version record needs an integer v >= 1')
  }
  if (typeof version.sha256 !== 'string' || !SHA256.test(version.sha256)) {
    throw new Error(`solar graph proof: version ${version.v} has no intake sha256`)
  }
  return version.sha256
}

// The ordinary settings request test_ordinary_run_on_an_upload_names_the_seed
// sends to a graphless upload. It must be refused `graph_seed_required`.
export function unseededSettingsParams() {
  return { expected_rev: 0, changes: { panels_in_sequence: 3 } }
}

// seed_params(digest): the `initialize` seed built from the version the graph
// is seeded on, bound to that version's own intake sha256.
export function seedParams(version) {
  return {
    expected_rev: 0,
    changes: { panels_in_sequence: 3 },
    initialize: {
      schema_version: 1,
      source_intake_sha256: intakeDigest(version),
      units: { ...SEED_UNITS, wcs_to_ucs: [...SEED_UNITS.wcs_to_ucs] },
    },
  }
}

// change(): the ordinary settings commit against graph revision `expectedRev`.
export function settingsChangeParams(expectedRev) {
  if (!Number.isInteger(expectedRev) || expectedRev < 0) {
    throw new Error('solar graph proof: expected_rev must be an integer >= 0')
  }
  return { expected_rev: expectedRev, changes: { num_mppt: 2 } }
}

// test_correction_still_needs_strings: an empty correction against `expectedRev`.
export function correctionParams(expectedRev) {
  if (!Number.isInteger(expectedRev) || expectedRev < 0) {
    throw new Error('solar graph proof: expected_rev must be an integer >= 0')
  }
  return { expected_rev: expectedRev, memberships: [] }
}

// run(): the POST /api/run body. `catalogDigest` is the digest GET /api/tools
// issued for this tool; the route refuses a run without the current one.
export function runBody({ tool, drawingId, params, catalogDigest }) {
  if (typeof tool !== 'string' || !tool) throw new Error('solar graph proof: tool is required')
  if (typeof drawingId !== 'string' || !drawingId) throw new Error('solar graph proof: drawing id is required')
  if (typeof catalogDigest !== 'string' || !catalogDigest) {
    throw new Error(`solar graph proof: no catalog digest for ${tool}`)
  }
  return { tool, dwg: drawingId, params: structuredClone(params), catalog_digest: catalogDigest }
}

// Two GET /versions views taken around one commit: the head moved exactly one
// version, head and latest agree, and the new head row names the old head as its
// parent. Returns the new head row.
export function expectHeadAdvanced(before, after) {
  for (const [label, view] of [['before', before], ['after', after]]) {
    if (!view || !positiveInteger(view.head) || !positiveInteger(view.latest) || !Array.isArray(view.versions)) {
      throw new Error(`solar graph proof: ${label} is not a versions view`)
    }
  }
  if (after.drawing_id !== before.drawing_id) {
    throw new Error(`solar graph proof: versions views name different drawings (${before.drawing_id}, ${after.drawing_id})`)
  }
  if (after.head !== before.head + 1 || after.latest !== after.head) {
    throw new Error(`solar graph proof: head did not advance one version (${before.head} -> ${after.head}, latest ${after.latest})`)
  }
  if (after.versions.length !== before.versions.length + 1) {
    throw new Error(`solar graph proof: expected ${before.versions.length + 1} versions, found ${after.versions.length}`)
  }
  const row = after.versions.find((entry) => entry?.v === after.head)
  if (!row || row.parent !== before.head) {
    throw new Error(`solar graph proof: version ${after.head} does not name parent ${before.head}`)
  }
  intakeDigest(row)
  return row
}
