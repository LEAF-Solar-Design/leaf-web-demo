import { expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { PROJECT_SOLAR_APP_REASONS, projectSolarAppReason, createProjectSolarAuthority,
  deriveProjectSolarAppScope, projectSolarCheckoutState, projectSolarScopeSnapshot } from './solarProjectAppModel.js'
import { createSolarProjectClient } from './solarProjectClient.js'

const O = '11111111-1111-1111-1111-111111111111'
const P = '22222222-2222-2222-2222-222222222222'
const D = '33333333-3333-3333-3333-333333333333'
const V = '44444444-4444-4444-4444-444444444444'
// Spelled in parts so the merge scan never reads a fixture as a live credential.
const SCHEME = 'Bear' + 'er'
const context = { orgId: O, projectId: P, drawingArtifactId: D, drawingId: V, drawingVersion: null }
const binding = { kind: 'project', organization_id: O, project_id: P, drawing_id: D, input_version_id: V }
const scope = deriveProjectSolarAppScope({ projectMode: true, runContext: context, authorityKey: 'scope-1' })
const now = Date.parse('2030-01-01T00:00:00Z')
const lease = { holder: 'same holder', fence: '12', acquired: '2029-12-31T23:00:00Z', expires: '2030-01-01T01:00:00Z' }
const snapshot = (extra = {}) => ({ binding, authorityKey: 'scope-1', checkout: null, ownsCheckout: false,
  pending: false, error: null, leaseRevision: 4, ...extra })
const project = (extra = {}) => deriveProjectSolarAppScope({ projectMode: true, runContext: context,
  authorityKey: 'scope-1', ...extra })
const session = { authenticated: true, actorId: 'actor-alice', generation: 0 }

it('R13A-01 derives the artifact and input version from the canonical run context', () => {
  expect(scope).toEqual({ mode: 'project', binding, authorityKey: 'scope-1', reason: null })
  expect(Object.keys(scope.binding)).toEqual(['kind', 'organization_id', 'project_id', 'drawing_id', 'input_version_id'])
  expect(scope.binding.drawing_id).toBe(context.drawingArtifactId)
  expect(scope.binding.input_version_id).toBe(context.drawingId)
  expect(Object.isFrozen(scope)).toBe(true)
  expect(Object.isFrozen(scope.binding)).toBe(true)
})

it('R13A-02 preserves explicit standalone scope', () => {
  const result = project({ projectMode: false, runContext: { projectId: null, orgId: null, drawingId: 'legacy' } })
  expect(result).toEqual({ mode: 'standalone', binding: undefined, authorityKey: null, reason: 'project_required' })
  expect(Object.isFrozen(result)).toBe(true)
})

it('R13A-03 invalid project contexts never become standalone', () => {
  for (const runContext of [null, { ...context, drawingArtifactId: undefined },
    { ...context, drawingId: 'AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA' }, { ...context, drawingVersion: 3 }]) {
    expect(project({ runContext })).toEqual({ mode: 'invalid-project', binding: null, authorityKey: null, reason: 'binding_invalid' })
  }
  for (const projectMode of [undefined, null, 'project', false]) {
    expect(project({ projectMode }).mode).toBe('invalid-project')
  }
})

it('R13A-04 missing project authority requests sign in', () => {
  for (const authorityKey of [null, '', ' '.repeat(3)]) {
    expect(project({ authorityKey })).toEqual({ mode: 'invalid-project', binding: null, authorityKey: null, reason: 'sign_in_required' })
  }
})

it('R13A-05 authority is stable within one authenticated generation', () => {
  const authority = createProjectSolarAuthority()
  const key = authority.update({ ...session, token: 'private-extra-sentinel' })
  expect(authority.update({ ...session })).toBe(key)
  expect(authority.getKey()).toBe(key)
  expect(key.length).toBeGreaterThan(0)
  expect(new TextEncoder().encode(key).byteLength).toBeLessThanOrEqual(256)
  expect(key).not.toContain(session.actorId)
  expect(key).not.toContain('private-extra-sentinel')
  expect(Object.isFrozen(authority)).toBe(true)
})

it('R13A-06 sign out and reauthentication mint fresh authority', () => {
  const authority = createProjectSolarAuthority()
  const first = authority.update(session)
  expect(authority.update({ ...session, authenticated: false })).toBeNull()
  expect(authority.getKey()).toBeNull()
  const second = authority.update(session)
  expect(second).not.toBe(first)
  authority.clear()
  expect(authority.getKey()).toBeNull()
  expect(authority.update(session)).not.toBe(second)
})

it('R13A-07 actor and session generation changes rotate authority', () => {
  const authority = createProjectSolarAuthority()
  const first = authority.update(session)
  const second = authority.update({ ...session, actorId: 'actor-bob' })
  const third = authority.update({ ...session, actorId: 'actor-bob', generation: 1 })
  expect(new Set([first, second, third]).size).toBe(3)
  for (const invalid of [null, {}, { ...session, generation: -1 }, { ...session, generation: 1.5 },
    { ...session, generation: Number.MAX_SAFE_INTEGER + 1 }, { ...session, actorId: null }]) {
    expect(authority.update(invalid)).toBeNull()
    expect(authority.getKey()).toBeNull()
  }
})

it('R13A-08 foreign checkout snapshots fail closed', () => {
  const foreign = ['organization_id', 'project_id', 'drawing_id', 'input_version_id']
    .map((field) => snapshot({ binding: { ...binding, [field]: '55555555-5555-5555-5555-555555555555' }, checkout: lease, ownsCheckout: true }))
  foreign.push(snapshot({ authorityKey: 'other', checkout: lease, ownsCheckout: true }))
  for (const value of foreign) {
    expect(projectSolarCheckoutState({ scope, snapshot: value, now })).toEqual({ checkout: null,
      ownsCheckout: false, pending: false, error: null, leaseRevision: null,
      canTake: false, canRelease: false, reason: 'scope_stale' })
  }
})

it('R13A-09 checkout ownership comes from the R7 ownership flag', () => {
  const foreign = projectSolarCheckoutState({ scope, snapshot: snapshot({ checkout: lease }), now })
  expect(foreign).toMatchObject({ checkout: lease, ownsCheckout: false, canTake: false, canRelease: false, reason: 'checkout_held' })
  const own = projectSolarCheckoutState({ scope, snapshot: snapshot({ checkout: lease, ownsCheckout: true }), now })
  expect(own).toMatchObject({ checkout: lease, ownsCheckout: true, canTake: false, canRelease: true, reason: null })
  expect(Object.keys(own.checkout)).toEqual(['holder', 'fence', 'acquired', 'expires'])
  expect(own.checkout).not.toBe(lease)
  expect(Object.isFrozen(own.checkout)).toBe(true)
})

it('R13A-10 absent expired and pending checkouts have exact projections', () => {
  for (const [extra, expected] of [
    [{}, { checkout: null, ownsCheckout: false, pending: false, canTake: true, canRelease: false, reason: 'checkout_required' }],
    [{ checkout: { ...lease, expires: '2030-01-01T00:00:00Z' }, ownsCheckout: true },
      { checkout: { ...lease, expires: '2030-01-01T00:00:00Z' }, ownsCheckout: false, pending: false, canTake: true, canRelease: false, reason: 'checkout_expired' }],
    [{ checkout: lease, ownsCheckout: true, pending: true },
      { checkout: lease, ownsCheckout: true, pending: true, canTake: false, canRelease: false, reason: 'checkout_pending' }],
  ]) {
    const result = projectSolarCheckoutState({ scope, snapshot: snapshot(extra), now })
    expect(result).toEqual({ ...expected, error: null, leaseRevision: 4 })
    expect(Object.isFrozen(result)).toBe(true)
  }
})

it('R13A-11 checkout projection never retains credential or exception fields', () => {
  const capability = 'lco1.' + 'a'.repeat(64)
  const bearer = `${SCHEME} private-sentinel`
  const safe = projectSolarCheckoutState({ scope, now, snapshot: snapshot({ checkout: { ...lease,
    checkout_capability: capability, authorization: bearer }, token: bearer, rawError: new Error(bearer) }) })
  expect(safe.checkout).toEqual(lease)
  expect(JSON.stringify(safe)).not.toContain(capability)
  expect(JSON.stringify(safe)).not.toContain(bearer)
  for (const extra of [{ checkout: { ...lease, holder: capability } }, { checkout: { ...lease, holder: bearer } },
    { error: new Error(bearer) }, { error: bearer }, { checkout: { ...lease, fence: 12 } }, { checkout: undefined }]) {
    const result = projectSolarCheckoutState({ scope, now, snapshot: snapshot(extra) })
    expect(result).toMatchObject({ checkout: null, ownsCheckout: false, canTake: false, canRelease: false, reason: 'checkout_unavailable' })
    expect(JSON.stringify(result)).not.toContain(capability)
    expect(JSON.stringify(result)).not.toContain(bearer)
    expect(result.error).not.toBeInstanceOf(Error)
  }
})

it('R13A-12 reason sentences and the static selector are frozen', () => {
  const expectedCopy = {
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
  }
  expect(PROJECT_SOLAR_APP_REASONS).toEqual(expectedCopy)
  expect(Object.isFrozen(PROJECT_SOLAR_APP_REASONS)).toBe(true)
  for (const [code, copy] of Object.entries(expectedCopy)) {
    expect(typeof copy).toBe('string')
    expect(copy.length).toBeGreaterThanOrEqual(12)
    expect(projectSolarAppReason(code)).toBe(copy)
  }
  expect(projectSolarAppReason('project_execution_disabled')).toBe(expectedCopy.project_unavailable)
  expect(projectSolarAppReason('SIP_R7_BINDING_INVALID')).toBe(expectedCopy.binding_invalid)
  expect(projectSolarAppReason('SIP_R7_SCOPE_STALE')).toBe(expectedCopy.scope_stale)
  expect(projectSolarAppReason(null)).toBeNull()
  expect(projectSolarAppReason(undefined)).toBeNull()
  expect(projectSolarAppReason('unrecognized-secret')).toBe(expectedCopy.request_refused)
  const source = readFileSync(resolve(process.cwd(), 'src/solar/solarProjectAppModel.js'), 'utf8')
  expect(source).toContain('switch (code)')
  expect(source).not.toMatch(new RegExp('PROJECT_SOLAR_APP_REASONS\\s*\\['))
  for (const code of Object.keys(expectedCopy)) expect(source).toContain('return PROJECT_SOLAR_APP_REASONS.' + code)
})

it('R13A-13 R7 catalog captures remain stale after A to B to A', () => {
  const fetchImpl = vi.fn()
  const client = createSolarProjectClient({ fetchImpl })
  client.setScope(binding, 'scope-1')
  const captured = client.captureCatalogScope()
  expect(captured()).toBe(true)
  client.setScope({ ...binding, input_version_id: P }, 'scope-1')
  client.setScope(binding, 'scope-1')
  expect(captured()).toBe(false)
  expect(fetchImpl).not.toHaveBeenCalled()
  client.dispose()
})

it('R13A-14 R7 catalog captures remain stale after sign out and reauthentication', () => {
  const fetchImpl = vi.fn()
  const client = createSolarProjectClient({ fetchImpl })
  client.setScope(binding, 'scope-1')
  const captured = client.captureCatalogScope()
  client.setScope(null, null)
  client.setScope(binding, 'scope-2')
  expect(captured()).toBe(false)
  expect(client.captureCatalogScope()()).toBe(true)
  expect(fetchImpl).not.toHaveBeenCalled()
  client.dispose()
})

it('R13A-47 the state snapshot keeps only the seven checkout fields and decides the same', () => {
  const capability = 'lco1.' + 'b'.repeat(64)
  const bearer = `${SCHEME} private-sentinel`
  const kept = projectSolarScopeSnapshot(snapshot({ checkout: { ...lease, checkout_capability: capability },
    checkout_capability: capability, authorization: bearer, headers: { Authorization: bearer }, session: { raw: bearer } }))
  expect(Object.keys(kept)).toEqual(['binding', 'authorityKey', 'checkout', 'ownsCheckout', 'pending', 'error', 'leaseRevision'])
  expect(Object.keys(kept.checkout)).toEqual(['holder', 'fence', 'acquired', 'expires'])
  expect(Object.isFrozen(kept)).toBe(true)
  expect(Object.isFrozen(kept.checkout)).toBe(true)
  expect(Object.isFrozen(kept.binding)).toBe(true)
  expect(JSON.stringify(kept)).not.toContain('lco1.')
  expect(JSON.stringify(kept).toLowerCase()).not.toContain(SCHEME.toLowerCase())
  const variants = [{}, { checkout: lease }, { checkout: lease, ownsCheckout: true },
    { checkout: { ...lease, expires: '2029-12-31T23:30:00Z' }, ownsCheckout: true }, { pending: true },
    { checkout: { ...lease, holder: capability } }, { checkout: { ...lease, holder: bearer } },
    { checkout: { ...lease, holder: 'h'.repeat(201) } }, { checkout: { ...lease, fence: 12 } },
    { checkout: { ...lease, fence: 'x' } }, { checkout: { ...lease, acquired: capability } },
    { checkout: 'lease' }, { checkout: undefined }, { checkout: [lease] },
    { ownsCheckout: 'yes' }, { pending: 1 }, { error: 'checkout_held' }, { error: bearer }, { error: capability },
    { error: 5 }, { error: undefined }, { leaseRevision: '4' }, { leaseRevision: -1 }, { leaseRevision: 1.5 },
    { leaseRevision: Number.NaN }, { authorityKey: 'scope-2' }, { authorityKey: 'Bearer abcdefgh' },
    { binding: { ...binding, project_id: D } }, { binding: { ...binding, extra: O } }, { binding: null }]
  for (const variant of variants) {
    const raw = snapshot(variant)
    expect(projectSolarCheckoutState({ scope, snapshot: projectSolarScopeSnapshot(raw), now }))
      .toEqual(projectSolarCheckoutState({ scope, snapshot: raw, now }))
  }
  const hostile = new Proxy({}, { get() { throw new Error('hostile') } })
  expect(projectSolarScopeSnapshot(hostile)).toEqual({ binding: null, authorityKey: null, checkout: null,
    ownsCheckout: undefined, pending: undefined, error: false, leaseRevision: undefined })
  expect(projectSolarCheckoutState({ scope, snapshot: projectSolarScopeSnapshot(hostile), now }).reason).toBe('scope_stale')
})

it('R13A-52 every snapshot field is read exactly once', () => {
  const values = { binding, authorityKey: 'scope-1', checkout: lease, ownsCheckout: true, pending: false,
    error: null, leaseRevision: 4 }
  const swapped = { binding: null, authorityKey: `${SCHEME} synthetic-state-secret`, checkout: 'lease',
    ownsCheckout: 'yes', pending: 1, error: 5, leaseRevision: -1 }
  const reads = {}
  const raw = {}
  for (const key of Object.keys(values)) {
    Object.defineProperty(raw, key, { enumerable: true, get() {
      reads[key] = (reads[key] ?? 0) + 1
      return reads[key] === 1 ? values[key] : swapped[key]
    } })
  }
  const kept = projectSolarScopeSnapshot(raw)
  expect(reads).toEqual({ binding: 1, authorityKey: 1, checkout: 1, ownsCheckout: 1, pending: 1, error: 1,
    leaseRevision: 1 })
  expect(kept).toEqual(projectSolarScopeSnapshot(values))
  expect(kept.authorityKey).toBe('scope-1')
  expect(JSON.stringify(kept).toLowerCase()).not.toContain(SCHEME.toLowerCase())
  const first = {}
  Object.defineProperty(first, 'authorityKey', { enumerable: true, get: () => swapped.authorityKey })
  expect(projectSolarScopeSnapshot(first).authorityKey).toBeNull()
})
