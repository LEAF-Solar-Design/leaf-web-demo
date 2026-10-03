// The drafting ribbon's clusters as DATA (W4d Slice A, W4e panels): pure
// builders over App state, so every panel the ribbon shows is one tested
// decision and the ribbon component itself stays a renderer of an ordered
// cluster list.
//
// Cluster shape: { id, label, kind: 'group' | 'family', note?, tools: [],
//                  widgets?: [] }.
// Tool shape:    { id, label, text?, icon?, size?: 'large'|'small'|'row',
//                  swatch?, title?, mcpSource?: { server_id, tool }, reason?,
//                  disabled?, pressed?, write?, expanded?, controls?, onClick }.
//   `label` is the accessible name (stable, tests key on it); `text` is the
//   ribbon's display label for large/row tools; `icon` is a CockpitIcon key.
//
// HONESTY CONTRACT (the reason a group exists here at all): every disabled
// tool carries the sentence that says WHY (`reason`), and a group that is
// unavailable as a whole carries it as `note`. A greyed control with no
// reason is the gap ToolsPanel's lock-note closed, and the ribbon must never
// reopen it. Nothing here is a stub: every onClick is a real handler App
// already owns, and the reference panels this engine cannot back yet are
// present, disabled, and say so (operator decision, W4e plan: mirror the
// reference's eight Draw-tab panels).
import { zoomViewer } from '../site/DrawingCockpit.jsx'
import { deriveIosState } from '../ios/IosSurface.jsx'
import { iosSourceApprovalState } from '../site/iosShipReadiness.js'
import { RIBBON_TABS, profileRibbonTabData } from './ribbonTabs.data.js'
import { DEFERRED_REASONS, REASONS, forCluster, ribbonTool } from './actionRegistry.js'
import { DEFAULT_TOOL_ICON, isWriteTool, toolIcon, toolMcpSource, toolPlacementSize, toolPlacementTab } from './toolRecord.js'
import { solarFormKeys, solarView } from '../solar/solarView.js'

// The reason vocabulary moved to the action registry with slice 10a, because
// `when(ctx)` is the registry's half of the honesty contract this file's header
// states. Re-exported unchanged so every importer and every pinned test reads
// the same frozen object it always did.
export { REASONS } from './actionRegistry.js'

export const RIBBON_RATIONALE = 'Ribbon selection. Confirm the exact tool and parameters before it runs.'
export const ZOOM_IN = 1.25
export const ZOOM_OUT = 0.8
// A drawing can carry hundreds of layers; the ribbon is a strip, not a
// palette. Past this the cluster says how many more live in the pane.
export const MAX_LAYER_TOOLS = 10

export const SOLAR_REFUSAL_REASONS = Object.freeze({
  entitlement_required: 'Your plan does not include this solar tool',
  entitlement_policy_unavailable: 'The plan policy could not be read, so this tool stays off',
  broker_adapter_unavailable: 'No engine runs this solar tool yet',
  drawing_context_required: 'Open a drawing to use this solar tool',
  invalid_drawing_context: 'The open drawing context is not valid for solar tools',
  persisted_graph_unavailable: 'This drawing has no saved solar design yet',
  graph_seed_required: 'Start the solar design with Solar settings first',
  not_current_head: 'Open the latest drawing version to start a solar design',
  seed_project_scope_unsupported: 'A project drawing cannot start a solar design yet',
  invalid_seed_request: 'The solar design start request is not valid',
  graph_already_embedded: 'This drawing already carries a solar design',
  unresolved_units: 'Set the drawing units in Solar settings first',
  strings_required: 'Create strings before correcting one',
  valid_settings_required: 'Complete valid Solar settings first',
  project_name_required: 'Name the project in Solar settings first',
  project_zip_required: 'Enter the project ZIP code in Solar settings first',
  invalid_project_zip: 'Enter a valid project ZIP code in Solar settings first',
  invalid_project_coordinates: 'Enter a valid project latitude and longitude in Solar settings first',
  project_units_required: 'Set the project drawing units in Solar settings first',
  sizing_confirmation_required: 'Confirm the string sizing first',
  sized_panel_groups_required: 'Create sized panel groups first',
  valid_strings_required: 'Solve valid strings first',
  equipment_assignment_required: 'Assign inverter equipment first',
  complete_routing_required: 'Route every homerun first',
  licensed_graph_commit_required: 'This drawing keeps its solar design in AutoCAD, so this tool runs there',
  module_power_required: 'Run Size strings again to record the module power',
  roof_installation_required: 'Panel groups are made on rooftop designs only',
  routing_topology_required: 'Give every string its panels, an inverter and a valid wire size first',
  inverter_assignment_mismatch: 'Assign every string to an input on its inverter first',
  degenerate_route: 'Move each inverter off the panels at either end of its string',
  invalid_route_point: 'A panel or inverter sits outside the range homeruns can route',
  proposal_job_required: 'Run Solve proposal first, then commit its result',
  frames_required: 'Create panel groups first',
  panels_required: 'Add panels to the design first',
  electrical_zones_required: 'Create electrical zones first',
  panels_already_present: 'This design already has its panels',
  panel_layer_filter_required: 'Set Panel layer contains in Solar settings first',
  rooftop_required: 'This solar tool works on rooftop designs only',
  drawing_units_unsupported: 'This solar tool needs drawing units of in, ft, mm, cm or m',
  guardrails_sizing_required: 'Size strings before reading guardrails',
  guardrails_sizing_ambiguous: 'String sizing differs across the design, so guardrails cannot pick one module',
  guardrails_project_coordinates_required: 'Set the project latitude and longitude in Solar settings first',
  guardrails_mixed_inverters: 'Guardrails need every inverter configured the same, and this design\'s inverters differ',
  guardrails_input_unsupported: 'Guardrails cannot read this design yet',
  electrical_zones_unsupported: 'Assign strings works on designs without electrical zones',
  unassigned_strings_required: 'Every string already has an inverter or collector',
  string_collectors_required: 'Add an inverter or collector for the strings to feed first',
  schedules_input_unsupported: 'Electrical schedules cannot read this design yet',
  schedules_kernel_refused: 'Electrical schedules cannot build tables from this design',
  schedules_mapping_failed: 'Electrical schedules cannot match this design to its circuits',
  schedules_optimizers_unsupported: 'Electrical schedules do not cover optimizer designs yet',
  schedules_zone_sizing_unsupported: 'Electrical schedules need one global string sizing, not zone sizing',
  solar_output_not_current: 'Rerun the earlier Solar steps so the whole design is current first',
  ground_installation_required: 'This solar tool works on Ground designs only',
  ground_conversion_in_use: 'Remove the strings, equipment, panels and other panel groups from this Ground design first',
  ground_physical_state_required: 'Lay out the Ground trackers first, then convert them',
  ground_tracker_rows_required: 'Add tracker rows to the Ground layout first',
  ground_units_mismatch: 'The Ground layout and the solar design use different drawing units',
  ground_layout_invalid: 'The Ground layout cannot be converted into panel groups',
  ground_layout_too_large: 'The Ground layout holds more panel slots, in total or on one tracker, than a design can carry',
  capability_availability_unavailable: 'Tool readiness has not loaded for this drawing',
  capability_not_ready: 'This solar tool is not ready for this drawing',
  unlisted: 'The server refused this solar tool',
})

export function solarRailReason(availability, options) {
  if (options?.openTypedForm === true) return ''
  if (!availability || typeof availability !== 'object' || Array.isArray(availability)) {
    return SOLAR_REFUSAL_REASONS.capability_availability_unavailable
  }
  if (['entitled', 'engine_ready', 'input_ready', 'implemented'].every((key) => availability[key] === true)) return ''
  const codes = availability.refusal_reasons
  if (!Array.isArray(codes) || codes.length === 0) return SOLAR_REFUSAL_REASONS.capability_not_ready
  return codes.map((code) => Object.hasOwn(SOLAR_REFUSAL_REASONS, code)
    ? SOLAR_REFUSAL_REASONS[code]
    : `${SOLAR_REFUSAL_REASONS.unlisted} (${code})`).join('; ')
}

// A Solar run refusal names its code in the server's case: a readiness refusal
// in lower case (ground_physical_state_required), a builtin refusal in upper case
// (GROUND_TRACKER_ROWS_REQUIRED). Both are one key of the map above. The three
// keys that describe the client's own state, never a server answer, are not run
// refusals. Bounded: at most 64 characters, one case, no regex on anything longer.
const SOLAR_RUN_REFUSAL_EXCLUDED = new Set(['capability_availability_unavailable', 'capability_not_ready', 'unlisted'])

export function solarRunRefusal(code) {
  if (typeof code !== 'string' || code.length === 0 || code.length > 64) return null
  if (!/^[a-z][a-z0-9_]*$/.test(code) && !/^[A-Z][A-Z0-9_]*$/.test(code)) return null
  const key = code.toLowerCase()
  if (SOLAR_RUN_REFUSAL_EXCLUDED.has(key) || !Object.hasOwn(SOLAR_REFUSAL_REASONS, key)) return null
  return SOLAR_REFUSAL_REASONS[key]
}

const plainRecord = (value) => value !== null && typeof value === 'object' && !Array.isArray(value)
  && [Object.prototype, null].includes(Object.getPrototypeOf(value))

// The envelope ResultPanel shows for a failed run of a Solar catalog tool: the
// error's message becomes the map's sentence when the refusal code is a map key.
// Everything else (another tool, a success, an unmapped code, any malformed part)
// returns the SAME envelope object, so nothing re-renders and the run's Details
// drawer still reads the raw code from the unmapped envelope.
export function solarRefusalEnvelope(envelope, tool) {
  if (!plainRecord(envelope) || envelope.ok === true || !plainRecord(envelope.error)) return envelope
  if (!plainRecord(tool) || envelope.tool !== tool.name) return envelope
  if (solarView(tool).state !== 'valid') return envelope
  // The synchronous rail answers reason_code at the top; a job record carries it
  // on the error (api.js recordToEnvelope); error.message repeats it either way.
  const code = [envelope.reason_code, envelope.error.reason_code, envelope.error.message]
    .find((value) => solarRunRefusal(value) !== null)
  if (code === undefined) return envelope
  return { ...envelope, error: { ...envelope.error, message: solarRunRefusal(code) } }
}

// Every catalog tool used to be one hardcoded icon and one hardcoded size. The
// record now answers both, and a record that answers neither renders exactly as
// it did — that equality is pinned in ribbonClusters.test.js.
export const CATALOG_TOOL_NOTE_ALL_PLACED = 'Every catalog tool sits on its own ribbon tab.'

// Profile reasons share one vocabulary. Ship reasons are selected from the
// mounted controller's phase and setup state, not inferred from a handler.
export const PROFILE_REASONS = Object.freeze({
  openProject: 'Sign in to open a project',
  changeProject: 'Sign in to change projects',
  createProject: 'Project creation is unavailable in this session',
  uploadDrawing: 'Drawing upload is unavailable in this session',
  newConversation: 'Conversations need a signed-in session',
  openJobs: 'The job monitor is unavailable in this session',
  openReceipts: 'Receipts are unavailable in this session',
  approvedRevision: 'No approved revision for this project yet',
  appleReadiness: 'Apple readiness is not mounted',
  testflightBuild: 'The ship lane is not ready; no launch control is available',
  shipNoRevision: 'Select an approved project revision before launching.',
  shipGrant: 'The Apple grant is not ready. Complete Apple grant setup.',
  shipExecutorBusy: 'The executor is busy. Wait for its current build to finish.',
  shipExecutorUnavailable: 'The executor is unavailable. Connect the ship executor.',
  shipIdle: 'Sign in and select the iOS profile to check ship readiness.',
  shipLoading: 'Checking ship readiness.',
  shipLaunching: 'The ship launch is being submitted.',
  shipRunning: 'A build is already running.',
  shipSucceeded: 'This build has succeeded.',
  shipFailed: 'This build has failed.',
  shipUnavailable: 'The ship lane is unavailable.',
  shipReceipts: 'No ship receipts yet',
  shipApproveOwner: 'Only the project owner can approve a source revision',
  shipApproveNoRevision: 'Select a canonical drawing version before approving',
  shipApproveNoSource: 'No imported source revision to approve yet',
  shipApproveApproved: 'Every imported source revision is already approved for the selected version',
  shipApproveBusy: 'The approval is being recorded.',
  shipApproveReady: 'Record the owner approval of this source revision for the selected version',
  stringingEmpty: 'No stringing tools in this catalog yet',
  placementEmpty: 'No placement tools in this catalog yet',
  measurementEmpty: 'No measurement tools in this catalog yet',
  selectionEmpty: 'No selection tools in this catalog yet',
})

const profileRecord = (value) => value && typeof value === 'object' && !Array.isArray(value) ? value : {}

export function profileReason(value, fallback) {
  return Object.values(PROFILE_REASONS).includes(value) ? value : fallback
}

export function shipLaunchReason({ phase, readiness } = {}) {
  const phaseReason = {
    idle: PROFILE_REASONS.shipIdle, loading: PROFILE_REASONS.shipLoading,
    launching: PROFILE_REASONS.shipLaunching, running: PROFILE_REASONS.shipRunning,
    succeeded: PROFILE_REASONS.shipSucceeded, failed: PROFILE_REASONS.shipFailed,
  }[phase]
  if (phase === 'ready') return ''
  if (phaseReason) return phaseReason
  return {
    'no-approved-revision': PROFILE_REASONS.shipNoRevision,
    'grant-not-ready': PROFILE_REASONS.shipGrant,
    'executor-busy': PROFILE_REASONS.shipExecutorBusy,
    'executor-unavailable': PROFILE_REASONS.shipExecutorUnavailable,
  }[readiness?.setupState] || PROFILE_REASONS.shipUnavailable
}

export function shipErrorSentence(error) {
  if (!error) return null
  const detail = String(error).trim().replace(/_/g, ' ')
  return detail ? `${detail.startsWith('Ship status: ') ? '' : 'Ship status: '}${detail}${/[.!?]$/.test(detail) ? '' : '.'}` : null
}
// A handler is a function or nothing; any other value is treated as absent.
const profileHandler = (value) => (typeof value === 'function' ? value : null)
const PROFILE_ICONS = Object.freeze({
  'project:open': 'open',
  'project:change': 'open',
  'project:create': 'new-file',
  'files:upload': 'import',
  'conversation:new': 'leader',
  'activity:jobs': 'history',
  'activity:receipts': 'save',
  'ship:revision': 'save',
  'ship:readiness': 'match',
  'ship:approve': 'match',
  'ship:launch': 'new-file',
  'ship:receipts': 'history',
})
const profileBase = (id, label) => ({ id, label, icon: PROFILE_ICONS[id] || DEFAULT_TOOL_ICON, title: label })

export function shipApproveRow(ship) {
  if (ship?.controllerLive !== true || !Array.isArray(ship.sources)) return null
  const sources = Array.isArray(ship.sources) ? ship.sources : []
  const approvals = Array.isArray(ship.approvals) ? ship.approvals : []
  const candidate = sources.find((source) => iosSourceApprovalState(source, approvals, ship.revision) === 'unapproved')
  const availability = ship.canApprove !== true ? { disabled: true, reason: PROFILE_REASONS.shipApproveOwner }
    : !ship.revision ? { disabled: true, reason: PROFILE_REASONS.shipApproveNoRevision }
      : sources.length === 0 ? { disabled: true, reason: PROFILE_REASONS.shipApproveNoSource }
        : !candidate ? { disabled: true, reason: PROFILE_REASONS.shipApproveApproved }
          : ship.approving === true ? { disabled: true, reason: PROFILE_REASONS.shipApproveBusy }
            : typeof ship.approve !== 'function' ? { disabled: true, reason: PROFILE_REASONS.shipApproveOwner }
              : { disabled: false, reason: PROFILE_REASONS.shipApproveReady }
  const label = candidate ? `Approve ${candidate.source_revision.slice(0, 8)}` : 'Approve revision'
  return { ...profileBase('ship:approve', label), ...availability,
    onClick: availability.disabled ? undefined : () => ship.approve(candidate.source_revision), title: label }
}

function wellFormedShipContract(contract) {
  return contract !== null && typeof contract === 'object' && !Array.isArray(contract)
    && typeof contract.receipt_id === 'string' && contract.receipt_id.trim().length > 0
    && typeof contract.reported_at === 'string' && contract.reported_at.trim().length > 0
    && contract.readiness !== null && typeof contract.readiness === 'object' && !Array.isArray(contract.readiness)
    && typeof contract.readiness.healthy === 'boolean'
    && typeof contract.readiness.launchable === 'boolean'
    && (contract.build_stage == null || typeof contract.build_stage === 'string')
}

export function shipStatusRows(contract, revision, onReceipts, ship) {
  if (ship?.controllerLive === true) {
    const state = ship.phase
    const stage = ship.execution?.failed_stage || ship.execution?.stage
    const approved = revision && ship.readiness?.approvedLaunch?.revision === revision
    const openReceipts = profileHandler(onReceipts)
    return [
      { ...profileBase('ship:revision', approved ? `Approved revision ${revision}`.slice(0, 64) : 'Approved revision'),
        disabled: !approved || !openReceipts,
        reason: approved ? PROFILE_REASONS.shipReceipts : PROFILE_REASONS.approvedRevision,
        onClick: approved ? openReceipts ?? undefined : undefined },
      { ...profileBase('ship:readiness', `Ship status: ${state}${stage ? ` (${stage})` : ''}`),
        state, pressed: state === 'ready', disabled: !openReceipts,
        reason: profileReason(ship.launchReason, shipLaunchReason(ship) || PROFILE_REASONS.shipReceipts),
        onClick: openReceipts ?? undefined },
    ]
  }
  const valid = wellFormedShipContract(contract) && typeof onReceipts === 'function'
  const state = valid ? deriveIosState(contract) : null
  const stage = valid ? contract.build_stage || '' : ''
  const readinessLabel = state === 'in-progress'
    ? `Apple readiness: in progress${stage ? ` (${stage})` : ''}`
    : `Apple readiness: ${state}`
  return [
    valid && revision
      ? { ...profileBase('ship:revision', `Approved revision ${revision}`.slice(0, 64)), disabled: false, title: `reported ${contract.reported_at}`.slice(0, 64), onClick: onReceipts }
      : { ...profileBase('ship:revision', 'Approved revision'), disabled: true, reason: PROFILE_REASONS.approvedRevision, onClick: undefined },
    valid
      ? { ...profileBase('ship:readiness', readinessLabel.slice(0, 64)), disabled: false, state, pressed: state === 'ready', onClick: onReceipts }
      : { ...profileBase('ship:readiness', 'Mounted Apple readiness'), disabled: true, reason: PROFILE_REASONS.appleReadiness, onClick: undefined },
  ]
}

function profileGroup(id, label, tools) {
  return { id, label, kind: 'group', tools }
}

export function solarRouteStatus({ eligible, previewing, head = 1, engineDirty, documentId, committedVersion, headDocumentId, solve, routes }) {
  if (!eligible) return 'ineligible'
  if (previewing || head !== 1 || engineDirty) return 'stale'
  // The starter opener is disabled on mock, and solved routes are mock-only.
  if (documentId !== null && !(typeof documentId === 'string' && documentId === headDocumentId && committedVersion === 1)) return 'foreign'
  if (solve == null || solve === 'pending') return 'loading'
  if (solve !== 'loaded' || !Array.isArray(routes) || routes.length === 0) return 'unavailable'
  return 'ready'
}

export function solarRouteDisplay({ status, shown = true, routes }) {
  return status === 'ready' && shown ? routes : undefined
}

export function solarStringsControl(status, shown, onToggle) {
  const base = { ...profileBase('solar-strings', 'Show solved rooftop strings'), icon: 'layers' }
  switch (status) {
    case 'ready': return { ...base, disabled: false, pressed: shown, reason: 'Show or hide solved rooftop routes', onClick: onToggle }
    case 'stale': return { ...base, disabled: true, pressed: false, reason: 'Solved routes are available only for the unchanged rooftop demo', onClick: undefined }
    case 'foreign': return { ...base, disabled: true, pressed: false, reason: 'Solved routes cover the rooftop demo only', onClick: undefined }
    case 'loading': return { ...base, disabled: true, pressed: false, reason: 'Solved routes are loading', onClick: undefined }
    case 'unavailable': return { ...base, disabled: true, pressed: false, reason: 'No solved routes are available for this drawing', onClick: undefined }
    default: return { ...base, disabled: true, pressed: false, reason: 'Solved routes are available only for the unchanged rooftop demo', onClick: undefined }
  }
}

export function profileEntryTab(previousProfile, profile, selected, home) {
  return profile === 'solar' && previousProfile !== profile ? home : selected
}

function solarRailTools(rows, gate, { openName, onOpenForm }, onRun, solarTypedForm) {
  const { running, previewing, writeLocked, writeEntitled, writeLockNote, engineDirty } = gate
  return rows.map((row) => ({ row, result: solarView(row) }))
    .filter(({ result }) => result.state !== 'absent')
    .sort((a, b) => (a.result.view?.wave ?? Infinity) - (b.result.view?.wave ?? Infinity)
      || (a.result.view?.order ?? Infinity) - (b.result.view?.order ?? Infinity)
      || a.row.name.localeCompare(b.row.name))
    .map(({ row, result }) => {
      const isWrite = isWriteTool(row)
      const locked = !!writeLocked && isWrite
      const entBlocked = isWrite && !writeEntitled
      const dirtyBlocked = isWrite && !!engineDirty
      const mcpSource = toolMcpSource(row)
      const formMode = result.view?.interaction.mode === 'form'
      const openTyped = formMode && typeof solarTypedForm === 'function' && solarTypedForm(row.name, row.availability) === true
      const reason = mcpSource
        ? REASONS.mcpToolNotWired
        : running
          ? REASONS.running
          : previewing
            ? REASONS.previewing
            : locked
              ? (writeLockNote || REASONS.writeLocked)
              : entBlocked
                ? REASONS.writeUnentitled
                : dirtyBlocked
                  ? REASONS.unsavedEngineEdits
                  : result.state === 'invalid'
                    ? SOLAR_REFUSAL_REASONS.capability_availability_unavailable
                    : solarRailReason(row.availability, { openTypedForm: openTyped })
      return {
        id: row.name,
        label: row.name,
        text: row.label || row.name,
        icon: toolIcon(row),
        size: toolPlacementSize(row),
        title: row.description || row.name,
        write: isWrite,
        disabled: !!reason,
        reason,
        expanded: formMode ? openName === row.name : undefined,
        controls: formMode ? 'solar-tool-form' : undefined,
        onClick: () => {
          if (formMode && solarFormKeys(row, result.view).length > 0) {
            onOpenForm(openName === row.name ? null : row)
          } else {
            onRun(row)
          }
        },
      }
    })
}

/** Tab strips for the shared workspace profiles; drafting keeps its caller's panels. */
export function profileRibbonTabs(profile, ctx = {}) {
  const drafting = () => RIBBON_TABS.map((tab) => ({ ...tab, clusters: [] }))
  if (profile !== 'solar' && profile !== 'project' && profile !== 'ship') return drafting()
  const context = profileRecord(ctx)
  const families = (Array.isArray(context.families) ? context.families : [])
    .filter((family) => family && typeof family === 'object' && !Array.isArray(family))
    .map((family) => ({
      ...family,
      family_id: typeof family.family_id === 'string' ? family.family_id : family.id,
      capabilities: (Array.isArray(family.capabilities) ? family.capabilities : [])
        .filter((tool) => tool && typeof tool === 'object' && !Array.isArray(tool) && typeof tool.name === 'string' && tool.name.trim()),
    }))
  const options = profileRecord(context.catalogOptions)
  // The existing catalog projection owns every run gate and every reason;
  // this only normalises the option shapes a caller could get wrong.
  const catalogOptions = {
    ...options,
    onRequestRun: profileHandler(options.onRequestRun) ?? undefined,
    onOpenFamily: profileHandler(options.onOpenFamily),
    writeLockNote: typeof options.writeLockNote === 'string' ? options.writeLockNote : '',
  }
  if (profile === 'solar') {
    const onRun = profileHandler(context.onRun)
    const solarRail = context.solarRail ? profileRecord(context.solarRail) : null
    const railFamilies = (Array.isArray(solarRail?.families) ? solarRail.families : [])
      .filter((family) => family && typeof family === 'object' && !Array.isArray(family))
      .map((family) => ({
        ...family,
        family_id: typeof family.family_id === 'string' ? family.family_id : family.id,
        capabilities: (Array.isArray(family.capabilities) ? family.capabilities : [])
          .filter((row) => row && typeof row === 'object' && !Array.isArray(row) && typeof row.name === 'string' && row.name.trim()),
      }))
    const gate = {
      ...catalogOptions,
      solarRail,
      writeEntitled: catalogOptions.writeEntitled ?? true,
      onRequestRun: onRun ? (tool) => onRun(tool) : undefined,
    }
    const familyTools = (id) => {
      const family = families.find((item) => item.family_id === id)
      if (solarRail) {
        const source = railFamilies.find((item) => item.family_id === id) || family
        if (!source?.capabilities.length) return null
        return [
          ...solarRailTools(source.capabilities, gate, solarRail, onRun, context.solarTypedForm),
          ...familyCluster(source, source.capabilities.filter((row) => solarView(row).state === 'absent'), gate, null).tools,
        ]
      }
      return family?.capabilities.length ? familyCluster(family, family.capabilities, gate, null).tools : null
    }
    // An absent or empty family is ONE honest disabled tool, never a fabricated command.
    const stringing = familyTools('stringing') ?? [
      { ...profileBase('stringing:empty', 'Stringing'), disabled: true, reason: PROFILE_REASONS.stringingEmpty, onClick: undefined },
    ]
    const placement = familyTools('placement') ?? [
      { ...profileBase('placement:empty', 'Equipment placement'), disabled: true, reason: PROFILE_REASONS.placementEmpty, onClick: undefined },
    ]
    const measurement = familyTools('measurement') ?? [
      { ...profileBase('measurement:empty', 'Measure'), disabled: true, reason: PROFILE_REASONS.measurementEmpty, onClick: undefined },
    ]
    const selection = familyTools('selection') ?? [
      { ...profileBase('selection:empty', 'Select'), disabled: true, reason: PROFILE_REASONS.selectionEmpty, onClick: undefined },
    ]
    const solar = profileRecord(context.solar)
    const onToggle = profileHandler(solar.onToggle)
    const onClear = profileHandler(context.onClearSelection)
    const tabs = drafting()
    tabs.splice(1, 0, { ...profileRibbonTabData('solar')[1], clusters: [
      // The engine consumer fills this seat with its four registry records.
      profileGroup('solar-panels', 'Panel placement', []),
      profileGroup('stringing', 'Stringing', [
        solarStringsControl(solar.status, solar.shown !== false, onToggle),
        ...stringing,
      ]),
      profileGroup('placement', 'Equipment placement', placement),
      profileGroup('measurement', 'Measure', measurement),
      profileGroup('selection', 'Select', [
        ...(solarRail ? [] : selection),
        { ...profileBase('solar-clear-selection', 'Clear selection'), icon: 'delete',
          disabled: !context.selectedHandle || !onClear, reason: 'Select an entity first', onClick: onClear ?? undefined },
        ...(solarRail ? selection : []),
      ]),
      ...(solarRail ? railFamilies
        .filter((family) => !['stringing', 'placement', 'measurement', 'selection'].includes(family.family_id))
        .map((family) => profileGroup(`solar-rail:${family.family_id}`, family.label,
          solarRailTools(family.capabilities, gate, solarRail, onRun, context.solarTypedForm)))
        .filter((cluster) => cluster.tools.length > 0) : []),
    ] })
    return tabs
  }
  if (profile === 'project') {
    const project = profileRecord(context.project)
    const onOpen = profileHandler(project.onOpen)
    const onChange = profileHandler(project.onChange)
    const onCreate = profileHandler(project.onCreate)
    const onUpload = profileHandler(profileRecord(context.files).onUpload)
    const onNew = profileHandler(profileRecord(context.conversation).onNew)
    const activity = profileRecord(context.activity)
    const onJobs = profileHandler(activity.onJobs)
    const onReceipts = profileHandler(activity.onReceipts)
    return [
      { ...profileRibbonTabData('project')[0], clusters: [
        profileGroup('project', 'Project', [
          { ...profileBase('project:open', 'Open project'), disabled: !onOpen, reason: PROFILE_REASONS.openProject, onClick: onOpen ?? undefined },
          { ...profileBase('project:change', 'Change project'), disabled: !onChange, reason: PROFILE_REASONS.changeProject, onClick: onChange ?? undefined },
          { ...profileBase('project:create', 'Create project'), disabled: !onCreate, reason: PROFILE_REASONS.createProject, onClick: onCreate ?? undefined },
        ]),
        profileGroup('files', 'Files', [
          { ...profileBase('files:upload', 'Upload drawing'), disabled: !onUpload, reason: PROFILE_REASONS.uploadDrawing, onClick: onUpload ?? undefined },
        ]),
        profileGroup('conversation', 'Conversation', [
          { ...profileBase('conversation:new', 'New conversation'), disabled: !onNew, reason: PROFILE_REASONS.newConversation, onClick: onNew ?? undefined },
        ]),
      ] },
      { ...profileRibbonTabData('project')[1], clusters: catalogClusters(families, catalogOptions) },
      { ...profileRibbonTabData('project')[2], clusters: [
        profileGroup('jobs', 'Jobs', [
          { ...profileBase('activity:jobs', 'Open job monitor'), disabled: !onJobs, reason: PROFILE_REASONS.openJobs, onClick: onJobs ?? undefined },
        ]),
        profileGroup('receipts', 'Receipts', [
          { ...profileBase('activity:receipts', 'Open receipts'), disabled: !onReceipts, reason: PROFILE_REASONS.openReceipts, onClick: onReceipts ?? undefined },
        ]),
      ] },
    ]
  }
  const ship = profileRecord(context.ship)
  const onLaunch = profileHandler(ship.onLaunch)
  const launchReason = ship.controllerLive === true ? profileReason(ship.launchReason, shipLaunchReason(ship)) : PROFILE_REASONS.testflightBuild
  const onShipReceipts = profileHandler(ship.onReceipts)
  const [revision, readiness] = shipStatusRows(ship.contract, ship.revision, onShipReceipts, ship)
  const approveRow = shipApproveRow(ship)
  return [{ ...profileRibbonTabData('ship')[0], clusters: [
    profileGroup('revision', 'Revision', [
      revision,
    ]),
    profileGroup('readiness', 'Readiness', [
      readiness,
    ]),
    ...(approveRow ? [profileGroup('approve', 'Approve', [approveRow])] : []),
    ...(ship.error ? [{ ...profileGroup('ship-error', 'Ship status', []), note: shipErrorSentence(ship.error) }] : []),
    // Without a launch handler there is no launch path, and the tool never implies one.
    profileGroup('ship', 'Ship', [
      { ...profileBase('ship:launch', 'TestFlight build'), disabled: !onLaunch, reason: launchReason, onClick: onLaunch ?? undefined },
    ]),
    profileGroup('receipts', 'Receipts', [
      { ...profileBase('ship:receipts', 'Open ship receipts'), disabled: !onShipReceipts, reason: PROFILE_REASONS.shipReceipts, onClick: onShipReceipts ?? undefined },
    ]),
  ] }]
}

/**
 * The catalog families of the active surface as `family` clusters — the
 * W4c-V1 ribbon, unchanged in behaviour: real commands through the
 * catalog run path with 'ribbon' attribution, ToolsPanel-parity write gating
 * with the reason readable. An empty fold is ONE honest cluster with the
 * sentence, never a fabricated button.
 */
export function catalogClusters(families, {
  onRequestRun,
  onOpenFamily = null,
  solarRail = null,
  running = false,
  previewing = false,
  writeLocked = false,
  writeEntitled = true,
  writeLockNote = '',
  engineDirty = false,
} = {}) {
  const list = Array.isArray(families) ? families : []
  if (list.length === 0) {
    return [{ id: 'tools', label: 'Tools', kind: 'group', note: 'No tools for this surface yet.', tools: [] }]
  }
  const gate = { onRequestRun, running, previewing, writeLocked, writeEntitled, writeLockNote, engineDirty, solarRail }
  // Tools that name their own tab leave this cluster (they are built by
  // catalogTabClusters instead); a family whose tools ALL moved leaves no
  // empty panel behind.
  const clusters = []
  for (const fam of list) {
    const tools = (fam.capabilities || []).filter((tool) => !toolPlacementTab(tool))
    if (tools.length === 0 && (fam.capabilities || []).length > 0) continue
    clusters.push(familyCluster(fam, tools, gate, onOpenFamily))
  }
  if (clusters.length === 0) {
    return [{ id: 'tools', label: 'Tools', kind: 'group', note: CATALOG_TOOL_NOTE_ALL_PLACED, tools: [] }]
  }
  return clusters
}

/**
 * The same catalog families as PER-TAB clusters, for the tools whose record
 * names a `placement.tab`. Cluster id is `<family_id>@<tab>` so a family can
 * seat tools on more than one tab and the two clusters stay distinct; the
 * label is the family's, unchanged.
 *
 * Returns an object keyed by tab id, holding only the tabs that actually got
 * tools — so a catalog where nothing declares a placement returns `{}` and the
 * ribbon is byte-identical to today.
 */
export function catalogTabClusters(families, {
  onRequestRun,
  onOpenFamily = null,
  solarRail = null,
  running = false,
  previewing = false,
  writeLocked = false,
  writeEntitled = true,
  writeLockNote = '',
  engineDirty = false,
} = {}) {
  const list = Array.isArray(families) ? families : []
  const gate = { onRequestRun, running, previewing, writeLocked, writeEntitled, writeLockNote, engineDirty, solarRail }
  const byTab = {}
  for (const fam of list) {
    // One pass per family, bucketed by tab: no per-tab rescan of the catalog.
    const buckets = new Map()
    for (const tool of fam.capabilities || []) {
      const tab = toolPlacementTab(tool)
      if (!tab) continue
      const bucket = buckets.get(tab)
      if (bucket) bucket.push(tool)
      else buckets.set(tab, [tool])
    }
    for (const [tab, tools] of buckets) {
      const cluster = familyCluster(fam, tools, gate, onOpenFamily)
      cluster.id = `${fam.family_id}@${tab}`
      if (byTab[tab]) byTab[tab].push(cluster)
      else byTab[tab] = [cluster]
    }
  }
  return byTab
}

/** One family cluster over an explicit tool list. The single tool projection. */
function familyCluster(fam, tools, gate, onOpenFamily) {
  const { onRequestRun, running, previewing, writeLocked, writeEntitled, writeLockNote, engineDirty } = gate
  return {
    id: fam.family_id,
    label: fam.label,
    kind: 'family',
    // The family label is a real command: open that family in the tool
    // rail (the spine's monogram used to do this; on drafting surfaces the
    // rail is hidden under the band, so the band carries the affordance).
    onLabelClick: onOpenFamily ? () => onOpenFamily(fam) : null,
    labelTitle: onOpenFamily ? `Open ${fam.label} in the tool rail (${(fam.capabilities || []).length} tools)` : '',
    tools: tools.map((tool) => {
      const result = solarView(tool)
      const isWrite = isWriteTool(tool)
      const locked = !!writeLocked && isWrite
      const entBlocked = isWrite && !writeEntitled
      const dirtyBlocked = isWrite && !!engineDirty
      // A tool projected from a connected MCP server (mcp_source) has no run
      // path on any surface yet — the projection itself is stubbed to emit
      // nothing until a later slice — so it is unconditionally unrunnable,
      // ahead of every transient gate below.
      const mcpSource = toolMcpSource(tool)
      const reason = mcpSource
        ? REASONS.mcpToolNotWired
        : running
          ? REASONS.running
          : previewing
            ? REASONS.previewing
            : locked
              ? (writeLockNote || REASONS.writeLocked)
              : entBlocked
                ? REASONS.writeUnentitled
                : dirtyBlocked
                  ? REASONS.unsavedEngineEdits
                  : gate.solarRail && result.state === 'invalid'
                    ? SOLAR_REFUSAL_REASONS.capability_availability_unavailable
                    : gate.solarRail ? result.state !== 'absent'
                      // CAD has no typed-form opener; input-unready form tools are reached through the Solar surface.
                      ? solarRailReason(tool.availability, { openTypedForm: false })
                      : ''
                      : plainRecord(tool.availability)
                        ? solarRailReason(tool.availability, { openTypedForm: false })
                        : ''
      return {
        id: tool.name,
        label: tool.name,
        text: tool.label || tool.name,
        // The record's own answer, with today's literals as the default.
        icon: toolIcon(tool),
        size: toolPlacementSize(tool),
        title: tool.description || tool.name,
        write: isWrite,
        ...(mcpSource ? { mcpSource } : {}),
        disabled: !!reason,
        reason,
        onClick: () => onRequestRun(tool, null, RIBBON_RATIONALE, 'ribbon'),
      }
    }),
  }
}

/**
 * Rail: the one command the hidden tool rail still needs from the band —
 * expand it. On drafting surfaces under the studio the rail sits behind
 * the band (the reference has no left rail at all), so this is the
 * affordance that brings it back; the rail's own header collapses it again.
 */
export function railCluster({ onExpand } = {}) {
  const ctx = { onExpand: () => onExpand?.() }
  return {
    id: 'rail',
    label: 'Rail',
    kind: 'group',
    tools: forCluster('rail').map((action) => ribbonTool(action, ctx)),
  }
}

/**
 * View: fit / zoom in / zoom out on the Viewer's ref surface (setView/getPose),
 * and (W4e round 3) the Properties pane toggle when the caller owns one: a
 * pressed-state tool, the way back after the pane's own close control.
 * `onBeforeJump` (S3), when given, runs once before Fit so the caller can
 * record a Back snapshot; zoom steps are not jumps and never call it.
 */
export function viewCluster({ viewerRef, hasDrawing = false, paneOpen = null, onTogglePane = null, onBeforeJump = null } = {}) {
  // The four records live in the registry; this builder supplies the CONTEXT
  // they close over (the viewer ref surface is React's, never the registry's).
  const ctx = {
    hasDrawing,
    paneOpen,
    onFit: () => {
      if (typeof onBeforeJump === 'function') onBeforeJump()
      viewerRef?.current?.setView?.('home')
    },
    onZoomIn: () => { zoomViewer(viewerRef?.current, ZOOM_IN) },
    onZoomOut: () => { zoomViewer(viewerRef?.current, ZOOM_OUT) },
    onTogglePane: () => onTogglePane?.(),
  }
  const tools = []
  for (const action of forCluster('view')) {
    // The pane toggle is seated only when the caller owns a pane; without one
    // there is nothing to toggle, so the record is absent rather than dead.
    if (action.id === 'properties-pane') {
      if (typeof onTogglePane !== 'function') continue
      tools.push(ribbonTool(action, ctx, { pressed: !!paneOpen }))
      continue
    }
    tools.push(ribbonTool(action, ctx))
  }
  return { id: 'view', label: 'View', kind: 'group', tools }
}

/** Version: undo / redo / history, under EXACTLY the toolbar's gates, each with its reason. */
export function versionCluster({
  hasVersions = false,
  canUndo = false,
  canRedo = false,
  versionBusy = false,
  running = false,
  previewing = false,
  mutationsBlocked = false,
  historyOpen = false,
  onUndo,
  onRedo,
  onToggleHistory,
} = {}) {
  // undo / redo / history are three registry records under EXACTLY the
  // toolbar's gates; the shared ladder (and history's shorter one, because
  // reading the chain is never a write) lives in each record's `when`.
  const ctx = {
    hasVersions,
    canUndo,
    canRedo,
    versionBusy,
    running,
    previewing,
    mutationsBlocked,
    onUndo: () => onUndo?.(),
    onRedo: () => onRedo?.(),
    onToggleHistory: () => onToggleHistory?.(),
  }
  return {
    id: 'version',
    label: 'Version',
    kind: 'group',
    tools: forCluster('version').map((action) => ribbonTool(
      action,
      ctx,
      action.id === 'history' ? { expanded: !!historyOpen } : {},
    )),
  }
}

/**
 * Layers: one pressed-state toggle per layer (the Legend's exact rule:
 * visible unless the map says false), bounded at MAX_LAYER_TOOLS with the
 * remainder named honestly. Each row is the reference's layer line: a bulb
 * (lit = shown), the layer's swatch, the name.
 */
export function layersCluster({ layers, counts = {}, visibleLayers = {}, onToggle, colorFor = null, max = MAX_LAYER_TOOLS } = {}) {
  const list = Array.isArray(layers) ? layers : []
  if (list.length === 0) {
    return { id: 'layers', label: 'Layers', kind: 'group', note: REASONS.noDrawing, tools: [] }
  }
  const bound = Math.max(0, Number.isInteger(max) ? max : MAX_LAYER_TOOLS)
  const shown = list.slice(0, bound)
  const rest = list.length - shown.length
  return {
    id: 'layers',
    label: 'Layers',
    kind: 'group',
    note: rest > 0 ? `+${rest} more in the Layers palette` : null,
    tools: shown.map((layer) => {
      const on = visibleLayers[layer] !== false
      const n = counts[layer] || 0
      const swatch = typeof colorFor === 'function' ? (colorFor(layer) || '') : ''
      return {
        id: `layer:${layer}`,
        label: layer,
        text: layer,
        icon: 'bulb',
        size: 'row',
        swatch,
        title: `${on ? 'Hide' : 'Show'} ${layer} (${n})`,
        pressed: on,
        onClick: () => onToggle?.(layer),
      }
    }),
  }
}

/**
 * Author: one real command — expand the rail and open "Author a tool".
 * Two distinct reasons, because they have two distinct fixes: a plan that
 * lacks `build` (entitlement) and a deployment whose authoring stage is off
 * (availability, the R5 rail). `entitled` is the plan's own answer;
 * `available` is the folded entitlement-AND-availability rule the rest of
 * the shell gates Generate on.
 */
export function authorCluster({
  onOpen, entitled = true, available = entitled,
  // The tool the author card just published, if any. It joins the cluster so
  // "run the one I just made" is a ribbon command too, and it says honestly
  // when the catalog has not caught up: no digest, no runnable tool.
  authored = null, onUseAuthored = null, running = false, previewing = false,
  // ToolsPanel-parity write gating (same rungs, same REASONS strings as
  // familyCluster): an authored WRITE tool is exactly as honest about a held
  // edit lock or a plan without editing tools as any catalog tool is.
  writeLocked = false, writeEntitled = true, writeLockNote = '',
} = {}) {
  // The two distinct reasons (an unentitled plan, an authoring stage that is
  // off) are the record's own ladder; this builder supplies only the context.
  const tools = forCluster('author').map((action) => ribbonTool(action, {
    entitled,
    available,
    onOpen: () => onOpen?.(),
  }))
  if (authored && authored.name) {
    const settled = typeof authored.catalog_digest === 'string' && !!authored.catalog_digest
    const isWrite = isWriteTool(authored)
    const locked = !!writeLocked && isWrite
    const entBlocked = isWrite && !writeEntitled
    const authoredReason = !settled
      ? REASONS.publishing
      : running
        ? REASONS.running
        : previewing
          ? REASONS.previewing
          : locked
            ? (writeLockNote || REASONS.writeLocked)
            : entBlocked
              ? REASONS.writeUnentitled
              : ''
    tools.push({
      id: `authored:${authored.name}`,
      label: authored.name,
      text: authored.name,
      icon: toolIcon(authored),
      size: toolPlacementSize(authored),
      title: authored.description || `Run ${authored.name}`,
      write: isWrite,
      disabled: !!authoredReason,
      reason: authoredReason,
      onClick: () => onUseAuthored?.(authored),
    })
  }
  return { id: 'author', label: 'Author', kind: 'group', tools }
}

// A reference panel this engine cannot back yet: present at the reference's
// place and width, every tool disabled with the same honest sentence, the
// panel's note carrying it too. Never a click handler that pretends.
function offTool(id, label, icon, size = 'small') {
  return { id, label, text: label, icon, size, title: label, disabled: true, reason: REASONS.notInEngine, onClick: () => {} }
}

// W4g-7b-05c: the four deferred controls carry their OWN reason instead of
// offTool's shared REASONS.notInEngine default. Written out as four literal
// records (never a shared helper taking `reason` as a parameter) so each
// keeps a literal `DEFERRED_REASONS.key` dot-path in source — the shape the
// honesty-ladder gate (check_honesty_ladder.mjs) can actually verify; a
// parameter or a bracketed lookup reads as a computed expression there and
// counts against its unverifiable-reason budget instead.

/**
 * The reference's Draw-tab panels beyond Draw and Modify (which the engine
 * consumer renders): Annotation, Layers widget (built by layersCluster),
 * Block, Properties, Groups, Clipboard — in the reference's order, at the
 * reference's shapes, all honestly unavailable.
 */
export function referencePanels() {
  const note = REASONS.notInEngine
  return [
    {
      // Dimensions and Leader have real engine tools, so their placeholders leave.
      id: 'annotation', label: 'Annotation', kind: 'group', note,
      tools: [
        offTool('annotation:text', 'Text', 'text', 'large'),
      ],
    },
    {
      // W4g-7b-02c: INSERT BLOCK is real with the engine flag on (App.jsx
      // drops this static cluster then; EngineRibbonClusters renders the
      // Block panel itself, the annotation seat idiom). CREATE BLOCK stays
      // the honest placeholder either way, with its own reason (W4g-7b-05c).
      id: 'block', label: 'Block', kind: 'group', note,
      tools: [
        offTool('draw:createBlock', 'Create Block', 'block-create', 'large'),
        offTool('block:insert', 'Insert Block', 'block-insert', 'large'),
      ],
    },
    {
      id: 'properties', label: 'Properties', kind: 'group', note,
      tools: [offTool('properties:match', 'Match', 'match', 'large')],
      widgets: [
        { id: 'prop-color', label: 'Color', value: 'ByLayer', disabled: true, reason: note },
        { id: 'prop-linetype', label: 'Linetype', value: 'ByLayer', disabled: true, reason: note },
        { id: 'prop-lineweight', label: 'Lineweight', value: 'ByLayer', disabled: true, reason: note },
      ],
    },
    {
      // W4g-7b-05c: groups are dictionary objects the contract does not
      // carry yet — the same reason for both, since neither exists without it.
      id: 'groups', label: 'Groups', kind: 'group', note,
      tools: [
        offTool('groups:group', 'Group', 'group', 'large'),
        offTool('groups:ungroup', 'Ungroup', 'ungroup', 'large'),
      ],
    },
    // W4g-5c: the engine renders a REAL Clipboard panel when the cad_edit
    // flag is on, and App drops this one then. It stays here for the flag-off
    // build, where the reference's row would otherwise lose a panel.
    {
      id: 'clipboard', label: 'Clipboard', kind: 'group', note,
      tools: [
        offTool('clipboard:paste', 'Paste', 'paste', 'large'),
        offTool('clipboard:cut', 'Cut', 'cut'),
        offTool('clipboard:copy', 'Copy', 'copy'),
      ],
    },
  ]
}
