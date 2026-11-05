import { useEffect, useRef, useState } from 'react'
import { deviceError, useDeviceCommand } from '../useDeviceRegistry.js'
import { createIntegrationsClient } from '../integrations/client.js'

const string = (value) => typeof value === 'string' ? value : ''
const ipv4 = (host) => /^(\d{1,3}\.){3}\d{1,3}$/.test(host) && host.split('.').every((part) => Number(part) <= 255 && String(Number(part)) === part)
const flowError = (data) => {
  const errors = data?.errors
  if (errors && typeof errors === 'object' && Object.keys(errors).length) {
    return deviceError({ code: string(errors.base) || 'flow_error' })
  }
  if (data?.type === 'abort') return deviceError({ code: string(data.reason) || 'flow_error' })
  return null
}

export default function HomeAssistantSetup({ deviceId, projectId, mock = false, active = true, disabled = false, onCommand, onGetCommand, onRelayToken }) {
  const [steps, setSteps] = useState({})
  const [hue, setHue] = useState(null)
  const [hueHost, setHueHost] = useState('')
  const [hueHostError, setHueHostError] = useState(null)
  const [seconds, setSeconds] = useState(0)
  const [bulbs, setBulbs] = useState([])
  const [host, setHost] = useState('')
  const [hostError, setHostError] = useState(null)
  const [lights, setLights] = useState([])
  const [selected, setSelected] = useState([])
  const [relayLabel, setRelayLabel] = useState('Ops')
  const [relay, setRelay] = useState(null)
  const [relayBusy, setRelayBusy] = useState(false)
  const [copyError, setCopyError] = useState(null)
  const epoch = useRef(0)
  const hueInitFlows = useRef(new Set())
  const relayLock = useRef(false)
  const opsRequest = useRef(null)
  const live = useRef(active)
  live.current = active
  const { run, pending } = useDeviceCommand({ active: active && !mock, onCommand, onGetCommand })
  const busy = !!pending || relayBusy || disabled || mock

  useEffect(() => {
    live.current = active
    setSteps({})
    setHue(null)
    hueInitFlows.current.clear()
    setHueHost('')
    setHueHostError(null)
    setRelay(null)
    setBulbs([])
    setLights([])
    setSelected([])
    setHost('')
    setHostError(null)
    setRelayBusy(false)
    relayLock.current = false
    return () => { epoch.current += 1; live.current = false; opsRequest.current?.abort() }
  }, [active, deviceId, projectId, mock])

  useEffect(() => {
    if (!active || !hue || hue.stepId === 'manual' || hue.stepId === 'init') return undefined
    const tick = () => setSeconds(Math.max(0, Math.ceil((hue.deadline - Date.now()) / 1000)))
    tick()
    const timer = setInterval(tick, 1000)
    return () => clearInterval(timer)
  }, [active, hue])

  const setStep = (name, state, message) => setSteps((current) => ({ ...current, [name]: { state, message } }))
  const execute = async (name, op, args, accept) => {
    if (busy || !active) return null
    const gen = epoch.current
    setStep(name, 'Working')
    try {
      const result = await run(deviceId, op, args)
      if (!result || gen !== epoch.current || !live.current) return null
      const accepted = accept ? await accept(result.data) : true
      if (accepted && gen === epoch.current && live.current) setStep(name, 'Done')
      return result
    } catch (error) {
      if (gen === epoch.current && live.current) setStep(name, 'Needs attention', deviceError(error))
      return null
    }
  }

  const acceptHue = async (data) => {
    const error = flowError(data)
    const id = string(data?.flow_id) || hue?.id
    if (data?.type === 'create_entry' && !error) { setHue(null); return true }
    if (id && data?.type !== 'abort') {
      const stepId = string(data?.step_id)
      setHue({ id, stepId, deadline: Date.now() + 30000 })
      setHueHostError(null)
      if (data.type === 'form' && stepId === 'init' && !error) {
        if (hueInitFlows.current.has(id)) {
          setStep('hue', 'Needs attention', 'The Hue setup did not provide a link step. Try linking again.')
          return false
        }
        hueInitFlows.current.add(id)
        setStep('hue', 'Working')
        const gen = epoch.current
        const result = await run(deviceId, 'ha.flow.step', { flow_id: id, data: { id: 'manual' } })
        if (!result || gen !== epoch.current || !live.current) return false
        return acceptHue(result.data)
      }
      setStep('hue', error ? 'Needs attention' : stepId === 'manual' ? 'Enter bridge IP' : 'Waiting for button', error)
    } else {
      setHue(null)
      setStep('hue', 'Needs attention', error || 'The Hue setup did not provide a link step. Try linking again.')
    }
    return false
  }

  const addBulb = (ip) => execute('wiz', 'ha.flow.start', { handler: 'wiz', data: { host: ip } }, (data) => {
    if (data?.type === 'create_entry' && !flowError(data)) return true
    setStep('wiz', 'Needs attention', flowError(data) || 'The light needs more setup. Try adding it again.')
    return false
  })

  const addAll = async () => {
    for (const bulb of bulbs) {
      const result = await addBulb(bulb.host)
      if (!result || result.data?.type !== 'create_entry' || !live.current) break
    }
  }

  const createRelay = async () => {
    if (busy || relayLock.current || !relayLabel.trim() || !active || !onRelayToken) return
    relayLock.current = true
    setRelayBusy(true)
    setRelay(null)
    setCopyError(null)
    setStep('ops', 'Working')
    const gen = epoch.current
    try {
      const result = await onRelayToken(deviceId, relayLabel.trim())
      if (!live.current || gen !== epoch.current) return
      if (!string(result?.relay_token) || !string(result?.relay_origin) || !string(result?.path_prefix)) throw new Error('The connector token could not be created. Try again.')
      setRelay({ token: string(result.relay_token), origin: string(result.relay_origin), prefix: string(result.path_prefix) })
      setStep('ops', 'Token ready')
    } catch (error) {
      if (live.current && gen === epoch.current) setStep('ops', 'Needs attention', deviceError(error))
    } finally {
      if (gen === epoch.current) { relayLock.current = false; setRelayBusy(false) }
    }
  }

  const useInOps = async () => {
    if (busy || relayLock.current || !active || !projectId || !onRelayToken || !relayLabel.trim() || relayLabel.trim().length > 120) return
    relayLock.current = true
    setRelayBusy(true)
    setRelay(null)
    setCopyError(null)
    setStep('ops', 'Connecting')
    const gen = epoch.current
    const controller = new AbortController()
    opsRequest.current = controller
    const options = { signal: controller.signal }
    const current = () => live.current && gen === epoch.current && !controller.signal.aborted
    const send = async (task) => {
      if (!current()) throw new Error()
      const result = await task()
      if (!current()) throw new Error()
      return result
    }
    const conflict = (error) => error?.status === 409 || ['already_exists', 'already-exists'].includes(error?.code)
    let message = 'Could not load the Home Assistant document. Try again.'
    let credential = null
    try {
      const client = createIntegrationsClient()
      const id = `ha-relay-${deviceId.slice(0, 8)}`
      const title = relayLabel.trim()
      const response = await send(() => fetch('/api/devices/relay-openapi/homeassistant.json', { ...options, cache: 'no-store', redirect: 'error' }))
      if (!response.ok) throw new Error()
      const bytes = await send(() => response.arrayBuffer())
      const digest = await send(() => crypto.subtle.digest('SHA-256', bytes))
      const hex = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('')
      message = 'The Home Assistant document could not be verified. Reload the page and try again.'
      if (hex !== response.headers.get('X-Leaf-Document-Sha256')) throw new Error()
      const document = JSON.parse(new TextDecoder().decode(bytes))
      const origin = document.servers?.[0]?.url
      if (origin !== window.location.origin) throw new Error()
      message = 'Could not import the Home Assistant connector. Check your project access and try again.'
      let imported
      try {
        imported = await send(() => client.importConnector(projectId, { document, options: { id, title } }, options))
        if (!imported.reference) throw new Error()
      } catch (error) { if (!conflict(error)) throw error }
      const connector = await send(() => client.getConnector(projectId, id, options))
      if (imported && (!['id', 'version', 'digest'].every((key) => connector.reference[key] === imported.reference[key]) || connector.revision !== imported.revision)) throw new Error()
      message = 'Could not publish the Home Assistant connector. Try again.'
      let published
      let operationIds
      if (connector.state === 'published') {
        published = await send(() => client.getConnectorVersion(projectId, id, connector.reference.version, options))
        operationIds = published.definition.operations.map((op) => op.id)
      } else {
        operationIds = connector.draft_definition.operations.map((op) => op.id)
        try {
          published = await send(() => client.publishConnector(projectId, id, { version: connector.reference.version,
            expectedRevision: connector.revision, readOperationIds: operationIds }, options))
        } catch (error) {
          if (!conflict(error)) throw error
          published = await send(() => client.getConnectorVersion(projectId, id, connector.reference.version, options))
          if (operationIds.length !== published.definition.operations.length || !operationIds.every((op) => published.definition.operations.some((item) => item.id === op))) throw new Error()
        }
      }
      if (!operationIds.includes('light-turn-on')) throw new Error()
      message = 'Could not create the Ops connection. Check your project access and try again.'
      let connection
      try {
        connection = await send(() => client.createConnection(projectId, { id, title, connectorRef: published.reference, origin, operationIds }, options))
      } catch (error) {
        if (!conflict(error)) throw error
        connection = await send(() => client.getConnectionSummary(projectId, id, options))
        if (!['setup', 'active', 'degraded', 'reconnect_required'].includes(connection.state) || connection.title !== title || connection.origin !== origin ||
            !['id', 'version', 'digest'].every((key) => connection.connector_ref[key] === published.reference[key]) ||
            connection.operation_ids.length !== operationIds.length || !operationIds.every((op) => connection.operation_ids.includes(op))) throw new Error()
      }
      message = 'Could not create the relay credential. Check that the device is available and try again.'
      credential = await send(() => onRelayToken(deviceId, title))
      if (!string(credential?.relay_token) || credential.relay_origin !== origin) throw new Error()
      message = 'Could not save the relay credential in Ops. Check your project access and try again.'
      try {
        await send(() => client.setConnectionCredential(projectId, id, { purpose: 'access_token', value: credential.relay_token, expectedRevision: connection.revision }, options))
      } finally { credential = null }
      message = 'The credential was saved, but Turn lights on is not available in Ops yet. Try again.'
      const cursors = new Set()
      let after
      do {
        const catalog = await send(() => client.getActionCatalog(projectId, { ...options, limit: 50, ...(after ? { after } : {}) }))
        if (catalog.actions.some((action) => action.connection_id === id && action.title === 'Turn lights on' && action.operation_ref.id === 'light-turn-on')) {
          setSteps((steps) => ({ ...steps, ops: { state: 'Connected to Ops', title: connection.title } }))
          return
        }
        after = catalog.next_cursor
        if (after && cursors.has(after)) throw new Error()
        cursors.add(after)
      } while (after)
      throw new Error()
    } catch {
      if (current()) setStep('ops', 'Needs attention', message)
    } finally {
      credential = null
      if (gen === epoch.current) { relayLock.current = false; setRelayBusy(false) }
    }
  }

  const copyRelay = async () => {
    try {
      await navigator.clipboard.writeText(relay.token)
      if (live.current) { setRelay(null); setStep('ops', 'Token copied') }
    } catch { if (live.current) setCopyError('Could not copy. Select and copy the token before hiding it.') }
  }

  const status = (name) => (
    <>
      <span className={`state ${steps[name]?.message ? 'fail' : 'sub'}`} role="status">
        <span className={`dot ${['Working', 'Connecting'].includes(steps[name]?.state) ? 'pulse' : ''}`} />{steps[name]?.state || 'Not started'}
      </span>
      {steps[name]?.title && <p className="ca-copy">{steps[name].title}</p>}
      {steps[name]?.message && <p className="field-err" role="alert"><span className="dot red" />{steps[name].message}</p>}
    </>
  )

  return (
    <section className="ca-setup" aria-label="Home Assistant setup">
      <b>Home Assistant setup</b>
      <ol className="ca-steps">
        <li>
          <button className="chip-act" disabled={busy} onClick={() => execute('onboard', 'ha.onboard', {})}>Set up Home Assistant</button>
          {status('onboard')}
        </li>
        <li>
          <button className="chip-act" disabled={busy} onClick={() => execute('hue', 'ha.flow.start', { handler: 'hue' }, acceptHue)}>Link Hue bridge</button>
          {status('hue')}
          {hue && hue.stepId !== 'init' && <div className="ca-setup">
            {hue.stepId === 'manual' ? <>
              <label className={`ca-field${hueHostError ? ' field-invalid' : ''}`}>
                <span className="ca-field-k">Hue bridge IP address</span>
                <input value={hueHost} autoComplete="off" inputMode="decimal" enterKeyHint="done" maxLength={15}
                  aria-invalid={!!hueHostError} onChange={(event) => { setHueHost(event.target.value); setHueHostError(null) }} disabled={busy} />
              </label>
              {hueHostError && <p className="field-err" role="alert">{hueHostError}</p>}
              <button className="chip-act" disabled={busy || !hueHost.trim()} onClick={() => {
                if (!ipv4(hueHost.trim())) { setHueHostError('Enter an IPv4 address, such as 192.168.1.20.'); return }
                void execute('hue', 'ha.flow.step', { flow_id: hue.id, data: { host: hueHost.trim() } }, acceptHue)
              }}>Use this bridge</button>
            </> : <>
            <p className="ca-copy">Press the round button on top of your Hue bridge, then click Continue</p>
            <p className="ca-copy">{seconds > 0 ? `${seconds} s remaining` : 'Button window expired. Press the button again, then Continue.'}</p>
            <button className="chip-act" disabled={busy} onClick={() => execute('hue', 'ha.flow.step', { flow_id: hue.id, data: {} }, acceptHue)}>Continue</button>
            </>}
          </div>}
        </li>
        <li>
          <button className="chip-act" disabled={busy} onClick={() => execute('wiz', 'ha.flow.list', {}, (data) => {
            const rows = Array.isArray(data) ? data : Array.isArray(data?.flows) ? data.flows : []
            const found = rows.filter((row) => row?.handler === 'wiz').map((row) => ({ host: string(row?.context?.host) || string(row?.data?.host) || string(row?.host) })).filter((row) => ipv4(row.host))
            setBulbs([...new Map(found.map((row) => [row.host, row])).values()])
            setStep('wiz', found.length ? 'Discovered' : 'No lights discovered')
            return false
          })}>Add WiZ lights</button>
          {status('wiz')}
          {steps.wiz?.state === 'No lights discovered' && <p className="ca-copy">No WiZ lights discovered. Add a light by IP.</p>}
          {bulbs.length > 0 && <div className="ca-setup">
            {bulbs.map((bulb) => <p className="ca-copy" key={bulb.host}>{bulb.host}</p>)}
            <button className="chip-act" disabled={busy} onClick={addAll}>Add all</button>
          </div>}
          <label className={`ca-field${hostError ? ' field-invalid' : ''}`}>
            <span className="ca-field-k">WiZ IPv4 address</span>
            <input value={host} autoComplete="off" title="A local light address, not a saved personal address" inputMode="decimal" enterKeyHint="done" maxLength={15}
              aria-invalid={!!hostError} onChange={(event) => { setHost(event.target.value); setHostError(null) }} disabled={busy} />
          </label>
          {hostError && <p className="field-err" role="alert">{hostError}</p>}
          <button className="chip-act" disabled={busy || !host.trim()} onClick={() => {
            if (!ipv4(host.trim())) { setHostError('Enter an IPv4 address, such as 192.168.1.20.'); return }
            void addBulb(host.trim())
          }}>Add by IP</button>
        </li>
        <li>
          <button className="chip-neutral" disabled={busy} onClick={() => execute('group', 'http.proxy', { package: 'homeassistant', method: 'GET', path: '/api/states' }, (data) => {
            if (data?.status !== 200 || !Array.isArray(data.body_json)) throw { code: 'upstream_error' }
            const rows = data.body_json.filter((row) => typeof row?.entity_id === 'string' && /^light\.[a-z0-9_]+$/.test(row.entity_id)).map((row) => ({ id: row.entity_id, name: string(row.attributes?.friendly_name) || row.entity_id }))
            setLights([...new Map(rows.map((row) => [row.id, row])).values()])
            setSelected([])
            setStep('group', rows.length ? 'Choose lights' : 'No lights found')
            return false
          })}>Load light entities</button>
          {lights.map((light) => <label className="ca-copy" key={light.id}>
            <input type="checkbox" checked={selected.includes(light.id)} disabled={busy} onChange={(event) => setSelected((current) => event.target.checked ? [...current, light.id] : current.filter((id) => id !== light.id))} />{light.name}
          </label>)}
          <button className="chip-act" disabled={busy || selected.length === 0} onClick={() => execute('group', 'ha.flow.start', { handler: 'group', data: { group_type: 'light', name: 'All lights', entities: selected } }, (data) => {
            if (data?.type === 'create_entry' && !flowError(data)) return true
            setStep('group', 'Needs attention', flowError(data) || 'The group was not created. Try again.')
            return false
          })}>Create All lights group</button>
          {status('group')}
        </li>
        <li>
          <label className="ca-field"><span className="ca-field-k">Connector label</span>
            <input value={relayLabel} onChange={(event) => setRelayLabel(event.target.value)} maxLength={128} autoComplete="off" title="A label for this connector only" enterKeyHint="done" disabled={busy} />
          </label>
          <button className="chip-act" disabled={busy || !active || !projectId || !onRelayToken || !relayLabel.trim() || relayLabel.trim().length > 120} onClick={useInOps}>Use in Ops</button>
          {!projectId && <p className="ca-copy">Open a project first</p>}
          <button className="chip-neutral" disabled={busy || !relayLabel.trim() || !!relay} onClick={createRelay}>Connect to Ops</button>
          {status('ops')}
          {active && !mock && relay && <div className="ca-setup">
            <p className="ca-note">Copy this token now. It will not be shown again after copying, hiding, or closing this panel.</p>
            <p className="ca-copy">Relay origin: <b>{relay.origin}</b></p>
            <p className="ca-copy">Path prefix: <b>{relay.prefix}</b></p>
            <pre className="code" aria-label="Relay token">{relay.token}</pre>
            <div className="ca-account-actions">
              <button className="chip-act" onClick={copyRelay}>Copy token</button>
              <button className="chip-neutral" onClick={() => { setRelay(null); setStep('ops', 'Token hidden') }}>Hide token</button>
            </div>
            {copyError && <p className="field-err" role="alert">{copyError}</p>}
          </div>}
        </li>
      </ol>
    </section>
  )
}
