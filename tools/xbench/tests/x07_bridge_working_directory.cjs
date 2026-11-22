const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path')
const { rpc, ctx, copies, component, React, render, screen, fireEvent, waitFor, cleanup, dispose,
  resources } = require('./x07_adapter_jsx_journey.cjs')
const Editor = component('X07CustomerProfileEditor')
const normalized = value => process.platform === 'win32' ? path.resolve(value).toLowerCase() : path.resolve(value)

async function main() {
  const originalDirectory = process.cwd()
  try {
    const context = ctx('working-directory')
    const pin = (await rpc(context, 'confirm', context, { ...copies[0], expected_version: 0 })).applied_profile
    const profile = await rpc(context, 'editor_load', context, context.customer_key)
    const repo = path.resolve(__dirname, '../../..')
    let loads = 0, saves = 0
    const names = ['unrelated work space', '雪🌞 Unicode space']
    for (const [index, name] of names.entries()) {
      const directory = path.join(resources().root, name); fs.mkdirSync(directory); process.chdir(directory)
      const identity = await rpc(context, 'identity', context)
      assert.equal(normalized(identity.cwd), normalized(repo))
      assert.equal(normalized(identity.database), normalized(resources().database))
      assert.ok(path.isAbsolute(identity.database))
      assert.deepEqual(await rpc(context, 'editor_load', context, context.customer_key), profile)
      render(React.createElement(Editor, { customerKey: context.customer_key, applicationRecord: pin,
        loadProfile: (customer, opts) => { loads++; return rpc(context, 'editor_load', context, customer, opts.signal) },
        saveProfile: (customer, roles, opts) => { saves++; return rpc(context, 'editor_save', context,
          { customer, roles, expected_version: opts.expected_version }, opts.signal) } }))
      await screen.findByText('Profile version: 1', {}, { timeout: 15000 })
      assert.equal(screen.getByLabelText('Module block').value, 'Panel')
      if (index === names.length-1) {
        await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
        fireEvent.change(screen.getByLabelText('Module layer'), { target: { value: 'modules' } })
        fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
        await screen.findByText('Profile version: 2', {}, { timeout: 15000 })
      }
      assert.deepEqual(fs.readdirSync(directory), [])
      assert.deepEqual((await rpc(context, 'load', context)).application_record, pin)
      assert.equal(resources().child_count, 0); cleanup()
    }
    assert.equal(loads, 2); assert.equal(saves, 1)
    console.log('PASS: unrelated parent working directories with spaces/Unicode yield identical scoped reads, measured fixed worker cwd/absolute DB, one explicit save and complete cleanup')
  } finally { process.chdir(originalDirectory); await dispose(); assert.equal(resources().child_count, 0) }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
