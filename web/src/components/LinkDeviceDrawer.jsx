import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import EscCap from './EscCap.jsx'
import HomeAssistantSetup from './HomeAssistantSetup.jsx'
import useExit from '../useExit.js'
import useEscapeOwner from '../lib/useEscapeOwner.js'
import { demoDevice, deviceError, projectAudit, projectDevice, useDeviceCommand } from '../useDeviceRegistry.js'
import './popovers.css'

const healthMessages = {
  running: 'Home Assistant is healthy',
  installed: 'Home Assistant is installed but is not running. Start it on the device, then check Health again.',
  stopped: 'Home Assistant is stopped. Start it on the device, then check Health again.',
  error: 'Home Assistant reported an error. Check it on the device, then retry Health.',
}

export function deviceStatus(device, now = Date.now()) {
  if (device.revoked_at || device.status === 'revoked') return 'Revoked'
  if (device.status === 'paused') return 'Paused'
  const seen = Date.parse(device.last_seen_at)
  return Number.isFinite(seen) && now - seen < 90000 && now >= seen ? 'Online' : 'Offline'
}

export default function LinkDeviceDrawer({
  devices, mock = false, loading = false, busy = false, error = null,
  open = false, onToggle, onEnroll, onRefresh, onCommand, onGetCommand,
  onAudit, onRevoke, onRelayToken,
  catalog = [], catalogLoading = false, catalogError = null, onLoadCatalog,
}) {
  const root = useRef(null)
  const trigger = useRef(null)
  const close = useRef(null)
  const dialog = useRef(null)
  const wasOpen = useRef(false)
  const epoch = useRef(0)
  const live = useRef(open)
  live.current = open
  const lock = useRef(false)
  const refreshing = useRef(false)
  const backgroundRefresh = useRef(null)
  const enrollmentIds = useRef(new Set())
  const [label, setLabel] = useState('')
  const [enrollment, setEnrollment] = useState(null)
  const [linked, setLinked] = useState(false)
  const [now, setNow] = useState(Date.now())
  const [localError, setLocalError] = useState(null)
  const [working, setWorking] = useState(false)
  const [confirm, setConfirm] = useState(null)
  const [activity, setActivity] = useState({})
  const [results, setResults] = useState({})
  const [healthy, setHealthy] = useState({})
  const [copied, setCopied] = useState(false)
  const { run, pending } = useDeviceCommand({ active: open && !mock, onCommand, onGetCommand })
  const pop = useExit(open)
  const list = (mock ? [demoDevice] : Array.isArray(devices) ? devices : []).map(projectDevice)
  const unavailable = busy || working || !!pending || mock
  const listFailed = error?.kind === 'list'
  const wired = mock || Array.isArray(devices)
  const catalogVersion = Array.isArray(catalog) ? catalog.find((pkg) => pkg?.package === 'homeassistant')?.version : null
  const version = !catalogLoading && !catalogError && typeof catalogVersion === 'string' && catalogVersion.trim() ? catalogVersion : null
  const expiresIn = enrollment ? Math.max(0, Math.ceil((Date.parse(enrollment.expires_at) - now) / 1000)) : 0
  backgroundRefresh.current = { onRefresh, enrolling: !!enrollment && expiresIn > 0 }

  useEscapeOwner('link-device', open, () => onToggle?.(false), { layer: 'menu', scope: root })
  useLayoutEffect(() => {
    if (open) close.current?.focus()
    else if (wasOpen.current && (document.activeElement === document.body || dialog.current?.contains(document.activeElement))) trigger.current?.focus()
    wasOpen.current = open
  }, [open])

  useEffect(() => {
    if (open && wired) void onLoadCatalog?.()
  }, [open, wired, onLoadCatalog])

  useEffect(() => {
    live.current = open
    setEnrollment(null)
    setConfirm(null)
    setLocalError(null)
    setLabel('')
    setLinked(false)
    setCopied(false)
    setWorking(false)
    setActivity({})
    setResults({})
    setHealthy({})
    lock.current = false
    if (!open) return undefined
    const tick = setInterval(() => setNow(Date.now()), 1000)
    const outside = (event) => { if (!root.current?.contains(event.target)) onToggle?.(false) }
    document.addEventListener('mousedown', outside)
    return () => {
      epoch.current += 1
      live.current = false
      clearInterval(tick)
      document.removeEventListener('mousedown', outside)
    }
  }, [open, mock, onToggle])

  useEffect(() => {
    if (!open || !wired || mock) return undefined
    const refresh = async () => {
      if (refreshing.current || backgroundRefresh.current.enrolling || lock.current) return
      refreshing.current = true
      try { await backgroundRefresh.current.onRefresh?.() }
      catch { /* Background list errors are handled by the registry. */ }
      finally { refreshing.current = false }
    }
    void refresh()
    const timer = setInterval(refresh, 15000)
    return () => clearInterval(timer)
  }, [open, wired, mock])

  useEffect(() => {
    if (!open || !enrollment || !expiresIn || mock) return undefined
    let cancelled = false
    let refreshing = false
    const timer = setInterval(async () => {
      if (refreshing) return
      refreshing = true
      try {
        const records = await onRefresh?.()
        if (!cancelled && Array.isArray(records) && records.some((row) => !enrollmentIds.current.has(row.device_id))) {
          setLinked(true)
          setEnrollment(null)
        }
      } catch (e) { if (!cancelled) setLocalError({ action: 'enroll', message: deviceError(e) }) }
      finally { refreshing = false }
    }, 3000)
    return () => { cancelled = true; clearInterval(timer) }
  }, [open, enrollment, !!expiresIn, mock, onRefresh])

  useEffect(() => {
    if (enrollment && devices?.some((row) => !enrollmentIds.current.has(row.device_id))) {
      setLinked(true)
      setEnrollment(null)
    }
  }, [devices, enrollment])

  if (!Array.isArray(devices) && !mock) return null

  const act = async (task, action = 'enroll') => {
    if (unavailable || lock.current || !open) return
    const gen = epoch.current
    lock.current = true
    setWorking(true)
    setLocalError(null)
    try { await task(() => live.current && epoch.current === gen) }
    catch (e) { if (live.current && epoch.current === gen) setLocalError({ action, message: deviceError(e) }) }
    finally { if (epoch.current === gen) { setWorking(false); lock.current = false } }
  }

  const enroll = () => act(async (valid) => {
    if (loading || listFailed) return
    enrollmentIds.current = new Set(list.map((device) => device.device_id))
    const result = await onEnroll?.(label.trim())
    if (!valid()) return
    if (!result?.install_command || !Number.isFinite(Date.parse(result.expires_at))) throw new Error('The device link could not be created. Try again.')
    setEnrollment({ install_command: result.install_command, expires_at: result.expires_at })
    setLabel('')
    setLinked(false)
    setCopied(false)
    setNow(Date.now())
  })

  const command = (device, op, args) => act(async (valid) => {
    setResults((current) => ({ ...current, [device.device_id]: { message: op === 'pkg.install' ? 'Installing Home Assistant' : 'Checking health', pending: true } }))
    try {
      const result = await run(device.device_id, op, args)
      if (!valid() || !result) return
      const healthStatus = result.data?.status
      const isHealthy = op === 'pkg.health' && healthStatus === 'running'
      if (op === 'pkg.health') setHealthy((current) => ({ ...current, [device.device_id]: isHealthy }))
      const healthMessage = Object.hasOwn(healthMessages, healthStatus) ? healthMessages[healthStatus] : 'Home Assistant needs attention. Check it on the device, then retry Health.'
      setResults((current) => ({ ...current, [device.device_id]: { message: op === 'pkg.install' ? 'Home Assistant installed' : healthMessage, failed: op === 'pkg.health' && !isHealthy } }))
      await onRefresh?.()
    } catch (e) {
      if (valid()) {
        if (op === 'pkg.health') setHealthy((current) => ({ ...current, [device.device_id]: false }))
        setResults((current) => ({ ...current, [device.device_id]: { message: deviceError(e), failed: true } }))
      }
    }
  })

  return (
    <span className="claude-acct link-svc" ref={root}>
      <button className="link-svc-trigger" ref={trigger} aria-haspopup="dialog" aria-expanded={open} onClick={() => onToggle?.(!open)}>
        <span className="ca-k">Devices</span><span className="ca-state">{loading && !list.length ? 'Checking' : listFailed ? 'Unavailable' : list.length}</span>
      </button>
      {pop.shown && <div className={`claude-pop vh-pop${pop.exiting ? ' exit' : ''}`} ref={dialog} role="dialog" aria-label="Devices" inert={!open ? '' : undefined} aria-hidden={!open || undefined}>
        <div className="ca-head"><span>Link a device</span><EscCap ref={close} onClick={() => onToggle?.(false)} label="Close devices panel" /></div>
        {mock && <p className="ca-note"><span className="dot square" />Demo devices. Actions are unavailable; no device is contacted.</p>}
        {loading && !list.length && <div className="skeleton-stack" role="status" aria-label="Loading devices"><div className="skeleton-row" /></div>}
        {/* Mutations are caught beside their action; registry errors outlive a drawer session. */}
        {listFailed && <div className="field-err" role="alert"><span className="dot red" />{error.message}
          <button className="chip-act" disabled={loading || mock} onClick={error.retry || onRefresh}>Retry</button>
        </div>}
        {!list.length && !loading && !listFailed && <p className="ca-copy">No devices yet. Link a device to manage Home Assistant here.</p>}
        {catalogLoading && <p className="ca-note" role="status">Loading package catalog. Install will be available when it loads.</p>}
        {catalogError && <div className="field-err" role="alert"><span className="dot red" />Package catalog unavailable. {catalogError}
          <button className="chip-act" disabled={catalogLoading || mock} onClick={onLoadCatalog}>Retry catalog</button>
        </div>}
        {!catalogLoading && !catalogError && !version && <p className="ca-note">Home Assistant is unavailable in the package catalog. Close and reopen this panel to retry.</p>}
        <div className="ca-account-list">
          {list.map((device) => {
            const state = mock ? 'Online' : deviceStatus(device, now)
            const ha = device.packages.find((pkg) => pkg.package === 'homeassistant')
            const disabled = unavailable || state !== 'Online'
            const result = results[device.device_id]
            const isHealthy = state === 'Online' && (healthy[device.device_id] ?? (ha?.status === 'running'))
            return <div className="ca-account" key={device.device_id}>
              <div className="ca-account-main"><b>{device.label}</b><span className={`state ${state === 'Online' ? 'done' : state === 'Revoked' ? 'fail' : 'sub'}`}><span className={`dot${state === 'Paused' || state === 'Offline' ? ' square' : ''}`} />{state}</span></div>
              <div className="ca-account-meta"><span>{device.hostname}</span><span>Agent {device.agent_version}</span></div>
              {device.packages.map((pkg) => <p className="ca-copy" key={pkg.package}>{pkg.package} {pkg.version} <span className={`state ${pkg.status === 'error' ? 'fail' : 'sub'}`}>{pkg.status}</span></p>)}
              <div className="ca-account-actions ca-account-meta">
                <button className="chip-act" disabled={disabled || !version} onClick={() => command(device, 'pkg.install', { package: 'homeassistant', version })}>Install Home Assistant</button>
                <button className="chip-neutral" disabled={disabled || !ha} onClick={() => command(device, 'pkg.health', { package: 'homeassistant' })}>Health</button>
                <button className="chip-neutral" disabled={unavailable} onClick={() => act(async (valid) => {
                  const events = await onAudit?.(device.device_id)
                  if (!valid()) return
                  if (!Array.isArray(events)) throw new Error('Device activity could not be loaded. Try again.')
                  setActivity((current) => ({ ...current, [device.device_id]: projectAudit(events) }))
                }, `activity:${device.device_id}`)}>Activity</button>
              </div>
              {open && result && <p className={result.failed ? 'field-err' : 'ca-copy'} role={result.failed ? 'alert' : 'status'}><span className={`dot${result.pending ? ' pulse' : ''}`} />{result.message}</p>}
              {open && localError?.action === `activity:${device.device_id}` && <p className="field-err" role="alert"><span className="dot red" />{localError.message}</p>}
              {state !== 'Revoked' && <div className="ca-account-actions ca-account-meta">
                {confirm !== device.device_id ? <button className="chip-danger" disabled={unavailable} onClick={() => setConfirm(device.device_id)}>Revoke</button> : <>
                  <span className="confirm-q">Revoke {device.label}?</span>
                  <button className="chip-danger-confirm" disabled={unavailable} onClick={() => act(async (valid) => {
                    const response = await onRevoke?.(device.device_id)
                    if (!valid()) return
                    if (!response?.revoked_at) throw new Error('The device was not revoked. Try again.')
                    setConfirm(null)
                  }, `revoke:${device.device_id}`)}>Confirm revoke</button>
                  <button className="chip-neutral" disabled={unavailable} onClick={() => setConfirm(null)}>Keep</button>
                </>}
              </div>}
              {open && localError?.action === `revoke:${device.device_id}` && <p className="field-err" role="alert"><span className="dot red" />{localError.message}</p>}
              {activity[device.device_id] && <section className="ca-setup" aria-label={`Activity for ${device.label}`}>
                <p className="ca-note">Newest {activity[device.device_id].length} events (up to 50). Times are UTC.</p>
                {!activity[device.device_id].length && <p className="ca-copy">No activity yet. Device actions appear here.</p>}
                {activity[device.device_id].map((event, index) => <p className="ca-copy" key={index}><time>{event.at}</time> {event.actor} {event.op} {event.target} <span className="state sub">{event.status}</span></p>)}
              </section>}
              {isHealthy && open && <HomeAssistantSetup deviceId={device.device_id} projectId={device.project_id} mock={mock} active={open} disabled={busy || working} onCommand={onCommand} onGetCommand={onGetCommand} onRelayToken={onRelayToken} />}
            </div>
          })}
        </div>
        {open && <div className="ca-setup">
          <label className="ca-field"><span className="ca-field-k">Device label</span>
            <input value={label} onChange={(event) => setLabel(event.target.value)} maxLength={128} autoComplete="off" title="A label for this device only" enterKeyHint="done" disabled={unavailable} />
          </label>
          <button className="btn primary ca-act" disabled={unavailable || loading || listFailed || !label.trim() || !!enrollment && expiresIn > 0} onClick={enroll}>Link a device</button>
          {localError?.action === 'enroll' && <p className="field-err" role="alert"><span className="dot red" />{localError.message}</p>}
          {enrollment && !mock && (expiresIn > 0 ? <>
            <p className="ca-copy">Run this one-use command on your device. Expires in {expiresIn} s.</p>
            <pre className="code" aria-label="Install command">{enrollment.install_command}</pre>
            <button className="chip-act" onClick={async () => {
              try { await navigator.clipboard.writeText(enrollment.install_command); if (live.current) setCopied(true) }
              catch { if (live.current) setLocalError({ action: 'enroll', message: 'Could not copy. Select and copy the command instead.' }) }
            }}>Copy</button>
            {copied && <p className="ca-copy" role="status">Command copied</p>}
          </> : <p className="field-err" role="status">Link expired. Enter a label and link a device again.</p>)}
          {linked && <p className="ca-copy" role="status">Linked</p>}
          <p className="ca-note">Device and Home Assistant credentials stay on your device. No secret paste is needed.</p>
        </div>}
      </div>}
    </span>
  )
}
