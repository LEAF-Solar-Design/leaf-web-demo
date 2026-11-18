// One owned offline DOM and dependency cache, shared by the two bounded fixtures.
const path = require('node:path'), fs = require('node:fs'), Module = require('node:module')
const h = require('./x07_adapter_jsx_journey.cjs')
const at = process.argv.indexOf('--deps'), deps = path.resolve(at < 0 ? 'web/node_modules' : process.argv[at + 1])
const { transformSync } = require(path.join(deps, 'esbuild'))
const { act } = require(path.join(deps, '@testing-library/react'))
const cache = new Map()
function load(file) {
  if (cache.has(file)) return cache.get(file)
  const compiled = new Module(file, module); compiled.filename = file; compiled.paths = [deps]
  const native = compiled.require.bind(compiled)
  compiled.require = name => name.startsWith('./') ? load(path.resolve(path.dirname(file), name)) : native(name)
  compiled._compile(transformSync(fs.readFileSync(file, 'utf8'), {
    loader: file.endsWith('.jsx') ? 'jsx' : 'js', format: 'cjs', jsx: 'automatic',
  }).code, file)
  cache.set(file, compiled.exports); return compiled.exports
}
const Review = load(path.resolve(__dirname, '../../../web/src/solar/X07DrawingRoleProfileReview.jsx')).default
const roles = { module: { block: 'Panel', layer: 'MODULES' } }
const pin = { customer_key: 'customer-a', project_key: 'project-a', profile_id: 'profile-a', version: 1, roles }
const ack = () => ({ ok: true, drawing_key: 'drawing-a', applied_profile: JSON.parse(JSON.stringify(pin)) })
const row = { block: 'Panel', layer: 'MODULES', count: 3, insertion_bounds: [0, 0, 2, 2] }
const reply = { ok: true, data: { role_discovery: { schema: 'leaf.drawing-role-discovery.v1',
  module: { first_choice: row, candidates: [row], competing: false }, roof: [], obstruction: [] } } }
const client = () => ({ readContext: async () => reply })
const props = () => ({ customerKey: 'customer-a', drawingKey: 'drawing-a', authorityKey: 'authority-a',
  solarBinding: { project_id: 'project-a' }, projectClient: client(), isCurrent: () => true,
  onConfirm: async () => ack(), loadProfile: async () => JSON.parse(JSON.stringify(pin)), saveProfile: async () => { throw Error('Unexpected save') } })
const draw = value => h.React.createElement(Review, value)
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } }
async function enter(button) {
  // jsdom has no browser default keyboard activation. Dispatch both keyboard
  // events and the resulting default click explicitly; native Enter is captured
  // independently with the canonical browser capture.
  button.focus(); h.fireEvent.keyDown(button, { key: 'Enter', code: 'Enter' })
  h.fireEvent.keyUp(button, { key: 'Enter', code: 'Enter' }); h.fireEvent.click(button)
}
module.exports = { ...h, act, props, pin, ack, client, draw, deferred, enter }
