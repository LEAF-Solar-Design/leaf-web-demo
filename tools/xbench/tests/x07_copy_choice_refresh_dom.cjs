const assert = require('node:assert/strict')
const { rpc, ctx, copies, intake, component, React, render, screen, fireEvent, waitFor, cleanup, dispose,
  resources } = require('./x07_adapter_jsx_journey.cjs')
const Picker = component('X07CopyPicker')
global.IS_REACT_ACT_ENVIRONMENT = true
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }

async function main() {
  try {
    for (const change of ['bounds', 'count', 'removed']) {
      const context = { ...ctx('refresh-' + change), customer_key: 'refresh-' + change }
      const gate = deferred()
      let calls = 0, producerWrites = 0, aborts = 0
      const original = await rpc(context, 'load', context)
      const pending = (value, opts) => {
        calls++
        opts.signal.addEventListener('abort', () => { aborts++ }, { once: true })
        // A retired producer reply deliberately ignores abort. The actual pin
        // producer is not invoked until a current explicit choice below.
        return gate.promise
      }
      const view = render(React.createElement(Picker, { copies, onPick: pending }))
      fireEvent.click(screen.getByRole('button', { name: 'Use copy 1' }))
      await waitFor(() => assert.equal(calls, 1))
      const nextIntake = structuredClone(intake)
      let nextCopies = structuredClone(copies)
      if (change === 'bounds') nextCopies[0] = { ...copies[0], design_window: [0, 0, 1, 0], oracle_count: 2 }
      if (change === 'count') {
        nextIntake.inserts.push({ name: 'Panel', layer: 'MODULES', x: 1, y: 0 })
        nextCopies[0].oracle_count = 4
      }
      if (change === 'removed') nextCopies = [nextCopies[1]]
      const fixture = { intake: nextIntake, copy_candidates: nextCopies }
      const refreshed = await rpc(context, 'load', context, null, null, null, null, fixture)
      assert.notEqual(refreshed.plan_token, original.plan_token)
      const save = (value, opts) => {
        calls++; producerWrites++
        return rpc(context, 'confirm', context, { ...value, plan_token: refreshed.plan_token, expected_version: 0 },
          opts.signal, null, null, fixture)
      }
      view.rerender(React.createElement(Picker, { copies: nextCopies, onPick: save }))
      assert.equal(aborts, 1)
      await React.act(async () => gate.resolve({ obsolete: true }))
      assert.equal(screen.queryByText('Design copy selected.'), null)
      assert.ok(!screen.getByRole('button', { name: 'Use copy 1' }).disabled)
      assert.equal(producerWrites, 0)
      assert.equal((await rpc(context, 'load', context)).application_record, null)
      assert.equal((await rpc(context, 'load', context)).expected_version, 0)
      fireEvent.click(screen.getByRole('button', { name: 'Use copy 1' }))
      await screen.findByText('Design copy selected.', {}, { timeout: 15000 })
      assert.equal(calls, 2); assert.equal(producerWrites, 1)
      const stored = await rpc(context, 'load', context, null, null, null, null, fixture)
      assert.equal(stored.application_record.version, 1)
      assert.deepEqual(stored.copies, nextCopies)
      cleanup()
    }
    console.log('PASS: changed bounds/counts or removed candidates abort old choices, retire obsolete replies and require one explicit fresh choice before pinning')
  } finally { await dispose(); assert.equal(resources().child_count, 0) }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
