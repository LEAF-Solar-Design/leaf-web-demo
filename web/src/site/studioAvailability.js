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
// WHERE A ROW APPLIES. Evidence is gathered on a host, so a row can name the
// hosts it speaks for (`hosts`). A row proven only on staging takes its control
// away on staging and nowhere else: a local stack, the offline demo and a
// later production promotion keep their own behaviour until someone walks
// THEM. A row with no `hosts` is true everywhere (the code itself decides it).
//
// THE WALK OVERRIDE. A lane that is fixing an unavailable feature has to walk
// it on staging before it may flip the row, and it cannot walk a control this
// table has disabled. `?availability=walk` in the address bar (or
// localStorage `leaf.availability.walk` = `1`) lifts every manifest disable
// for that browser, so the verifier sees the raw product. It is a walking
// aid, never a grant: the server still decides what runs.
//
// Pure on purpose: plain frozen data plus pure functions, no React, and one
// import (the app host list), so the landing bundle, the console, a plain Node
// walker and a test can all read the same table.
// ---------------------------------------------------------------------------

import { APP_ONLY_HOSTS } from './appHosts.js'
import { isInvestorSession } from '../investorDemo.js'
import { serviceState } from '../lib/serviceAuthSession.js'

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

export const STAGING_HOST = 'platform-staging.leafdesign.ai'
const STAGING_ONLY = Object.freeze([STAGING_HOST])
const PRODUCTION_HOSTS = Object.freeze([...APP_ONLY_HOSTS].filter((host) => host !== STAGING_HOST))
const APP_HOSTS = Object.freeze([...APP_ONLY_HOSTS])

// Every row: { workspace, label, status, disclaimer, evidence, hosts?, tool?, note?, adopted? }.
//   workspace  site | sign-in | browser | cad | solar | ios | ops | lab |
//              project | ask | shared
//   label      the visible name of the control or panel
//   evidence   audit:<path> under EVIDENCE_ROOTS.audit, capability:<gap key or
//              class> under EVIDENCE_ROOTS.capability, or source:<the control>
//              for one the code itself has always shipped disabled
//   hosts      the hostnames an UNAVAILABLE row applies on; absent = everywhere
//   tool       the catalog tool name this row speaks for (catalogToolDisclaimer)
//   note       an ENABLED feature's standing prerequisite, shown as a hint
//   adopted    true when the control already shows this exact sentence on its
//              own disabled state. The manifest lists the feature, and the
//              test (which knows the file) pins the two equal; the control
//              keeps its own gate, because flipping such a row cannot build
//              the feature.
//
// NO SOURCE PATH MAY APPEAR IN A VALUE BELOW. This module ships in the
// production bundle, and the element source fence refuses any readable source
// path there. The test owns the map from an adopted row to its file.
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
    // Every app host hands the sheets hub to the marketing site; the sheets
    // render locally everywhere else.
    hosts: APP_HOSTS,
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
  // -- ask bar and catalog (every workspace) --------------------------------
  'ask.typed-request': {
    workspace: 'ask',
    label: 'Type a request in the command bar',
    status: 'enabled',
    disclaimer: null,
    evidence: 'audit:before/journeys/cad-typed-count.desktop1440.json',
  },
  'shared.catalog-run': {
    workspace: 'shared',
    label: 'Review and run a catalog tool',
    status: 'enabled',
    disclaimer: null,
    evidence: 'audit:before/journeys/cad-catalog-run.desktop1440.json',
  },
  'shared.author-tool': {
    workspace: 'shared',
    label: 'Author a tool',
    status: 'unavailable',
    disclaimer: 'Not yet available. Authoring will build a new CAD tool from your plain English description.',
    hosts: STAGING_ONLY,
    evidence: 'audit:before/journeys/author-from-sentence.desktop1440.json',
  },
  'shared.account-panels': {
    workspace: 'shared',
    label: 'Details, costs, Claude accounts and Add-ons',
    status: 'enabled',
    disclaimer: null,
    evidence: 'audit:before/journeys/shell-panels.desktop1440.json',
  },
  // -- Solar CAD ------------------------------------------------------------
  'solar.arlo-design': {
    workspace: 'solar',
    label: 'Arlo design',
    status: 'unavailable',
    disclaimer: 'Not yet available. ARLO will propose electrical feeder assemblies for this layout.',
    hosts: STAGING_ONLY,
    tool: 'arlo-design',
    evidence: 'audit:before/journeys/solar-arlo.desktop1440.json',
  },
  // -- CAD -------------------------------------------------------------------
  // Operator decision D2 (2026-11-27): sync back to AutoCAD is coming soon.
  // Hidden on the production hosts; staging and local keep the row so it can
  // be rehearsed. Inverse of the staging-evidence rows above, by decision.
  'cad.drawing-sync': {
    workspace: 'cad',
    label: 'Drawing sync (Pull and Publish) in the AutoCAD palette',
    status: 'unavailable',
    disclaimer: 'Sync back to AutoCAD is not yet available.',
    hosts: PRODUCTION_HOSTS,
    evidence: 'source:AutoCAD palette Drawing sync row, decision D2',
  },
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
    adopted: true,
    evidence: 'source:quick bar Print control',
  },
  'cad.model-tab': {
    workspace: 'cad',
    label: 'Model',
    status: 'unavailable',
    disclaimer: '3D modelling is not in this engine yet',
    adopted: true,
    evidence: 'source:ribbon tab table, Model tab',
  },
  'cad.snap-and-polar': {
    workspace: 'cad',
    label: 'Snap mode and Polar tracking',
    status: 'unavailable',
    disclaimer: 'not in the browser viewer yet',
    adopted: true,
    evidence: 'source:drawing status bar toggles',
  },
})

const UNDECLARED = Object.freeze({
  id: null, workspace: null, label: null, status: 'enabled', available: true, disclaimer: null, note: null, evidence: null, declared: false,
})

export const WALK_OVERRIDE_PARAM = 'availability'
export const WALK_OVERRIDE_VALUE = 'walk'
export const WALK_OVERRIDE_STORAGE_KEY = 'leaf.availability.walk'

/**
 * Where this code is running: the hostname a row's `hosts` is matched against
 * and whether the walk override is on. Read fresh when a reader needs it and
 * was not handed one; every reader below takes it as an optional argument, so
 * a test or a Node walker states its own host instead of faking a window.
 */
export function availabilityEnv() {
  if (typeof window === 'undefined') return { hostname: '', walk: false, investorDemo: false }
  let walk = false
  try {
    walk = new URLSearchParams(window.location.search || '').get(WALK_OVERRIDE_PARAM) === WALK_OVERRIDE_VALUE
      || window.localStorage?.getItem(WALK_OVERRIDE_STORAGE_KEY) === '1'
  } catch { /* a blocked storage read means no override */ }
  return { hostname: String(window.location.hostname || '').toLowerCase(), walk,
    investorDemo: isInvestorSession() && !serviceState().mode }
}

// `env` is resolved LAZILY: an enabled row and a tool no row names never read
// the address bar or walk override. After the memoized investor check, a
// per-tool call in a 67 tool ribbon needs only an own-property check for a miss.
function rowApplies(row, env) {
  if (row.status !== 'unavailable') return false
  const here = env || availabilityEnv()
  if (here.walk === true) return false
  return !row.hosts || row.hosts.includes(String(here.hostname || '').toLowerCase())
}

/**
 * One feature's row plus derived fields: `available` (the row does not take
 * the feature away HERE), `declared` (the id has a row) and `status` as the
 * table states it. An id with no row is UNDECLARED, which is never "off": the
 * manifest has no opinion, so the control keeps whatever its own, more
 * specific gate says. studioAvailability.test.js pins every id a source file
 * names to a real row, so an undeclared read cannot ship by typo.
 */
export function studioFeature(id, env) {
  const row = Object.prototype.hasOwnProperty.call(STUDIO_AVAILABILITY, id) ? STUDIO_AVAILABILITY[id] : null
  if (!row) return UNDECLARED
  return { id, note: null, ...row, available: !rowApplies(row, env), declared: true }
}

/** True unless the manifest declares the feature unavailable. */
export function studioFeatureAvailable(id, env) {
  return studioFeature(id, env).available
}

/** The disclaimer a disabled control shows, or null when the feature is available here. */
export function notYetAvailable(id, env) {
  const feature = studioFeature(id, env)
  return feature.available ? null : feature.disclaimer
}

const TOOL_ROWS = Object.freeze(Object.fromEntries(
  Object.entries(STUDIO_AVAILABILITY).filter(([, row]) => row.tool).map(([id, row]) => [row.tool, id]),
))

/**
 * The disclaimer for a CATALOG TOOL the manifest takes away here, or null.
 * The catalog's own per-tool availability from the server stays the first and
 * more specific answer; this covers a tool the server lists as runnable and
 * the staging walk proved is not.
 */
export function catalogToolDisclaimer(toolName, env) {
  const investor = env ? env.investorDemo === true : isInvestorSession() && !serviceState().mode
  if (investor) return 'Not available in the view-only demo'
  const id = typeof toolName === 'string' && Object.prototype.hasOwnProperty.call(TOOL_ROWS, toolName) ? TOOL_ROWS[toolName] : null
  return id ? notYetAvailable(id, env) : null
}

/** An enabled feature's standing prerequisite, or null. Never set on an unavailable row. */
export function studioFeatureNote(id, env) {
  const feature = studioFeature(id, env)
  return feature.status === 'enabled' ? feature.note : null
}

/**
 * Props for a plain <button> gated by one manifest row. Available: an empty
 * object, so the button is untouched. Unavailable: aria-disabled (the control
 * stays focusable, so a keyboard or screen-reader visitor can still reach the
 * sentence), the disclaimer as its title, and `data-availability` for the
 * walk. The caller must also refuse the click: see `guardUnavailable`.
 */
export function availabilityProps(id, env) {
  const reason = notYetAvailable(id, env)
  if (!reason) return {}
  return { 'aria-disabled': true, title: reason, 'data-availability': 'unavailable', 'data-availability-id': id }
}

/** Wraps a handler so an unavailable feature never runs it. */
export function guardUnavailable(id, handler, env) {
  if (studioFeatureAvailable(id, env)) return handler
  return (event) => { event?.preventDefault?.() }
}

/**
 * Every row as the TABLE states it, in table order, for the capability measure
 * and the audit: `available` here means status is enabled, on no particular
 * host and with no override.
 */
export function listStudioAvailability() {
  return Object.entries(STUDIO_AVAILABILITY).map(([id, row]) => ({
    id, note: null, hosts: null, ...row, available: row.status === 'enabled', declared: true,
  }))
}
