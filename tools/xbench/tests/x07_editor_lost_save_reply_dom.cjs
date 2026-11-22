const assert = require('node:assert/strict')
const { rpc, ctx, copies, component, React, render, screen, fireEvent, waitFor, dispose,
  resources } = require('./x07_adapter_jsx_journey.cjs')
const Editor = component('X07CustomerProfileEditor')

async function main() {
  try {
    const context = ctx('lost-editor-reply')
    const pin = (await rpc(context, 'confirm', context, { ...copies[0], expected_version: 0 })).applied_profile
    let attempts = 0, lost = 0
    render(React.createElement(Editor, { customerKey: context.customer_key, drawingKey: context.drawing_key,
      applicationRecord: pin,
      loadProfile: (customer, opts) => rpc(context, 'editor_load', context, customer, opts.signal),
      saveProfile: async (customer, roles, opts) => {
        attempts++
        try { return await rpc(context, 'editor_save', context, { customer, roles, expected_version: opts.expected_version },
          opts.signal, null, attempts === 1 ? 'lost_save_reply' : null) }
        catch (error) { if (attempts === 1) { assert.equal(error.code, 'BRIDGE_EOF'); lost++ } throw error }
      }
    }))
    await screen.findByText('Profile version: 1', {}, { timeout: 15000 })
    await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
    fireEvent.change(screen.getByLabelText('Module layer'), { target: { value: 'mOdUlEs' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
    await screen.findByRole('alert', {}, { timeout: 15000 })
    assert.equal(lost, 1); assert.equal(attempts, 1)
    assert.equal(screen.getByLabelText('Module layer').value, 'mOdUlEs')
    assert.equal((await rpc(context, 'editor_load', context, context.customer_key)).version, 2)
    assert.deepEqual((await rpc(context, 'load', context)).application_record, pin)
    // A changed draft must not turn an uncertain first outcome into a blind
    // second version. The stale expected version refuses until explicit reload.
    fireEvent.click(screen.getByRole('button', { name: 'Add roof role' }))
    fireEvent.change(screen.getByLabelText('Roof layer'), { target: { value: 'ROOF' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
    await screen.findByText('The profile changed. Your edits are kept. Reload the version to retry.', {}, { timeout: 15000 })
    assert.equal(attempts, 2)
    assert.equal((await rpc(context, 'editor_load', context, context.customer_key)).version, 2)
    fireEvent.click(screen.getByRole('button', { name: 'Reload version, keep edits' }))
    await screen.findByText('Profile version: 2', {}, { timeout: 15000 })
    assert.equal(screen.getByLabelText('Roof layer').value, 'ROOF')
    assert.equal(screen.getByLabelText('Module layer').value, 'mOdUlEs')
    await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
    fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
    await screen.findByText('Profile version: 3', {}, { timeout: 15000 })
    assert.equal(attempts, 3)
    const latest = await rpc(context, 'editor_load', context, context.customer_key)
    assert.equal(latest.roles.roof.layer, 'ROOF')
    assert.deepEqual((await rpc(context, 'load', context)).application_record, pin)
    console.log('PASS: actual save commits before lost reply; draft remains, blind stale retry adds no version, explicit reload/retry saves new edits and preserves old pin')
  } finally { await dispose(); assert.equal(resources().child_count, 0) }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
