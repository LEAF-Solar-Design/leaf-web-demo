const assert = require('node:assert/strict')
const h = require('./x07_profile_review_dom_harness.cjs')
const record = customer => ({ customer_key: customer, profile_id: 'profile-' + customer, version: 1,
  roles: { module: { block: 'Panel', layer: customer === 'a' ? 'MODULES' : 'OTHER' }, roof: { layer: 'ROOF' } } })
async function main() {
  try {
    const Editor = h.component('X07CustomerProfileEditor'), pending = h.deferred()
    let calls = 0, submitted, signal
    const props = { customerKey: 'a', drawingKey: 'a:v1', loadProfile: async customer => record(customer),
      saveProfile: async (customer, roles, options) => { calls++; submitted = roles; signal = options.signal; return pending.promise } }
    const view = h.render(h.React.createElement(Editor, props))
    h.fireEvent.change(await h.screen.findByRole('textbox', { name: 'Module layer' }), { target: { value: 'SUBMITTED' } })
    h.fireEvent.click(h.screen.getByRole('button', { name: 'Remove roof role' }))
    h.fireEvent.click(h.screen.getByRole('button', { name: 'Save profile' }))
    await h.waitFor(() => assert.equal(calls, 1))
    await h.enter(h.screen.getByRole('button', { name: 'Restore loaded roof value' }))
    assert.equal(document.activeElement, h.screen.getByRole('textbox', { name: 'Roof layer' }))
    assert.equal(h.screen.getByRole('textbox', { name: 'Module layer' }).value, 'SUBMITTED')
    await h.act(async () => pending.resolve({ ...record('a'), version: 2, roles: submitted }))
    await h.screen.findByText('Profile saved. Newer edits are unsaved.')
    assert.equal(h.screen.getByRole('textbox', { name: 'Roof layer' }).value, 'ROOF')
    assert.ok(h.screen.getByRole('region', { name: 'Draft changes from loaded version 2' }).textContent.includes('1 added'))
    assert.equal(calls, 1); view.unmount(); h.cleanup()

    const later = h.deferred()
    props.saveProfile = async (customer, roles, options) => { calls++; signal = options.signal; submitted = roles; return later.promise }
    const next = h.render(h.React.createElement(Editor, props))
    h.fireEvent.change(await h.screen.findByRole('textbox', { name: 'Module layer' }), { target: { value: 'RETIRED' } })
    h.fireEvent.click(h.screen.getByRole('button', { name: 'Save profile' }))
    await h.waitFor(() => assert.equal(calls, 2))
    await h.enter(h.screen.getByRole('button', { name: 'Restore loaded module value' }))
    next.rerender(h.React.createElement(Editor, { ...props, customerKey: 'b', drawingKey: 'b:v1' }))
    await h.screen.findByText('Profile id: profile-b')
    assert.equal(signal.aborted, true)
    await h.act(async () => later.resolve({ ...record('a'), version: 2, roles: submitted }))
    assert.equal(h.screen.getByRole('textbox', { name: 'Module layer' }).value, 'OTHER')
    assert.equal(h.screen.queryByText('Profile saved. Newer edits are unsaved.'), null)
    assert.equal(h.screen.queryByRole('button', { name: /Restore loaded/ }), null)
    assert.equal(calls, 2)
    console.log('PASS: selective restore pending-save revisions and retired scope acknowledgments')
  } finally { await h.dispose() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
