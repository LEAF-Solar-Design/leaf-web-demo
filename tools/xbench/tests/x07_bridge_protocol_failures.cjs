const fs = require('node:fs'), assert = require('node:assert/strict')
const { rpc, ctx, copies, mount, screen, fireEvent, waitFor, cleanup, dispose, resources } = require('./x07_adapter_jsx_journey.cjs')

async function main() {
  try {
    const codes = { eof: 'BRIDGE_EOF', malformed: 'BRIDGE_MALFORMED_JSON', nonzero: 'BRIDGE_EXIT' }
    for (const fault of Object.keys(codes)) {
      const context = { ...ctx('protocol-' + fault), customer_key: 'synthetic-' + fault }
      const original = await rpc(context, 'confirm', context, { ...copies[0], expected_version: 0 })
      await assert.rejects(rpc(context, 'editor_load', context, context.customer_key, null, null, fault), error => {
        assert.equal(error.code, codes[fault]); assert.ok(error.message.length <= 550); return true
      })
      assert.equal(resources().child_count, 0)
      let attempts = 0
      mount(context, { saveProfile: (customer, roles, opts) => rpc(context, 'editor_save', context,
        { customer, roles, expected_version: opts.expected_version }, opts.signal, null, ++attempts === 1 ? fault : null) })
      await screen.findByRole('button', { name: 'Use copy 1' }, { timeout: 15000 })
      fireEvent.click(screen.getByRole('button', { name: 'Use copy 1' }))
      await screen.findByText('Profile version: 1', {}, { timeout: 15000 })
      await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
      fireEvent.change(screen.getByLabelText('Module block'), { target: { value: 'pAnEl' } })
      fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
      assert.equal((await screen.findByRole('alert', {}, { timeout: 15000 })).textContent,
        'The profile could not be saved. Your edits are kept.')
      assert.equal(screen.getByLabelText('Module block').value, 'pAnEl')
      const plan = await rpc(context, 'load', context)
      assert.equal(plan.expected_version, 1); assert.deepEqual(plan.application_record, original.applied_profile)
      await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
      fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
      await screen.findByText('Profile version: 2', {}, { timeout: 15000 })
      await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
      assert.ok(screen.getByText('Project is pinned to version 1.')); assert.equal(attempts, 2)
      assert.equal(resources().child_count, 0); cleanup()
    }
    console.log('PASS: bounded EOF/malformed/nonzero errors, actual JSX preserved draft, safe retry, unchanged pins and closed children')
  } finally {
    await dispose()
    assert.equal(resources().child_count, 0)
    assert.equal(fs.existsSync(resources().database), false)
    assert.equal(fs.existsSync(resources().root), false)
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
