// ---------------------------------------------------------------------------
// THE STUDIO AVAILABILITY MANIFEST (operator mandate 2026-11-18: "transparent
// disables and disclaimers of what is not yet available").
//
// ONE table, feature id -> enabled, or unavailable with the disclaimer the
// control shows. A feature a visitor can reach is in one of two states here:
//
//   enabled      it was walked on staging and it works; `evidence` says where.
//   unavailable  it is not built, not provisioned, or it failed the walk. The
//                control is DISABLED and carries `disclaimer`: a short, plain
//                sentence that says it is not yet available and what it will
//                do. No date is ever promised.
//
// The rule that decides a row (never a guess):
//   DOES with staging evidence  -> enabled
//   DOES NOT                    -> unavailable + disclaimer
//   UNVERIFIED                  -> walk it on staging: works -> enabled,
//                                  fails -> unavailable + disclaimer + a gap
//
// HOW A LANE LANDS A FEATURE: flip its row to `status: 'enabled'`, set
// `disclaimer: null`, and point `evidence` at the staging walk that proves it,
// in the same pull request that makes it work. Never delete a row: the
// capability measure reads this table (`node scripts/studio_availability.mjs
// --json`) and a missing row reads as a feature nobody is tracking.
//
// SCOPE: availability and messaging only. This module never decides what an
// engine, a tool or a server may do, and it is never a security control. It
// sits BESIDE the existing honesty mechanisms, never over them: the catalog's
// per-tool `availability` from the server, the ribbon's reason maps
// (lib/ribbonClusters.js, lib/actionRegistry.js), entitlements and checkout
// authority all keep their own, more specific sentences. A row here is for a
// SHELL feature those mechanisms have no answer for.
//
// Pure and dependency-free on purpose: plain frozen data plus pure functions,
// no React and no imports, so the landing bundle, the console, a plain Node
// walker and a test can all read the same table.
// ---------------------------------------------------------------------------

export const STUDIO_AVAILABILITY_SCHEMA = 'leaf.studio-availability.v1'

// The words every NEW disclaimer carries. Rows that adopt a sentence the
// product already showed before this manifest existed keep that sentence byte
// for byte (`adopted`), because the walk harness other lanes run pins those
// strings against live staging.
export const NOT_YET_AVAILABLE = 'not yet available'

export const AVAILABILITY_STATUSES = Object.freeze(['enabled', 'unavailable'])

// Where the evidence lives. A row's `evidence` is a path under one of these
// roots (or a gap key from the capability measure), never a bare claim.
export const EVIDENCE_ROOTS = Object.freeze({
  audit: 'leaf-research/studio-availability-overhaul-2026-11',
  capability: '.claude/state/studio-accel',
})

function freezeDeep(value) {
  if (value === null || typeof value !== 'object') return value
  Object.freeze(value)
  for (const key of Object.keys(value)) freezeDeep(value[key])
  return value
}

// Every row: { workspace, label, status, disclaimer, evidence, note?, adopted? }.
//   workspace  site | sign-in | browser | cad | solar | ios | ops | lab |
//              project | ask | shared
//   label      the visible name of the control or panel
//   evidence   audit:<path> under EVIDENCE_ROOTS.audit, capability:<gap key or
//              class> under EVIDENCE_ROOTS.capability, or source:<file> for a
//              control the code itself has always shipped disabled
//   note       an ENABLED feature's standing prerequisite, shown as a hint
//   adopted    the source file (under web/) that already spells this exact
//              sentence on its own disabled control. The manifest lists the
//              feature and the test pins the two equal; the control keeps its
//              own gate, because flipping such a row cannot build the feature.
export const STUDIO_AVAILABILITY = freezeDeep({
  // -- site (the landing an emailed Studio link opens) -----------------------
  'site.sample-rooftop': {
    workspace: 'site',
    label: 'Try Branch on the sample rooftop',
    status: 'enabled',
    disclaimer: null,
    evidence: 'audit:before/journeys/landing-to-sample.desktop1440.json',
  },
  'site.open-workspace': {
    workspace: 'site',
    label: 'Open workspace',
    status: 'enabled',
    disclaimer: null,
    evidence: 'audit:before/journeys/landing-open-workspace.desktop1440.json',
  },
  'site.sheets': {
    workspace: 'site',
    label: 'Sheet tabs 02 to 06 (The problem, How Branch works, Proof, Pricing, Docs)',
    status: 'unavailable',
    disclaimer: 'Not yet available. Sheets 02 to 06 will open on the Leaf Automation website.',
    evidence: 'audit:before/journeys/landing-open-workspace.desktop1440.json',
  },
  'site.pricing': {
    workspace: 'site',
    label: 'Pricing',
    status: 'unavailable',
    disclaimer: 'Pricing is not yet available.',
    evidence: 'audit:before/state/landing.guest.desktop1440.json',
  },
  // -- sign-in and sign-up ---------------------------------------------------
  'sign-in.hosted-login': {
    workspace: 'sign-in',
    label: 'Sign in',
    status: 'enabled',
    disclaimer: null,
    evidence: 'audit:before/journeys/sign-in.desktop1440.json',
  },
  'sign-in.sign-up': {
    workspace: 'sign-in',
    label: 'Sign up',
    status: 'enabled',
    disclaimer: null,
    evidence: 'audit:before/journeys/sign-in.desktop1440.json',
  },
  // -- project board ---------------------------------------------------------
  'project.open': {
    workspace: 'project',
    label: 'Open a project',
    status: 'enabled',
    disclaimer: null,
    evidence: 'audit:before/clickwalk/browser.desktop1440.json',
  },
  'project.create-from-sample-drawing': {
    workspace: 'project',
    label: 'Create project from this drawing',
    status: 'unavailable',
    disclaimer: 'Not yet available for the sample drawing. Upload a DWG or DXF to start a project from it.',
    evidence: 'audit:before/clickwalk/browser.desktop1440.json',
  },
  // -- CAD -------------------------------------------------------------------
  'cad.sample-drawing': {
    workspace: 'cad',
    label: 'Sample rooftop drawing',
    status: 'enabled',
    disclaimer: null,
    evidence: 'audit:before/state/cad.signed.desktop1440.json',
  },
  'cad.connect-autocad': {
    workspace: 'cad',
    label: 'Connect AutoCAD',
    status: 'enabled',
    disclaimer: null,
    note: 'Needs desktop AutoCAD and the Leaf Automation bridge running on this computer.',
    evidence: 'audit:before/clickwalk/cad.desktop1440.json',
  },
  'cad.print': {
    workspace: 'cad',
    label: 'Print',
    status: 'unavailable',
    disclaimer: "Printing isn't available yet",
    adopted: 'src/App.jsx',
    evidence: 'source:web/src/App.jsx',
  },
  'cad.model-tab': {
    workspace: 'cad',
    label: 'Model',
    status: 'unavailable',
    disclaimer: '3D modelling is not in this engine yet',
    adopted: 'src/lib/ribbonTabs.data.js',
    evidence: 'source:web/src/lib/ribbonTabs.data.js',
  },
  'cad.snap-and-polar': {
    workspace: 'cad',
    label: 'Snap mode and Polar tracking',
    status: 'unavailable',
    disclaimer: 'not in the browser viewer yet',
    adopted: 'src/site/DrawingCockpit.jsx',
    evidence: 'source:web/src/site/DrawingCockpit.jsx',
  },
})

const UNDECLARED = Object.freeze({
  id: null, workspace: null, label: null, status: 'enabled', available: true, disclaimer: null, note: null, evidence: null, declared: false,
})

/**
 * One feature's row plus two derived fields: `available` (status is enabled)
 * and `declared` (the id has a row). An id with no row is UNDECLARED, which is
 * never "off": the manifest has no opinion, so the control keeps whatever its
 * own, more specific gate says. studioAvailability.test.js pins every id a
 * source file names to a real row, so an undeclared read cannot ship by typo.
 */
export function studioFeature(id) {
  const row = Object.prototype.hasOwnProperty.call(STUDIO_AVAILABILITY, id) ? STUDIO_AVAILABILITY[id] : null
  if (!row) return UNDECLARED
  return { id, note: null, ...row, available: row.status === 'enabled', declared: true }
}

/** True unless the manifest declares the feature unavailable. */
export function studioFeatureAvailable(id) {
  return studioFeature(id).available
}

/** The disclaimer a disabled control shows, or null when the feature is available. */
export function notYetAvailable(id) {
  const feature = studioFeature(id)
  return feature.available ? null : feature.disclaimer
}

/** An enabled feature's standing prerequisite, or null. Never set on an unavailable row. */
export function studioFeatureNote(id) {
  const feature = studioFeature(id)
  return feature.available ? feature.note : null
}

/**
 * Props for a plain <button> gated by one manifest row. Available: an empty
 * object, so the button is untouched. Unavailable: aria-disabled (the control
 * stays focusable, so a keyboard or screen-reader visitor can still reach the
 * sentence), the disclaimer as its title, and `data-availability` for the
 * walk. The caller must also refuse the click: see `guardUnavailable`.
 */
export function availabilityProps(id) {
  const reason = notYetAvailable(id)
  if (!reason) return {}
  return { 'aria-disabled': true, title: reason, 'data-availability': 'unavailable', 'data-availability-id': id }
}

/** Wraps a handler so an unavailable feature never runs it. */
export function guardUnavailable(id, handler) {
  if (studioFeatureAvailable(id)) return handler
  return (event) => { event?.preventDefault?.() }
}

/** Every row, in table order, for the capability measure and the audit. */
export function listStudioAvailability() {
  return Object.keys(STUDIO_AVAILABILITY).map((id) => studioFeature(id))
}
