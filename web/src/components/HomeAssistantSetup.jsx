import { useEffect, useRef, useState } from 'react'
import { deviceError, useDeviceCommand } from '../useDeviceRegistry.js'

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

export default function HomeAssistantSetup({ deviceId, mock = false, active = true, disabled = false, onCommand, onGetCommand, onRelayToken }) {
  const [steps, setSteps] = useState({})
  const [hue, setHue] = useState(null)
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
  const relayLock = useRef(false)
  const live = useRef(active)
  live.current = active
  const { run, pending } = useDeviceCommand({ active: active && !mock, onCommand, onGetCommand })
  const busy = !!pending || relayBusy || disabled || mock

  useEffect(() => {
    live.current = active
    setSteps({})
    setHue(null)
    setRelay(null)
    setBulbs([])
    setLights([])
    setSelected([])
    setHost('')
    setHostError(null)
    setRelayBusy(false)
    relayLock.current = false
    return () => { epoch.current += 1; live.current = false }
  }, [active, deviceId, mock])

  useEffect(() => {
    if (!active || !hue) return undefined
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
      const accepted = accept ? accept(result.data) : true
      if (accepted) setStep(name, 'Done')
      return result
    } catch (error) {
      if (gen === epoch.current && live.current) setStep(name, 'Needs attention', deviceError(error))
      return null
    }
  }

  const acceptHue = (data) => {
    const error = flowError(data)
    const id = string(data?.flow_id) || hue?.id
    if (data?.type === 'create_entry' && !error) { setHue(null); return true }
    if (id && data?.type !== 'abort') {
      setHue({ id, deadline: Date.now() + 30000 })
      setStep('hue', error ? 'Needs attention' : 'Waiting for button', error)
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

  const copyRelay = async () => {
    try {
      await navigator.clipboard.writeText(relay.token)
      if (live.current) { setRelay(null); setStep('ops', 'Token copied') }
    } catch { if (live.current) setCopyError('Could not copy. Select and copy the token before hiding it.') }
  }

  const status = (name) => (
    <>
      <span className={`state ${steps[name]?.message ? 'fail' : 'sub'}`} role="status">
        <span className={`dot ${steps[name]?.state === 'Working' ? 'pulse' : ''}`} />{steps[name]?.state || 'Not started'}
      </span>
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
          {hue && <div className="ca-setup">
            <p className="ca-copy">Press the round button on top of your Hue bridge, then click Continue</p>
            <p className="ca-copy">{seconds > 0 ? `${seconds} s remaining` : 'Button window expired. Press the button again, then Continue.'}</p>
            <button className="chip-act" disabled={busy} onClick={() => execute('hue', 'ha.flow.step', { flow_id: hue.id, data: {} }, acceptHue)}>Continue</button>
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
          <button className="chip-act" disabled={busy || !relayLabel.trim() || !!relay} onClick={createRelay}>Connect to Ops</button>
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
