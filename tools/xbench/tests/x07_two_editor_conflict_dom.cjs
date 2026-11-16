const path = require('node:path'), assert = require('node:assert/strict')
const { rpc, ctx, copies, component, React, render, screen, within, waitFor, dispose } = require('./x07_adapter_jsx_journey.cjs')
const at = process.argv.indexOf('--keyboard-deps')
if (at < 0) throw new Error('Pass --keyboard-deps with an installed @testing-library/user-event directory')
const userEvent = require(path.join(path.resolve(process.argv[at+1]), '@testing-library/user-event')).default
const Editor = component('X07CustomerProfileEditor')
async function tabTo(user, element) {
  for (let i=0; i<40 && document.activeElement !== element; i++) await user.tab()
  assert.equal(document.activeElement, element)
}
async function main() {
  try {
    const context = ctx('two-editors'), initial = await rpc(context, 'confirm', context, { ...copies[0], expected_version: 0 })
    const versions = [[], []]
    const editor = index => React.createElement('section', { 'aria-label': index ? 'Second editor' : 'First editor', key: index },
      React.createElement(Editor, { customerKey: context.customer_key, applicationRecord: initial.applied_profile,
        loadProfile: (customer, opts) => rpc(context, 'editor_load', context, customer, opts.signal),
        saveProfile: (customer, roles, opts) => { versions[index].push(opts.expected_version); return rpc(context, 'editor_save', context,
          { customer, roles, expected_version: opts.expected_version }, opts.signal) } }))
    render(React.createElement('div', null, editor(0), editor(1)))
    const one = within(screen.getByRole('region', { name: 'First editor' }))
    const two = within(screen.getByRole('region', { name: 'Second editor' }))
    await one.findByText('Profile version: 1', {}, { timeout: 15000 }); await two.findByText('Profile version: 1')
    const ready = async view => waitFor(() => assert.ok(!view.getByRole('button', { name: 'Save profile' }).disabled))
    await ready(one); await ready(two)
    const user = userEvent.setup({ document })
    await tabTo(user, one.getByRole('button', { name: 'Add roof role' })); await user.keyboard('{Enter}')
    await tabTo(user, one.getByRole('button', { name: 'Save profile' })); await user.keyboard('{Enter}')
    assert.equal(one.getByRole('alert').textContent, 'Each added role needs a layer.')
    assert.equal(versions[0].length, 0)
    await tabTo(user, one.getByLabelText('Roof layer')); await user.keyboard('ROOF')
    await tabTo(user, one.getByRole('button', { name: 'Add obstruction role' })); await user.keyboard('{Enter}')
    await tabTo(user, one.getByLabelText('Obstruction layer')); await user.keyboard('OBSTRUCTION')
    await tabTo(user, one.getByRole('button', { name: 'Save profile' })); await user.keyboard('{Enter}')
    await one.findByText('Profile version: 2', {}, { timeout: 15000 }); await ready(one)
    let latest = await rpc(context, 'editor_load', context, context.customer_key)
    assert.deepEqual(latest.roles, { module: { block: 'Panel', layer: 'MODULES' }, roof: { layer: 'ROOF' }, obstruction: { layer: 'OBSTRUCTION' } })
    await tabTo(user, one.getByRole('button', { name: 'Remove obstruction role' })); await user.keyboard('{Enter}')
    await tabTo(user, one.getByRole('button', { name: 'Save profile' })); await user.keyboard('{Enter}')
    await one.findByText('Profile version: 3', {}, { timeout: 15000 }); await ready(one)
    latest = await rpc(context, 'editor_load', context, context.customer_key)
    assert.equal(latest.roles.obstruction, undefined); assert.equal(latest.roles.module.block, 'Panel')

    await tabTo(user, two.getByLabelText('Module block')); await user.keyboard('{Control>}a{/Control}pAnEl')
    await tabTo(user, two.getByRole('button', { name: 'Save profile' })); await user.keyboard('{Enter}')
    await two.findByRole('alert', {}, { timeout: 15000 })
    assert.equal(two.getByLabelText('Module block').value, 'pAnEl')
    assert.ok(two.getByRole('button', { name: 'Save profile' }).disabled)
    // First editor advances again while the second keeps its stale draft.
    await tabTo(user, one.getByLabelText('Module block')); await user.keyboard('{Control>}a{/Control}PANEL')
    await tabTo(user, one.getByRole('button', { name: 'Save profile' })); await user.keyboard('{Enter}')
    await one.findByText('Profile version: 4', {}, { timeout: 15000 }); await ready(one)
    await tabTo(user, two.getByRole('button', { name: 'Reload version, keep edits' })); await user.keyboard('{Enter}')
    await two.findByText('Profile version: 4', {}, { timeout: 15000 }); await ready(two)
    assert.equal(two.getByLabelText('Module block').value, 'pAnEl')
    await tabTo(user, two.getByRole('button', { name: 'Save profile' })); await user.keyboard('{Enter}')
    await two.findByText('Profile version: 5', {}, { timeout: 15000 }); await ready(two)
    assert.deepEqual(versions, [[1,2,3], [1,4]])
    assert.equal((await rpc(context, 'editor_load', context, context.customer_key)).roles.module.block, 'Panel')
    assert.deepEqual((await rpc(context, 'load', context)).application_record, initial.applied_profile)
    assert.ok(one.getByText('Project is pinned to version 1.')); assert.ok(two.getByText('Project is pinned to version 1.'))
    console.log('PASS: keyboard optional-role add/remove, real save/reload, two-editor stale drafts/refreshed retries and immutable old pin')
  } finally { await dispose() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
