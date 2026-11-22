const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path')
const { rpc, ctx, copies, component, React, render, screen, fireEvent, waitFor, cleanup, dispose,
  resources } = require('./x07_adapter_jsx_journey.cjs')
const Editor = component('X07CustomerProfileEditor')

async function main() {
  const unhandled = [], record = error => unhandled.push(error)
  process.on('unhandledRejection', record); process.on('uncaughtExceptionMonitor', record)
  try {
    const invalid = path.join(resources().root, 'invalid-executable.exe')
    fs.writeFileSync(invalid, Buffer.from([0x7f, 0x45, 0x4c, 0x46, 0, 0, 0, 0])); fs.chmodSync(invalid, 0o700)
    for (const [name, interpreter] of [['missing', path.join(resources().root, 'missing-python')], ['invalid', invalid]]) {
      const context = { ...ctx('spawn-' + name), customer_key: 'spawn-' + name }
      const pin = (await rpc(context, 'confirm', context, { ...copies[0], expected_version: 0 })).applied_profile
      let attempts = 0, failures = 0, successes = 0
      render(React.createElement(Editor, { customerKey: context.customer_key, drawingKey: context.drawing_key,
        applicationRecord: pin, loadProfile: (customer, opts) => rpc(context, 'editor_load', context, customer, opts.signal),
        saveProfile: async (customer, roles, opts) => {
          attempts++
          try { const result = await rpc(context, 'editor_save', context,
            { customer, roles, expected_version: opts.expected_version }, opts.signal, null, null, { interpreter })
            successes++; return result
          } catch (error) { failures++; assert.ok(['BRIDGE_STARTUP', 'BRIDGE_EXIT'].includes(error.code)); assert.ok(error.message.length <= 550); throw error }
        }
      }))
      await screen.findByText('Profile version: 1', {}, { timeout: 15000 })
      await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
      fireEvent.change(screen.getByLabelText('Module block'), { target: { value: 'pAnEl' } })
      fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
      await screen.findByRole('alert', {}, { timeout: 15000 })
      assert.equal(attempts, 1); assert.equal(failures, 1); assert.equal(successes, 0)
      assert.equal(screen.getByLabelText('Module block').value, 'pAnEl')
      assert.equal(resources().child_count, 0); assert.deepEqual(unhandled, [])
      assert.equal((await rpc(context, 'editor_load', context, context.customer_key)).version, 1)
      assert.deepEqual((await rpc(context, 'load', context)).application_record, pin)
      cleanup()
    }
    console.log('PASS: missing interpreter and invalid executable refuse once with bounded startup/loader errors, retain drafts/pins and leave no unhandled error or worker')
  } finally {
    await dispose(); assert.equal(resources().child_count, 0)
    process.removeListener('unhandledRejection', record); process.removeListener('uncaughtExceptionMonitor', record)
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
