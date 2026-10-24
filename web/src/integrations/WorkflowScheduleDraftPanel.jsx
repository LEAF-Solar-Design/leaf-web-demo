import { useLayoutEffect, useRef, useState } from 'react'
import { IntegrationRefV1, IntegrationWorkflowScheduleDraftV1 } from '../../../vendor/assistant-contract/src/integration'
import { IntegrationWorkflowScheduleDraftForm, IntegrationWorkflowScheduleDraftSaveError } from '../../../vendor/assistant-react/src/actions'
import { UUID } from './client.js'

const unavailable = 'Schedule draft saving is unavailable.'
const failed = () => new Error(unavailable)

function contextFor(props, key, binding) {
  let initialDraft = null
  try {
    if (typeof props.projectId !== 'string' || !UUID.test(props.projectId) || typeof props.client?.registerWorkflowScheduleDraft !== 'function' ||
        typeof props.client?.getWorkflowScheduleDraft !== 'function' || typeof props.guard !== 'function' ||
        typeof props.onForbidden !== 'function' || typeof props.signal?.aborted !== 'boolean' ||
        typeof props.signal?.addEventListener !== 'function' || typeof props.signal?.removeEventListener !== 'function') throw failed()
    const workflow_ref = IntegrationRefV1.parse(props.workflowRef)
    const identity = globalThis.crypto?.randomUUID?.()
    if (typeof identity !== 'string' || !UUID.test(identity)) throw failed()
    initialDraft = IntegrationWorkflowScheduleDraftV1.parse({ schema_version: 1, id: `schedule-draft-${identity}`, version: 1,
      title: props.title, workflow_ref, schedule: { kind: 'daily', timezone: 'UTC', hour: 9, minute: 0,
        gap_policy: 'next_valid_minute', fold_policy: 'first_occurrence' }, state: 'draft' })
  } catch { /* Fixed unavailable copy; no identity fallback. */ }
  return { client: props.client, projectId: props.projectId, signal: props.signal, binding, key, initialDraft }
}

export function WorkflowScheduleDraftPanel(props) {
  const parsed = IntegrationRefV1.safeParse(props.workflowRef)
  const binding = JSON.stringify([parsed.success ? parsed.data : null, typeof props.title === 'string' ? props.title : null,
    typeof props.guard === 'function', typeof props.onForbidden === 'function',
    typeof props.client?.registerWorkflowScheduleDraft === 'function', typeof props.client?.getWorkflowScheduleDraft === 'function'])
  const [context, setContext] = useState(() => contextFor(props, 1, binding))
  if (context.client !== props.client || context.projectId !== props.projectId || context.signal !== props.signal || context.binding !== binding) {
    setContext(contextFor(props, context.key + 1, binding))
    return null
  }
  if (!context.initialDraft) return <p role="status">{unavailable}</p>
  return <ScopedScheduleDraft key={context.key} {...props} context={context} />
}

function ScopedScheduleDraft({ client, projectId, signal, guard, onForbidden, context, mode = 'light' }) {
  const [availability, setAvailability] = useState(signal.aborted ? 'forbidden' : 'ready')
  const scope = useRef(null)
  useLayoutEffect(() => {
    const ctx = { controller: new AbortController() }
    scope.current = ctx
    const abort = () => { ctx.controller.abort(); setAvailability('forbidden') }
    signal.addEventListener('abort', abort, { once: true })
    if (signal.aborted) abort()
    return () => { scope.current = null; ctx.controller.abort(); signal.removeEventListener('abort', abort) }
  }, [signal])

  async function save(input, saveSignal) {
    // Keep the comparison snapshot independent of objects passed to injected clients.
    const serialized = JSON.stringify(IntegrationWorkflowScheduleDraftV1.parse(input))
    const ctx = scope.current
    const controller = new AbortController()
    const abort = () => controller.abort()
    const current = () => scope.current === ctx && ctx && !ctx.controller.signal.aborted && !signal.aborted
    const assertCurrent = () => { if (!current() || controller.signal.aborted) throw failed() }
    const invalidate = (forbidden) => {
      if (!current()) return
      ctx.controller.abort()
      setAvailability(forbidden ? 'forbidden' : 'unavailable')
      if (forbidden) onForbidden()
    }
    const checkGuard = async () => {
      try { await guard(controller.signal) }
      catch (error) { assertCurrent(); invalidate(error?.status === 401 || error?.status === 403); throw failed() }
      assertCurrent()
    }
    if (!current() || saveSignal.aborted) throw failed()
    ctx.controller.signal.addEventListener('abort', abort, { once: true })
    saveSignal.addEventListener('abort', abort, { once: true })
    let postConflict = false
    try {
      const snapshot = JSON.parse(serialized)
      const initial = context.initialDraft
      if (snapshot.id !== initial.id || snapshot.version !== initial.version ||
          JSON.stringify(snapshot.workflow_ref) !== JSON.stringify(initial.workflow_ref)) throw failed()
      const payload = JSON.parse(serialized)
      await checkGuard()
      assertCurrent()
      let response
      try { response = await client.registerWorkflowScheduleDraft(projectId, payload, { signal: controller.signal }) }
      catch (error) {
        assertCurrent()
        if (error?.status === 401 || error?.status === 403) invalidate(true)
        else if (error?.status === 409) postConflict = true
        throw failed()
      }
      assertCurrent()
      const reference = IntegrationRefV1.parse(response)
      if (reference.id !== snapshot.id || reference.version !== snapshot.version) throw failed()
      await checkGuard()
      assertCurrent()
      const readback = await client.getWorkflowScheduleDraft(projectId, { ...reference }, { signal: controller.signal })
      assertCurrent()
      if (JSON.stringify(IntegrationWorkflowScheduleDraftV1.parse(readback)) !== serialized) throw failed()
      await checkGuard()
      assertCurrent()
      return reference
    } catch (error) {
      if (current() && !controller.signal.aborted && (error?.status === 401 || error?.status === 403)) invalidate(true)
      if (postConflict) throw new IntegrationWorkflowScheduleDraftSaveError('conflict')
      throw failed()
    } finally {
      ctx.controller.signal.removeEventListener('abort', abort)
      saveSignal.removeEventListener('abort', abort)
    }
  }

  return <div className="workflow-schedule-draft-host">
    <style>{`.ops-workspace .workflow-schedule-draft-host .leaf-schedule-draft label,
      .ops-workspace .workflow-schedule-draft-host .leaf-schedule-draft button { margin: 0; }`}</style>
    <IntegrationWorkflowScheduleDraftForm initialDraft={context.initialDraft} contextKey={String(context.key)}
      availability={availability} onSave={save} mode={mode} />
  </div>
}
