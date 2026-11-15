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
const { render, screen, fireEvent, act, cleanup } = dependency('@testing-library/react')
const { transformSync } = dependency('esbuild')
const file = path.resolve(__dirname, '../../../web/src/solar/X07RoleConfirmation.jsx')
const compiled = new Module(file, module)
compiled.filename = file
compiled.paths = [deps]
compiled._compile(transformSync(fs.readFileSync(file, 'utf8'), { loader: 'jsx', format: 'cjs', jsx: 'automatic' }).code, file)
const Confirmation = compiled.exports.default
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
  dom.window.close()
  console.log('PASS: competitor choice, single confirmation, retry, cancellation and late completion retirement')
}
main().catch(error => { cleanup(); dom.window.close(); console.error(error); process.exitCode = 1 })
