// Actual composed JSX, no browser/network. All async receipts are controlled.
const fs = require('node:fs'), path = require('node:path'), Module = require('node:module')
const assert = require('node:assert/strict')
const at = process.argv.indexOf('--deps')
const deps = path.resolve(at < 0 ? 'web/node_modules' : process.argv[at + 1])
const dependency = name => require(path.join(deps, name))
const { JSDOM } = dependency('jsdom')
const dom = new JSDOM('<html><body></body></html>', { url: 'http://localhost' })
global.window = dom.window; global.document = dom.window.document
Object.defineProperty(global, 'navigator', { configurable: true, value: dom.window.navigator })
global.HTMLElement = dom.window.HTMLElement; global.MutationObserver = dom.window.MutationObserver
global.fetch = dom.window.fetch = () => { throw new Error('Network is forbidden in offline workflow checks') }
const React = dependency('react'), { render, screen, fireEvent, act, cleanup } = dependency('@testing-library/react')
const { transformSync } = dependency('esbuild'), cache = new Map()
function component(name) {
  if (cache.has(name)) return cache.get(name)
  const file = path.resolve(__dirname, '../../../web/src/solar/' + name + '.jsx')
  const compiled = new Module(file, module); compiled.filename = file; compiled.paths = [deps]
  const nativeRequire = compiled.require.bind(compiled)
  compiled.require = request => request.startsWith('./X07') ? { default: component(request.slice(2)), __esModule: true } : nativeRequire(request)
  compiled._compile(transformSync(fs.readFileSync(file, 'utf8'), { loader: 'jsx', format: 'cjs', jsx: 'automatic' }).code, file)
  cache.set(name, compiled.exports.default); return compiled.exports.default
}
const Workflow = component('X07ProfileWorkflow')
const context = (drawing = 'd1:v1', customer = 'a') => ({ customer_key: customer, project_key: 'p-' + customer, drawing_key: drawing })
const row = { block: 'Panel', layer: 'MODULES', count: 3, insertion_bounds: [0, 0, 2, 0] }
const copies = [0, 10].map(x => ({ block: 'Panel', layer: 'MODULES', oracle_count: 3, design_window: [x, 0, x + 2, 0] }))
const profile = (customer = 'a') => ({ customer_key: customer, project_key: 'p-' + customer,
  profile_id: 'profile-' + customer, version: 1, roles: { module: { block: 'Panel', layer: 'MODULES' } } })
const plan = (ctx, reuse = false, choices = []) => ({ ...ctx, expected_version: 0, copies: choices,
  compatibility: { customer_key: ctx.customer_key, decision: reuse ? 'apply' : 'propose_fresh',
    proposal: { module: { candidates: [row], first_choice: row } } } })
const applied = ctx => ({ ...ctx, state: 'applied', applied_profile: profile(ctx.customer_key), selection: { count: 3 } })
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
const flush = () => act(async () => {})
const element = (ctx, props = {}) => React.createElement(Workflow, { customerKey: ctx.customer_key,
  projectKey: ctx.project_key, drawingKey: ctx.drawing_key, loadWorkflow: () => Promise.resolve(plan(ctx)), ...props })

async function happyPaths() {
  const ctx = context(); let confirmed = [], announced = []
  render(element(ctx, { confirmRoles: (scope, choice) => { confirmed.push({ scope, choice }); return applied(scope) },
    onApplied: value => announced.push(value), loadProfile: () => Promise.resolve(profile()), saveProfile: () => {} }))
  await screen.findByRole('button', { name: 'Confirm roles' })
  fireEvent.click(screen.getByRole('button', { name: 'Confirm roles' }))
  await screen.findByText('Drawing profile applied.')
  await screen.findByLabelText('Module block') // Third child is composed after application.
  assert.equal(confirmed.length, 1); assert.equal(announced.length, 1)
  assert.deepEqual(confirmed[0].scope, ctx); assert.equal(confirmed[0].choice.expected_version, 0)
  cleanup()

  let picks = [], saves = 0
  render(element(ctx, { loadWorkflow: () => Promise.resolve(plan(ctx, false, copies)),
    confirmRoles: (scope, choice) => { saves++; picks.push(choice); return applied(scope) } }))
  await screen.findByRole('button', { name: 'Confirm roles' })
  fireEvent.click(screen.getByRole('button', { name: 'Confirm roles' }))
  await screen.findByRole('button', { name: 'Use copy 2' })
  assert.equal(saves, 0)
  fireEvent.click(screen.getByRole('button', { name: 'Use copy 2' }))
  await screen.findByText('Drawing profile applied.')
  assert.deepEqual(picks[0].design_window, [10, 0, 12, 0]); assert.equal(saves, 1)
  cleanup()

  let applies = []
  render(element(ctx, { loadWorkflow: () => Promise.resolve(plan(ctx, true, copies)),
    applyProfile: (scope, choice) => { applies.push(choice); return applied(scope) } }))
  await screen.findByRole('button', { name: 'Use copy 1' })
  assert.equal(screen.queryByRole('button', { name: 'Confirm roles' }), null)
  fireEvent.click(screen.getByRole('button', { name: 'Use copy 1' }))
  await screen.findByText('Drawing profile applied.'); assert.equal(applies.length, 1)
  cleanup()
  render(element(ctx, { loadWorkflow: () => Promise.resolve(plan(ctx, true, copies.slice(0, 1))),
    applyProfile: scope => { applies.push('automatic'); return applied(scope) } }))
  await screen.findByText('Drawing profile applied.'); assert.equal(applies[1], 'automatic')
  cleanup()
}

async function races() {
  const first = context(), next = context('d2:v1', 'b')
  for (const kind of ['drawing', 'cancel', 'unmount']) {
    const save = deferred(); let signal, announced = []
    const view = render(element(first, { confirmRoles: (_scope, _choice, options) => { signal = options.signal; return save.promise },
      onApplied: value => announced.push(value) }))
    await screen.findByRole('button', { name: 'Confirm roles' })
    fireEvent.click(screen.getByRole('button', { name: 'Confirm roles' })); await flush()
    if (kind === 'drawing') {
      view.rerender(element(next, { onApplied: value => announced.push(value) }))
      await screen.findByRole('button', { name: 'Confirm roles' })
    } else if (kind === 'cancel') fireEvent.click(screen.getByRole('button', { name: 'Cancel workflow' }))
    else view.unmount()
    assert.ok(signal.aborted)
    await act(async () => save.resolve(applied(first)))
    assert.equal(announced.length, 0); assert.equal(screen.queryByText('Drawing profile applied.'), null)
    if (kind === 'cancel') assert.ok(screen.getByText('Profile workflow cancelled.'))
    cleanup()
  }
  const receipt = deferred(); let signal, announced = 0
  const view = render(element(first, { loadWorkflow: () => Promise.resolve(plan(first, true, copies)),
    applyProfile: (_ctx, _value, opts) => { signal = opts.signal; return receipt.promise }, onApplied: () => announced++ }))
  await screen.findByRole('button', { name: 'Use copy 2' })
  fireEvent.click(screen.getByRole('button', { name: 'Use copy 2' })); await flush()
  view.rerender(element(next)); await screen.findByRole('button', { name: 'Confirm roles' })
  await act(async () => receipt.resolve(applied(first)))
  assert.ok(signal.aborted); assert.equal(announced, 0); cleanup()

  const oldLoad = deferred(); let oldSignal
  const loader = render(element(first, { loadWorkflow: (_ctx, opts) => { oldSignal = opts.signal; return oldLoad.promise } }))
  await flush(); loader.rerender(element(next)); await screen.findByRole('button', { name: 'Confirm roles' })
  await act(async () => oldLoad.resolve(plan(first, true, copies)))
  assert.ok(oldSignal.aborted); assert.equal(screen.queryByRole('button', { name: 'Use copy 1' }), null); cleanup()

  let forbidden = 0
  render(element(first, { confirmRoles: () => forbidden++ }))
  await screen.findByRole('button', { name: 'Confirm roles' })
  fireEvent.click(screen.getByRole('button', { name: 'Confirm roles' }))
  fireEvent.click(screen.getByRole('button', { name: 'Cancel workflow' })); await flush()
  assert.equal(forbidden, 0); cleanup()

  let events = 0
  render(element(first, { confirmRoles: () => applied(next), onApplied: () => events++ }))
  await screen.findByRole('button', { name: 'Confirm roles' }); fireEvent.click(screen.getByRole('button', { name: 'Confirm roles' }))
  await screen.findByRole('alert'); assert.equal(events, 0); cleanup()
  render(React.createElement(React.StrictMode, null, element(first)))
  await screen.findByRole('button', { name: 'Confirm roles' }); cleanup()
}
async function main() {
  try { await happyPaths(); await races(); console.log('PASS: composed confirmation/save/editor, reuse/copy and controlled drawing/cancel/unmount races') }
  finally { cleanup(); dom.window.close() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
