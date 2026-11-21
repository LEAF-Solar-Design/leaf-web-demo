const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path')
const { rpc, ctx, copies, component, React, render, screen, fireEvent, waitFor, dispose, resources } = require('./x07_adapter_jsx_journey.cjs')
async function main() {
  try {
    const marker = 'x07-sentinel-' + path.basename(resources().root)
    const database = path.join(resources().root, 'profiles & mkdir ' + marker + ' &.sqlite')
    const receipts = path.join(resources().root, "receipts; mkdir " + marker + "; '$`().sqlite")
    const fixture = { database, receipt_database: receipts }
    const context = ctx('literal-path')
    const pin = (await rpc(context, 'confirm', context, { ...copies[0], expected_version: 0 }, null, null, null, fixture)).applied_profile
    const identity = await rpc(context, 'identity', context, null, null, null, null, fixture)
    assert.equal(identity.database, database)
    const Editor = component('X07CustomerProfileEditor')
    render(React.createElement(Editor, { customerKey: context.customer_key, applicationRecord: pin,
      loadProfile: (customer, opts) => rpc(context, 'editor_load', context, customer, opts.signal, null, null, fixture),
      saveProfile: (customer, roles, opts) => rpc(context, 'editor_save', context,
        { customer, roles, expected_version: opts.expected_version }, opts.signal, null, null, fixture) }))
    await screen.findByText('Profile version: 1', {}, { timeout: 15000 })
    fireEvent.click(screen.getByRole('button', { name: 'Add roof role' }))
    fireEvent.change(screen.getByLabelText('Roof layer'), { target: { value: 'ROOF' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
    await screen.findByText('Profile version: 2', {}, { timeout: 15000 })
    assert.equal((await rpc(context, 'editor_load', context, context.customer_key, null, null, null, fixture)).version, 2)
    assert.deepEqual((await rpc(context, 'load', context, null, null, null, null, fixture)).application_record, pin)
    assert.ok(fs.statSync(database).isFile()); assert.ok(fs.statSync(receipts).isFile())
    for (const directory of [resources().root, path.dirname(resources().root), path.resolve(__dirname, '../../..')])
      assert.equal(fs.existsSync(path.join(directory, marker)), false)
    await waitFor(() => assert.equal(resources().child_count, 0))
    console.log('PASS: shell metacharacters stay literal in disposable database paths; actual JSX save/reload retains old pin, creates no sentinel and closes children')
  } finally { await dispose() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
