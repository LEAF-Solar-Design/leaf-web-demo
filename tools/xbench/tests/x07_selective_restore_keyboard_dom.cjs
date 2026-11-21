const assert = require('node:assert/strict')
const h = require('./x07_profile_review_dom_harness.cjs')
async function main() {
  try {
    const Editor = h.component('X07CustomerProfileEditor')
    const roles = { module: { block: 'Panel', layer: 'Mo\u0301D' }, roof: { layer: 'R\u00f3F' } }
    const original = JSON.stringify(roles); let saves = 0
    h.render(h.React.createElement(Editor, { customerKey: 'a', drawingKey: 'a:v1',
      loadProfile: async () => ({ customer_key: 'a', profile_id: 'profile-a', version: 1, roles }),
      saveProfile: async () => { saves++; throw Error('Unexpected implicit save') } }))
    h.fireEvent.change(await h.screen.findByRole('textbox', { name: 'Module layer' }), { target: { value: 'OTHER' } })
    h.fireEvent.click(h.screen.getByRole('button', { name: 'Remove roof role' }))
    h.fireEvent.click(h.screen.getByRole('button', { name: 'Add obstruction role' }))
    h.fireEvent.change(h.screen.getByRole('textbox', { name: 'Obstruction layer' }), { target: { value: 'DRAFT' } })
    for (const role of ['roof', 'obstruction', 'module']) {
      await h.enter(h.screen.getByRole('button', { name: `Restore loaded ${role} value` }))
      assert.equal(h.screen.queryByRole('button', { name: `Restore loaded ${role} value` }), null)
      const focus = h.screen.getByRole('textbox', { name: role === 'roof' ? 'Roof layer' : 'Module layer' })
      await h.waitFor(() => assert.equal(document.activeElement, focus)); assert.ok(focus.isConnected)
      assert.equal(saves, 0)
    }
    assert.equal(h.screen.getByRole('textbox', { name: 'Module layer' }).value, roles.module.layer)
    assert.equal(h.screen.getByRole('textbox', { name: 'Roof layer' }).value, roles.roof.layer)
    assert.equal(h.screen.queryByRole('textbox', { name: 'Obstruction layer' }), null)
    assert.ok(h.screen.getByRole('region', { name: 'Draft changes from loaded version 1' }).textContent.includes('No role changes.'))
    assert.equal(JSON.stringify(roles), original)
    console.log('PASS: selective restore keyboard names, disappearing action focus and zero implicit saves')
  } finally { await h.dispose() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
