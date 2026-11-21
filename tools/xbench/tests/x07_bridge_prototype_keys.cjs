const assert = require('node:assert/strict')
const { rpc, ctx, copies, strictJSON, component, React, render, screen, fireEvent, waitFor, cleanup, dispose, resources } = require('./x07_adapter_jsx_journey.cjs')
const Editor = component('X07CustomerProfileEditor')

async function main() {
  try {
    const prototypes = [Object.prototype, Array.prototype, Function.prototype]
    const before = prototypes.map(value => Object.getOwnPropertyDescriptors(value))
    const context = ctx('prototype-keys')
    const original = await rpc(context, 'confirm', context, { ...copies[0], expected_version: 0 })
    const raw = '{"__proto__":{"x07_polluted":true},"constructor":{"prototype":{"x07_polluted":true}},"prototype":{"x07_polluted":true}}'
    const parsed = strictJSON(raw)
    for (const key of ['__proto__', 'constructor', 'prototype']) assert.ok(Object.hasOwn(parsed, key))
    assert.equal(Object.getPrototypeOf(parsed), Object.prototype)
    const response = await rpc(context, 'editor_load', context, context.customer_key, null, null, 'json_grammar',
      { reply_json: '{"ok":true,"result":' + raw + '}' })
    assert.deepEqual(response, JSON.parse(raw))
    const loaded = await rpc(context, 'editor_load', context, context.customer_key, null, null, null, { request_json_value: raw })
    assert.equal(loaded.version, 1)
    let attempts = 0
    render(React.createElement(Editor, { customerKey: context.customer_key, applicationRecord: original.applied_profile,
      loadProfile: (customer, opts) => rpc(context, 'editor_load', context, customer, opts.signal),
      saveProfile: (customer, roles, opts) => {
        attempts++
        return rpc(context, 'editor_save', context, { customer, roles, expected_version: opts.expected_version }, opts.signal,
          null, attempts === 1 ? 'json_grammar' : null, attempts === 1 ? { reply_json: '{"ok":true,"result":' + raw + '}' } : {})
      } }))
    await screen.findByText('Profile version: 1', {}, { timeout: 15000 })
    fireEvent.click(screen.getByRole('button', { name: 'Add roof role' }))
    fireEvent.change(screen.getByLabelText('Roof layer'), { target: { value: 'ROOF' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
    await screen.findByRole('alert')
    assert.equal(screen.getByLabelText('Roof layer').value, 'ROOF')
    assert.equal((await rpc(context, 'editor_load', context, context.customer_key)).version, 1)
    await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
    fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
    await screen.findByText('Profile version: 2', {}, { timeout: 15000 })
    assert.equal(attempts, 2)
    assert.deepEqual((await rpc(context, 'load', context)).application_record, original.applied_profile)
    prototypes.forEach((value, index) => assert.deepEqual(Object.getOwnPropertyDescriptors(value), before[index]))
    assert.equal({}.x07_polluted, undefined)
    assert.equal(resources().child_count, 0)
    cleanup()
    console.log('PASS: own prototype-like JSON keys remain data; malformed profile reply retains drafts, explicit retry writes once and prototypes/pins remain intact')
  } finally { await dispose() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
