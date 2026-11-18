const assert = require('node:assert/strict')
const h = require('./x07_profile_review_dom_harness.cjs')
async function main() {
  try {
    const Editor = h.component('X07CustomerProfileEditor'), roles = { module: { block: 'Panel', layer: 'Mo\u0301D' }, roof: { layer: 'R\u00f3F' } }
    let saves = 0
    h.render(h.React.createElement(Editor, { customerKey: 'a', drawingKey: 'a:v1',
      loadProfile: async () => ({ customer_key: 'a', profile_id: 'profile-a', version: 1, roles }),
      saveProfile: async () => { saves++; throw Error('Unexpected save') } }))
    await h.screen.findByRole('textbox', { name: 'Module layer' })
    h.fireEvent.click(h.screen.getByRole('button', { name: 'Remove roof role' }))
    await h.enter(h.screen.getByRole('button', { name: 'Restore loaded roof value' }))
    await h.enter(h.screen.getByRole('button', { name: 'Undo last restore' }))
    assert.equal(h.screen.queryByRole('textbox', { name: 'Roof layer' }), null)
    assert.ok(h.screen.getByRole('button', { name: 'Restore loaded roof value' }))
    assert.equal(document.activeElement, h.screen.getByRole('textbox', { name: 'Module layer' }))
    h.fireEvent.click(h.screen.getByRole('button', { name: 'Add obstruction role' }))
    h.fireEvent.change(h.screen.getByRole('textbox', { name: 'Obstruction layer' }), { target: { value: 'DRAFT\u00e9' } })
    await h.enter(h.screen.getByRole('button', { name: 'Restore loaded obstruction value' }))
    await h.enter(h.screen.getByRole('button', { name: 'Undo last restore' }))
    const obstruction = h.screen.getByRole('textbox', { name: 'Obstruction layer' })
    assert.equal(obstruction.value, 'DRAFT\u00e9'); assert.equal(document.activeElement, obstruction); assert.ok(obstruction.isConnected)
    assert.equal(h.screen.getByRole('textbox', { name: 'Module layer' }).value, roles.module.layer)
    assert.equal(h.screen.queryByRole('button', { name: 'Undo last restore' }), null)
    assert.equal(saves, 0)
    console.log('PASS: restore undo keyboard focus, removed/draft-only roles and exact Unicode with zero saves')
  } finally { await h.dispose() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
