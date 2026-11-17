const assert = require('node:assert/strict')
const { rpc, ctx, copies, component, React, render, screen, fireEvent, waitFor, dispose } = require('./x07_adapter_jsx_journey.cjs')
const Editor = component('X07CustomerProfileEditor')
global.IS_REACT_ACT_ENVIRONMENT = true

async function main() {
  try {
    const context = ctx('pending-optional')
    const first = await rpc(context, 'confirm', context, { ...copies[0], expected_version: 0 })
    let release, saves = 0, submitted
    render(React.createElement(Editor, { customerKey: context.customer_key, drawingKey: context.drawing_key,
      applicationRecord: first.applied_profile,
      loadProfile: (customer, opts) => rpc(context, 'editor_load', context, customer, opts.signal),
      saveProfile: async (customer, roles, opts) => {
        saves++
        if (saves === 1) { submitted = roles; await new Promise(resolve => { release = resolve }) }
        return rpc(context, 'editor_save', context, { customer, roles, expected_version: opts.expected_version }, opts.signal)
      }
    }))
    await screen.findByText('Profile version: 1', {}, { timeout: 15000 })
    await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
    fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
    await waitFor(() => assert.ok(release))
    for (const role of ['roof', 'obstruction']) {
      fireEvent.click(screen.getByRole('button', { name: 'Add ' + role + ' role' }))
      fireEvent.click(screen.getByRole('button', { name: 'Remove ' + role + ' role' }))
      fireEvent.click(screen.getByRole('button', { name: 'Add ' + role + ' role' }))
      fireEvent.change(screen.getByLabelText(role[0].toUpperCase() + role.slice(1) + ' layer'), { target: { value: role.toUpperCase() } })
    }
    assert.deepEqual(Object.keys(submitted), ['module'])
    await React.act(async () => release())
    await screen.findByText('Profile saved. Newer edits are unsaved.', {}, { timeout: 15000 })
    assert.equal(screen.getByLabelText('Roof layer').value, 'ROOF')
    assert.equal(screen.getByLabelText('Obstruction layer').value, 'OBSTRUCTION')
    assert.equal(saves, 1)
    const persisted = await rpc(context, 'editor_load', context, context.customer_key)
    assert.equal(persisted.version, 2)
    assert.deepEqual(Object.keys(persisted.roles), ['module'])
    fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
    await screen.findByText('Profile version: 3', {}, { timeout: 15000 })
    assert.equal(saves, 2)
    const fresh = await rpc(context, 'editor_load', context, context.customer_key)
    assert.equal(fresh.roles.roof.layer, 'ROOF')
    assert.equal(fresh.roles.obstruction.layer, 'OBSTRUCTION')
    assert.deepEqual((await rpc(context, 'load', context)).application_record, first.applied_profile)
    console.log('PASS: optional role toggles during save retain newer draft, mark it unsaved, and persist only on explicit second save')
  } finally { await dispose() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
