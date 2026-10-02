// Pure predicates over leaf.walk-evidence.v1 + optional measurements. These
// fields are observations supplied by a collector, never executable recipes.
export const rubricSections = Object.freeze({
  Copy: Object.freeze([34, 52]), 'Ease of use': Object.freeze([59, 77]),
  Layout: Object.freeze([84, 110]), Chronology: Object.freeze([117, 134]),
  Streamlinedness: Object.freeze([141, 155]), Automation: Object.freeze([162, 178]),
  Familiarity: Object.freeze([185, 200]), Enjoyment: Object.freeze([207, 221]),
})
const get = (object, path) => path.split('.').reduce((value, key) =>
  value && Object.hasOwn(value, key) ? value[key] : undefined, object)
const text = (value) => typeof value === 'string'
const nonempty = (value) => text(value) && value.trim().length > 0
const bool = (value) => typeof value === 'boolean'
const number = (value) => Number.isFinite(value) && value >= 0
const coordinate = (value) => Number.isFinite(value)
const count = (value) => number(value) && Number.isInteger(value)
const id = (value) => text(value) && value.length <= 200 && /^[A-Za-z][A-Za-z0-9_-]*(?:[.:][A-Za-z0-9_-]+)*$/.test(value)
  && !hasCredential(value)
const strings = (value) => Array.isArray(value) && value.length > 0 && value.every(nonempty)
const rectangles = (value) => Array.isArray(value) && value.length >= 2
  && value.every((rect) => rect && id(rect.id) && coordinate(rect.x) && coordinate(rect.y)
    && number(rect.width) && rect.width > 0 && number(rect.height) && rect.height > 0)
  && new Set(value.map((rect) => rect.id)).size === value.length
const overlaps = (rects) => rects.some((a, i) => rects.slice(i + 1).some((b) =>
  a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height))
const credentialPatterns = [
  /\bBearer\s+[^\s"'<>;,]+/gi,
  /\bAuthorization\s*[:=]\s*[^\r\n]+/gi,
  /\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b/g,
  /\b(?:access_token|refresh_token|id_token|token|jwt|api[_-]?key|password|secret)\s*[:=]\s*[^\s,;]+/gi,
  /\b(?:set-cookie|cookie)\s*[:=][^\r\n]*/gi,
]
const hasCredential = (value) => credentialPatterns.some((pattern) => new RegExp(pattern).test(value))
const redact = (value) => credentialPatterns.reduce((result, pattern) =>
  result.replace(new RegExp(pattern), '[REDACTED]'), value).slice(0, 2000)
const safeValue = (value) => typeof value === 'string' ? redact(value)
  : Array.isArray(value) ? value.map(safeValue)
    : value && typeof value === 'object' ? Object.fromEntries(['id', 'x', 'y', 'width', 'height']
      .map((key) => [key, safeValue(value[key])])) : value
const contextFields = { schema: (value) => value === 'leaf.walk-evidence.v1',
  featureId: id, state: nonempty, viewport: nonempty, 'measurements.reference': nonempty }
const m = (fields) => Object.fromEntries(Object.entries(fields).map(([key, validator]) => [`measurements.${key}`, validator]))

function predicate(assertionId, dimensions, fields, check, parts, provenance = 'measured', applicability = () => true) {
  const validators = { ...contextFields, ...m(fields) }
  const requiredEvidenceFields = Object.freeze(Object.keys(validators))
  return Object.freeze({
    id: assertionId, assertion_id: assertionId, dimensions: Object.freeze(dimensions),
    rubric: Object.freeze(dimensions.map((dimension) => Object.freeze({
      dimension, source: 'ui-loop/rubric.md', lines: rubricSections[dimension],
    }))), requiredEvidenceFields,
    evaluate(evidence) {
      const missing = requiredEvidenceFields.filter((path) => !validators[path](get(evidence, path)))
      if (missing.length) return { assertion_id: assertionId, status: 'unknown', missing }
      if (!applicability(evidence)) return { assertion_id: assertionId, status: 'unknown', reason: 'Required observation modality is absent' }
      if (check(evidence.measurements)) return { assertion_id: assertionId, status: 'pass', provenance }
      // Only declared typed fields enter a finding; input prose never creates
      // commands, selectors, path grants, or further assertions.
      const observations = Object.fromEntries(requiredEvidenceFields.filter((path) => path.startsWith('measurements.'))
        .map((path) => [path.slice(13), safeValue(get(evidence, path))]))
      return { assertion_id: assertionId, status: 'finding', finding: {
        feature_id: evidence.featureId, summary: parts.observation, category: dimensions[0],
        severity: 'moderate', assertion_id: assertionId,
        evidence: {
          observation: parts.observation, evidence: observations, mechanism: parts.mechanism,
          consequence: parts.consequence, desired_outcome: parts.desired_outcome,
          reference: redact(evidence.measurements.reference), provenance,
          context: { viewport: redact(evidence.viewport), state: redact(evidence.state),
            journey_id: id(evidence.journeyId) ? evidence.journeyId : null },
          smallest_correction: parts.smallest_correction, reevaluation_check: parts.reevaluation_check,
        },
      } }
    },
  })
}
const parts = (observation, mechanism, consequence, desired_outcome, smallest_correction, reevaluation_check) =>
  ({ observation, mechanism, consequence, desired_outcome, smallest_correction, reevaluation_check })
function lens(name, predicates) {
  return Object.freeze({ name, dimensions: Object.freeze([...new Set(predicates.flatMap((p) => p.dimensions))]),
    requiredEvidenceFields: Object.freeze([...new Set(predicates.flatMap((p) => p.requiredEvidenceFields))]),
    predicates: Object.freeze(predicates), evaluate(evidence) {
      const results = predicates.map((p) => p.evaluate(evidence))
      return { lens: name, status: results.some((r) => r.status === 'finding') ? 'finding'
        : results.some((r) => r.status === 'unknown') ? 'unknown' : 'pass', results }
    } })
}

export const accessibility = lens('accessibility', [predicate('accessibility.dialog-keyboard', ['Layout', 'Familiarity'], {
  'overlay.role': nonempty, 'overlay.name': text, 'overlay.modal': bool,
  'focus.openTarget': nonempty, 'focus.actualOpenTarget': nonempty,
  'focus.tabRemainsInside': bool, 'focus.escapeClosed': bool,
  'focus.returnTarget': nonempty, 'focus.actualReturnTarget': nonempty,
}, (m) => m.overlay.role === 'dialog' && m.overlay.name.trim() !== '' && m.overlay.modal
  && m.focus.openTarget === m.focus.actualOpenTarget && m.focus.tabRemainsInside
  && m.focus.escapeClosed && m.focus.returnTarget === m.focus.actualReturnTarget,
parts('Dialog semantics or keyboard transitions failed.', 'The overlay does not preserve its declared dialog and focus contract.',
  'Keyboard users can lose the task context.', 'A named modal dialog contains focus and returns it after Escape.',
  'Repair the observed semantic or focus transition.', 'Reopen, tab through, and dismiss the same overlay.'))])

export const endUser = lens('end-user', [predicate('end-user.expected-effect', ['Ease of use', 'Automation'], {
  'task.expectedEffect': nonempty, 'task.observedEffect': text, 'task.manualApplyCount': count,
}, (m) => m.task.expectedEffect === m.task.observedEffect && m.task.manualApplyCount === 0,
parts('The intended task did not complete directly.', 'The observed effect differs or a second manual apply is required.',
  'Users must repeat or repair the task.', 'The initial action produces the expected effect.',
  'Remove the observed extra apply or repair the effect.', 'Repeat the task and inspect its actual result.'))])

export const mobileTouch = lens('mobile-touch', [predicate('mobile-touch.reachable-target', ['Layout', 'Ease of use'], {
  'input.touch': bool, 'input.coarsePointer': bool, 'control.reachable': bool, 'control.clipped': bool,
  'control.rect.x': coordinate, 'control.rect.y': coordinate, 'control.rect.width': number, 'control.rect.height': number,
  'viewport.width': number, 'viewport.height': number,
}, (m) => m.control.reachable && !m.control.clipped && m.control.rect.width >= 44 && m.control.rect.height >= 44
  && m.control.rect.x >= 0 && m.control.rect.y >= 0
  && m.control.rect.x + m.control.rect.width <= m.viewport.width
  && m.control.rect.y + m.control.rect.height <= m.viewport.height,
parts('The required phone control is too small, clipped, or unreachable.', 'Touch activation or measured control bounds violate Layout rule 44 px.',
  'The next action cannot be reliably tapped.', 'The control is reachable and fully visible with a target at least 44 px.',
  'Adjust the affected control bounds or disclosure.', 'Measure bounds and activate with touch on phone.'), 'measured',
  (e) => e.viewport === 'phone' && e.measurements.input.touch && e.measurements.input.coarsePointer)])

export const opsSre = lens('ops-sre', [predicate('ops-sre.wait-recovery', ['Chronology', 'Automation'], {
  'wait.observed': bool, 'wait.progress': text, 'wait.reason': text,
  'recovery.failureObserved': bool, 'recovery.action': text, 'recovery.outcome': text, 'recovery.outcomeVisible': bool,
}, (m) => (!m.wait.observed || nonempty(m.wait.progress) || nonempty(m.wait.reason))
  && (!m.recovery.failureObserved || (nonempty(m.recovery.action) && nonempty(m.recovery.outcome) && m.recovery.outcomeVisible)),
parts('An observed wait or failure lacks an exposed outcome.', 'Progress, explanation, or recovery outcome is absent from the observed trace.',
  'Users cannot determine whether to wait or recover.', 'Waits explain their state and failures expose the recovery outcome.',
  'Expose the missing state in the affected flow.', 'Reproduce the wait and failure and observe recovery.'), 'measured',
  (e) => e.measurements.wait.observed || e.measurements.recovery.failureObserved)])

export const performance = lens('performance', [predicate('performance.declared-baseline', ['Ease of use', 'Streamlinedness'], {
  'baseline.reference': nonempty, 'baseline.maxDurationMs': number, 'baseline.maxRequiredSteps': count,
  'task.durationMs': number, 'task.requiredSteps': count, 'wait.observed': bool, 'wait.progress': text, 'wait.reason': text,
}, (m) => m.task.durationMs <= m.baseline.maxDurationMs && m.task.requiredSteps <= m.baseline.maxRequiredSteps
  && (!m.wait.observed || nonempty(m.wait.progress) || nonempty(m.wait.reason)),
parts('The task exceeds its declared baseline or has an unexplained wait.', 'Measured duration or required steps exceed the supplied task rule, or wait state is silent.',
  'The task consumes unnecessary effort or time.', 'Meet the supplied baseline and explain waits.',
  'Correct the observed excess step, delay, or wait message.', 'Repeat against the same supplied baseline.'))])

export const qcDev = lens('qc-dev', [predicate('qc-dev.action-result-sequence', ['Automation', 'Chronology'], {
  'sequence.action': nonempty, 'sequence.expectedResult': nonempty, 'sequence.observedResult': text,
  'sequence.expectedOrder': strings, 'sequence.observedOrder': strings,
}, (m) => m.sequence.expectedResult === m.sequence.observedResult
  && m.sequence.expectedOrder.includes(m.sequence.action)
  && m.sequence.expectedOrder.indexOf(m.sequence.expectedResult) > m.sequence.expectedOrder.indexOf(m.sequence.action)
  && m.sequence.expectedOrder.length === m.sequence.observedOrder.length
  && m.sequence.expectedOrder.every((event, i) => event === m.sequence.observedOrder[i]),
parts('A named action has an incorrect result or sequence.', 'Observed events diverge from the declared action/result trace.',
  'The flow can expose stale or premature state.', 'The action produces its result in the declared order.',
  'Repair the first divergent event or result.', 'Capture the same named action and ordered result trace.'))])

export const redHat = lens('red-hat', [predicate('red-hat.failure-refusal', ['Copy', 'Automation'], {
  'failure.visibleText': nonempty, 'refusal.observed': bool, 'refusal.protectedBefore': text, 'refusal.protectedAfter': text,
}, (m) => !hasCredential(m.failure.visibleText) && m.refusal.protectedBefore === m.refusal.protectedAfter,
parts('Failure copy exposes credential material or refusal changes a protected effect.', 'Visible failure text or before/after protected state violates the refusal boundary.',
  'Users may disclose credentials or change protected work.', 'Redacted failure text and unchanged protected effects.',
  'Redact the affected message or stop the refused effect.', 'Repeat refusal and inspect redacted copy and protected state.'), 'measured',
  (e) => e.measurements.refusal.observed)])

export const support = lens('support', [predicate('support.error-recovery', ['Copy', 'Ease of use'], {
  'error.observed': bool, 'error.message': text, 'error.whatHappened': text,
  'error.recoveryName': text, 'error.recoveryReachable': bool,
}, (m) => nonempty(m.error.message) && nonempty(m.error.whatHappened)
  && nonempty(m.error.recoveryName) && m.error.recoveryReachable,
parts('An observed error lacks explanation or reachable recovery.', 'Error copy or the named recovery control is absent or inaccessible.',
  'Users cannot understand or resolve the failure.', 'Explain what happened and expose a reachable recovery action.',
  'Repair the affected explanation or recovery control.', 'Trigger the same error and reach its recovery action.'), 'measured',
  (e) => e.measurements.error.observed)])

export const uiUxDev = lens('ui-ux-dev', [
  predicate('ui-ux-dev.completion-proxy', ['Layout', 'Copy', 'Enjoyment'], {
    'layout.controls': rectangles, 'completion.observed': bool,
    'completion.acknowledgement': text, 'completion.inPlace': bool,
  }, (m) => !overlaps(m.layout.controls) && nonempty(m.completion.acknowledgement) && m.completion.inPlace,
  parts('Controls overlap or completed work lacks an in-place acknowledgement.', 'Measured layout or acknowledgement omits a clear completion cue.',
    'Completion confidence may be reduced; this is an enjoyment proxy, not measured dissatisfaction.',
    'Separate controls and acknowledge completed work in place.', 'Repair the affected overlap or completion cue.',
    'Complete the same task and inspect layout and acknowledgement.'), 'inferred', (e) => e.measurements.completion.observed),
  predicate('ui-ux-dev.human-reaction', ['Enjoyment'], {
    'humanFeedback.quote': nonempty, 'humanFeedback.reaction': (v) => ['satisfied', 'dissatisfied'].includes(v),
    'humanFeedback.reference': nonempty,
  }, (m) => m.humanFeedback.reaction === 'satisfied',
  parts('Supplied human feedback reports dissatisfaction.', 'The supplied participant reaction identifies dissatisfaction with the observed task.',
    'The participant reports an unsatisfactory experience.', 'Resolve the difficulty described by supplied feedback.',
    'Review the supplied feedback for the smallest task-specific correction.', 'Collect new human feedback after that correction.')),
])

export const unfamiliar = lens('unfamiliar', [predicate('unfamiliar.next-step', ['Copy', 'Familiarity', 'Ease of use'], {
  'primary.name': text, 'primary.role': nonempty, 'primary.reachable': bool,
  'navigation.role': nonempty, 'navigation.name': text, 'navigation.nextStepFound': bool,
}, (m) => nonempty(m.primary.name) && ['button', 'link'].includes(m.primary.role) && m.primary.reachable
  && m.navigation.role === 'navigation' && nonempty(m.navigation.name) && m.navigation.nextStepFound,
parts('The labeled primary action or conventional navigation does not expose the next step.', 'Accessible control names, roles, or observed discoverability break the navigation contract.',
  'An unfamiliar user cannot find the next task action.', 'Label the primary action and provide discoverable conventional navigation.',
  'Repair the affected label or navigation control.', 'Find and reach the next step using visible labels.'))])

export const lenses = Object.freeze([accessibility, endUser, mobileTouch, opsSre, performance, qcDev, redHat, support, uiUxDev, unfamiliar])
export const predicates = Object.freeze(lenses.flatMap((entry) => entry.predicates))
export function evaluateLenses(evidence) {
  return lenses.map((entry) => entry.evaluate(evidence))
}
