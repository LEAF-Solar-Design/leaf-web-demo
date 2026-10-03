import { createHash } from 'node:crypto'
import { readFileSync, realpathSync, writeFileSync } from 'node:fs'
import { resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { buildFeatureMap } from './featureMap.mjs'
import { stateRecipe } from '../e2e/walk/probes.mjs'
import { assertionAlias, packUxEvidence, uxObservation } from '../e2e/walk/uxEvidence.mjs'

export const MAX_RECEIPT_BYTES = 1024 * 1024
export const MAX_INPUT_BYTES = 16 * MAX_RECEIPT_BYTES
const repoRoot = fileURLToPath(new URL('../../', import.meta.url))
const artifactsRoot = resolve(repoRoot, 'web/artifacts')
const schema = JSON.parse(readFileSync(new URL('./fixtures/leaf.studio-walk.v1.schema.json', import.meta.url), 'utf8'))
const runnerFile = 'web/e2e/walk/fixtures.mjs'
const journeyFile = 'web/e2e/walk/journeys/first-run-open.spec.mjs'
const censusFile = 'web/e2e/walk/control-inventory.spec.mjs'
const sources = new Map([runnerFile, journeyFile, censusFile].map((file) => [file,
  readFileSync(resolve(repoRoot, file), 'utf8').split(/\r?\n/)]))
const wireVerdict = { pass: 'PASS', fail: 'FAIL', unsupported_local: 'UNAVAILABLE', staging_only: 'UNAVAILABLE', queued: 'UNKNOWN' }
const compare = (a, b) => a < b ? -1 : a > b ? 1 : 0
const count = (value) => Array.isArray(value) ? value.length : 0

// Bound structure before serialization as well as bytes afterwards. In particular,
// deep evidence and circular input must fail explicitly, not exhaust the stack.
function boundInput(value) {
  const pending = [{ value, depth: 0 }]
  const ancestors = new WeakSet()
  let bytes = 0
  let nodes = 0
  while (pending.length) {
    const item = pending.pop()
    if (item.leave) {
      ancestors.delete(item.value)
      continue
    }
    if (++nodes > 250_000 || item.depth > 32) throw new Error('Report input exceeds structural bounds')
    if (typeof item.value === 'string') bytes += Buffer.byteLength(item.value)
    else if (item.value && typeof item.value === 'object') {
      if (ancestors.has(item.value)) throw new Error('Report input must be an acyclic JSON tree')
      ancestors.add(item.value)
      // Shared references serialize at every occurrence; only an active
      // ancestor is a cycle. Keep counting each occurrence towards bounds.
      pending.push({ value: item.value, leave: true })
      for (const [key, child] of Object.entries(item.value)) {
        bytes += Buffer.byteLength(key) + 4
        pending.push({ value: child, depth: item.depth + 1 })
      }
    } else bytes += 8
    if (bytes > MAX_INPUT_BYTES) throw new Error('Report input exceeds byte bound')
  }
}

export function redact(value, limit = 2000) {
  return String(value ?? '')
    .replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '')
    .replace(/\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b/g, '[REDACTED]')
    .replace(/\bBearer\s+[^\s"'<>;,]+/gi, 'Bearer [REDACTED]')
    .replace(/((?:[?&]|\b)(?:access_token|refresh_token|id_token|token|jwt|api[_-]?key|signature|sig|code)=)[^\s&#"'<>]*/gi, '$1[REDACTED]')
    .replace(/\b(?:set-cookie|cookie)\s*[:=][^\r\n]*/gi, 'cookie: [REDACTED]')
    .replace(/["']?\bstorageState["']?\s*[:=][^\r\n]*/gi, 'storageState: [REDACTED]')
    .replace(/(?:[A-Za-z]:[\\/]|\\\\)[^\s"'<>\r\n)]+/g, '[REDACTED_PATH]')
    .replace(/\/(?:Users|home|root|tmp|private|var|mnt)\/[^\s"'<>\r\n)]+/gi, '[REDACTED_PATH]')
    .slice(0, limit)
}

function clean(value, depth = 0) {
  if (depth > 32) throw new Error('Receipt exceeds depth bound')
  if (typeof value === 'string') return redact(value)
  if (Array.isArray(value)) return value.map((item) => clean(item, depth + 1) ?? null)
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.keys(value).sort(compare).map((key) => {
      if (key.length > 2000) throw new Error('Receipt key exceeds string bound')
      if (/^(?:__proto__|prototype|constructor)$/i.test(key)) throw new Error('Unsafe receipt key')
      // These keys never carry data into the controller, including identity blobs.
      if (/cookie|storageState|authorization|password|secret|token|jwt|owned|command/i.test(key)) {
        return [redact(key), '[REDACTED]']
      }
      return [redact(key), clean(value[key], depth + 1)]
    }).filter(([key, child]) => child !== undefined && !/owned|command/i.test(key)))
  }
  if (['function', 'symbol', 'bigint'].includes(typeof value)) throw new Error('Receipt must contain JSON data only')
  if (typeof value === 'number' && !Number.isFinite(value)) throw new Error('Receipt contains nonfinite number')
  return value
}

function encodedEffect(effect) {
  const text = JSON.stringify(clean(effect))
  if (text.length > 2000) throw new Error('Expected effect exceeds string bound')
  return text
}

function* specs(suites) {
  for (const suite of suites || []) {
    yield* suite.specs || []
    yield* specs(suite.suites)
  }
}

// Attachment paths are reporter metadata, never fields inside walk-evidence.
// Only trace/screenshots below web/artifacts can become evidence references.
export function artifactReference(path) {
  if (typeof path !== 'string') return null
  const normalized = path.replace(/\\/g, '/')
  if (/[\x00-\x1f?#%]/.test(normalized)) return null
  const marker = normalized.lastIndexOf('/web/artifacts/')
  const relative = marker >= 0 ? normalized.slice(marker + 15)
    : normalized.startsWith('web/artifacts/') ? normalized.slice(14)
      : normalized.startsWith('walk/') ? normalized : null
  if (!relative || relative.length > 2000 || relative.split('/').some((part) => !part || part === '.' || part === '..')
    || !/^[A-Za-z0-9_./-]+$/.test(relative)) return null
  return relative
}

function attachmentJson(attachment) {
  let body
  if (typeof attachment.body === 'string') {
    if (attachment.body.length > MAX_INPUT_BYTES) throw new Error('Evidence attachment exceeds byte bound')
    if (!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(attachment.body)) {
      throw new Error('Evidence attachment is not base64')
    }
    body = Buffer.from(attachment.body, 'base64')
  } else if (attachment.path) {
    const reference = artifactReference(attachment.path)
    if (!reference) throw new Error('Evidence attachment path is outside artifacts')
    const actual = realpathSync(resolve(artifactsRoot, reference))
    const root = realpathSync(artifactsRoot) + sep
    if (!actual.startsWith(root)) throw new Error('Evidence attachment escapes artifacts')
    body = readFileSync(actual)
  } else throw new Error('Evidence attachment has no body or artifact path')
  if (body.length > MAX_INPUT_BYTES) throw new Error('Evidence attachment exceeds byte bound')
  let data
  try { data = JSON.parse(body.toString('utf8')) } catch { throw new Error('Evidence attachment is not JSON') }
  boundInput(data)
  if (!data || Array.isArray(data) || typeof data !== 'object') throw new Error('Evidence attachment must be an object')
  return data
}

function evidenceFor(result) {
  const records = (result.attachments || []).filter((item) => item.name === 'walk-evidence')
  if (records.length > 1) throw new Error('Duplicate walk-evidence attachment')
  return records.length ? attachmentJson(records[0]) : {}
}

function stackWitness(evidence, featureId) {
  if (!Object.hasOwn(evidence, 'stack')) return undefined
  const stack = evidence.stack
  // Frozen pre-witness reports used stack only for launcher metadata.
  if (stack && typeof stack === 'object' && !Array.isArray(stack)
    && !Object.hasOwn(stack, 'ready') && !Object.hasOwn(stack, 'instance')
    && Object.keys(stack).every((key) => ['baseURL', 'ports', 'metricsPath'].includes(key))
    && typeof stack.baseURL === 'string' && typeof stack.metricsPath === 'string'
    && stack.ports && typeof stack.ports === 'object' && !Array.isArray(stack.ports)) return undefined
  if (!stack || typeof stack !== 'object' || Array.isArray(stack) || typeof stack.ready !== 'boolean'
    || (stack.ready && !Object.hasOwn(stack, 'instance'))
    || (Object.hasOwn(stack, 'instance') && (typeof stack.instance !== 'string' || !/^[a-f0-9]{64}$/.test(stack.instance)))) {
    throw new TypeError(`Invalid stack witness for ${featureId}`)
  }
  return { stack_ready: stack.ready, ...(stack.instance === undefined ? {} : { stack_ref: stack.instance }) }
}

function errorsFor(result) {
  return result.errors?.length ? result.errors : result.error ? [result.error] : []
}

function resultClass(result, evidence) {
  // Explicit runner markers precede expected-failure handling: a certification
  // deliberately fails in Playwright but must never be reported as a PASS.
  const declared = evidence.certification || evidence.result
  if (declared?.result === 'unsupported_local') return { verdict: 'unsupported_local', reason: declared.reason }
  if (['staging', 'staging_only'].includes(declared?.result)) return { verdict: 'staging_only', reason: declared.reason }
  const message = errorsFor(result).map((error) => error.message || '').join('\n')
  const marker = message.match(/(?:CERTIFICATION_CLASS:\s*(unsupported_local|staging)|UNSUPPORTED_LOCAL|QUEUED):\s*([^\n]*)/)
  if (marker) return { verdict: /QUEUED:/.test(marker[0]) ? 'queued'
    : marker[1] === 'staging' ? 'staging_only' : 'unsupported_local', reason: marker[2] }
  if (result.status === 'passed') return { verdict: 'pass' }
  if (['failed', 'timedOut'].includes(result.status)) return { verdict: 'fail' }
  return { verdict: 'queued', reason: 'No completed effect assertion' }
}

function sourceLocation(file, line) {
  if (typeof file !== 'string' || !Number.isInteger(line)) return null
  const normalized = file.replace(/\\/g, '/')
  const known = [...sources.keys()].find((candidate) => normalized === candidate || normalized.endsWith('/' + candidate))
  return known && /\bexpect(?:\s*\(|\.poll\s*\()/.test(sources.get(known)[line - 1] || '') ? `${known}:${line}` : null
}

function assertionSite(result, journey, census = false) {
  const locations = []
  const visitSteps = (steps) => {
    for (const step of steps || []) {
      if (step.error?.location) locations.push(step.error.location)
      visitSteps(step.steps)
    }
  }
  for (const error of errorsFor(result)) if (error.location) locations.push(error.location)
  visitSteps(result.steps)
  for (const location of locations) {
    const site = sourceLocation(location.file, location.line)
    if (site) return { site, observed: true }
  }
  const file = census ? censusFile : journey ? journeyFile : runnerFile
  const lines = sources.get(file)
  const begin = journey || census ? 0 : lines.findIndex((line) => line.startsWith('export async function runProbe'))
  const index = lines.findIndex((line, i) => i >= begin && (census ? line.includes('expect(census.ok')
    : /\bexpect(?:\s*\(|\.poll\s*\()/.test(line)))
  if (index < 0) throw new Error('Trusted runner has no expect call site')
  return { site: `${file}:${index + 1}`, observed: false }
}

function oracle(entry, state, viewport, result, journey = false, census = false) {
  const effect = journey ? { kind: 'renders', target: 'first-run-open:sample-drawing' } : entry.expected_effect[state]
  const location = assertionSite(result, journey, census)
  const assertion_id = 'assertion:' + createHash('sha256')
    .update(JSON.stringify([effect.kind, effect.target ?? null, location.site])).digest('hex')
  const setup = census ? { steps: [{ kind: 'control-census', scope: entry.id.slice('control-census:'.length), state }] }
    : journey ? { firstRun: true, steps: [{ kind: 'navigate', url: '/try' },
    { kind: 'dismiss-coach' }, { kind: 'open-sample-rooftop' }, { kind: 'verify-drawing' }] } : stateRecipe(entry, state)
  // A slash-menu recipe uses a trusted action label, not an executable command.
  const steps = setup.steps.map(({ command, ...step }) => command === undefined ? step : { ...step, input_label: command })
  const first_error_line = redact(errorsFor(result)[0]?.message, 300).split(/\r?\n/)[0]
  return {
    feature_id: entry.id, state, viewport, first_error_line, assertion_id,
    fixture_recipe: { name: census ? 'control-census' : journey ? 'first-run-open' : 'stateRecipe', parameters: { feature_id: entry.id, state,
      ...setup, steps } },
    expected_effect: encodedEffect(effect),
    evidence: { state, viewport, first_error_line,
      assertion_site: location.site, assertion_observed: location.observed,
      references: (result.attachments || []).filter((a) => ['trace', 'screenshot'].includes(a.name))
        .map((a) => ({ kind: a.name, reference: artifactReference(a.path) })).filter((a) => a.reference) },
  }
}

function observations(feature_id, state, viewport, evidence) {
  const findings = []
  const add = (kind, measurement) => findings.push({ feature_id, category: kind,
    evidence: { kind, state, viewport, ...measurement } })
  add('console_errors', { count: count(evidence.consoleErrors) })
  add('page_errors', { count: count(evidence.pageErrors) })
  add('failed_requests', { count: count(evidence.failedRequests) })
  add('steps', { count: count(evidence.steps) })
  if (Number.isFinite(evidence.timeToTaskMs) && evidence.timeToTaskMs >= 0) add('time_to_task', { milliseconds: evidence.timeToTaskMs })
  for (const violation of evidence.accessibility?.violations || []) {
    // Never propagate html, targets, help text, model prose or arbitrary ids.
    if (typeof violation.id !== 'string' || !/^[a-z][a-z0-9-]{0,99}$/.test(violation.id)) continue
    add('axe_violation', { violation_id: violation.id, count: count(violation.nodes) })
  }
  return findings
}

/** Controller wire schema is authoritative. The lossless triple projection lives
 * in evidence.cases; constrained wire rows retain their feature id and context in
 * reason/evidence. expected_effect is canonical JSON because the schema says string.
 */
export function buildReceipt({ playwrightReport, featureMap, identity }) {
  boundInput(playwrightReport)
  boundInput(featureMap)
  boundInput(identity)
  if (!Array.isArray(playwrightReport?.suites) || !Array.isArray(featureMap?.entries)) throw new Error('Report and feature map need suites and entries')
  const receipt = { schema: 'leaf.studio-walk.v1', feature_id: 'walk:run',
    deployment_identity: identity?.deployment_identity, fixture_version: identity?.fixture_version,
    catalog_version: identity?.catalog_version, browser_version: identity?.browser_version, attempt: identity?.attempt,
    created_at: new Date().toISOString(), verdicts: [], coverage_gaps: [], flakes: [], unavailable_features: [], failures: [], findings: [],
    evidence: { cases: [], coverage_gaps: [], flakes: [], unavailable: [] } }
  if (playwrightReport.stats?.startTime) {
    receipt.started_at = playwrightReport.stats.startTime
    if (Number.isFinite(playwrightReport.stats.duration) && playwrightReport.stats.duration >= 0
      && Number.isFinite(Date.parse(receipt.started_at))) receipt.completed_at = new Date(Date.parse(receipt.started_at) + playwrightReport.stats.duration).toISOString()
  }
  const triples = new Map()
  for (const entry of featureMap.entries) for (const state of entry.states) for (const viewport of entry.viewports) {
    const triple = { feature_id: entry.id, state, viewport }
    const key = JSON.stringify([entry.id, state, viewport])
    if (triples.has(key)) throw new Error('Duplicate feature-map triple')
    triples.set(key, { entry, triple, runs: [] })
  }
  const journeys = new Map()
  const censuses = new Map()
  let evidenceRuns = 0
  let readyRuns = 0
  let unready = false
  let witnessed = false
  const stackRefs = new Set()
  for (const spec of specs(playwrightReport.suites)) {
    const census = spec.title?.match(/^(control-census:[a-z0-9]+(?:-[a-z0-9]+)*) \[([^\]]+)\] @(desktop|phone)$/)
    const match = spec.title?.match(/^([^\s]+) \[([^\]]+)\] @(desktop|phone)$/)
    const journey = spec.title?.match(/^first-run-open @(desktop|phone)$/)
    let row
    if (census) {
      const [id, state, viewport] = census.slice(1)
      const key = JSON.stringify([id, state, viewport])
      if (!censuses.has(key)) censuses.set(key, { entry: { id, expected_effect: {
        [state]: { kind: 'renders', target: `${id}:mapped-or-baselined` } } },
        triple: { feature_id: id, state, viewport }, runs: [], census: true })
      row = censuses.get(key)
    } else if (match) {
      row = triples.get(JSON.stringify(match.slice(1)))
      if (!row) throw new Error('Test names a triple absent from the feature map')
    } else if (journey) {
      const key = journey[1]
      if (!journeys.has(key)) journeys.set(key, { entry: { id: 'journey:first-run-open' },
        triple: { feature_id: 'journey:first-run-open', state: 'first-run', viewport: key }, runs: [], journey: true })
      row = journeys.get(key)
    } else throw new Error('Unrecognized walk test title')
    for (const test of spec.tests || []) for (const result of test.results || []) {
      if (test.projectName && test.projectName !== row.triple.viewport) throw new Error('Test project does not match viewport')
      const evidence = evidenceFor(result)
      if ((result.attachments || []).some((item) => item.name === 'walk-evidence')) evidenceRuns++
      const stack = stackWitness(evidence, row.entry.id)
      if (stack) {
        witnessed = true
        if (stack.stack_ready) readyRuns++
        else unready = true
        if (stack.stack_ref) stackRefs.add(stack.stack_ref)
      }
      let ux
      if (Object.hasOwn(evidence, 'ux_observations')) {
        try { ux = packUxEvidence(evidence.ux_observations).ux_observations }
        catch (error) { throw new TypeError(`Invalid UX evidence for ${row.entry.id}: ${error.message}`) }
      }
      const outcome = resultClass(result, evidence)
      if (ux) outcome.ux_observations = ux
      if (test.expectedStatus === 'failed' && outcome.verdict === 'pass') outcome.verdict = 'fail'
      if (test.expectedStatus === 'skipped') { outcome.verdict = 'queued'; outcome.reason = 'Skipped effect assertion' }
      row.runs.push(outcome)
      if (outcome.verdict === 'fail') {
        const failure = oracle(row.entry, row.triple.state, row.triple.viewport, result, row.journey, row.census)
        if (stack) failure.evidence.context = stack
        receipt.failures.push(failure)
      }
      if (outcome.verdict === 'unsupported_local') receipt.unavailable_features.push({ feature_id: row.entry.id,
        reason: `${row.triple.state} @${row.triple.viewport}: ${redact(outcome.reason)}` })
      if (Object.keys(evidence).length) receipt.findings.push(...observations(row.entry.id, row.triple.state, row.triple.viewport, evidence))
      for (const observation of ux || []) {
        if (observation.observed > 0 && ['control_covered_at_rest', 'scroll_needed_steps', 'extra_steps'].includes(observation.metric_id)) {
          receipt.findings.push({ feature_id: row.entry.id, assertion_id: assertionAlias(observation.metric_id), category: 'ux',
            summary: `${observation.metric_id} = ${observation.observed} at ${observation.viewport}/${observation.state}`,
            evidence: uxObservation({ lensId: observation.lens_id, metricId: observation.metric_id,
              viewport: observation.viewport, state: observation.state, observed: observation.observed }) })
        }
      }
    }
  }
  for (const row of [...triples.values(), ...journeys.values(), ...censuses.values()]) {
    const { triple, runs } = row
    if (!runs.length) {
      receipt.coverage_gaps.push({ feature_id: triple.feature_id, reason: `No test result: ${triple.state} @${triple.viewport}` })
      receipt.evidence.coverage_gaps.push(triple)
      continue
    }
    const outcome = runs.at(-1)
    const flaky = runs.some((run, index) => run.verdict === 'fail' && runs.slice(index + 1).some((later) => later.verdict === 'pass'))
    const detail = { ...triple, verdict: outcome.verdict }
    if (runs.some((run) => run.ux_observations)) detail.ux_observations = runs.flatMap((run) => run.ux_observations || [])
    if (outcome.reason) detail.reason = redact(outcome.reason)
    receipt.evidence.cases.push(detail)
    receipt.verdicts.push({ feature_id: triple.feature_id, verdict: wireVerdict[outcome.verdict],
      reason: `${triple.state} @${triple.viewport}: ${outcome.verdict}` })
    if (flaky) {
      receipt.flakes.push({ feature_id: triple.feature_id, reason: `Failed then passed: ${triple.state} @${triple.viewport}`, occurrences: runs.length })
      receipt.evidence.flakes.push({ ...triple, occurrences: runs.length })
    }
    for (const run of runs) if (run.verdict === 'unsupported_local') receipt.evidence.unavailable.push({ ...triple, reason: redact(run.reason) })
  }
  if (witnessed) {
    const maxStackRefs = Math.min(evidenceRuns, 1024)
    if (stackRefs.size > maxStackRefs) throw new Error(`Stack witness has ${stackRefs.size} distinct instance refs; maximum is ${maxStackRefs}`)
    receipt.evidence.context = { stack_refs: [...stackRefs].sort(compare) }
    if (unready) receipt.evidence.context.stack_ready = false
    else if (evidenceRuns > 0 && readyRuns === evidenceRuns) receipt.evidence.context.stack_ready = true
  }
  const output = clean(receipt)
  if (Buffer.byteLength(JSON.stringify(output)) > MAX_RECEIPT_BYTES) throw new Error('Receipt exceeds 1 MiB bound')
  const validation = validateReceipt(output)
  if (!validation.valid) throw new Error(validation.errors[0])
  return output
}

// Implements exactly the keywords used by the vendored controller schema. No
// optional format plugin: date-time enforcement is identical on every Node host.
export function validateReceipt(receipt, contract = schema) {
  const errors = []
  const visit = (value, rule, path) => {
    if (rule.$ref) {
      if (!rule.$ref.startsWith('#/')) { errors.push(`${path}: external schema reference rejected`); return }
      const target = rule.$ref.slice(2).split('/').reduce((node, key) => node?.[key.replace(/~1/g, '/').replace(/~0/g, '~')], contract)
      if (!target) { errors.push(`${path}: unresolved schema reference`); return }
      visit(value, target, path)
      return
    }
    if (rule.oneOf) {
      const successes = rule.oneOf.filter((branch) => {
        const start = errors.length
        visit(value, branch, path)
        const valid = errors.length === start
        errors.splice(start)
        return valid
      }).length
      if (successes !== 1) errors.push(`${path}: must match exactly one schema branch`)
    }
    if ('const' in rule && value !== rule.const) errors.push(`${path}: const mismatch`)
    if (rule.enum && !rule.enum.includes(value)) errors.push(`${path}: enum mismatch`)
    const object = value !== null && typeof value === 'object' && !Array.isArray(value)
    const types = { object, array: Array.isArray(value), string: typeof value === 'string', integer: Number.isInteger(value),
      number: typeof value === 'number' && Number.isFinite(value), boolean: typeof value === 'boolean', null: value === null }
    if (rule.type && !types[rule.type]) { errors.push(`${path}: expected ${rule.type}`); return }
    if (object) {
      for (const key of rule.required || []) if (!Object.hasOwn(value, key)) errors.push(`${path}.${key}: required`)
      if (rule.minProperties && Object.keys(value).length < rule.minProperties) errors.push(`${path}: too few properties`)
      for (const [key, child] of Object.entries(value)) {
        if (rule.properties?.[key]) visit(child, rule.properties[key], `${path}.${key}`)
        else if (rule.additionalProperties === false) errors.push(`${path}.${key}: additional property`)
      }
    }
    if (Array.isArray(value) && rule.items) value.forEach((child, i) => visit(child, rule.items, `${path}[${i}]`))
    if (typeof value === 'string') {
      const length = [...value].length
      if (rule.minLength && length < rule.minLength) errors.push(`${path}: string too short`)
      if (rule.maxLength && length > rule.maxLength) errors.push(`${path}: string too long`)
      if (rule.pattern && !new RegExp(rule.pattern).test(value)) errors.push(`${path}: pattern mismatch`)
      if (rule.format === 'date-time' && !validDateTime(value)) errors.push(`${path}: invalid date-time`)
    }
    if (typeof value === 'number' && rule.minimum !== undefined && value < rule.minimum) errors.push(`${path}: below minimum`)
  }
  try {
    boundInput(receipt)
    if (Buffer.byteLength(JSON.stringify(receipt)) > MAX_RECEIPT_BYTES) errors.push('$: receipt exceeds 1 MiB bound')
    const checkStrings = (value) => {
      if (typeof value === 'string' && value.length > 2000) errors.push('$: string exceeds 2000 character bound')
      else if (value && typeof value === 'object') for (const [key, child] of Object.entries(value)) {
        if (key.length > 2000) errors.push('$: key exceeds string bound')
        checkStrings(child)
      }
    }
    checkStrings(receipt)
    visit(receipt, contract, '$')
  } catch { errors.push('$: invalid or oversized receipt structure') }
  return { valid: errors.length === 0, errors }
}

function validDateTime(value) {
  const match = value.match(/^(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:[Zz]|([+-])(\d{2}):(\d{2}))$/)
  if (!match) return false
  const [, year, month, day, hour, minute, second, , offsetHour, offsetMinute] = match
  const y = Number(year), m = Number(month), d = Number(day)
  const days = [31, y % 4 === 0 && (y % 100 !== 0 || y % 400 === 0) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
  return m >= 1 && m <= 12 && d >= 1 && d <= days[m - 1] && Number(hour) < 24
    && Number(minute) < 60 && Number(second) < 60 && (!offsetHour || Number(offsetHour) < 24 && Number(offsetMinute) < 60)
}

function readJsonInput(file) {
  const bytes = readFileSync(file)
  if (bytes.length > MAX_INPUT_BYTES) throw new Error('JSON input exceeds byte bound')
  try { return JSON.parse(bytes.toString('utf8')) } catch { throw new Error('Input is not JSON') }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const flags = new Map()
    for (let index = 2; index < process.argv.length; index += 2) {
      const key = process.argv[index], value = process.argv[index + 1]
      if (!['--report', '--identity', '--out'].includes(key) || !value || flags.has(key)) throw new Error('Usage: --report <json> --identity <json> --out <json>')
      flags.set(key, value)
    }
    if (flags.size !== 3) throw new Error('Usage: --report <json> --identity <json> --out <json>')
    const receipt = buildReceipt({ playwrightReport: readJsonInput(flags.get('--report')),
      featureMap: buildFeatureMap(), identity: readJsonInput(flags.get('--identity')) })
    const validation = validateReceipt(receipt)
    if (!validation.valid) throw new Error(validation.errors[0])
    writeFileSync(flags.get('--out'), JSON.stringify(receipt) + '\n', 'utf8')
  } catch (error) {
    process.stderr.write(redact(error.message, 300) + '\n')
    process.exitCode = 2
  }
}
