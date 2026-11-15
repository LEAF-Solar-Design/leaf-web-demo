// Browser-free DOM verification. Inject installed dependencies with --deps.
const fs = require('node:fs')
const path = require('node:path')
const assert = require('node:assert/strict')
const Module = require('node:module')
const index = process.argv.indexOf('--deps')
const deps = path.resolve(index < 0 ? 'web/node_modules' : process.argv[index + 1])
const dependency = name => require(path.join(deps, name))
const { JSDOM } = dependency('jsdom')
const dom = new JSDOM('<html><body></body></html>', { url: 'http://localhost' })
global.window = dom.window
global.document = dom.window.document
Object.defineProperty(global, 'navigator', { configurable: true, value: dom.window.navigator })
global.HTMLElement = dom.window.HTMLElement
global.MutationObserver = dom.window.MutationObserver
const React = dependency('react')
const { render, screen, fireEvent, act, cleanup, waitFor } = dependency('@testing-library/react')
const { transformSync } = dependency('esbuild')
function component(name) {
  const file = path.resolve(__dirname, '../../../web/src/solar/' + name + '.jsx')
  const compiled = new Module(file, module)
  compiled.filename = file
  compiled.paths = [deps]
  compiled._compile(transformSync(fs.readFileSync(file, 'utf8'), { loader: 'jsx', format: 'cjs', jsx: 'automatic' }).code, file)
  return compiled.exports.default
}
const Confirmation = component('X07RoleConfirmation')
const row = block => ({ block, layer: 'MODULES', count: 3, insertion_bounds: [0, 0, 2, 2] })
const proposal = { module: { candidates: [row('Panel A'), row('Panel B')], first_choice: row('Panel A') } }
const mount = props => render(React.createElement(Confirmation, { proposal, ...props }))

async function main() {
  let calls = 0, finish, chosen
  mount({ onConfirm: value => { calls++; chosen = value; return new Promise(done => { finish = done }) } })
  fireEvent.click(screen.getByRole('radio', { name: /Panel B/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Confirm roles' }))
  fireEvent.click(screen.getByRole('button', { name: 'Saving roles…' }))
  await act(async () => {})
  assert.equal(calls, 1)
  assert.equal(chosen.block, 'Panel B')
  await act(async () => finish({ ok: true }))
  assert.ok(screen.getByText('Roles confirmed.'))
  cleanup()

  let attempts = 0
  mount({ onConfirm: () => ++attempts === 1 ? Promise.reject(new Error('Retry save')) : Promise.resolve() })
  fireEvent.click(screen.getByRole('radio', { name: /Panel B/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Confirm roles' }))
  assert.equal((await screen.findByRole('alert')).textContent, 'Retry save')
  assert.ok(screen.getByRole('radio', { name: /Panel B/ }).checked)
  fireEvent.click(screen.getByRole('button', { name: 'Confirm roles' }))
  assert.ok(await screen.findByText('Roles confirmed.'))
  assert.equal(attempts, 2)
  cleanup()

  let signal, complete, cancelled = 0
  mount({ onConfirm: (_value, options) => { signal = options.signal; return new Promise(done => { complete = done }) },
    onCancel: () => cancelled++ })
  fireEvent.click(screen.getByRole('button', { name: 'Confirm roles' }))
  await act(async () => {})
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
  assert.equal(cancelled, 1)
  assert.ok(signal.aborted)
  await act(async () => complete())
  assert.ok(screen.getByText('Role confirmation cancelled.'))
  assert.equal(screen.queryByText('Roles confirmed.'), null)
  cleanup()

  let saved = 0
  mount({ onConfirm: () => saved++, onCancel: () => cancelled++ })
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
  assert.equal(saved, 0)
  cleanup()
  await copyAndEditorChecks()
  dom.window.close()
  console.log('PASS: confirmation, copy choice, profile edits, customer switches, retry, pins and cancelled responses')
}

async function copyAndEditorChecks() {
  const Copy = component('X07CopyPicker'), Editor = component('X07CustomerProfileEditor')
  const copies = [0, 10].map(x => ({ block: 'Panel', layer: 'MODULES',
    design_window: [x, 0, x + 2, 2], oracle_count: 3, count: 999 }))
  let picks = 0, picked
  let view = render(React.createElement(Copy, { copies, onPick: value => { picks++; picked = value } }))
  assert.equal(screen.getAllByRole('cell', { name: '3' }).length, 2)
  assert.equal(screen.queryByText('999'), null)
  fireEvent.click(screen.getByRole('button', { name: 'Use copy 2' }))
  assert.ok(await screen.findByText('Design copy selected.'))
  assert.equal(picks, 1); assert.deepEqual(picked.design_window, [10, 0, 12, 2])
  cleanup()
  let cancelled = 0
  view = render(React.createElement(Copy, { copies: [], onCancel: () => cancelled++ }))
  assert.ok(screen.getByText('No design copies are available.'))
  view.rerender(React.createElement(Copy, { copies: [{ ...copies[0], design_window: [0, 0, Infinity, 2] }], onCancel: () => cancelled++ }))
  assert.ok(screen.getByRole('alert')); assert.equal(screen.queryByRole('button', { name: /Use copy/ }), null)
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' })); assert.equal(cancelled, 1)
  cleanup()
  let forbiddenPick = 0
  render(React.createElement(Copy, { copies, onPick: () => forbiddenPick++ }))
  fireEvent.click(screen.getByRole('button', { name: 'Use copy 1' }))
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
  await act(async () => {})
  assert.equal(forbiddenPick, 0); assert.ok(screen.getByText('Copy choice cancelled.'))
  cleanup()

  const record = (customer, version = 1, roles = { module: { block: 'Panel', layer: 'MODULES' } }) =>
    ({ customer_key: customer, profile_id: 'profile-' + customer, version, roles })
  let loads = 0, saves = []
  render(React.createElement(Editor, { customerKey: 'a', applicationRecord: record('a'),
    loadProfile: () => Promise.resolve(record('a', ++loads)),
    saveProfile: (customer, roles, options) => {
      saves.push({ customer, roles, version: options.expected_version })
      return saves.length === 1 ? Promise.reject({ code: 'STALE_VERSION' }) : Promise.resolve(record('a', 3, roles))
    } }))
  await screen.findByLabelText('Module layer')
  await waitFor(() => assert.ok(!screen.getByLabelText('Module layer').disabled))
  fireEvent.change(screen.getByLabelText('Module layer'), { target: { value: 'EDITED' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
  await screen.findByRole('alert')
  assert.equal(screen.getByLabelText('Module layer').value, 'EDITED')
  await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Reload version, keep edits' }).disabled))
  fireEvent.click(screen.getByRole('button', { name: 'Reload version, keep edits' }))
  await screen.findByText('Profile version: 2')
  await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
  assert.equal(screen.getByLabelText('Module layer').value, 'EDITED')
  fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
  await screen.findByText('Profile saved.')
  assert.deepEqual(saves.map(value => value.version), [1, 2])
  assert.equal(saves[1].roles.module.layer, 'EDITED')
  assert.ok(screen.getByText('Project is pinned to version 1.'))
  cleanup()

  let late, oldSignal
  view = render(React.createElement(Editor, { customerKey: 'a',
    loadProfile: (_customer, options) => { oldSignal = options.signal; return new Promise(done => { late = done }) } }))
  await act(async () => {})
  view.rerender(React.createElement(Editor, { customerKey: 'b', applicationRecord: record('a'), loadProfile: () => Promise.resolve(record('b')) }))
  await screen.findByText('Profile id: profile-b')
  await act(async () => late(record('a')))
  assert.ok(oldSignal.aborted); assert.equal(screen.queryByText('Profile id: profile-a'), null)
  assert.equal(screen.queryByText(/Project is pinned/), null)
  cleanup()
  render(React.createElement(React.StrictMode, null, React.createElement(Editor, {
    customerKey: 'a', loadProfile: () => Promise.resolve(record('a')) })))
  assert.ok(await screen.findByLabelText('Module layer'))
  cleanup()
}
main().catch(error => { cleanup(); dom.window.close(); console.error(error); process.exitCode = 1 })
