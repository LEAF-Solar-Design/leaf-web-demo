const assert = require('node:assert/strict')
const { rpc, ctx, copies, component, React, render, screen, fireEvent, waitFor, cleanup, dispose,
  resources } = require('./x07_adapter_jsx_journey.cjs')
const Editor = component('X07CustomerProfileEditor')

async function main() {
  try {
    for (const boundary of ['before-spawn', 'split-utf8']) {
      const context = { ...ctx('abort-' + boundary), customer_key: 'abort-' + boundary }
      const pin = (await rpc(context, 'confirm', context, { ...copies[0], expected_version: 0 })).applied_profile
      let attempts = 0, cancellations = 0, successes = 0, chunks = 0
      const controller = new AbortController()
      if (boundary === 'before-spawn') controller.abort()
      render(React.createElement(Editor, { customerKey: context.customer_key, drawingKey: context.drawing_key,
        applicationRecord: pin,
        loadProfile: (customer, opts) => rpc(context, 'editor_load', context, customer, opts.signal),
        saveProfile: async (customer, roles, opts) => {
          attempts++
          try {
            const value = await rpc(context, 'editor_save', context,
              { customer, roles, expected_version: opts.expected_version }, controller.signal, null,
              boundary === 'split-utf8' ? 'partial_utf8' : null, {
                on_stdout_chunk: chunk => {
                  chunks++
                  assert.equal(chunk.at(-1), 0xe9, 'worker stops after first byte of a three-byte Unicode label')
                  assert.equal(resources().child_count, 1)
                  controller.abort()
                }
              })
            successes++; return value
          } catch (error) { assert.equal(error.code, 'BRIDGE_CANCELLED'); cancellations++; throw error }
        }
      }))
      await screen.findByText('Profile version: 1', {}, { timeout: 15000 })
      await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
      fireEvent.change(screen.getByLabelText('Module block'), { target: { value: 'pAnEl' } })
      fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
      await screen.findByRole('alert', {}, { timeout: 15000 })
      assert.equal(attempts, 1); assert.equal(cancellations, 1); assert.equal(successes, 0)
      assert.equal(chunks, boundary === 'split-utf8' ? 1 : 0)
      assert.equal(screen.getByLabelText('Module block').value, 'pAnEl')
      assert.equal(resources().child_count, 0)
      assert.equal((await rpc(context, 'editor_load', context, context.customer_key)).version, 1)
      assert.deepEqual((await rpc(context, 'load', context)).application_record, pin)
      cleanup()
    }
    console.log('PASS: pre-spawn and partial-UTF8 cancellation each reject once, never succeed, retain drafts/pins and clean owned workers')
  } finally { await dispose(); assert.equal(resources().child_count, 0) }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
