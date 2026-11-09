import { useCallback, useEffect, useRef, useState } from 'react'

const interval = 15000
const byte = (value) => Number.isInteger(value) && value >= 0 && value <= 255

function projectLights(rows) {
  const lights = new Map()
  for (const row of rows) {
    if (typeof row?.entity_id !== 'string' || !/^light\.[a-z0-9_]+$/.test(row.entity_id) || !['on', 'off'].includes(row.state)) continue
    const attributes = row.attributes || {}
    lights.set(row.entity_id, {
      entity_id: row.entity_id,
      name: typeof attributes.friendly_name === 'string' && attributes.friendly_name ? attributes.friendly_name : row.entity_id,
      state: row.state,
      brightness_pct: byte(attributes.brightness) ? Math.round(attributes.brightness * 100 / 255) : null,
      rgb: Array.isArray(attributes.rgb_color) && attributes.rgb_color.length === 3 && attributes.rgb_color.every(byte) ? [...attributes.rgb_color] : null,
    })
    if (lights.size === 100) break
  }
  return [...lights.values()]
}

export default function useHomeLights({ deviceId, onCommand, onGetCommand, active }) {
  const [snapshot, setSnapshot] = useState({ lights: [], status: 'idle', updatedAt: null })
  const [visible, setVisible] = useState(() => document.visibilityState === 'visible')
  const [now, setNow] = useState(Date.now())
  const inFlight = useRef(false)
  const kick = useRef(null)
  const refresh = useCallback(() => kick.current?.(), [])

  useEffect(() => {
    const change = () => setVisible(document.visibilityState === 'visible')
    document.addEventListener('visibilitychange', change)
    return () => document.removeEventListener('visibilitychange', change)
  }, [])

  useEffect(() => {
    setSnapshot({ lights: [], status: 'idle', updatedAt: null })
  }, [deviceId, onCommand, onGetCommand])

  useEffect(() => {
    if (snapshot.updatedAt === null) return undefined
    setNow(Date.now())
    const timer = setTimeout(() => setNow(Date.now()), Math.max(0, snapshot.updatedAt + 60001 - Date.now()))
    return () => clearTimeout(timer)
  }, [snapshot.updatedAt])

  useEffect(() => {
    if (!active || !visible || !deviceId || !onCommand || !onGetCommand) return undefined
    let cancelled = false
    let timer
    let sleeper
    let delay = interval
    const valid = () => !cancelled && document.visibilityState === 'visible'
    const read = async () => {
      if (!valid() || inFlight.current) return
      clearTimeout(timer)
      inFlight.current = true
      setSnapshot((current) => ({ ...current, status: 'loading' }))
      try {
        const queued = await onCommand(deviceId, 'http.proxy', { package: 'homeassistant', method: 'GET', path: '/api/states' })
        if (!valid()) return
        if (!queued?.command_id) throw new Error('Invalid command')
        for (let count = 0; count < 600 && valid(); count += 1) {
          const response = await onGetCommand(deviceId, queued.command_id)
          if (!valid()) return
          if (response?.result) {
            const result = response.result
            if (result.status !== 'ok' || result.error || result.data?.status !== 200 || !Array.isArray(result.data.body_json)) throw new Error('Invalid result')
            const updatedAt = Date.now()
            setSnapshot({ lights: projectLights(result.data.body_json), status: 'ready', updatedAt })
            delay = interval
            return
          }
          await new Promise((resolve) => { sleeper = { timer: setTimeout(resolve, 1000), resolve } })
          sleeper = null
        }
        if (valid()) throw new Error('Command timed out')
      } catch {
        if (valid()) {
          delay = Math.min(delay * 2, 120000)
          setSnapshot((current) => ({ ...current, status: 'error' }))
        }
      } finally {
        inFlight.current = false
        if (valid()) timer = setTimeout(read, delay)
        else if (cancelled) kick.current?.()
      }
    }
    kick.current = read
    void read()
    return () => {
      cancelled = true
      kick.current = null
      clearTimeout(timer)
      if (sleeper) { clearTimeout(sleeper.timer); sleeper.resolve() }
    }
  }, [active, visible, deviceId, onCommand, onGetCommand])

  return { ...snapshot, stale: snapshot.updatedAt !== null && Math.max(now, Date.now()) - snapshot.updatedAt > 60000, refresh }
}

export { useHomeLights }
