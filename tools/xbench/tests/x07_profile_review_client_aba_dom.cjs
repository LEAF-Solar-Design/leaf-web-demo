const assert = require('node:assert/strict')
const h = require('./x07_profile_review_dom_harness.cjs')
async function main() {
  try {
    const props = h.props(), a = props.projectClient, b = h.client(), pending = h.deferred()
    let loads = 0, oldSignal, saves = 0, saveSignal, saveReply
    props.loadProfile = async (customer, options) => {
      if (++loads === 1) { oldSignal = options.signal; return pending.promise }
      return JSON.parse(JSON.stringify(h.pin))
    }
    props.saveProfile = async (customer, roles, options) => {
      saves++; saveSignal = options.signal; saveReply = h.deferred(); return saveReply.promise
    }
    const view = h.render(h.draw(props))
    await h.enter(await h.screen.findByRole('button', { name: 'Confirm roles' }))
    await h.waitFor(() => assert.equal(loads, 1))
    view.rerender(h.draw({ ...props, projectClient: b })); view.rerender(h.draw({ ...props, projectClient: a }))
    await h.act(async () => pending.resolve({ ...h.pin, roles: { module: { block: 'RETIRED_CLIENT_DATA', layer: 'OLD' } } }))
    assert.equal(oldSignal.aborted, true)
    assert.equal(h.screen.queryByRole('textbox'), null)
    assert.equal(h.screen.queryByText('Roles confirmed.'), null)
    assert.equal(document.body.textContent.includes('RETIRED_CLIENT_DATA'), false); assert.equal(saves, 0)
    await h.enter(await h.screen.findByRole('button', { name: 'Confirm roles' }))
    await h.screen.findByRole('textbox', { name: 'Module layer' })
    // Settled confirmation must also retire, even if the old client object returns.
    view.rerender(h.draw({ ...props, projectClient: b })); view.rerender(h.draw(props))
    assert.equal(h.screen.queryByRole('textbox'), null)
    await h.enter(await h.screen.findByRole('button', { name: 'Confirm roles' }))
    h.fireEvent.change(await h.screen.findByRole('textbox', { name: 'Module layer' }), { target: { value: 'NEXT' } })
    await h.enter(h.screen.getByRole('button', { name: 'Save profile' }))
    await h.waitFor(() => assert.equal(saves, 1))
    view.rerender(h.draw({ ...props, projectClient: b })); view.rerender(h.draw(props))
    await h.act(async () => saveReply.resolve({ ...h.pin, version: 2 }))
    assert.equal(saveSignal.aborted, true); assert.equal(saves, 1)
    assert.equal(h.screen.queryByText('Profile saved.'), null)
    assert.equal(h.screen.queryByRole('textbox'), null)
    console.log('PASS: profile review client A-B-A retires confirmed state and old load/save replies')
  } finally { await h.dispose() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
