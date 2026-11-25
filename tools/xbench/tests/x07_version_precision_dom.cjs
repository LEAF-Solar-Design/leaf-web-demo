const assert = require('node:assert/strict')
const { rpc, ctx, copies, component, React, render, screen, fireEvent, waitFor, cleanup, dispose } = require('./x07_adapter_jsx_journey.cjs')
const Editor = component('X07CustomerProfileEditor'), MAX = Number.MAX_SAFE_INTEGER
async function ready() { await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled)) }
async function main() {
  try {
    const invalid = [MAX+1, JSON.parse('9007199254740993'), 1.5, true]
    for (let index=0; index<invalid.length; index++) {
      const context = { ...ctx('precision-' + index), customer_key: 'synthetic-' + index }
      const initial = await rpc(context, 'confirm', context, { ...copies[0], expected_version: 0 })
      let fixture = {}, loaded = 0
      render(React.createElement(Editor, { customerKey: context.customer_key, applicationRecord: initial.applied_profile,
        loadProfile: (customer, opts) => { loaded++; return rpc(context, 'editor_load', context, customer, opts.signal, null, null, fixture) },
        saveProfile: (customer, roles, opts) => rpc(context, 'editor_save', context, { customer, roles, expected_version: opts.expected_version }, opts.signal) }))
      await screen.findByText('Profile version: 1', {}, { timeout: 15000 }); await ready()
      fireEvent.change(screen.getByLabelText('Module block'), { target: { value: 'pAnEl' } })
      await rpc(context, 'editor_save', context, { customer: context.customer_key, roles: initial.applied_profile.roles, expected_version: 1 })
      fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
      await screen.findByRole('alert', {}, { timeout: 15000 })
      fixture = { receipt_version: invalid[index] }
      await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Reload version, keep edits' }).disabled))
      fireEvent.click(screen.getByRole('button', { name: 'Reload version, keep edits' }))
      await waitFor(() => assert.equal(screen.getByRole('alert').textContent, 'The profile could not be loaded for this customer.'), { timeout: 15000 })
      assert.equal(screen.getByLabelText('Module block').value, 'pAnEl')
      assert.ok(screen.getByRole('button', { name: 'Save profile' }).disabled)
      assert.equal((await rpc(context, 'editor_load', context, context.customer_key)).version, 2)
      assert.deepEqual((await rpc(context, 'load', context)).application_record, initial.applied_profile)
      fixture = {}
      await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Reload version, keep edits' }).disabled))
      fireEvent.click(screen.getByRole('button', { name: 'Reload version, keep edits' }))
      await screen.findByText('Profile version: 2', {}, { timeout: 15000 }); await ready()
      assert.equal(screen.getByLabelText('Module block').value, 'pAnEl'); assert.equal(loaded, 3)
      cleanup()
    }
    const context = { ...ctx('precision-max'), customer_key: 'synthetic-max' }
    const initial = await rpc(context, 'confirm', context, { ...copies[0], expected_version: 0 })
    await rpc(context, 'seed_max', context)
    render(React.createElement(Editor, { customerKey: context.customer_key, applicationRecord: initial.applied_profile,
      loadProfile: (customer, opts) => rpc(context, 'editor_load', context, customer, opts.signal),
      saveProfile: (customer, roles, opts) => rpc(context, 'editor_save', context, { customer, roles, expected_version: opts.expected_version }, opts.signal) }))
    await screen.findByText('Profile version: ' + MAX, {}, { timeout: 15000 }); await ready()
    fireEvent.change(screen.getByLabelText('Module block'), { target: { value: 'pAnEl' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
    await screen.findByRole('alert', {}, { timeout: 15000 }); await ready()
    assert.equal(screen.getByLabelText('Module block').value, 'pAnEl')
    assert.equal((await rpc(context, 'editor_load', context, context.customer_key)).version, MAX)
    for (const value of invalid) await assert.rejects(rpc(context, 'editor_save', context,
      { customer: context.customer_key, roles: initial.applied_profile.roles, expected_version: value }))
    assert.deepEqual((await rpc(context, 'load', context)).application_record, initial.applied_profile)
    console.log('PASS: safe ceiling, rounded/fractional/boolean receipt refusal, backend no-write guards and preserved stale editor drafts')
  } finally { await dispose() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
