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
    if (name === './X07RoleConfirmation.jsx' || name === './X07CustomerProfileEditor.jsx') {
      return { default: h.component(path.basename(name, '.jsx')), __esModule: true }
    }
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
    const ProfileReview = load(path.resolve(__dirname, '../../../web/src/solar/X07DrawingRoleProfileReview.jsx')).default
    const scope = h.ctx('role-review')
    const binding = { project_id: scope.project_key, drawing_id: scope.drawing_key }
    const intake = { inserts: [0, 1, 2].map(x => ({ name: 'Panel', layer: 'MODULES', x, y: 0 })), polylines: [{ layer: 'ROOF' }] }
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
      return { ...result, ok: true }
    }
    h.render(h.React.createElement(ProfileReview, { projectClient: client, solarBinding: binding,
      authorityKey: 'synthetic-authority', isCurrent: () => true, onConfirm, customerKey: scope.customer_key,
      drawingKey: scope.drawing_key,
      loadProfile: (customer, options) => h.rpc(scope, 'editor_load', scope, customer, options.signal, null, null, fixture),
      saveProfile: (customer, roles, options) => h.rpc(scope, 'editor_save', scope,
        { customer, roles, expected_version: options.expected_version }, options.signal, null, null, fixture) }))
    h.fireEvent.click(await h.screen.findByRole('button', { name: 'Confirm roles' }, { timeout: 15000 }))
    h.fireEvent.submit(h.screen.getByRole('form', { name: 'Confirm drawing roles' }))
    await h.screen.findByText('Roles confirmed.', {}, { timeout: 15000 })
    assert.equal(confirmations, 1)
    await h.screen.findByText('Profile version: 1', {}, { timeout: 15000 })
    h.fireEvent.click(h.screen.getByRole('button', { name: 'Add roof role' }))
    h.fireEvent.change(h.screen.getByRole('textbox', { name: 'Roof layer' }), { target: { value: 'ROOF' } })
    h.fireEvent.click(h.screen.getByRole('button', { name: 'Save profile' }))
    await h.screen.findByText('Profile version: 2', {}, { timeout: 15000 })
    assert.ok(h.screen.getByText('Project is pinned to version 1.'))
    const pinned = h.screen.getByRole('region', { name: 'Pinned drawing roles' })
    assert.ok(pinned.textContent.includes('Pinned profile version: 1'))
    assert.ok(pinned.textContent.includes('Panel on MODULES'))
    assert.equal(pinned.textContent.includes('ROOF'), false)
    const firstPin = (await h.rpc(scope, 'load', scope, null, null, null, null, fixture)).application_record
    h.cleanup()

    const expectedPins = [[scope.project_key, scope.customer_key, 1]]
    for (const count of [4, 5]) {
      const later = h.ctx('later-' + count)
      const laterIntake = { inserts: Array.from({ length: count }, (_, x) => ({ name: 'Panel', layer: 'MODULES', x, y: 0 })),
        polylines: [{ layer: 'ROOF' }] }
      const laterFixture = { ...fixture, intake: laterIntake,
        copy_candidates: [{ block: 'Panel', layer: 'MODULES', design_window: [0, 0, count - 1, 0], oracle_count: count }] }
      let applied
      h.mount(later, {
        loadWorkflow: (context, options) => h.rpc(later, 'load', context, null, options.signal, null, null, laterFixture),
        confirmRoles: () => { throw new Error('Unexpected repeated confirmation') },
        applyProfile: (context, choice, options) => h.rpc(later, 'apply', context, choice, options.signal, null, null, laterFixture),
        onApplied: result => { applied = result },
        loadProfile: (customer, options) => h.rpc(later, 'editor_load', later, customer, options.signal, null, null, laterFixture),
      })
      await h.screen.findByText('Drawing profile applied.', {}, { timeout: 15000 })
      assert.equal(applied.selection.count, laterIntake.inserts.length)
      assert.equal(applied.applied_profile.version, 2)
      assert.equal(applied.applied_profile.customer_key, scope.customer_key)
      expectedPins.push([later.project_key, later.customer_key, 2])
      h.cleanup()
    }

    const other = { ...h.ctx('b-seed'), customer_key: 'synthetic-b' }
    const otherIntake = { inserts: [0, 1].map(x => ({ name: 'OtherPanel', layer: 'B_MODULES', x, y: 0 })) }
    const otherFixture = { ...fixture, intake: otherIntake,
      copy_candidates: [{ block: 'OtherPanel', layer: 'B_MODULES', design_window: [0, 0, 1, 0], oracle_count: 2 }] }
    let otherPlan, otherConfirmations = 0
    h.render(h.React.createElement(Review, { authorityKey: 'synthetic-b-authority', solarBinding: { project_id: other.project_key },
      isCurrent: () => true,
      projectClient: { readContext: async ({ signal }) => {
        otherPlan = await h.rpc(other, 'load', other, null, signal, null, null, otherFixture)
        assert.equal(otherPlan.compatibility.decision, 'propose_fresh')
        assert.equal(JSON.stringify(otherPlan).includes(firstPin.profile_id), false)
        return { ok: true, data: { role_discovery: otherPlan.compatibility.proposal } }
      } }, onConfirm: async (selection, options) => {
        otherConfirmations++
        const result = await h.rpc(other, 'confirm', other, { ...selection, expected_version: 0, plan_token: otherPlan.plan_token },
          options.signal, null, null, otherFixture)
        assert.equal(result.selection.count, otherIntake.inserts.length)
        assert.equal(result.customer_key, other.customer_key)
        return { ok: true }
      } }))
    h.fireEvent.click(await h.screen.findByRole('button', { name: 'Confirm roles' }, { timeout: 15000 }))
    await h.screen.findByText('Roles confirmed.', {}, { timeout: 15000 })
    assert.equal(otherConfirmations, 1)
    expectedPins.push([other.project_key, other.customer_key, 1])
    h.cleanup()

    const otherNext = { ...other, project_key: 'b-copies', drawing_key: 'b-copies:v1' }
    const copyIntake = { inserts: [0, 1, 10, 11, 12].map(x => ({ name: 'OtherPanel', layer: 'B_MODULES', x, y: 0 })) }
    const copyFixture = { ...fixture, intake: copyIntake, copy_candidates: [
      { block: 'OtherPanel', layer: 'B_MODULES', design_window: [0, 0, 1, 0], oracle_count: 2 },
      { block: 'OtherPanel', layer: 'B_MODULES', design_window: [10, 0, 12, 0], oracle_count: 3 }] }
    let copyApplied, picks = 0
    h.mount(otherNext, {
      loadWorkflow: (context, options) => h.rpc(otherNext, 'load', context, null, options.signal, null, null, copyFixture),
      confirmRoles: () => { throw new Error('Unexpected B repeated confirmation') },
      applyProfile: (context, choice, options) => {
        picks++
        return h.rpc(otherNext, 'apply', context, choice, options.signal, null, null, copyFixture)
      }, onApplied: result => { copyApplied = result },
      loadProfile: (customer, options) => h.rpc(otherNext, 'editor_load', otherNext, customer, options.signal, null, null, copyFixture),
    })
    h.fireEvent.click(await h.screen.findByRole('button', { name: 'Use copy 2' }, { timeout: 15000 }))
    await h.screen.findByText('Drawing profile applied.', {}, { timeout: 15000 })
    assert.equal(picks, 1)
    assert.equal(copyApplied.selection.count, copyIntake.inserts.filter(row => row.x >= 10 && row.x <= 12).length)
    assert.equal(copyApplied.applied_profile.customer_key, other.customer_key)
    assert.notEqual(copyApplied.applied_profile.profile_id, firstPin.profile_id)
    expectedPins.push([otherNext.project_key, otherNext.customer_key, 1])
    h.cleanup()

    const cancelled = { ...h.ctx('cancelled-commit'), customer_key: 'synthetic-c' }
    let cancelPlan, committed, release, cancelConfirmations = 0
    h.render(h.React.createElement(Review, { authorityKey: 'synthetic-c-authority', solarBinding: { project_id: cancelled.project_key },
      isCurrent: () => true,
      projectClient: { readContext: async ({ signal }) => {
        cancelPlan = await h.rpc(cancelled, 'load', cancelled, null, signal, null, null, fixture)
        return { ok: true, data: { role_discovery: cancelPlan.compatibility.proposal } }
      } }, onConfirm: async (selection, options) => {
        cancelConfirmations++
        committed = await h.rpc(cancelled, 'confirm', cancelled, { ...selection, expected_version: 0, plan_token: cancelPlan.plan_token },
          options.signal, null, null, fixture)
        return new Promise(resolve => { release = resolve })
      } }))
    h.fireEvent.click(await h.screen.findByRole('button', { name: 'Confirm roles' }, { timeout: 15000 }))
    await h.waitFor(() => assert.equal(committed?.state, 'applied'), { timeout: 15000 })
    h.fireEvent.click(h.screen.getByRole('button', { name: 'Cancel' }))
    await h.screen.findByText('Role confirmation cancelled.')
    release({ ok: true })
    assert.equal(h.screen.queryByText('Roles confirmed.'), null)
    const inspected = await h.rpc(cancelled, 'load', cancelled, null, null, null, null, fixture)
    assert.equal(inspected.application_record.version, 1)
    const recovered = await h.rpc(cancelled, 'apply', cancelled, { ...fixture.copy_candidates[0], plan_token: inspected.plan_token },
      null, null, null, fixture)
    assert.equal(recovered.applied_profile.version, 1)
    assert.equal(cancelConfirmations, 1)
    assert.equal(h.screen.queryByText('Roles confirmed.'), null)
    expectedPins.push([cancelled.project_key, cancelled.customer_key, 1])
    const retained = await h.rpc(scope, 'load', scope, null, null, null, null, fixture)
    assert.deepEqual(retained.application_record, firstPin)
    assert.equal(confirmations, 1)
    h.cleanup()
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
    assert.deepEqual(rows.versions, [[scope.customer_key, 1], [scope.customer_key, 2], [other.customer_key, 1], [cancelled.customer_key, 1]])
    assert.deepEqual(rows.pins, expectedPins.sort((a, b) => a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0))
    assert.equal(rows.receipts, 3)
    assert.equal(rows.immutable, true)
    assert.equal(rows.pinned, true)
    assert.equal(h.resources().child_count, 0)
    console.log('PASS: role review adapter journey, counts3/4/5 and B copy3, scoped versions and old pins, cancelled-commit inspect/apply without reconfirmation')
  } finally { await h.dispose() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
