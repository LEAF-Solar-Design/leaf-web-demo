// Reuse the real local Python/JSON bridge; inject only negative fixture faults.
const assert = require('node:assert/strict')
const { rpc, ctx, copies, mount, screen, fireEvent, waitFor, cleanup, dispose } = require('./x07_adapter_jsx_journey.cjs')

async function main() {
  try {
    const old = ctx('old-a')
    const original = await rpc(old, 'confirm', old, { ...copies[0], expected_version: 0 })
    const next = { customer_key: 'synthetic-b', project_key: 'new-b', drawing_key: 'new-b:v1' }
    mount(next, { confirmRoles: (scope, value, opts) => rpc(next, 'confirm', scope, value, opts.signal, 'pin') })
    await screen.findByRole('button', { name: 'Confirm roles' }, { timeout: 15000 })
    fireEvent.click(screen.getByRole('button', { name: 'Confirm roles' }))
    await screen.findByRole('button', { name: 'Use copy 1' })
    fireEvent.click(screen.getByRole('button', { name: 'Use copy 1' }))
    assert.equal((await screen.findByRole('alert', {}, { timeout: 15000 })).textContent, 'Copy choice could not be saved. Try again.')
    assert.equal(screen.queryByText('Drawing profile applied.'), null)
    let plan = await rpc(next, 'load', next)
    assert.equal(plan.application_record, null); assert.equal(plan.expected_version, 1)
    await assert.rejects(rpc(next, 'confirm', next, { ...copies[0], expected_version: 0 }), /profile version changed/)
    assert.deepEqual((await rpc(old, 'load', old)).application_record, original.applied_profile)
    cleanup()

    // Safe pin recovery reloads inspect and applies saved version1, no confirm.
    let saves = 0
    mount(next, { saveProfile: (customer, roles, opts) => rpc(next, 'editor_save', next,
      { customer, roles, expected_version: opts.expected_version }, opts.signal, ++saves === 1 ? 'save' : null) })
    await screen.findByRole('button', { name: 'Use copy 2' }, { timeout: 15000 })
    assert.equal(screen.queryByRole('button', { name: 'Confirm roles' }), null)
    fireEvent.click(screen.getByRole('button', { name: 'Use copy 2' }))
    await screen.findByText('Drawing profile applied.', {}, { timeout: 15000 })
    await screen.findByText('Profile version: 1', {}, { timeout: 15000 })
    await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
    const pinned = (await rpc(next, 'load', next)).application_record
    fireEvent.change(screen.getByLabelText('Module block'), { target: { value: 'pAnEl' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
    assert.equal((await screen.findByRole('alert', {}, { timeout: 15000 })).textContent,
      'The profile could not be saved. Your edits are kept.')
    assert.equal(screen.getByLabelText('Module block').value, 'pAnEl')
    assert.equal((await rpc(next, 'load', next)).expected_version, 1)
    await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
    fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
    await screen.findByText('Profile version: 2', {}, { timeout: 15000 })
    assert.ok(screen.getByText('Project is pinned to version 1.'))
    plan = await rpc(next, 'load', next)
    assert.deepEqual(plan.application_record, pinned)
    assert.deepEqual((await rpc(old, 'load', old)).application_record, original.applied_profile)
    assert.equal(saves, 2)
    console.log('PASS: actual JSX pin/save failures, visible errors, preserved editor draft, safe inspect/apply retry and unchanged old pins')
  } finally { await dispose() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
