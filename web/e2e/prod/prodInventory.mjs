const open = 'open-project-prod-readonly.spec.mjs'
const unified = 'unified-prod-readonly.spec.mjs'
export const PROD_INVENTORY = Object.freeze([
  [open, 'P-146 production projects are readable'],
  [open, 'P-146 production project board renders read-only'],
  [open, 'P-058 production solar_template_beta is on'],
  [open, 'P-058 production conv_durable is on'],
  [unified, 'production-like unified route is reachable without mutation'],
  [unified, 'production app health reports its served source'],
  [unified, 'production web health reports its served source'],
  [unified, 'production deployment identity answers without a bearer'],
  [unified, 'production auth ladder rejects absent and invalid bearers'],
  [unified, 'production app displays its served build stamp'],
  [unified, 'production app renders the studio shell'],
  [unified, 'production app and web serve the expected candidate'],
].map(([file, title], index) => Object.freeze({ file, title, id: `PROD_${String(index + 1).padStart(2, '0')}` })))

export const CANARY_INVENTORY = Object.freeze([Object.freeze({
  file: 'artifact-failure.canary.mjs', title: 'authenticated artifact failure canary', id: 'CANARY_01',
})])

const key = ({ file, title }) => JSON.stringify([file.replaceAll('\\', '/').split('/').at(-1), title])
const annotatedFailure = (test) => (test.annotations || []).some(({ type }) => ['skip', 'fixme', 'fail'].includes(type))

export class InventoryRun {
  constructor(expected = PROD_INVENTORY) {
    this.expected = expected
    this.collected = new Map()
    this.started = new Map()
    this.completed = new Map()
    this.invalid = false
    this.errors = 0
    this.denials = {}
  }
  collect(tests) {
    for (const test of tests) {
      const identity = key(test)
      if (this.collected.has(identity) || !this.expected.some((row) => key(row) === identity)
          || test.expectedStatus !== 'passed' || annotatedFailure(test) || (test.repeatEachIndex ?? 0) !== 0) this.invalid = true
      this.collected.set(identity, test)
    }
    if (this.collected.size !== this.expected.length) this.invalid = true
  }
  begin(test, result) {
    const identity = key(test)
    this.started.set(identity, (this.started.get(identity) || 0) + 1)
    if (!this.collected.has(identity) || result.retry !== 0) this.invalid = true
  }
  end(test, result) {
    const identity = key(test)
    if (this.completed.has(identity) || !this.collected.has(identity) || result.status !== 'passed'
        || result.retry !== 0 || test.expectedStatus !== 'passed' || annotatedFailure(test)
        || (result.errors || []).length || (result.attachments || []).length) this.invalid = true
    this.completed.set(identity, result.status)
  }
  error() { this.errors += 1 }
  finish(status) {
    const passed = status === 'passed' && !this.invalid && !this.errors
      && this.completed.size === this.expected.length
      && this.expected.every((row) => this.collected.has(key(row)) && this.started.get(key(row)) === 1 && this.completed.get(key(row)) === 'passed')
    return {
      schema: 'prod-readonly-v1', status: passed ? 'passed' : 'failed', code: passed ? 'PROD_OK' : 'PROD_CONTRACT_FAILED',
      tests: this.expected.map((row) => ({ id: row.id, started: this.started.get(key(row)) || 0, status: this.completed.get(key(row)) === 'passed' ? 'passed' : 'failed' })),
      denials: { ...this.denials },
    }
  }
}

export function validateReceipt(value, expected = PROD_INVENTORY) {
  if (!value || value.schema !== 'prod-readonly-v1' || !['passed', 'failed'].includes(value.status)
      || value.code !== (value.status === 'passed' ? 'PROD_OK' : 'PROD_CONTRACT_FAILED')
      || Object.keys(value).sort().join(',') !== 'code,denials,schema,status,tests'
      || !Array.isArray(value.tests) || value.tests.length !== expected.length) throw new Error('PROD_RECEIPT_INVALID')
  const tests = value.tests.map((row, index) => {
    if (Object.keys(row).sort().join(',') !== 'id,started,status' || row.id !== expected[index].id
        || !Number.isSafeInteger(row.started) || row.started < 0 || !['passed', 'failed'].includes(row.status)
        || (value.status === 'passed' && (row.started !== 1 || row.status !== 'passed'))) throw new Error('PROD_RECEIPT_INVALID')
    return { id: row.id, started: row.started, status: row.status }
  })
  const denials = {}
  if (!value.denials || typeof value.denials !== 'object' || Array.isArray(value.denials)) throw new Error('PROD_RECEIPT_INVALID')
  for (const [code, count] of Object.entries(value.denials)) {
    if (!['METHOD', 'URL', 'ORIGIN', 'REDIRECT', 'WEBSOCKET', 'ESCAPE', 'TRANSPORT', 'ARTIFACT'].includes(code)
        || !Number.isSafeInteger(count) || count < 0 || (value.status === 'passed' && count)) throw new Error('PROD_RECEIPT_INVALID')
    denials[code] = count
  }
  return { schema: value.schema, status: value.status, code: value.code, tests, denials }
}
