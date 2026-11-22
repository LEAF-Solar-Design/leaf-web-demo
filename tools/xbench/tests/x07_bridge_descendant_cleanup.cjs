const assert = require('node:assert/strict')
const { rpc, ctx, copies, component, React, render, screen, fireEvent, waitFor, cleanup, dispose, resources } = require('./x07_adapter_jsx_journey.cjs')
const Editor = component('X07CustomerProfileEditor')
const alive = pid => { try { process.kill(pid, 0); return true } catch { return false } }
async function main() {
  const owned = new Set()
  try {
    const context = ctx('descendant')
    const pin = (await rpc(context, 'confirm', context, { ...copies[0], expected_version: 0 })).applied_profile
    for (const mode of ['timeout', 'cancel']) {
      const controller = new AbortController()
      let output = '', descendant, rejected = 0
      render(React.createElement(Editor, { customerKey: context.customer_key, applicationRecord: pin,
        loadProfile: (customer, opts) => rpc(context, 'editor_load', context, customer, opts.signal),
        saveProfile: async (customer, roles, opts) => {
          try {
            return await rpc(context, 'editor_save', context, { customer, roles, expected_version: opts.expected_version },
              controller.signal, null, 'descendant', { timeout_ms: 4000, on_stdout_chunk: chunk => {
                output += chunk.toString()
                const match = output.match(/DESCENDANT:(\d+)/)
                if (match && !descendant) {
                  descendant = Number(match[1]); owned.add(descendant)
                  assert.ok(alive(descendant))
                  if (mode === 'cancel') controller.abort()
                }
              } })
          } catch (error) {
            assert.equal(error.code, mode === 'timeout' ? 'BRIDGE_TIMEOUT' : 'BRIDGE_CANCELLED')
            rejected++; throw error
          }
        } }))
      await screen.findByText('Profile version: 1', {}, { timeout: 15000 })
      fireEvent.click(screen.getByRole('button', { name: 'Add roof role' }))
      fireEvent.change(screen.getByLabelText('Roof layer'), { target: { value: 'ROOF' } })
      fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
      await screen.findByRole('alert', {}, { timeout: 15000 })
      assert.ok(descendant, 'fixture spawned a real grandchild before termination')
      assert.equal(alive(descendant), false)
      assert.equal(rejected, 1); assert.equal(resources().child_count, 0)
      assert.equal(screen.getByLabelText('Roof layer').value, 'ROOF')
      await waitFor(() => assert.ok(!screen.getByRole('button', { name: 'Save profile' }).disabled))
      assert.equal((await rpc(context, 'editor_load', context, context.customer_key)).version, 1)
      assert.deepEqual((await rpc(context, 'load', context)).application_record, pin)
      cleanup()
    }
    console.log('PASS: timeout/cancellation terminate owned worker and real grandchild before callback refusal; actual JSX drafts, old pins and explicit controls retained')
  } finally {
    await dispose()
    for (const pid of owned) if (alive(pid)) process.kill(pid)
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
