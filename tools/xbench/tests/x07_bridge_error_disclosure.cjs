const assert = require('node:assert/strict')
const { rpc, ctx, copies, component, React, render, screen, fireEvent, waitFor, cleanup, dispose, resources } = require('./x07_adapter_jsx_journey.cjs')
const Editor = component('X07CustomerProfileEditor')
async function main() {
  try {
    const context = ctx('error-disclosure')
    const pin = (await rpc(context, 'confirm', context, { ...copies[0], expected_version: 0 })).applied_profile
    const sentinel = 'PRIVATE_CUSTOMER_SENTINEL_' + 'secret-label-'.repeat(500)
    for (const fault of ['stderr_sentinel', 'json_grammar']) {
      let refusal, successes = 0
      render(React.createElement(Editor, { customerKey: context.customer_key, applicationRecord: pin,
        loadProfile: (customer, opts) => rpc(context, 'editor_load', context, customer, opts.signal),
        saveProfile: async (customer, roles, opts) => {
          try {
            const result = await rpc(context, 'editor_save', context, { customer, roles, expected_version: opts.expected_version },
              opts.signal, null, fault, { sentinel, reply_json: '{"ok":true,"result":' + JSON.stringify(sentinel) + ' trailing-malformed' })
            successes++; return result
          } catch (error) { refusal = error; throw error }
        } }))
      await screen.findByText('Profile version: 1', {}, { timeout: 15000 })
      fireEvent.click(screen.getByRole('button', { name: 'Add roof role' }))
      fireEvent.change(screen.getByLabelText('Roof layer'), { target: { value: 'ROOF' } })
      fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
      const alert = await screen.findByRole('alert', {}, { timeout: 15000 })
      assert.equal(refusal.code, fault === 'stderr_sentinel' ? 'BRIDGE_EXIT' : 'BRIDGE_MALFORMED_JSON')
      assert.ok(refusal.message.length < 100)
      assert.equal(refusal.message.includes('PRIVATE_CUSTOMER'), false)
      assert.equal(alert.textContent.includes('secret-label'), false)
      assert.equal(alert.textContent.includes('PRIVATE_CUSTOMER'), false)
      assert.equal(screen.getByLabelText('Roof layer').value, 'ROOF')
      assert.equal(successes, 0)
      await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
      assert.equal((await rpc(context, 'editor_load', context, context.customer_key)).version, 1)
      assert.deepEqual((await rpc(context, 'load', context)).application_record, pin)
      assert.equal(resources().child_count, 0)
      cleanup()
    }
    console.log('PASS: sentinel stderr and malformed reply payloads produce bounded public errors, retain actual JSX drafts and old pins, and close owned children')
  } finally { await dispose() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
