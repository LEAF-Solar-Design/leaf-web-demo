const assert = require('node:assert/strict')
const { rpc, ctx, copies, component, React, render, screen, fireEvent, waitFor, cleanup, dispose,
  resources, protocolStats } = require('./x07_adapter_jsx_journey.cjs')
const Editor = component('X07CustomerProfileEditor')

async function main() {
  try {
    for (const fault of ['reply_then_nonzero', 'reply_then_terminate']) {
      const context = { ...ctx(fault), customer_key: fault }
      const pin = (await rpc(context, 'confirm', context, { ...copies[0], expected_version: 0 })).applied_profile
      const profile = await rpc(context, 'editor_load', context, context.customer_key)
      const valid = JSON.stringify({ ok: true, result: { ...profile, version: 2 } })
      let attempts = 0, failures = 0, successes = 0
      render(React.createElement(Editor, { customerKey: context.customer_key, drawingKey: context.drawing_key,
        applicationRecord: pin, loadProfile: (customer, opts) => rpc(context, 'editor_load', context, customer, opts.signal),
        saveProfile: async (customer, roles, opts) => {
          attempts++
          try { const value = await rpc(context, 'editor_save', context, { customer, roles, expected_version: opts.expected_version },
            opts.signal, null, fault, { reply_json: valid }); successes++; return value }
          catch (error) { assert.equal(error.code, 'BRIDGE_EXIT'); failures++; throw error }
        }
      }))
      await screen.findByText('Profile version: 1', {}, { timeout: 15000 })
      await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
      fireEvent.change(screen.getByLabelText('Module block'), { target: { value: 'pAnEl' } })
      fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
      await screen.findByRole('alert', {}, { timeout: 15000 })
      assert.equal(attempts, 1); assert.equal(failures, 1); assert.equal(successes, 0)
      assert.equal(protocolStats.at(-1).receipts, 0)
      assert.ok(protocolStats.at(-1).bytes >= Buffer.byteLength(valid))
      assert.equal(screen.getByLabelText('Module block').value, 'pAnEl')
      assert.equal(resources().child_count, 0)
      assert.equal((await rpc(context, 'editor_load', context, context.customer_key)).version, 1)
      assert.deepEqual((await rpc(context, 'load', context)).application_record, pin)
      cleanup()
    }
    console.log('PASS: valid reply bytes before nonzero exit or termination never produce success, reject once, retain drafts/pins and close workers')
  } finally { await dispose(); assert.equal(resources().child_count, 0) }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
