const assert = require('node:assert/strict')
const { rpc, ctx, component, React, render, screen, fireEvent, waitFor, cleanup, dispose, resources } = require('./x07_adapter_jsx_journey.cjs')
const Workflow = component('X07ProfileWorkflow')
global.IS_REACT_ACT_ENVIRONMENT = true

async function main() {
  let listeners = 0, pending = 0, maximum = 0, applied = 0
  const active = new Set(), requests = []
  try {
    const a = ctx('lifecycle-a'), b = { customer_key: 'lifecycle-b', project_key: 'lifecycle-b', drawing_key: 'b:v1' }
    const plans = [await rpc(a, 'load', a), await rpc(b, 'load', b)]
    const deferred = (scope, opts) => new Promise(resolve => {
      let retired = false
      const entry = { scope, resolve: value => { retire(); resolve(value) } }
      const retire = () => {
        if (retired) return
        retired = true; opts.signal.removeEventListener('abort', retire)
        listeners--; pending--; active.delete(entry)
      }
      listeners++; pending++; maximum = Math.max(maximum, pending)
      opts.signal.addEventListener('abort', retire, { once: true })
      active.add(entry); requests.push(entry)
    })
    const props = context => ({ customerKey: context.customer_key, projectKey: context.project_key, drawingKey: context.drawing_key,
      loadWorkflow: deferred, confirmRoles: (scope, value, opts) => deferred(scope, opts),
      applyProfile: (scope, value, opts) => deferred(scope, opts), onApplied: () => { applied++ } })
    for (let iteration = 0; iteration < 12; iteration++) {
      requests.length = 0
      const view = render(React.createElement(Workflow, props(a)))
      await waitFor(() => assert.equal(requests.length, 1))
      let retired = requests[0]
      if (iteration % 2) {
        await React.act(async () => retired.resolve(plans[0]))
        fireEvent.click(await screen.findByRole('button', { name: 'Confirm roles' }))
        fireEvent.click(await screen.findByRole('button', { name: 'Use copy 1' }))
        await waitFor(() => assert.equal(requests.length, 2))
        retired = requests[1]
      }
      const count = requests.length
      view.rerender(React.createElement(Workflow, props(b)))
      await waitFor(() => assert.equal(requests.length, count + 1))
      assert.equal(pending, 1); assert.equal(listeners, 1)
      await React.act(async () => retired.resolve({ ...plans[0], state: 'applied', applied_profile: { customer_key: a.customer_key, project_key: a.project_key, profile_id: 'retired-a', version: 1 } }))
      assert.equal(screen.queryByText('Drawing profile applied.'), null)
      assert.equal(screen.queryByRole('button', { name: 'Confirm roles' }), null)
      const last = requests.at(-1)
      view.unmount()
      assert.equal(pending, 0); assert.equal(listeners, 0); assert.equal(active.size, 0)
      await React.act(async () => last.resolve(plans[1]))
      assert.equal(applied, 0); assert.equal(resources().child_count, 0)
      cleanup()
    }
    assert.equal(maximum, 1)
    console.log('PASS: 12 workflow lifecycle cycles retire load/confirmation replies, bound owned abort listeners/pending callbacks to1 and clean all bridge children')
  } finally {
    await dispose()
    assert.equal(pending, 0); assert.equal(listeners, 0); assert.equal(active.size, 0)
    assert.equal(resources().child_count, 0)
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
