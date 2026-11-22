// Actual composed JSX with user-event keyboard defaults in jsdom. No browser.
// --keyboard-deps can name an isolated install of @testing-library/user-event.
const fs = require('node:fs'), path = require('node:path'), Module = require('node:module')
const assert = require('node:assert/strict')
const arg = (flag, fallback) => { const at = process.argv.indexOf(flag); return path.resolve(at < 0 ? fallback : process.argv[at + 1]) }
const deps = arg('--deps', 'web/node_modules'), keyboardDeps = arg('--keyboard-deps', deps)
const dependency = name => require(path.join(deps, name))
const { JSDOM } = dependency('jsdom')
const dom = new JSDOM('<html><body></body></html>', { url: 'http://localhost' })
// jsdom lacks CSS.escape. These tests use only identifier-safe radio names.
dom.window.CSS = { escape: name => { assert.match(name, /^[a-zA-Z0-9_-]+$/); return name } }
global.window = dom.window; global.document = dom.window.document
Object.defineProperty(global, 'navigator', { configurable: true, value: dom.window.navigator })
global.HTMLElement = dom.window.HTMLElement; global.MutationObserver = dom.window.MutationObserver
global.fetch = dom.window.fetch = () => { throw new Error('Offline keyboard checks forbid network') }
const React = dependency('react'), { render, screen, cleanup, waitFor } = dependency('@testing-library/react')
const userEvent = require(path.join(keyboardDeps, '@testing-library/user-event')).default
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
const ctx = { customer_key: 'a', project_key: 'p-a', drawing_key: 'd-a:v1' }
const row = block => ({ block, layer: 'MODULES', count: 2, insertion_bounds: [0, 0, 1, 0] })
const copies = [0, 10].map(x => ({ block: 'Panel B', layer: 'MODULES', oracle_count: 2, design_window: [x, 0, x + 1, 0] }))
const plan = (reuse = false, options = copies) => ({ ...ctx, expected_version: 0, copies: options,
  compatibility: { customer_key: 'a', decision: reuse ? 'apply' : 'propose_fresh',
    proposal: { module: { first_choice: row('Panel A'), candidates: [row('Panel A'), row('Panel B')] } } } })
const applied = () => ({ ...ctx, state: 'applied', applied_profile: { customer_key: 'a', project_key: 'p-a', profile_id: 'profile-a', version: 1 } })
const mount = props => render(React.createElement(Workflow, { customerKey: 'a', projectKey: 'p-a', drawingKey: 'd-a:v1', ...props }))

async function main() {
  try {
    let calls = [], attempts = 0
    let user = userEvent.setup({ document: dom.window.document })
    mount({ loadWorkflow: () => Promise.resolve(plan(false, copies.slice(0, 1))), confirmRoles: (_context, choice) => {
      calls.push(choice); if (++attempts === 1) throw new Error('Please retry the save.'); return applied()
    } })
    await screen.findByRole('button', { name: 'Confirm roles' })
    await user.tab()
    assert.equal(document.activeElement, screen.getByRole('radio', { name: /Panel A on MODULES/ }))
    await user.keyboard('{ArrowRight}')
    assert.ok(screen.getByRole('radio', { name: /Panel B on MODULES/ }).checked)
    await user.tab(); assert.equal(document.activeElement, screen.getByRole('button', { name: 'Confirm roles' }))
    await user.keyboard('{Enter}')
    assert.equal((await screen.findByRole('alert')).textContent, 'Please retry the save.')
    assert.ok(screen.getByRole('radio', { name: /Panel B/ }).checked)
    await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Confirm roles' }).disabled))
    await user.keyboard('{Enter}')
    await screen.findByText('Drawing profile applied.')
    assert.equal(calls.length, 2); assert.ok(calls.every(choice => choice.block === 'Panel B'))
    cleanup()

    user = userEvent.setup({ document: dom.window.document }); calls = []
    mount({ loadWorkflow: () => Promise.resolve(plan()), confirmRoles: (_context, choice) => { calls.push(choice); return applied() } })
    await screen.findByRole('button', { name: 'Confirm roles' })
    await user.tab(); await user.keyboard('{ArrowRight}'); await user.tab(); await user.keyboard(' ')
    await screen.findByRole('button', { name: 'Use copy 2' })
    assert.equal(calls.length, 0)
    // The confirmation unmounts. Start from the document and use tab order only.
    await user.tab(); assert.equal(document.activeElement, screen.getByRole('button', { name: 'Use copy 1' }))
    await user.tab(); assert.equal(document.activeElement, screen.getByRole('button', { name: 'Use copy 2' }))
    await user.keyboard('{Enter}'); await screen.findByText('Drawing profile applied.')
    assert.deepEqual(calls[0].design_window, [10, 0, 11, 0]); cleanup()

    user = userEvent.setup({ document: dom.window.document }); let attemptsCopy = 0
    mount({ loadWorkflow: () => Promise.resolve(plan(true)), applyProfile: () => {
      if (++attemptsCopy === 1) return Promise.reject(new Error('temporary copy failure')); return applied()
    } })
    await screen.findByRole('button', { name: 'Use copy 1' })
    await user.tab(); await user.keyboard(' ')
    assert.equal((await screen.findByRole('alert')).textContent, 'Copy choice could not be saved. Try again.')
    await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Use copy 1' }).disabled))
    await user.keyboard('{Enter}'); await screen.findByText('Drawing profile applied.'); assert.equal(attemptsCopy, 2)
    cleanup()

    user = userEvent.setup({ document: dom.window.document }); let saves = 0, cancels = 0
    mount({ loadWorkflow: () => Promise.resolve(plan(true)), applyProfile: () => saves++, onCancel: () => cancels++ })
    await screen.findByRole('button', { name: 'Use copy 1' })
    await user.tab(); await user.tab(); await user.tab()
    assert.equal(document.activeElement, screen.getByRole('button', { name: 'Cancel' }))
    await user.keyboard('{Enter}'); await screen.findByText('Profile workflow cancelled.')
    assert.equal(cancels, 1); assert.equal(saves, 0)
    console.log('PASS: keyboard tab/radio arrows/Enter/Space, confirmation and copy retries, alert roles, copy choice and cancellation')
  } finally { cleanup(); dom.window.close() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
