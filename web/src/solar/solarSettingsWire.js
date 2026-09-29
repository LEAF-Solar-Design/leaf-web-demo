export const SOLAR_SETTINGS_TOOL_NAME = 'solar-settings'
export const SEED_OPENABLE_REASON_CODES = Object.freeze(['graph_seed_required', 'persisted_graph_unavailable'])

export function isSolarReasonCode(value) {
  return typeof value === 'string' && /^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(value)
}

export function canOpenSolarSettingsForm(name, availability) {
  return name === SOLAR_SETTINGS_TOOL_NAME && availability !== null &&
    typeof availability === 'object' && !Array.isArray(availability) &&
    availability.entitled === true && availability.implemented === true &&
    availability.engine_ready === true && Array.isArray(availability.refusal_reasons) &&
    availability.refusal_reasons.length === 1 &&
    SEED_OPENABLE_REASON_CODES.includes(availability.refusal_reasons[0])
}

export function canInitializeSolarSettings(name, availability, params) {
  return canOpenSolarSettingsForm(name, availability) && params !== null &&
    typeof params === 'object' &&
    (Object.getPrototypeOf(params) === Object.prototype || Object.getPrototypeOf(params) === null) &&
    Object.prototype.hasOwnProperty.call(params, 'initialize')
}

export function solarSettingsFormChoice({ enabled, mock, toolName, context }) {
  const standalone = enabled === true && mock === false &&
    context !== null && typeof context === 'object' && context.projectId === null &&
    [Object.prototype, null].includes(Object.getPrototypeOf(context)) &&
    typeof context.drawingId === 'string' && context.drawingId.length > 0
  if (!standalone) return 'generic'
  if (toolName === SOLAR_SETTINGS_TOOL_NAME) return 'typed'
  return toolName === 'solar-string-conductors' ? 'conductors' : 'generic'
}

export function solarSettingsScope({ enabled, mock, profile, context }) {
  if (enabled === true && mock === false && profile === 'solar' && context?.projectId === null &&
      typeof context.drawingId === 'string' && context.drawingId.length > 0) {
    return { drawingId: context.drawingId, drawingVersion: context.drawingVersion ?? null, solarSettingsFormEnabled: true }
  }
  return { drawingId: undefined, drawingVersion: undefined, solarSettingsFormEnabled: false }
}

export function catalogRunOverlays({ enabled, toolName, selectedHandle, isWrite }) {
  if (!selectedHandle || (enabled === true && [SOLAR_SETTINGS_TOOL_NAME, 'solar-string-conductors'].includes(toolName))) return {}
  return { target_handle: selectedHandle, ...(isWrite ? { handle: selectedHandle } : {}) }
}

export function solarSettingsLoaders({ getDrawingIntake, getDrawingVersions }) {
  return Object.freeze({
    readIntake: (id, version) => getDrawingIntake(false, id, version),
    readVersions: (id) => getDrawingVersions(false, id),
  })
}

export const SOLAR_SETTINGS_RUN_MESSAGES = Object.freeze({
  invalid_seed_parent: 'This saved drawing version cannot be used to start a Solar design.',
  not_applied: 'Solar settings were not applied.',
})

export function solarSettingsRunFeedback({ association, context }) {
  if (!association || typeof association !== 'object' ||
      association.drawingId !== context?.drawingId ||
      association.drawingVersion !== context?.drawingVersion || context?.projectId != null ||
      !association.envelope || typeof association.envelope !== 'object' || association.envelope.ok === true) return null
  const envelope = association.envelope
  const code = [envelope.reason_code, envelope.error?.message, envelope.error?.error_code].find(isSolarReasonCode)
  if (code?.toLowerCase() === 'invalid_seed_parent') {
    return { text: SOLAR_SETTINGS_RUN_MESSAGES.invalid_seed_parent, code: 'INVALID_SEED_PARENT' }
  }
  return { text: SOLAR_SETTINGS_RUN_MESSAGES.not_applied, code: code?.toUpperCase() ?? null }
}
