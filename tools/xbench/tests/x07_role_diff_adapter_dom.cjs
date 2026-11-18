const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path')
const { execFileSync } = require('node:child_process')
if (!process.argv.includes('--deps')) process.argv.push('--deps', path.resolve(__dirname, '../../../web/node_modules'))
const h = require('./x07_profile_review_dom_harness.cjs')
async function main() {
  try {
    const Editor = h.component('X07CustomerProfileEditor'), Diff = h.component('X07ProfileRoleDiff')
    const oldPins = []
    for (const customer of ['synthetic-a', 'synthetic-b']) {
      const context = { ...h.ctx(customer + '-diff'), customer_key: customer }
      const nextLayer = customer === 'synthetic-a' ? 'Mo\u0301D' : 'M\u00f3D'
      const fixture = { intake: { ...h.intake, inserts: [...h.intake.inserts, { name: 'Panel', layer: nextLayer, x: 20, y: 0 }] } }
      const rpc = (op, value, signal) => h.rpc(context, op, context, value, signal, null, null, fixture)
      const plan = await rpc('load')
      const confirmed = await rpc('confirm', { ...h.copies[0], expected_version: 0, plan_token: plan.plan_token })
      const pin = confirmed.applied_profile; oldPins.push(JSON.parse(JSON.stringify(pin)))
      h.render(h.React.createElement(Editor, { customerKey: customer, drawingKey: context.drawing_key,
        applicationRecord: pin, pinnedRoles: pin.roles,
        loadProfile: (value, options) => rpc('editor_load', value, options.signal),
        saveProfile: (value, roles, options) => rpc('editor_save',
          { customer: value, roles, expected_version: options.expected_version }, options.signal) }))
      await h.screen.findByRole('textbox', { name: 'Module layer' })
      h.fireEvent.click(h.screen.getByRole('button', { name: 'Add roof role' }))
      h.fireEvent.change(h.screen.getByRole('textbox', { name: 'Roof layer' }), { target: { value: 'ROOF' } })
      h.fireEvent.click(h.screen.getByRole('button', { name: 'Save profile' }))
      await h.screen.findByText('Profile version: 2')
      const second = await rpc('editor_load', customer)
      assert.deepEqual(second.roles, { module: { block: 'Panel', layer: 'MODULES' }, roof: { layer: 'ROOF' } })
      h.fireEvent.click(h.screen.getByRole('button', { name: 'Remove roof role' }))
      h.fireEvent.change(h.screen.getByRole('textbox', { name: 'Module layer' }), { target: { value: nextLayer } })
      const preview = h.screen.getByRole('region', { name: 'Draft changes from loaded version 2' })
      assert.ok(preview.textContent.includes('0 added, 1 removed, 1 changed.'))
      h.fireEvent.click(h.screen.getByRole('button', { name: 'Save profile' }))
      await h.screen.findByText('Profile version: 3')
      const third = await rpc('editor_load', customer)
      assert.deepEqual(third.roles, { module: { block: 'Panel', layer: nextLayer } })
      assert.deepEqual((await rpc('load')).application_record, pin)
      const drift = h.screen.getByRole('region', { name: 'Pinned versus current roles' })
      assert.ok(drift.textContent.includes('0 added, 0 removed, 1 changed.'))
      assert.ok(h.screen.getByText('Project is pinned to version 1.'))
      h.cleanup()
      const database = h.resources().database, original = fs.readFileSync(database)
      const args = [path.resolve(__dirname, '../x07_profile_version_diff.py'), '--database', database, '--customer', customer,
        '--profile-id', third.profile_id, '--before', '2', '--after', '3']
      const oracle = JSON.parse(execFileSync('python', args, { timeout: 10000, encoding: 'utf8', windowsHide: true }))
      assert.deepEqual(oracle.added, {})
      assert.deepEqual(oracle.removed, { roof: { layer: 'ROOF' } })
      assert.deepEqual(oracle.changed, { module: { before: { block: 'Panel', layer: 'MODULES' }, after: { block: 'Panel', layer: nextLayer } } })
      h.render(h.React.createElement(Diff, { beforeRoles: second.roles, afterRoles: third.roles }))
      assert.ok(h.screen.getByRole('region', { name: 'Removed roof' }).textContent.includes('"ROOF"'))
      const values = [...h.screen.getByRole('region', { name: 'Changed module' }).querySelectorAll('code')].map(node => JSON.parse(node.textContent))
      assert.deepEqual(values, ['Panel', 'MODULES', 'Panel', nextLayer]); h.cleanup()
      assert.deepEqual(fs.readFileSync(database), original)
      const foreign = [...args]; foreign[foreign.indexOf('--customer') + 1] = customer === 'synthetic-a' ? 'synthetic-b' : 'synthetic-a'
      assert.throws(() => execFileSync('python', foreign, { timeout: 10000, encoding: 'utf8', windowsHide: true }), error => error.status === 2
        && JSON.parse(error.stdout).error === 'SCOPED_VERSIONS_UNAVAILABLE')
      assert.deepEqual(fs.readFileSync(database), original)
    }
    for (const pin of oldPins) {
      const context = { customer_key: pin.customer_key, project_key: pin.project_key, drawing_key: pin.project_key + ':v1' }
      assert.deepEqual((await h.rpc(context, 'load', context)).application_record, pin)
    }
    assert.equal(h.resources().child_count, 0)
    console.log('PASS: role diff adapter independent maps/Python oracle, two customers, optional removal and retained old pins')
  } finally { await h.dispose() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
