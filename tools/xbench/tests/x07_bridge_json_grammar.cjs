const assert = require('node:assert/strict')
const { rpc, ctx, copies, component, React, render, screen, fireEvent, waitFor, cleanup, dispose, resources } = require('./x07_adapter_jsx_journey.cjs')
const Editor = component('X07CustomerProfileEditor')
const nested = count => '['.repeat(count) + '0' + ']'.repeat(count)

async function main() {
  try {
    const context = ctx('json-grammar')
    const first = await rpc(context, 'confirm', context, { ...copies[0], expected_version: 0 })
    for (const direction of ['request', 'reply']) {
      for (const fault of ['NaN', 'Infinity', 'trailing', 'depth']) {
        let code
        const fixture = direction === 'request'
          ? fault === 'trailing' ? { trailing_json: true } : { request_json_value: fault === 'depth' ? nested(32) : fault }
          : { reply_json: fault === 'trailing' ? '{} {}' : '{"ok":true,"result":' + (fault === 'depth' ? nested(32) : fault) + '}' }
        render(React.createElement(Editor, { customerKey: context.customer_key, drawingKey: context.drawing_key,
          applicationRecord: first.applied_profile,
          loadProfile: (customer, opts) => rpc(context, 'editor_load', context, customer, opts.signal),
          saveProfile: async (customer, roles, opts) => {
            try { return await rpc(context, 'editor_save', context, { customer, roles, expected_version: opts.expected_version },
              opts.signal, null, direction === 'reply' ? 'json_grammar' : null, fixture) }
            catch (error) { code = error.code; throw error }
          }
        }))
        await screen.findByText('Profile version: 1', {}, { timeout: 15000 })
        await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
        fireEvent.change(screen.getByLabelText('Module block'), { target: { value: 'pAnEl' } })
        fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
        await screen.findByRole('alert', {}, { timeout: 15000 })
        assert.equal(code, fault === 'depth' ? 'BRIDGE_JSON_DEPTH' : 'BRIDGE_MALFORMED_JSON')
        assert.equal(screen.getByLabelText('Module block').value, 'pAnEl')
        assert.equal((await rpc(context, 'editor_load', context, context.customer_key)).version, 1)
        assert.deepEqual((await rpc(context, 'load', context)).application_record, first.applied_profile)
        assert.equal(resources().child_count, 0)
        cleanup()
      }
    }
    const profile = await rpc(context, 'editor_load', context, context.customer_key, null, null, null,
      { request_json_value: nested(31) })
    assert.equal(profile.version, 1)
    const boundary = '{"ok":true,"result":' + JSON.stringify(profile) + ',"metadata":' + nested(31) + '}'
    assert.deepEqual(await rpc(context, 'editor_load', context, context.customer_key, null, null, 'json_grammar',
      { reply_json: boundary }), profile)
    console.log('PASS: nonfinite/trailing/excess-depth JSON requests and replies refuse; depth32 boundaries pass, drafts/pins retained and workers cleaned')
  } finally { await dispose() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
