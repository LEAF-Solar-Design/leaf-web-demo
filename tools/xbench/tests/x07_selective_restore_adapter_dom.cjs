const assert = require('node:assert/strict'), { execFileSync } = require('node:child_process')
const h = require('./x07_profile_review_dom_harness.cjs')
async function main() {
  try {
    const Editor = h.component('X07CustomerProfileEditor'), context = h.ctx('selective-restore')
    const fixture = { intake: { ...h.intake, inserts: [...h.intake.inserts, { name: 'Panel', layer: 'NEXT', x: 20, y: 0 }] } }
    const rpc = (op, value, signal) => h.rpc(context, op, context, value, signal, null, null, fixture)
    const plan = await rpc('load'), confirmed = await rpc('confirm', { ...h.copies[0], expected_version: 0, plan_token: plan.plan_token })
    const pin = JSON.parse(JSON.stringify(confirmed.applied_profile)); let saves = 0
    h.render(h.React.createElement(Editor, { customerKey: context.customer_key, drawingKey: context.drawing_key,
      applicationRecord: pin, pinnedRoles: pin.roles,
      loadProfile: (customer, options) => rpc('editor_load', customer, options.signal),
      saveProfile: (customer, roles, options) => { saves++; return rpc('editor_save', { customer, roles, expected_version: options.expected_version }, options.signal) } }))
    await h.screen.findByRole('textbox', { name: 'Module layer' })
    h.fireEvent.click(h.screen.getByRole('button', { name: 'Add roof role' }))
    h.fireEvent.change(h.screen.getByRole('textbox', { name: 'Roof layer' }), { target: { value: 'ROOF' } })
    h.fireEvent.click(h.screen.getByRole('button', { name: 'Save profile' })); await h.screen.findByText('Profile version: 2')
    h.fireEvent.change(h.screen.getByRole('textbox', { name: 'Module layer' }), { target: { value: 'NEXT' } })
    h.fireEvent.click(h.screen.getByRole('button', { name: 'Remove roof role' }))
    h.fireEvent.click(h.screen.getByRole('button', { name: 'Add obstruction role' }))
    h.fireEvent.change(h.screen.getByRole('textbox', { name: 'Obstruction layer' }), { target: { value: 'DRAFT_ONLY' } })
    await h.enter(h.screen.getByRole('button', { name: 'Restore loaded roof value' }))
    await h.enter(h.screen.getByRole('button', { name: 'Restore loaded obstruction value' }))
    assert.equal(saves, 1)
    assert.equal((await rpc('editor_load', context.customer_key)).version, 2)
    assert.equal(h.screen.getByRole('textbox', { name: 'Module layer' }).value, 'NEXT')
    assert.equal(h.screen.getByRole('textbox', { name: 'Roof layer' }).value, 'ROOF')
    assert.equal(h.screen.queryByRole('textbox', { name: 'Obstruction layer' }), null)
    await h.enter(h.screen.getByRole('button', { name: 'Save profile' })); await h.screen.findByText('Profile version: 3')
    assert.equal(saves, 2)
    const audit = `import json,sqlite3,sys
with sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True) as db:
 rows=db.execute('SELECT version,roles_json FROM profile_versions ORDER BY version').fetchall()
 pin=db.execute('SELECT customer_key,version FROM project_applications').fetchall()
 print(json.dumps({'versions':[[v,json.loads(r)] for v,r in rows],'pins':pin}))`
    const readback = JSON.parse(execFileSync('python', ['-P', '-c', audit, h.resources().database], { timeout: 10000, encoding: 'utf8', windowsHide: true }))
    assert.deepEqual(readback.versions, [
      [1, { module: { block: 'Panel', layer: 'MODULES' } }],
      [2, { module: { block: 'Panel', layer: 'MODULES' }, roof: { layer: 'ROOF' } }],
      [3, { module: { block: 'Panel', layer: 'NEXT' }, roof: { layer: 'ROOF' } }],
    ])
    assert.deepEqual(readback.pins, [[context.customer_key, 1]])
    assert.deepEqual((await rpc('load')).application_record, pin)
    console.log('PASS: selective restore adapter explicit version writes, unrelated edits and original pinned roles')
  } finally { await h.dispose() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
