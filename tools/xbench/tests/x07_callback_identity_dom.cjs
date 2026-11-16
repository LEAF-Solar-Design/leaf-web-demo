const assert = require('node:assert/strict')
const { rpc, ctx, copies, component, React, render, screen, fireEvent, waitFor, cleanup, dispose,
  resources } = require('./x07_adapter_jsx_journey.cjs')
const Editor = component('X07CustomerProfileEditor'), Workflow = component('X07ProfileWorkflow')
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done }); return { promise, resolve } }
global.IS_REACT_ACT_ENVIRONMENT = true

async function main() {
  try {
    const a = ctx('callback-identities'), b = { customer_key: 'identity-b', project_key: 'identity-b', drawing_key: 'identity-b:v1' }
    const first = await rpc(a, 'confirm', a, { ...copies[0], expected_version: 0 })
    const profile = await rpc(a, 'editor_load', a, a.customer_key)
    const load = deferred(), save = deferred()
    let loads = 0, saves = 0, obsoleteLoad = 0
    const editorProps = generation => ({ customerKey: a.customer_key, drawingKey: a.drawing_key,
      applicationRecord: first.applied_profile,
      loadProfile: () => { loads++; return load.promise },
      saveProfile: async (customer, roles, opts) => {
        saves++
        assert.equal(generation, saves === 1 ? 2 : 3)
        const value = await rpc(a, 'editor_save', a, { customer, roles, expected_version: opts.expected_version }, opts.signal)
        if (saves === 1) { await save.promise }
        return value
      }
    })
    const view = render(React.createElement(Editor, editorProps(1)))
    await waitFor(() => assert.equal(loads, 1))
    view.rerender(React.createElement(Editor, editorProps(2)))
    await React.act(async () => load.resolve(profile))
    await screen.findByText('Profile version: 1')
    assert.equal(loads, 1)
    fireEvent.change(screen.getByLabelText('Module block'), { target: { value: 'pAnEl' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
    await waitFor(() => assert.equal(saves, 1))
    view.rerender(React.createElement(Editor, editorProps(3)))
    fireEvent.change(screen.getByLabelText('Module layer'), { target: { value: 'modules' } })
    await React.act(async () => save.resolve())
    await screen.findByText('Profile version: 2', {}, { timeout: 15000 })
    assert.equal(screen.getByLabelText('Module layer').value, 'modules')
    await screen.findByText('Profile saved. Newer edits are unsaved.')
    assert.equal(saves, 1); assert.equal(loads, 1)
    fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
    await screen.findByText('Profile version: 3', {}, { timeout: 15000 })
    assert.equal(saves, 2)
    assert.deepEqual((await rpc(a, 'load', a)).application_record, first.applied_profile)
    // A new context must retire the pending producer even when it ignores abort.
    const staleLoad = deferred()
    view.rerender(React.createElement(Editor, { ...editorProps(4), drawingKey: a.drawing_key + ':next',
      loadProfile: () => { obsoleteLoad++; return staleLoad.promise } }))
    await waitFor(() => assert.equal(obsoleteLoad, 1))
    view.rerender(React.createElement(Editor, { customerKey: b.customer_key, drawingKey: b.drawing_key,
      loadProfile: () => Promise.resolve({ ...profile, customer_key: b.customer_key, profile_id: 'synthetic-b', version: 1 }),
      saveProfile: () => { throw new Error('no implicit foreign write') } }))
    await screen.findByText('Profile id: synthetic-b')
    await React.act(async () => staleLoad.resolve({ ...profile, version: 99 }))
    assert.equal(screen.queryByText('Profile version: 99'), null)
    assert.equal(saves, 2)
    cleanup()

    const loadPlan = deferred(), apply = deferred()
    const planA = await rpc(a, 'load', a), planB = await rpc(b, 'load', b)
    let workflowLoads = 0, applies = 0, accepted = 0
    const workflowProps = (scope, generation) => ({ customerKey: scope.customer_key, projectKey: scope.project_key,
      drawingKey: scope.drawing_key,
      loadWorkflow: () => { workflowLoads++; return scope === a ? loadPlan.promise : Promise.resolve(planB) },
      applyProfile: () => { applies++; assert.equal(generation, 2); return apply.promise },
      confirmRoles: () => { throw new Error('no implicit confirmation') }, onApplied: () => { accepted++ } })
    const flow = render(React.createElement(Workflow, workflowProps(a, 1)))
    await waitFor(() => assert.equal(workflowLoads, 1))
    flow.rerender(React.createElement(Workflow, workflowProps(a, 2)))
    await React.act(async () => loadPlan.resolve(planA))
    fireEvent.click(await screen.findByRole('button', { name: 'Use copy 1' }))
    await waitFor(() => assert.equal(applies, 1))
    flow.rerender(React.createElement(Workflow, workflowProps(a, 3)))
    assert.equal(workflowLoads, 1); assert.equal(applies, 1)
    flow.rerender(React.createElement(Workflow, workflowProps(b, 4)))
    await screen.findByRole('button', { name: 'Confirm roles' })
    await React.act(async () => apply.resolve({ ...a, state: 'applied', applied_profile: first.applied_profile }))
    assert.equal(accepted, 0); assert.equal(applies, 1); assert.equal(workflowLoads, 2)
    assert.equal(screen.queryByText('Drawing profile applied.'), null)
    assert.equal((await rpc(b, 'load', b)).expected_version, 0)
    console.log('PASS: callback identity rerenders retain one load/save/apply, latest callbacks serve explicit actions, newer drafts survive and old-context replies retire')
  } finally { cleanup(); await dispose(); assert.equal(resources().child_count, 0) }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
