const assert = require('node:assert/strict')
const h = require('./x07_profile_review_dom_harness.cjs')
const record = customer => ({ customer_key: customer, profile_id: 'profile-' + customer, version: 1,
  roles: { module: { block: 'Panel', layer: 'MODULES' } } })
async function main() {
  try {
    const Editor = h.component('X07CustomerProfileEditor'), pending = h.deferred(); let calls = 0, submitted
    const props = { customerKey: 'a', drawingKey: 'a:v1', loadProfile: async customer => record(customer),
      saveProfile: async (customer, roles) => { calls++; submitted = roles; return pending.promise } }
    const view = h.render(h.React.createElement(Editor, props))
    h.fireEvent.change(await h.screen.findByRole('textbox', { name: 'Module layer' }), { target: { value: 'UNDO_NEXT' } })
    await h.enter(h.screen.getByRole('button', { name: 'Restore loaded module value' }))
    await h.enter(h.screen.getByRole('button', { name: 'Save profile' }))
    await h.waitFor(() => assert.equal(calls, 1))
    await h.enter(h.screen.getByRole('button', { name: 'Undo last restore' }))
    assert.equal(h.screen.getByRole('textbox', { name: 'Module layer' }).value, 'UNDO_NEXT')
    await h.act(async () => pending.resolve({ ...record('a'), version: 2, roles: submitted }))
    await h.screen.findByText('Profile saved. Newer edits are unsaved.')
    assert.equal(h.screen.getByRole('textbox', { name: 'Module layer' }).value, 'UNDO_NEXT')
    assert.equal(calls, 1); view.unmount(); h.cleanup()

    for (const field of ['customerKey', 'drawingKey']) {
      const old = h.deferred(); let signal
      props.saveProfile = async (customer, roles, options) => { calls++; signal = options.signal; return old.promise }
      const current = h.render(h.React.createElement(Editor, props))
      h.fireEvent.change(await h.screen.findByRole('textbox', { name: 'Module layer' }), { target: { value: 'RETIRED' } })
      await h.enter(h.screen.getByRole('button', { name: 'Restore loaded module value' }))
      await h.enter(h.screen.getByRole('button', { name: 'Save profile' }))
      await h.waitFor(() => assert.ok(signal))
      current.rerender(h.React.createElement(Editor, { ...props, [field]: field === 'customerKey' ? 'b' : 'b:v1' }))
      await h.screen.findByRole('textbox', { name: 'Module layer' })
      current.rerender(h.React.createElement(Editor, props))
      await h.screen.findByText('Profile id: profile-a')
      assert.equal(signal.aborted, true)
      await h.act(async () => old.resolve({ ...record('a'), version: 2 }))
      assert.equal(h.screen.queryByRole('button', { name: 'Undo last restore' }), null)
      assert.equal(h.screen.getByRole('textbox', { name: 'Module layer' }).value, 'MODULES')
      assert.equal(h.screen.queryByText('Profile version: 2'), null)
      current.unmount(); h.cleanup()
    }
    assert.equal(calls, 3)
    console.log('PASS: restore undo advances revision and retires customer/drawing A-B-A replies')
  } finally { await h.dispose() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
