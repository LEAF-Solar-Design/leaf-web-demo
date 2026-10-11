import { mintCorrelationId } from '../runIntent.js'
import { createSolarDrawingBinding, solarDrawingBindingFromRunContext, sameSolarDrawingBinding } from './solarDrawingBinding.js'

export const PROJECT_SOLAR_APP_REASONS = Object.freeze({
  EQUIPMENT_VOLTAGE_UNKNOWN: 'The inverter has no verified string voltage.',
  EQUIPMENT_POWER_UNKNOWN: 'The inverter has no verified module power.',
  EQUIPMENT_ASSIGNMENT_REQUIRED: 'Homeruns require valid equipment assignments. Review the electrical reasons on the inverter.',
  VALID_SETTINGS_REQUIRED: 'Feeders require valid L2 collector settings.',
  STRING_COLLECTORS_REQUIRED: 'Feeders require string collectors.',
  ROUTING_TOPOLOGY_REQUIRED: 'The schedule requires routing topology.',
  COMPLETE_ROUTING_REQUIRED: 'The schedule requires complete current homeruns.',
  SOLAR_OUTPUT_NOT_CURRENT: 'Electrical output is unavailable because this Solar design is not current.',
  request_refused: 'The server refused this Solar request. Review the reported code.',
  output_unreadable: 'The run published a drawing version, but that version could not be read. Refresh before continuing.',
  project_unavailable: 'Project Solar runs are unavailable in this view.',
  sign_in_required: 'Sign in to use Solar in this project.',
  project_required: 'Open a project drawing version to use project Solar.',
  binding_invalid: 'Select a valid project drawing version to use Solar.',
  scope_stale: 'The project Solar scope changed. Refresh before continuing.',
  scope_pending: 'Project Solar is preparing this drawing version.',
  checkout_required: 'Take the project checkout before changing this drawing.',
  checkout_held: 'Another session holds the project checkout.',
  checkout_expired: 'The project checkout expired. Take it again before continuing.',
  checkout_pending: 'Wait for the project checkout request to finish.',
  checkout_unavailable: 'Project checkout details are unavailable. Refresh before continuing.',
  project_tools_unavailable: 'Project tools are unavailable.',
  unsupported_form: 'This tool has no project form in this view.',
})

export function projectSolarAppReason(code) {
  switch (code) {
    case 'EQUIPMENT_VOLTAGE_UNKNOWN': return PROJECT_SOLAR_APP_REASONS.EQUIPMENT_VOLTAGE_UNKNOWN
    case 'EQUIPMENT_POWER_UNKNOWN': return PROJECT_SOLAR_APP_REASONS.EQUIPMENT_POWER_UNKNOWN
    case 'EQUIPMENT_ASSIGNMENT_REQUIRED': return PROJECT_SOLAR_APP_REASONS.EQUIPMENT_ASSIGNMENT_REQUIRED
    case 'VALID_SETTINGS_REQUIRED': return PROJECT_SOLAR_APP_REASONS.VALID_SETTINGS_REQUIRED
    case 'STRING_COLLECTORS_REQUIRED': return PROJECT_SOLAR_APP_REASONS.STRING_COLLECTORS_REQUIRED
    case 'ROUTING_TOPOLOGY_REQUIRED': return PROJECT_SOLAR_APP_REASONS.ROUTING_TOPOLOGY_REQUIRED
    case 'COMPLETE_ROUTING_REQUIRED': return PROJECT_SOLAR_APP_REASONS.COMPLETE_ROUTING_REQUIRED
    case 'SOLAR_OUTPUT_NOT_CURRENT': return PROJECT_SOLAR_APP_REASONS.SOLAR_OUTPUT_NOT_CURRENT
    case 'request_refused': return PROJECT_SOLAR_APP_REASONS.request_refused
    case 'output_unreadable': return PROJECT_SOLAR_APP_REASONS.output_unreadable
    case 'project_execution_disabled':
    case 'project_unavailable': return PROJECT_SOLAR_APP_REASONS.project_unavailable
    case 'sign_in_required': return PROJECT_SOLAR_APP_REASONS.sign_in_required
    case 'project_required': return PROJECT_SOLAR_APP_REASONS.project_required
    case 'SIP_R7_BINDING_INVALID':
    case 'binding_invalid': return PROJECT_SOLAR_APP_REASONS.binding_invalid
    case 'SIP_R7_SCOPE_STALE':
    case 'scope_stale': return PROJECT_SOLAR_APP_REASONS.scope_stale
    case 'scope_pending': return PROJECT_SOLAR_APP_REASONS.scope_pending
    case 'checkout_required': return PROJECT_SOLAR_APP_REASONS.checkout_required
    case 'checkout_held': return PROJECT_SOLAR_APP_REASONS.checkout_held
    case 'checkout_expired': return PROJECT_SOLAR_APP_REASONS.checkout_expired
    case 'checkout_pending': return PROJECT_SOLAR_APP_REASONS.checkout_pending
    case 'checkout_unavailable': return PROJECT_SOLAR_APP_REASONS.checkout_unavailable
    case 'project_tools_unavailable': return PROJECT_SOLAR_APP_REASONS.project_tools_unavailable
    case 'unsupported_form': return PROJECT_SOLAR_APP_REASONS.unsupported_form
    case null:
    case undefined: return null
    default: return PROJECT_SOLAR_APP_REASONS.request_refused
  }
}

const credential = new RegExp('lco1\\.[0-9a-f]{64}|\\bbearer\\s+\\S+|eyJ[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+', 'i')
const boundedKey = (value) => typeof value === 'string' && value.trim().length > 0 &&
  new TextEncoder().encode(value).byteLength <= 256 && !credential.test(value)

export function createProjectSolarAuthority() {
  let actor = null
  let generation = null
  let key = null
  const clear = () => { actor = null; generation = null; key = null }
  return Object.freeze({
    update(session) {
      try {
        const authenticated = session?.authenticated
        const actorId = session?.actorId
        const nextGeneration = session?.generation
        if (authenticated !== true || typeof actorId !== 'string' || !actorId.trim() || credential.test(actorId) ||
            !Number.isSafeInteger(nextGeneration) || nextGeneration < 0) {
          clear()
          return null
        }
        if (key === null || actor !== actorId || generation !== nextGeneration) {
          key = 'project-solar:' + mintCorrelationId()
          actor = actorId
          generation = nextGeneration
        }
        return key
      } catch {
        clear()
        return null
      }
    },
    clear,
    getKey: () => key,
  })
}

export function deriveProjectSolarAppScope({ projectMode, runContext, authorityKey }) {
  const invalid = (reason) => Object.freeze({ mode: 'invalid-project', binding: null, authorityKey: null, reason })
  try {
    const projectBearing = runContext?.projectId != null || runContext?.orgId != null ||
      runContext?.drawingArtifactId != null
    if (projectMode === false && !projectBearing) {
      return Object.freeze({ mode: 'standalone', binding: undefined, authorityKey: null, reason: 'project_required' })
    }
    if (!boundedKey(authorityKey)) return invalid('sign_in_required')
    if (projectMode !== true) return invalid('binding_invalid')
    const binding = solarDrawingBindingFromRunContext(runContext)
    return Object.freeze({ mode: 'project', binding, authorityKey, reason: null })
  } catch { return invalid('binding_invalid') }
}

export function projectSolarCheckoutState({ scope, snapshot, now = Date.now() }) {
  const empty = (reason, error = null) => Object.freeze({ checkout: null, ownsCheckout: false,
    pending: false, error, leaseRevision: null, canTake: false, canRelease: false, reason })
  try {
    const inactiveReason = scope?.reason === 'project_required' ? 'project_required' :
      scope?.reason === 'sign_in_required' ? 'sign_in_required' :
        scope?.reason === 'binding_invalid' ? 'binding_invalid' : 'scope_stale'
    if (scope?.mode !== 'project' || !boundedKey(scope.authorityKey) ||
        !sameSolarDrawingBinding(scope.binding, snapshot?.binding) ||
        scope.authorityKey !== snapshot?.authorityKey) return empty(inactiveReason)
    const { checkout: lease, ownsCheckout, pending, error, leaseRevision } = snapshot
    const code = new RegExp('^[A-Za-z][A-Za-z0-9_]{0,127}$')
    if (typeof ownsCheckout !== 'boolean' || typeof pending !== 'boolean' ||
        !Number.isSafeInteger(leaseRevision) || leaseRevision < 0 || !Number.isFinite(now) ||
        (error !== null && (typeof error !== 'string' || !code.test(error) || credential.test(error)))) {
      return empty('checkout_unavailable', 'checkout_unavailable')
    }
    if (error !== null) return empty('checkout_unavailable', error)
    let checkout = null
    let expires = null
    if (lease !== null) {
      const holder = lease?.holder
      const fence = lease?.fence
      const acquired = lease?.acquired
      const expiry = lease?.expires
      if (typeof holder !== 'string' || !holder.trim() || Array.from(holder).length > 200 || credential.test(holder) ||
          typeof fence !== 'string' || !new RegExp('^[0-9]{1,128}$').test(fence) ||
          typeof acquired !== 'string' || typeof expiry !== 'string' || acquired.length > 64 || expiry.length > 64 ||
          credential.test(acquired) || credential.test(expiry) ||
          !Number.isFinite(Date.parse(acquired)) || !Number.isFinite(Date.parse(expiry)) ||
          Date.parse(expiry) < Date.parse(acquired)) return empty('checkout_unavailable', 'checkout_unavailable')
      checkout = Object.freeze({ holder, fence, acquired, expires: expiry })
      expires = Date.parse(expiry)
    }
    const expired = checkout !== null && expires <= now
    const owned = checkout !== null && !expired && ownsCheckout
    return Object.freeze({ checkout, ownsCheckout: owned, pending, error: null, leaseRevision,
      canTake: !pending && (checkout === null || expired), canRelease: !pending && owned,
      reason: pending ? 'checkout_pending' : checkout === null ? 'checkout_required' :
        expired ? 'checkout_expired' : owned ? null : 'checkout_held' })
  } catch { return empty('checkout_unavailable', 'checkout_unavailable') }
}

// The one shape of a client snapshot the app may keep in React state: the seven
// fields projectSolarCheckoutState reads and nothing else. An extra field, a
// non-object lease or a credential-shaped string never enters state; each is
// dropped or replaced by a value projectSolarCheckoutState refuses the same way.
export function projectSolarScopeSnapshot(raw) {
  const text = (value) => typeof value === 'string' && !credential.test(value) ? value : undefined
  try {
    let binding = null
    try { binding = createSolarDrawingBinding(raw?.binding) } catch { binding = null }
    const lease = raw?.checkout
    const checkout = lease === null ? null : lease !== undefined && typeof lease === 'object'
      ? Object.freeze({ holder: text(lease.holder), fence: text(lease.fence),
        acquired: text(lease.acquired), expires: text(lease.expires) })
      : Object.freeze({})
    const error = raw?.error
    return Object.freeze({
      binding,
      authorityKey: boundedKey(raw?.authorityKey) ? raw.authorityKey : null,
      checkout,
      ownsCheckout: typeof raw?.ownsCheckout === 'boolean' ? raw.ownsCheckout : undefined,
      pending: typeof raw?.pending === 'boolean' ? raw.pending : undefined,
      error: error === null ? null : text(error) ?? false,
      leaseRevision: typeof raw?.leaseRevision === 'number' ? raw.leaseRevision : undefined,
    })
  } catch {
    return Object.freeze({ binding: null, authorityKey: null, checkout: null, ownsCheckout: undefined,
      pending: undefined, error: false, leaseRevision: undefined })
  }
}
