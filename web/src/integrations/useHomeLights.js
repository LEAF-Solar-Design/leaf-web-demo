import { useCallback, useEffect, useRef, useState } from 'react'

const interval = 15000
const byte = (value) => Number.isInteger(value) && value >= 0 && value <= 255

export function kelvinToRgb(kelvin) {
  if (!Number.isFinite(kelvin) || kelvin < 1000 || kelvin > 12000) return null
  const temperature = kelvin / 100
  const red = temperature <= 66 ? 255 : 329.698727446 * Math.pow(temperature - 60, -0.1332047592)
  const green = temperature <= 66 ? 99.4708025861 * Math.log(temperature) - 161.1195681661 : 288.1221695283 * Math.pow(temperature - 60, -0.0755148492)
  const blue = temperature >= 66 ? 255 : temperature <= 19 ? 0 : 138.5177312231 * Math.log(temperature - 10) - 305.0447927307
  return [red, green, blue].map((value) => Math.round(Math.max(0, Math.min(255, value))))
}

function projectLights(rows) {
  const lights = new Map()
  for (const row of rows) {
    if (typeof row?.entity_id !== 'string' || !/^light\.[a-z0-9_]+$/.test(row.entity_id) || !['on', 'off'].includes(row.state)) continue
    const attributes = row.attributes || {}
    const colour = [
      Array.isArray(attributes.rgb_color) && attributes.rgb_color.length === 3 ? attributes.rgb_color : null,
      attributes.rgbw_color,
      attributes.rgbww_color,
    ].find((value) => Array.isArray(value) && value.length >= 3 && [0, 1, 2].every((index) => byte(value[index])))
    const kelvin = !colour && Number.isFinite(attributes.color_temp_kelvin) && attributes.color_temp_kelvin >= 1000 && attributes.color_temp_kelvin <= 12000 ? Math.round(attributes.color_temp_kelvin) : null
    lights.set(row.entity_id, {
      entity_id: row.entity_id,
      name: typeof attributes.friendly_name === 'string' && attributes.friendly_name ? attributes.friendly_name : row.entity_id,
      state: row.state,
      brightness_pct: byte(attributes.brightness) ? Math.round(attributes.brightness * 100 / 255) : null,
      rgb: colour ? colour.slice(0, 3) : kelvin !== null ? kelvinToRgb(attributes.color_temp_kelvin) : null,
      kelvin,
      color_mode: typeof attributes.color_mode === 'string' && attributes.color_mode.length <= 32 ? attributes.color_mode : null,
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
