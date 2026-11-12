import { useEffect, useRef, useState } from 'react'
import { useSurfaceFrame } from '../site/SurfaceFrame.jsx'
import { deviceStatus } from '../components/LinkDeviceDrawer.jsx'
import { projectDevice } from '../useDeviceRegistry.js'
import useHomeLights from './useHomeLights.js'
import './ops-home.css'

export default function OpsHomePanel({ active }) {
  const frame = useSurfaceFrame()
  const devices = frame?.devices
  const [now, setNow] = useState(Date.now())
  const [selectedId, setSelectedId] = useState(null)
  const refreshing = useRef(false)
  useEffect(() => {
    if (!active) return undefined
    const timer = setInterval(() => setNow(Date.now()), 15000)
    return () => clearInterval(timer)
  }, [active])
  useEffect(() => {
    if (!active || devices?.mock || typeof devices?.onRefresh !== 'function') return undefined
    const timer = setInterval(async () => {
      if (document.visibilityState !== 'visible' || refreshing.current) return
      refreshing.current = true
      try { await devices.onRefresh() }
      catch { /* The registry exposes refresh errors through the frame. */ }
      finally { refreshing.current = false }
    }, 30000)
    return () => clearInterval(timer)
  }, [active, devices?.mock, devices?.onRefresh])
  const list = Array.isArray(devices?.devices) ? devices.devices.map(projectDevice) : []
  const available = list.filter((item) => deviceStatus(item, now) !== 'Revoked')
  const device = available.find((item) => item.device_id === selectedId)
    || available.find((item) => deviceStatus(item, now) === 'Online' && item.packages.some((pkg) => pkg.package === 'homeassistant' && pkg.status === 'running'))
    || available.reduce((newest, item) => !newest || (Date.parse(item.last_seen_at) || 0) > (Date.parse(newest.last_seen_at) || 0) ? item : newest, null)
    || list[0]
  const deviceId = device?.device_id ?? null
  useEffect(() => { setSelectedId(deviceId) }, [deviceId])
  const state = device ? deviceStatus(device, now) : null
  const ha = device?.packages.find((pkg) => pkg.package === 'homeassistant')
  const { lights, status, updatedAt, stale, refresh } = useHomeLights({
    deviceId: device?.device_id, onCommand: devices?.onCommand, onGetCommand: devices?.onGetCommand,
    active: !!active && !devices?.mock && state === 'Online' && ha?.status === 'running',
  })
  if (!frame) return null
  return <section className="ops-home" aria-label="Home">
    {!device ? <>
      <p>No home device linked</p>
      <button type="button" onClick={() => devices?.onToggle?.(true)}>Open Devices</button>
    </> : <>
      <header><h2>{device.label}</h2><span>{state}</span></header>
      {(state === 'Online' || lights.length > 0) && (state === 'Online' && ha?.status !== 'running' ? <p>Home Assistant is not set up</p> : <>
        <p>Home Assistant <span>{ha?.version}</span></p>
        {status === 'loading' && <p role="status">Loading lights</p>}
        {status === 'error' && <p role="status">Could not update lights</p>}
        <ul className="ops-home-lights" aria-label="Lights">
          {lights.map((light) => <li key={light.entity_id} className={`ops-home-light${stale || state !== 'Online' ? ' stale' : ''}`}>
            <b>{light.name}</b><span>{light.state === 'on' ? 'On' : 'Off'}</span>
            {light.brightness_pct !== null && <span>{light.brightness_pct}%</span>}
            {light.rgb && <span className="ops-home-colour">
              <span className="ops-home-swatch" data-testid="light-swatch" aria-label={light.kelvin !== null ? `White ${light.kelvin} K` : `Colour ${light.rgb.join(', ')}`} style={{ background: `rgb(${light.rgb.join(',')})` }} />
              {light.kelvin !== null && <span>{light.kelvin} K</span>}
            </span>}
          </li>)}
        </ul>
        <footer>{updatedAt !== null && <span>Updated {new Date(updatedAt).toLocaleTimeString()}</span>}
          <button type="button" onClick={refresh} disabled={!active || devices?.mock || state !== 'Online' || status === 'loading'}>Refresh</button>
        </footer>
      </>)}
    </>}
  </section>
}
