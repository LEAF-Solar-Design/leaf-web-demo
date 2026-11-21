const assert = require('node:assert/strict')
const fs = require('node:fs')
const { rpc, ctx, copies, component, React, render, screen, fireEvent, waitFor, cleanup, dispose, resources } = require('./x07_adapter_jsx_journey.cjs')
const Editor = component('X07CustomerProfileEditor')

async function main() {
  try {
    const context = ctx('invalid-utf8')
    const first = await rpc(context, 'confirm', context, { ...copies[0], expected_version: 0 })
    for (const direction of ['request', 'reply']) {
      for (const [utf8_bytes, utf8_truncated] of [[[0xc3, 0x28], false], [[0x80], false], [[0xe9, 0x9b], true]]) {
        let code
        const fixture = { utf8_bytes, utf8_truncated, invalid_request_utf8: direction === 'request' }
        render(React.createElement(Editor, {
          customerKey: context.customer_key, drawingKey: context.drawing_key, applicationRecord: first.applied_profile,
          loadProfile: (customer, opts) => rpc(context, 'editor_load', context, customer, opts.signal),
          saveProfile: async (customer, roles, opts) => {
            try {
              return await rpc(context, 'editor_save', context, { customer, roles, expected_version: opts.expected_version },
                opts.signal, null, direction === 'reply' ? 'invalid_utf8' : null, fixture)
            } catch (error) { code = error.code; throw error }
          }
        }))
        await screen.findByText('Profile version: 1', {}, { timeout: 15000 })
        await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
        fireEvent.change(screen.getByLabelText('Module layer'), { target: { value: 'modules' } })
        fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
        await screen.findByRole('alert', {}, { timeout: 15000 })
        assert.equal(code, 'BRIDGE_UTF8')
        assert.equal(screen.getByLabelText('Module layer').value, 'modules')
        assert.equal((await rpc(context, 'editor_load', context, context.customer_key)).version, 1)
        assert.deepEqual((await rpc(context, 'load', context)).application_record, first.applied_profile)
        assert.equal(resources().child_count, 0)
        cleanup()
      }
    }
    console.log('PASS: invalid UTF-8, lone continuation and truncated codepoints refuse requests/replies; actual JSX drafts, versions and pins retained')
  } finally {
    await dispose()
    assert.equal(resources().child_count, 0)
    assert.equal(fs.existsSync(resources().database), false)
    assert.equal(fs.existsSync(resources().root), false)
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
