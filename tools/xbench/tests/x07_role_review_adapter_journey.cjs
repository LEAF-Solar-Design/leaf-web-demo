// Actual scoped review JSX -> existing offline JSON bridge -> WorkflowAdapter.
const assert = require('node:assert/strict')
const fs = require('node:fs'), path = require('node:path'), Module = require('node:module')
const { execFileSync } = require('node:child_process')
const h = require('./x07_adapter_jsx_journey.cjs')
const at = process.argv.indexOf('--deps')
const deps = path.resolve(at < 0 ? 'web/node_modules' : process.argv[at + 1])
const { transformSync } = require(path.join(deps, 'esbuild'))
const cache = new Map()
function load(file) {
  if (cache.has(file)) return cache.get(file)
  const compiled = new Module(file, module)
  compiled.filename = file; compiled.paths = [deps]
  const nativeRequire = compiled.require.bind(compiled)
  compiled.require = name => {
    if (name === './X07RoleConfirmation.jsx') return { default: h.component('X07RoleConfirmation'), __esModule: true }
    if (name.startsWith('./')) return load(path.resolve(path.dirname(file), name))
    return nativeRequire(name)
  }
  compiled._compile(transformSync(fs.readFileSync(file, 'utf8'), {
    loader: file.endsWith('.jsx') ? 'jsx' : 'js', format: 'cjs', jsx: 'automatic',
  }).code, file)
  cache.set(file, compiled.exports)
  return compiled.exports
}

async function main() {
  try {
    const Review = load(path.resolve(__dirname, '../../../web/src/solar/X07DrawingRoleReview.jsx')).default
    const scope = h.ctx('role-review')
    const binding = { project_id: scope.project_key, drawing_id: scope.drawing_key }
    const intake = { inserts: [0, 1, 2].map(x => ({ name: 'Panel', layer: 'MODULES', x, y: 0 })) }
    const copies = [{ block: 'Panel', layer: 'MODULES', design_window: [0, 0, 2, 0], oracle_count: 3 }]
    const fixture = { intake, copy_candidates: copies, receipt_database: path.join(h.resources().root, 'receipts.sqlite') }
    let plan, confirmations = 0
    const client = { readContext: async ({ signal }) => {
      plan = await h.rpc(scope, 'load', scope, undefined, signal, null, null, fixture)
      return { ok: true, data: { role_discovery: plan.compatibility.proposal } }
    } }
    const onConfirm = async (selection, options) => {
      confirmations++
      assert.equal(options.authorityKey, 'synthetic-authority')
      assert.deepEqual(options.solarBinding, binding)
      const result = await h.rpc(scope, 'confirm', scope, { ...selection, expected_version: 0,
        plan_token: plan.plan_token }, options.signal, null, null, fixture)
      const independent = intake.inserts.filter(item => item.name === selection.block && item.layer === selection.layer
        && item.x >= selection.design_window[0] && item.x <= selection.design_window[2]
        && item.y >= selection.design_window[1] && item.y <= selection.design_window[3]).length
      assert.equal(independent, 3)
      assert.equal(result.selection.count, independent)
      assert.equal(result.customer_key, scope.customer_key)
      assert.equal(result.project_key, scope.project_key)
      assert.equal(result.drawing_key, scope.drawing_key)
      return { ok: true }
    }
    h.render(h.React.createElement(Review, { projectClient: client, solarBinding: binding,
      authorityKey: 'synthetic-authority', isCurrent: () => true, onConfirm }))
    h.fireEvent.click(await h.screen.findByRole('button', { name: 'Confirm roles' }, { timeout: 15000 }))
    h.fireEvent.submit(h.screen.getByRole('form', { name: 'Confirm drawing roles' }))
    await h.screen.findByText('Roles confirmed.', {}, { timeout: 15000 })
    assert.equal(confirmations, 1)
    const audit = `import json,sqlite3,sys
with sqlite3.connect('file:'+sys.argv[1]+'?mode=rw',uri=True) as db:
 versions=db.execute('SELECT customer_key,version FROM profile_versions ORDER BY customer_key,version').fetchall()
 pins=db.execute('SELECT project_key,customer_key,version FROM project_applications ORDER BY project_key').fetchall()
 immutable=False
 try: db.execute('UPDATE profile_versions SET version=2')
 except sqlite3.IntegrityError as error: immutable=str(error)=='profile versions are immutable'
 pinned=False
 try: db.execute('UPDATE project_applications SET version=2')
 except sqlite3.IntegrityError as error: pinned=str(error)=='project applications are pinned'
with sqlite3.connect('file:'+sys.argv[2]+'?mode=ro',uri=True) as db:
 receipts=db.execute('SELECT COUNT(*) FROM confirmations').fetchone()[0]
print(json.dumps({'versions':versions,'pins':pins,'receipts':receipts,'immutable':immutable,'pinned':pinned}))`
    const rows = JSON.parse(execFileSync('python', ['-P', '-c', audit, h.resources().database, fixture.receipt_database], {
      timeout: 10000, windowsHide: true, encoding: 'utf8',
    }))
    assert.deepEqual(rows.versions, [[scope.customer_key, 1]])
    assert.deepEqual(rows.pins, [[scope.project_key, scope.customer_key, 1]])
    assert.equal(rows.receipts, 1)
    assert.equal(rows.immutable, true)
    assert.equal(rows.pinned, true)
    assert.equal(h.resources().child_count, 0)
    console.log('PASS: role review adapter journey, selected count3, captured scope, one version, one pin, one receipt')
  } finally { await h.dispose() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
