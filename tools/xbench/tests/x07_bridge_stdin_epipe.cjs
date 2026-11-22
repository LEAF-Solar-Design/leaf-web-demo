const assert = require('node:assert/strict')
const { rpc, ctx, copies, component, React, render, screen, fireEvent, waitFor, dispose,
  resources } = require('./x07_adapter_jsx_journey.cjs')
const Editor = component('X07CustomerProfileEditor')

async function main() {
  const unhandled = []
  const record = error => unhandled.push(error)
  process.on('unhandledRejection', record); process.on('uncaughtExceptionMonitor', record)
  try {
    const context = ctx('stdin-epipe')
    const pin = (await rpc(context, 'confirm', context, { ...copies[0], expected_version: 0 })).applied_profile
    let calls = 0, rejected = 0, succeeded = 0
    const pipeErrors = []
    render(React.createElement(Editor, { customerKey: context.customer_key, drawingKey: context.drawing_key,
      applicationRecord: pin, loadProfile: (customer, opts) => rpc(context, 'editor_load', context, customer, opts.signal),
      saveProfile: async (customer, roles, opts) => {
        calls++
        try {
          const value = await rpc(context, 'editor_save', context, { customer, roles, expected_version: opts.expected_version },
            opts.signal, null, 'stdin_epipe', { padding: 'x'.repeat(512 * 1024), on_stdin_error: code => pipeErrors.push(code) })
          succeeded++; return value
        } catch (error) { assert.equal(error.code, 'BRIDGE_STDIN_FAILURE'); rejected++; throw error }
      }
    }))
    await screen.findByText('Profile version: 1', {}, { timeout: 15000 })
    await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
    fireEvent.change(screen.getByLabelText('Module block'), { target: { value: 'pAnEl' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
    await screen.findByRole('alert', {}, { timeout: 15000 })
    assert.equal(calls, 1); assert.equal(rejected, 1); assert.equal(succeeded, 0)
    assert.deepEqual(pipeErrors, ['EPIPE'])
    assert.equal(screen.getByLabelText('Module block').value, 'pAnEl')
    assert.equal(resources().child_count, 0)
    assert.deepEqual((await rpc(context, 'load', context)).application_record, pin)
    assert.equal((await rpc(context, 'editor_load', context, context.customer_key)).version, 1)
    assert.deepEqual(unhandled, [])
    console.log('PASS: child closes stdin before delivery, real EPIPE rejects once with bounded diagnostic, drafts/pins remain and no unhandled error or worker leak')
  } finally {
    await dispose(); assert.equal(resources().child_count, 0)
    process.removeListener('unhandledRejection', record); process.removeListener('uncaughtExceptionMonitor', record)
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
