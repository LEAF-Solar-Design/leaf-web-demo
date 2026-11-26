const assert = require('node:assert/strict'), { execFileSync } = require('node:child_process')
const h = require('./x07_profile_review_dom_harness.cjs')
// These states settle after the real Python worker responds.
const adapterWait = { timeout: 15000 }
async function main() {
  try {
    const Editor = h.component('X07CustomerProfileEditor'), pins = []
    for (const customer of ['synthetic-a', 'synthetic-b']) {
      const context = { ...h.ctx(customer + '-undo'), customer_key: customer }
      const fixture = { intake: { ...h.intake, inserts: [...h.intake.inserts, { name: 'Panel', layer: 'NEXT', x: 20, y: 0 }],
        polylines: [...h.intake.polylines, { layer: 'DRAFT_ONLY' }] } }
      const rpc = (op, value, signal) => h.rpc(context, op, context, value, signal, null, null, fixture)
      const plan = await rpc('load'), result = await rpc('confirm', { ...h.copies[0], expected_version: 0, plan_token: plan.plan_token })
      const pin = JSON.parse(JSON.stringify(result.applied_profile)); pins.push(pin); let saves = 0
      h.render(h.React.createElement(Editor, { customerKey: customer, drawingKey: context.drawing_key, applicationRecord: pin,
        loadProfile: (value, options) => rpc('editor_load', value, options.signal),
        saveProfile: (value, roles, options) => { saves++; return rpc('editor_save', { customer: value, roles, expected_version: options.expected_version }, options.signal) } }))
      await h.screen.findByRole('textbox', { name: 'Module layer' }, adapterWait)
      h.fireEvent.click(h.screen.getByRole('button', { name: 'Add roof role' }))
      h.fireEvent.change(h.screen.getByRole('textbox', { name: 'Roof layer' }), { target: { value: 'ROOF' } })
      await h.enter(h.screen.getByRole('button', { name: 'Save profile' })); await h.screen.findByText('Profile version: 2', {}, adapterWait)
      h.fireEvent.change(h.screen.getByRole('textbox', { name: 'Module layer' }), { target: { value: 'NEXT' } })
      h.fireEvent.click(h.screen.getByRole('button', { name: 'Remove roof role' }))
      h.fireEvent.click(h.screen.getByRole('button', { name: 'Add obstruction role' }))
      h.fireEvent.change(h.screen.getByRole('textbox', { name: 'Obstruction layer' }), { target: { value: 'DRAFT_ONLY' } })
      for (const role of ['roof', 'obstruction']) {
        await h.enter(h.screen.getByRole('button', { name: `Restore loaded ${role} value` }))
        await h.enter(h.screen.getByRole('button', { name: 'Undo last restore' }))
      }
      assert.equal(h.screen.queryByRole('textbox', { name: 'Roof layer' }), null)
      assert.equal(h.screen.getByRole('textbox', { name: 'Module layer' }).value, 'NEXT')
      assert.equal(h.screen.getByRole('textbox', { name: 'Obstruction layer' }).value, 'DRAFT_ONLY')
      assert.equal((await rpc('editor_load', customer)).version, 2); assert.equal(saves, 1)
      await h.enter(h.screen.getByRole('button', { name: 'Save profile' })); await h.screen.findByText('Profile version: 3', {}, adapterWait)
      assert.equal(saves, 2)
      assert.deepEqual((await rpc('editor_load', customer)).roles, { module: { block: 'Panel', layer: 'NEXT' }, obstruction: { layer: 'DRAFT_ONLY' } })
      assert.deepEqual((await rpc('load')).application_record, pin); h.cleanup()
    }
    const audit = `import sqlite3,json,sys
with sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True) as db:
 print(json.dumps({'versions':db.execute('SELECT customer_key,version FROM profile_versions ORDER BY customer_key,version').fetchall(),'pins':db.execute('SELECT customer_key,version FROM project_applications ORDER BY customer_key').fetchall()}))`
    const state = JSON.parse(execFileSync('python', ['-P', '-c', audit, h.resources().database], { timeout: 10000, encoding: 'utf8', windowsHide: true }))
    assert.deepEqual(state.versions, ['synthetic-a', 'synthetic-b'].flatMap(customer => [1, 2, 3].map(version => [customer, version])))
    assert.deepEqual(state.pins, [['synthetic-a', 1], ['synthetic-b', 1]])
    for (const pin of pins) assert.deepEqual((await h.rpc({ customer_key: pin.customer_key, project_key: pin.project_key, drawing_key: pin.project_key + ':v1' }, 'load',
      { customer_key: pin.customer_key, project_key: pin.project_key, drawing_key: pin.project_key + ':v1' })).application_record, pin)
    console.log('PASS: restore undo actual adapter explicit versions and both customer old pins')
  } finally { await h.dispose() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
