import { useEffect, useRef, useState } from 'react'

const errors = {
  forbidden: 'You do not have permission to control these lights.',
  revoked: 'The connection permission was removed. Connect to Ops again.',
  conflict: 'The connection changed. Refresh and try again.',
  idempotency_conflict: 'This request conflicts with an earlier request. Try again.',
  invalid_input: 'The light settings were not accepted.',
  quota: 'Too many requests. Wait a moment and try again.',
  timeout: 'The lights did not confirm in time. Check them before trying again.',
  unavailable: 'The lights are unavailable. Try again later.',
  outcome_unknown: 'The result is unknown. Check the lights before trying again.',
  invalid_response: 'The lights returned an unexpected response.',
}
const errorText = (code) => Object.hasOwn(errors, code) ? errors[code] : 'Could not control the lights. Try again.'

export default function LightsControl({ client, projectId, connectionId, lights = [], onLoadLights, disabled = false }) {
  const [actions, setActions] = useState({})
  const [loading, setLoading] = useState(true)
  const [catalogError, setCatalogError] = useState(false)
  const [refresh, setRefresh] = useState(0)
  const [statuses, setStatuses] = useState({})
  const scope = useRef(null)
  const catalogEpoch = useRef(0)

  useEffect(() => {
    const current = { controller: new AbortController(), rows: new Set(), timers: new Map() }
    scope.current = current
    setStatuses({})
    return () => {
      current.controller.abort()
      for (const [timer, resolve] of current.timers) { clearTimeout(timer); resolve() }
    }
  }, [client, projectId, connectionId])

  useEffect(() => {
    const generation = ++catalogEpoch.current
    const controller = new AbortController()
    setActions({})
    setLoading(true)
    setCatalogError(false)
    const load = async () => {
      try {
        const found = {}
        let after
        const seen = new Set()
        for (let page = 0; page < 4; page += 1) {
          const catalog = await client.getActionCatalog(projectId, { signal: controller.signal, limit: 50, ...(after ? { after } : {}) })
          if (controller.signal.aborted) return
          for (const action of catalog.actions) {
            if (action.connection_id === connectionId && ['light-turn-on', 'light-turn-off'].includes(action.operation_ref?.id)) found[action.operation_ref.id] = action
          }
          after = catalog.next_cursor
          if (!after || seen.has(after)) break
          seen.add(after)
        }
        if (generation === catalogEpoch.current) setActions(found)
      } catch {
        if (!controller.signal.aborted) setCatalogError(true)
      } finally {
        if (!controller.signal.aborted && generation === catalogEpoch.current) setLoading(false)
      }
    }
    void load()
    return () => controller.abort()
  }, [client, projectId, connectionId, refresh])

  const tap = async (entityId, operationId, color) => {
    const current = scope.current
    const action = actions[operationId]
    if (disabled || !action || !current || current.controller.signal.aborted || current.rows.has(entityId)) return
    current.rows.add(entityId)
    const update = (text) => {
      if (!current.controller.signal.aborted) setStatuses((values) => ({ ...values, [entityId]: text }))
    }
    update('Sending')
    const controller = new AbortController()
    const abort = () => controller.abort()
    current.controller.signal.addEventListener('abort', abort, { once: true })
    const options = { signal: controller.signal }
    let deadlineTimer
    let timedOut = false
    try {
      const connection = await client.getConnection(projectId, connectionId, options)
      if (options.signal.aborted) return
      const random = crypto.getRandomValues(new Uint8Array(12))
      const result = await client.submitAction(projectId, {
        schema_version: 1, connection_id: connectionId,
        connector_ref: action.connector_ref, operation_ref: action.operation_ref,
        idempotency_key: `lights-${Array.from(random, (byte) => byte.toString(16).padStart(2, '0')).join('')}`,
        expected_grant_revision: connection.grant_revision,
        arguments: { entity_id: entityId, ...(color ? { color } : {}) },
      }, options)
      if (options.signal.aborted) return
      if (result.status !== 'admitted') { update(errorText(result.code)); return }
      deadlineTimer = setTimeout(() => {
        current.timers.delete(deadlineTimer)
        timedOut = true
        update(errorText('timeout'))
        controller.abort()
      }, 15000)
      current.timers.set(deadlineTimer, () => {})
      let snapshot = { run: result.run }
      for (let attempt = 0; attempt <= 15; attempt += 1) {
        if (options.signal.aborted) return
        if (snapshot.run.state === 'succeeded') { update('Done'); return }
        if (['failed', 'uncertain'].includes(snapshot.run.state)) {
          update(errorText(snapshot.run.state === 'uncertain' ? 'outcome_unknown' : snapshot.receipts?.at(-1)?.failure_code))
          return
        }
        if (attempt === 15) break
        await new Promise((resolve) => {
          const timer = setTimeout(() => { current.timers.delete(timer); resolve() }, 1000)
          current.timers.set(timer, resolve)
        })
        if (options.signal.aborted) return
        snapshot = await client.getAction(projectId, result.run.run_id, options)
      }
      update(errorText('timeout'))
    } catch (error) { if (!options.signal.aborted) update(errorText(error?.code)) }
    finally {
      clearTimeout(deadlineTimer)
      current.timers.delete(deadlineTimer)
      current.controller.signal.removeEventListener('abort', abort)
      if (timedOut) update(errorText('timeout'))
      current.rows.delete(entityId)
    }
  }

  const entities = [...new Map(lights.map((light) => {
    const id = light.id || light.entity_id
    const name = typeof light.name === 'string' ? light.name : light.attributes?.friendly_name
    return [id, { id, name: typeof name === 'string' && name ? name : id }]
  }).filter(([id]) => typeof id === 'string' && /^(light|group)\.[a-z0-9_]{1,64}$/.test(id))).values()]
  const all = entities.find((light) => light.id === 'light.all_lights') || entities.find((light) => light.name === 'All lights')
  const rows = all ? [{ ...all, name: 'All lights' }, ...entities.filter((light) => light.id !== all.id && light.id.startsWith('light.'))] : entities.filter((light) => light.id.startsWith('light.'))

  return <section className="ca-setup" aria-label="Lights control">
    <b>Lights</b>
    <button className="chip-neutral" disabled={disabled || loading} onClick={() => setRefresh((value) => value + 1)}>Refresh</button>
    {loading ? <p className="ca-copy">Loading light controls</p> : catalogError ? <p role="alert">Could not load light controls. Try Refresh.</p> : !Object.keys(actions).length ? <p className="ca-copy">Connect to Ops first</p> : <>
      {!rows.length && <button className="chip-act" disabled={disabled || !onLoadLights} onClick={onLoadLights}>Load lights</button>}
      {rows.map((light) => <div className="ca-setup" role="group" aria-label={light.name} key={light.id}>
        <span className="ca-copy">{light.name}</span><span className="ca-copy">{light.id}</span>
        <button className="chip-act" disabled={disabled || statuses[light.id] === 'Sending' || !actions['light-turn-on']} onClick={() => tap(light.id, 'light-turn-on')}>On</button>
        <button className="chip-act" disabled={disabled || statuses[light.id] === 'Sending' || !actions['light-turn-off']} onClick={() => tap(light.id, 'light-turn-off')}>Off</button>
        <input type="color" aria-label={`${light.name} color`} disabled={disabled || statuses[light.id] === 'Sending' || !actions['light-turn-on']} onChange={(event) => tap(light.id, 'light-turn-on', event.target.value)} />
        {statuses[light.id] && <span role="status">{statuses[light.id]}</span>}
      </div>)}
    </>}
  </section>
}
