export const ERROR_ACTOR_LABELS = {
  user: 'You',
  workspace_admin: 'Workspace admin',
  approver: 'Approver',
  operator: 'Support',
  service: 'Leaf service',
}

export function errorPresentation(input, fallback = 'Something went wrong.', reasonCode = '') {
  if (!input) {
    return { message: fallback, code: '', nextAction: '', actor: '', retryable: false }
  }
  if (typeof input === 'string') {
    return { message: input, code: '', nextAction: '', actor: '', retryable: false }
  }

  const envelope = input?.body?.error || input?.error || input
  // Keep a specific product cause ahead of the envelope's generic error class.
  // Authoring can still supply its separate reason code explicitly.
  const opaqueReasonCode = [reasonCode, envelope?.reason_code].find(
    (value) => typeof value === 'string' && /^[A-Za-z][A-Za-z0-9_-]{0,127}$/.test(value),
  ) || ''
  return {
    message: envelope?.message || input?.message || fallback,
    code: opaqueReasonCode || envelope?.error_code || envelope?.code || input?.errorCode || '',
    nextAction: envelope?.next_action || input?.nextAction || '',
    actor: envelope?.actor || input?.actor || '',
    retryable: Boolean(envelope?.retryable ?? input?.retryable),
  }
}

export function errorActorLabel(actor) {
  return ERROR_ACTOR_LABELS[actor] || actor || ''
}

const REFUSAL_ACTIONS = {
  SOLAREDGE_GRID_UNMATCHED: 'Redraw this group.',
  SOLAREDGE_GRID_AMBIGUOUS: 'Select one grid.',
  LAYER_NOT_FOUND: 'Choose an existing layer.',
  PANEL_LAYER_FILTER_REQUIRED: 'Set panel_layer_contains in Solar settings.',
  NO_PANELS_RECOGNISED: 'Choose a matching panel layer filter.',
  CLOUD_REQUEST_REJECTED: 'Correct the cloud request.',
  SOLVE_TIMEOUT: 'Reconcile the solve status before retrying.',
  solve_outcome_unknown: 'Reconcile the solve status before retrying.',
  INVALID_SEED_REQUEST: 'Correct the Solar setup field.',
  UNKNOWN_UNITS: 'Set recognised drawing units.',
  SIP_R1_CHECKOUT_EXPIRED: 'Take the drawing checkout again.',
  IMPORT_PDF_MALFORMED: 'Upload a valid PDF.',
}

const REFUSAL_CAUSES = {
  LAYER_NOT_FOUND: 'Layer not found',
  PANEL_LAYER_FILTER_REQUIRED: 'Panel layer filter is missing',
  NO_PANELS_RECOGNISED: 'No panels matched the panel layer filter',
  CLOUD_REQUEST_REJECTED: 'Cloud request rejected',
  SOLVE_TIMEOUT: 'Solve timed out',
  solve_outcome_unknown: 'Solve outcome unknown',
  INVALID_SEED_REQUEST: 'Solar setup field is invalid',
  UNKNOWN_UNITS: 'Drawing units are not recognised',
  SIP_R1_CHECKOUT_EXPIRED: 'Drawing checkout expired',
  IMPORT_PDF_MALFORMED: 'PDF is malformed',
}

// Accessors are not response data, including on an actual thrown Error.
function ownData(value, key) {
  if (!value || typeof value !== 'object') return undefined
  const descriptor = Object.getOwnPropertyDescriptor(value, key)
  return descriptor && Object.hasOwn(descriptor, 'value') ? descriptor.value : undefined
}

function plainRefusalText(value) {
  if (typeof value !== 'string' || value.length > 2048) return ''
  return value.replace(/<[^>]*>/g, '').replace(/[<>]/g, '')
    .replace(/[\p{Cc}\p{Cf}\p{Cs}]/gu, '').replace(/\s+/g, ' ').trim()
}

// A compact notice is separate from the legacy presentation's exact field contract.
// Walk only envelope links, with a fixed work budget even for cycles or deep bodies.
export function compactRefusal(input) {
  try {
    const pending = [input]
    const seen = new Set()
    const envelopes = []
    while (pending.length && seen.size < 32) {
      const value = pending.shift()
      if (!value || typeof value !== 'object' || seen.has(value)) continue
      seen.add(value)
      envelopes.push(value)
      for (const key of ['body', 'error']) {
        const child = ownData(value, key)
        if (child && typeof child === 'object') pending.push(child)
      }
    }
    let envelope
    let code
    // Specific reasons win over generic classes at any envelope level.
    for (const key of ['reason_code', 'error_code', 'code']) {
      envelope = envelopes.find((value) => {
        const candidate = ownData(value, key)
        return typeof candidate === 'string' && (key === 'reason_code'
          ? /^[A-Za-z][A-Za-z0-9_-]{0,127}$/.test(candidate)
          : Object.hasOwn(REFUSAL_ACTIONS, candidate))
      })
      if (envelope) {
        code = ownData(envelope, key)
        break
      }
    }
    if (!envelope || !Object.hasOwn(REFUSAL_ACTIONS, code)) return null
    const action = REFUSAL_ACTIONS[code]
    let cause
    if (code.startsWith('SOLAREDGE_GRID_')) {
      const unmatched = code === 'SOLAREDGE_GRID_UNMATCHED'
      const frame = ownData(envelope, 'frame_ref')
      const candidates = ownData(envelope, 'candidate_ids')
      const token = ownData(envelope, 'action')
      let valid = typeof frame === 'string' && /^[A-Za-z0-9_.:-]{1,128}$/.test(frame)
        && Array.isArray(candidates) && candidates.length <= 20
        && token === (unmatched ? 'redraw_frame' : 'pick_candidate')
      const ids = []
      if (valid) {
        for (let index = 0; index < candidates.length; index += 1) {
          const id = ownData(candidates, String(index))
          if (!Number.isSafeInteger(id) || id < 0 || ids.includes(id)) valid = false
          ids.push(id)
        }
        valid = valid && (unmatched ? ids.length === 0 : ids.length > 0)
      }
      const problem = unmatched ? 'no matching SolarEdge grid' : 'multiple SolarEdge grids'
      cause = valid
        ? `Group ${frame}: ${problem}${unmatched ? '' : ` (candidates ${ids.join(', ')})`}.`
        : `Group identity unresolved: ${problem}.`
      if (`${cause} ${action}`.length > 160) cause = `Group identity unresolved: ${problem}.`
    } else {
      const message = plainRefusalText(ownData(envelope, 'message'))
      // The server sometimes repeats instructions in both message and next_action.
      // Keep the causal sentence(s), then append exactly one known action.
      cause = message.split(/(?:^|[.!?]\s+)(?:Next:|Try\b|Retry\b|Set\b|Choose\b|Select\b|Redraw\b|Take\b|Upload\b|Correct\b|Reconcile\b|Check\b|Wait\b|Run\b|Contact\b)/i)[0].trim()
      const unnamedCause = cause.replace(/[.!?]$/, '').toLowerCase()
      if (!cause || unnamedCause === REFUSAL_CAUSES[code].toLowerCase()
        || (code === 'INVALID_SEED_REQUEST' && unnamedCause === 'solar setup was refused')
        || `${cause} ${action}`.length > 160) {
        cause = `${REFUSAL_CAUSES[code]}; affected object identity unresolved.`
      }
      if (!/[.!?]$/.test(cause)) cause += '.'
      if (`${cause} ${action}`.length > 160) {
        cause = `${REFUSAL_CAUSES[code]}; affected object identity unresolved.`
      }
    }
    return { text: `${cause} ${action}`, code,
      reconcileFirst: code === 'SOLVE_TIMEOUT' || code === 'solve_outcome_unknown' }
  } catch {
    return null
  }
}
