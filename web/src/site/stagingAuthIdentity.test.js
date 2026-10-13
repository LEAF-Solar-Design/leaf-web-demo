import { describe, expect, it } from 'vitest'
import { createHash } from 'node:crypto'
import {
  assertStagingTokenIdentity,
  StagingIdentityConfigError,
  STAGING_EXPECTED_SUBJECT_SHA256_ENV as SUBJECT_ENV,
  STAGING_EXPECTED_TENANT_SHA256_ENV as TENANT_ENV,
  STAGING_FORBIDDEN_SUBJECT_SHA256S_ENV as FORBIDDEN_ENV,
} from '../../e2e/staging/stagingConfig.mjs'

const subject = 'fabricated-subject-private-sentinel-166d'
const tenant = 'fabricated-tenant-private-sentinel-166d'
const tenantClaim = 'https://leafdesign.ai/tenant_id'
const hash = (value) => createHash('sha256').update(value, 'utf8').digest('hex')
const encode = (value) => Buffer.from(JSON.stringify(value), 'utf8').toString('base64url')
const header = encode({ alg: 'none' })
const jwt = (claims) => `${header}.${encode(claims)}.`
const token = jwt({ sub: subject, [tenantClaim]: tenant })
const matchingEnv = () => ({ [SUBJECT_ENV]: hash(subject) })

// Every rejection also checks the public error contract and credential privacy.
function expectRejection(input, env, check, variable, privateValues = [subject, tenant]) {
  let error
  try {
    assertStagingTokenIdentity(input, env)
  } catch (caught) {
    error = caught
  }
  expect(error).toBeInstanceOf(StagingIdentityConfigError)
  expect(error.name).toBe('StagingIdentityConfigError')
  expect(error.check).toBe(check)
  expect(error.message).toContain(`${check}:`)
  if (variable) expect(error.message).toContain(variable)
  const exposed = `${error.message}\n${String(error)}\n${error.stack}\n${JSON.stringify(error)}`
  for (const value of privateValues) {
    if (typeof value === 'string' && value) expect(exposed).not.toContain(value)
  }
  if (typeof input === 'string' && input) expect(exposed).not.toContain(input)
  expect(Object.keys(error).sort()).toEqual(['check', 'name'])
  for (const property of ['token', 'payload', 'subject', 'tenant', 'cause', 'parserError']) {
    expect(error).not.toHaveProperty(property)
  }
  return error
}

describe('staging JWT identity configuration', () => {
  it('exports the exact environment names', () => {
    expect(SUBJECT_ENV).toBe('LEAF_E2E_STAGING_EXPECTED_SUBJECT_SHA256')
    expect(TENANT_ENV).toBe('LEAF_E2E_STAGING_EXPECTED_TENANT_SHA256')
    expect(FORBIDDEN_ENV).toBe('LEAF_E2E_STAGING_FORBIDDEN_SUBJECT_SHA256S')
  })

  it('accepts a matching dedicated subject without optional configuration', () => {
    expect(assertStagingTokenIdentity(jwt({ sub: subject }), matchingEnv())).toBe(true)
    expect(assertStagingTokenIdentity(token, { [SUBJECT_ENV]: ` \t${hash(subject)}\n` })).toBe(true)
  })

  it.each([undefined, null, ''])('accepts no token (%s) without parsing settings', (input) => {
    expect(assertStagingTokenIdentity(input)).toBe(true)
    expect(assertStagingTokenIdentity(input, {})).toBe(true)
    expect(assertStagingTokenIdentity(input, {
      [SUBJECT_ENV]: 'unused invalid subject setting',
      [TENANT_ENV]: 'unused invalid tenant setting',
      [FORBIDDEN_ENV]: ',unused,',
    })).toBe(true)
  })

  it.each([undefined, '', ' \t\n'])('requires an expected subject (%s)', (setting) => {
    const env = setting === undefined ? {} : { [SUBJECT_ENV]: setting }
    expectRejection(token, env, 'expected_subject_unset', SUBJECT_ENV)
  })

  it('requires subject configuration before decoding the token or optional settings', () => {
    expectRejection('bad.token.', { [TENANT_ENV]: 'bad' }, 'expected_subject_unset', SUBJECT_ENV)
  })

  it('rejects a different subject hash', () => {
    expectRejection(token, { [SUBJECT_ENV]: hash('fabricated-other-subject') }, 'subject_hash_mismatch', SUBJECT_ENV)
  })

  it('rejects a forbidden subject even when it is expected, before checking tenant', () => {
    expectRejection(token, {
      ...matchingEnv(),
      [FORBIDDEN_ENV]: ` ${hash('fabricated-other-subject')} , ${hash(subject)} , ${hash(subject)} `,
      [TENANT_ENV]: hash('fabricated-other-tenant'),
    }, 'forbidden_subject', FORBIDDEN_ENV)
  })

  it.each(['', ' \t', ` ${hash('fabricated-other-subject')} , ${hash('fabricated-third-subject')} `])(
    'accepts empty or nonmatching forbidden entries (%s)', (setting) => {
      expect(assertStagingTokenIdentity(token, { ...matchingEnv(), [FORBIDDEN_ENV]: setting })).toBe(true)
    },
  )

  it.each([undefined, '', ' \t\n'])('disables an absent or blank tenant hash (%s)', (setting) => {
    const env = matchingEnv()
    if (setting !== undefined) env[TENANT_ENV] = setting
    expect(assertStagingTokenIdentity(jwt({ sub: subject }), env)).toBe(true)
  })

  it('accepts the exact namespaced tenant hash with trimmed configuration', () => {
    expect(assertStagingTokenIdentity(token, { ...matchingEnv(), [TENANT_ENV]: ` ${hash(tenant)} ` })).toBe(true)
  })

  it('hashes exact UTF-8 claim strings without trimming', () => {
    const exactSubject = ` ${subject}-é `
    const exactTenant = ` ${tenant}-é `
    const exactToken = jwt({ sub: exactSubject, [tenantClaim]: exactTenant })
    const env = { [SUBJECT_ENV]: hash(exactSubject), [TENANT_ENV]: hash(exactTenant) }
    expect(assertStagingTokenIdentity(exactToken, env)).toBe(true)
    expectRejection(exactToken, { ...env, [SUBJECT_ENV]: hash(exactSubject.trim()) },
      'subject_hash_mismatch', SUBJECT_ENV, [exactSubject, exactTenant])
    expectRejection(exactToken, { ...env, [TENANT_ENV]: hash(exactTenant.trim()) },
      'tenant_hash_mismatch', TENANT_ENV, [exactSubject, exactTenant])
  })

  it.each([
    { [tenantClaim]: 'fabricated-different-tenant-sentinel' },
    {},
    { [tenantClaim]: '' },
    { [tenantClaim]: 166 },
    { [tenantClaim]: null },
    { tenant_id: tenant },
    { org_id: tenant },
  ])('rejects mismatched or missing namespaced tenants (%j)', (claims) => {
    expectRejection(jwt({ sub: subject, ...claims }), { ...matchingEnv(), [TENANT_ENV]: hash(tenant) },
      'tenant_hash_mismatch', TENANT_ENV, [subject, tenant, claims[tenantClaim]])
  })

  it('does not use sub as a tenant fallback', () => {
    expectRejection(jwt({ sub: subject }), { ...matchingEnv(), [TENANT_ENV]: hash(subject) },
      'tenant_hash_mismatch', TENANT_ENV)
  })

  const invalidHashes = [hash(subject).toUpperCase(), 'a'.repeat(63), 'a'.repeat(65), 'g'.repeat(64),
    `sha256:${hash(subject)}`, subject, 166]
  for (const variable of [SUBJECT_ENV, TENANT_ENV, FORBIDDEN_ENV]) {
    it.each(invalidHashes)(`rejects invalid ${variable} (%s)`, (setting) => {
      expectRejection(token, { ...matchingEnv(), [variable]: setting }, 'identity_hash_config', variable)
    })
  }

  it.each([',', `${hash(subject)},`, `,${hash(subject)}`, `${hash(subject)}, ,${hash(tenant)}`])(
    'rejects empty entries in a nonblank forbidden list (%s)', (setting) => {
      expectRejection(token, { ...matchingEnv(), [FORBIDDEN_ENV]: setting }, 'identity_hash_config', FORBIDDEN_ENV)
    },
  )

  it('validates optional configuration before decoding a malformed token', () => {
    expectRejection('bad.token.', { ...matchingEnv(), [TENANT_ENV]: 'bad' }, 'identity_hash_config', TENANT_ENV)
  })
})

describe('staging JWT payload validation', () => {
  const malformed = [
    ['non-string number', 166],
    ['non-string object', { sub: subject }],
    ['one segment', header],
    ['two segments', `${header}.${encode({ sub: subject })}`],
    ['four segments', `${token}.extra`],
    ['empty header', `.${encode({ sub: subject })}.`],
    ['empty payload', `${header}..`],
    ['invalid header alphabet', `!.${encode({ sub: subject })}.`],
    ['invalid payload alphabet', `${header}.!.`],
    ['padded payload', `${header}.${encode({ sub: subject })}=.`],
    ['invalid signature alphabet', `${token}!`],
    ['impossible payload length', `${header}.A.`],
    ['noncanonical payload bits', `${header}.Zh.`],
    ['invalid UTF-8', `${header}.${Buffer.from([0xc3, 0x28]).toString('base64url')}.`],
    ['invalid UTF-8 inside JSON', `${header}.${Buffer.concat([
      Buffer.from(`{"sub":"${subject}","private":"`), Buffer.from([0xff]), Buffer.from('"}'),
    ]).toString('base64url')}.`],
    ['invalid JSON', `${header}.${Buffer.from(`{"sub":"${subject}",`).toString('base64url')}.`],
    ['null payload', jwt(null)],
    ['array payload', jwt([{ sub: subject }])],
    ['string payload', jwt(subject)],
    ['number payload', jwt(166)],
    ['boolean payload', jwt(true)],
    ['missing subject', jwt({ [tenantClaim]: tenant })],
    ['empty subject', jwt({ sub: '', [tenantClaim]: tenant })],
    ['number subject', jwt({ sub: 166, [tenantClaim]: tenant })],
    ['null subject', jwt({ sub: null })],
    ['object subject', jwt({ sub: { private: subject } })],
  ]
  it.each(malformed)('safely rejects %s', (_label, input) => {
    expectRejection(input, matchingEnv(), 'malformed_token', 'sub')
  })

  it('accepts a base64url signature without verifying it or requiring other claims', () => {
    expect(assertStagingTokenIdentity(`${jwt({ sub: subject })}fabricated_signature-166d`, matchingEnv())).toBe(true)
  })

  it('does not mutate explicit configuration', () => {
    const env = Object.freeze({ ...matchingEnv(), [TENANT_ENV]: hash(tenant), [FORBIDDEN_ENV]: '' })
    expect(assertStagingTokenIdentity(token, env)).toBe(true)
    expect(env).toEqual({ ...matchingEnv(), [TENANT_ENV]: hash(tenant), [FORBIDDEN_ENV]: '' })
  })
})
