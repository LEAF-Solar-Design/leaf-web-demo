const assert = require('node:assert/strict')
const { rpc, ctx, copies, component, React, render, screen, fireEvent, waitFor, cleanup, dispose,
  resources, protocolStats } = require('./x07_adapter_jsx_journey.cjs')
const Editor = component('X07CustomerProfileEditor')

async function main() {
  try {
    for (const mode of ['two-in-one-chunk', 'unsolicited-after-reply']) {
      const context = { ...ctx('extra-frames-' + mode), customer_key: 'frames-' + mode }
      const first = await rpc(context, 'confirm', context, { ...copies[0], expected_version: 0 })
      const profile = await rpc(context, 'editor_load', context, context.customer_key)
      const valid = JSON.stringify({ ok: true, result: { ...profile, version: 2 } }) + '\n'
      const extra = mode === 'two-in-one-chunk' ? valid : '{"event":"unsolicited"}\n'
      let writes = 0, successes = 0, refusals = 0, code
      render(React.createElement(Editor, { customerKey: context.customer_key, drawingKey: context.drawing_key,
        applicationRecord: first.applied_profile,
        loadProfile: (customer, opts) => rpc(context, 'editor_load', context, customer, opts.signal),
        saveProfile: async (customer, roles, opts) => {
          writes++
          try {
            const value = writes === 1
              ? await rpc(context, 'editor_save', context, { customer, roles, expected_version: opts.expected_version }, opts.signal,
                null, 'json_grammar', { reply_json: valid + extra,
                  chunk_pattern: mode === 'two-in-one-chunk' ? [1024 * 1024] : [Buffer.byteLength(valid), 1] })
              : await rpc(context, 'editor_save', context, { customer, roles, expected_version: opts.expected_version }, opts.signal)
            successes++; return value
          } catch (error) { refusals++; code = error.code; throw error }
        }
      }))
      await screen.findByText('Profile version: 1', {}, { timeout: 15000 })
      await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
      fireEvent.change(screen.getByLabelText('Module block'), { target: { value: 'pAnEl' } })
      fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
      await screen.findByRole('alert', {}, { timeout: 15000 })
      assert.equal(code, 'BRIDGE_MALFORMED_JSON')
      const stat = protocolStats.at(-1)
      assert.equal(stat.receipts, 0)
      assert.equal(stat.parts === 1, mode === 'two-in-one-chunk')
      assert.equal(writes, 1); assert.equal(successes, 0); assert.equal(refusals, 1)
      assert.equal(screen.getByLabelText('Module block').value, 'pAnEl')
      assert.equal((await rpc(context, 'editor_load', context, context.customer_key)).version, 1)
      assert.deepEqual((await rpc(context, 'load', context)).application_record, first.applied_profile)
      assert.equal(resources().child_count, 0)
      fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
      await screen.findByText('Profile version: 2', {}, { timeout: 15000 })
      assert.equal(writes, 2); assert.equal(successes, 1); assert.equal(refusals, 1)
      assert.deepEqual((await rpc(context, 'load', context)).application_record, first.applied_profile)
      cleanup()
    }
    console.log('PASS: complete extra frames refuse as one protocol response across coalesced/split delivery; no success callback, drafts/pins retained and explicit retry writes once')
  } finally { await dispose(); assert.equal(resources().child_count, 0) }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
