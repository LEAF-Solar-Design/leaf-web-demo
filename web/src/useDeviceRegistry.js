import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { authHeaders, config, noteUnauthorized } from './api.js'
import { humanizeError } from './errorHumanize.js'

const text = (value) => typeof value === 'string' ? value : ''
const array = (value) => Array.isArray(value) ? value : []

// Closed projections: neither transport records nor opaque HA data are spread
// into a component's props. Credentials have only their explicit one-use UI.
export function projectDevice(row) {
  return {
    device_id: text(row?.device_id), project_id: text(row?.project_id),
    label: text(row?.label), hostname: text(row?.hostname),
    platform: { os: text(row?.platform?.os), os_version: text(row?.platform?.os_version), arch: text(row?.platform?.arch) },
    agent_version: text(row?.agent_version), status: text(row?.status),
    enrolled_at: text(row?.enrolled_at), last_seen_at: text(row?.last_seen_at) || null,
    revoked_at: text(row?.revoked_at) || null,
    packages: array(row?.packages).slice(0, 64).map((pkg) => ({
      package: text(pkg?.package), version: text(pkg?.version),
      status: text(pkg?.status), last_health_at: text(pkg?.last_health_at) || null,
    })),
  }
}

export function projectAudit(rows) {
  return array(rows).map((row) => ({
    at: text(row?.at), actor: text(row?.actor), op: text(row?.op),
    target: text(row?.target), status: text(row?.status),
  })).sort((a, b) => b.at.localeCompare(a.at)).slice(0, 50)
}

// The drawer needs only these catalog fields; never retain opaque extra data.
const projectCatalog = (rows) => rows.map((row) => ({
  package: text(row?.package), display_name: text(row?.display_name), version: text(row?.version),
}))
const demoCatalog = [{ package: 'homeassistant', display_name: 'Home Assistant', version: '2026.10.4' }]

const errorWords = {
  install_failed: 'Home Assistant could not be installed. Try Install again.',
  paused: 'This device is paused. Resume the agent on the device, then retry.',
  device_revoked: 'This device was revoked. Link it again.',
  device_offline: 'This device is offline. Check its connection and retry.',
  version_unknown: 'This version is not in the device catalog. Refresh the agent and retry.',
  package_unknown: 'This package is not in the device catalog.',
  command_expired: 'The command expired before it ran. Try again.',
  timeout: 'The device took too long to respond. Try again.',
  unsupported_op: 'This agent does not support that action. Update the agent and retry.',
  upstream_error: 'Home Assistant could not be reached on the device. Check Health and retry.',
  path_denied: 'This action is not allowed by the device catalog.',
  invalid_args: 'The device could not accept these settings. Check them and retry.',
  not_linked: 'The Hue button has not been detected yet. Press it and try Continue again.',
  forbidden: 'Only a project owner can link devices. Ask your workspace owner to make you an owner of this project.',
  unauthorized: 'Your session expired. Sign in again.',
  not_found: 'This device is no longer available in this project.',
  rate_limited: 'Too many requests. Try again in a minute.',
  service_unavailable: 'Device management is temporarily unavailable. Try again.',
}
const knownErrorCode = (code) => typeof code === 'string' && Object.hasOwn(errorWords, code) ? code : null

export class DeviceRequestError extends Error {
  constructor(status, code = null) {
    super('Device request failed.')
    this.name = 'DeviceRequestError'
    this.status = status
    this.code = knownErrorCode(code)
  }
}

export function deviceError(error) {
  const code = error?.body?.error?.code || error?.error?.code || error?.code
  // Never render an arbitrary server error message, which could carry secrets.
  if (error?.status === 401) return errorWords.unauthorized
  if (error?.status === 403) return errorWords.forbidden
  if (error?.status === 429) return errorWords.rate_limited
  // Specific device guidance takes precedence over other HTTP status fallbacks.
  const knownCode = knownErrorCode(code)
  if (knownCode) return errorWords[knownCode]
  if (error?.status === 503 || error?.status === 0) return errorWords.service_unavailable
  if (typeof error?.status === 'number' || code) return "That didn't work. Try again."
  return humanizeError(error)
}

export const demoDevice = projectDevice({
  device_id: 'demo-device', project_id: 'demo', label: 'Living room Mac mini',
  hostname: 'mac-mini.local', platform: { os: 'darwin', os_version: '15', arch: 'arm64' },
  agent_version: 'Demo', status: 'online', enrolled_at: '2026-11-02T10:00:00Z',
  last_seen_at: new Date().toISOString(), revoked_at: null,
  packages: [{ package: 'homeassistant', version: 'Demo', status: 'running', last_health_at: '2026-11-02T10:00:00Z' }],
})

export default function useDeviceRegistry({ mock = false, projectId } = {}) {
  const [devices, setDevices] = useState([])
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [listError, setListError] = useState(null)
  const [catalog, setCatalog] = useState([])
  const [catalogLoading, setCatalogLoading] = useState(false)
  const [catalogError, setCatalogError] = useState(null)
  const scope = `${mock ? 'mock' : 'live'}:${projectId || ''}`
  const current = useRef(scope)
  current.current = scope
  const lifetime = useRef(0)
  const generation = useRef(0)
  const catalogGeneration = useRef(0)
  const requests = useRef(new Set())
  const [loadedScope, setLoadedScope] = useState(scope)
  const available = !mock && !!projectId

  const request = useCallback(async (path, method = 'GET', body) => {
    if (!available || current.current !== scope) return null
    const life = lifetime.current
    const controller = new AbortController()
    requests.current.add(controller)
    const headers = { ...authHeaders(), ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) }
    try {
      let response
      try {
        response = await fetch(`${config.apiBase}${path}`, {
          method, headers, signal: controller.signal, cache: 'no-store',
          ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        })
      } catch {
        if (current.current !== scope || life !== lifetime.current) return null
        throw new DeviceRequestError(0)
      }
      response = noteUnauthorized(response, path, headers.Authorization)
      if (current.current !== scope || life !== lifetime.current) return null
      let result = null
      try { result = response.status === 204 ? null : await response.json() }
      catch (e) { if (response.ok) throw e }
      if (current.current !== scope || life !== lifetime.current) return null
      if (!response.ok) {
        throw new DeviceRequestError(response.status, knownErrorCode(result?.error?.code) || knownErrorCode(result?.detail))
      }
      return result
    } finally { requests.current.delete(controller) }
  }, [available, scope])

  const refresh = useCallback(async () => {
    if (!available || current.current !== scope) return []
    const gen = ++generation.current
    setLoading(true)
    try {
      const result = await request(`/api/devices?project_id=${encodeURIComponent(projectId)}`)
      if (!result || current.current !== scope || gen !== generation.current) return null
      if (!Array.isArray(result.devices)) throw new Error('Devices could not be loaded. Try again.')
      const list = array(result.devices).map(projectDevice)
      setDevices(list)
      setLoadedScope(scope)
      setListError(null)
      return list
    } catch (e) {
      if (current.current === scope && gen === generation.current) setListError(deviceError(e))
      return null
    } finally {
      if (current.current === scope && gen === generation.current) setLoading(false)
    }
  }, [available, scope, projectId, request])

  // Lazy: opening the drawer requests the catalog once, separately from the
  // three-second device enrollment poll. A failed read cannot authorize install
  // from a cached or already-installed version.
  const loadCatalog = useCallback(async () => {
    if (mock) return demoCatalog
    if (!available || current.current !== scope) return null
    const gen = ++catalogGeneration.current
    setCatalogLoading(true)
    setCatalogError(null)
    setCatalog([])
    try {
      const result = await request('/api/devices/packages')
      if (!result || current.current !== scope || gen !== catalogGeneration.current) return null
      if (!Array.isArray(result.packages)) throw new Error('The package catalog could not be loaded. Try again.')
      const packages = projectCatalog(result.packages)
      if (packages.some((pkg) => !pkg.package || !pkg.version.trim())) throw new Error('The package catalog could not be loaded. Try again.')
      setCatalog(packages)
      return packages
    } catch (e) {
      if (current.current === scope && gen === catalogGeneration.current) setCatalogError(deviceError(e))
      return null
    } finally {
      if (current.current === scope && gen === catalogGeneration.current) setCatalogLoading(false)
    }
  }, [mock, available, scope, request])

  // Clear before a newly mounted drawer's passive effect starts its lazy read.
  useLayoutEffect(() => {
    setCatalog([])
    setCatalogLoading(false)
    setCatalogError(null)
  }, [scope])

  useEffect(() => {
    setDevices([])
    setLoadedScope(scope)
    setError(null)
    setListError(null)
    setBusy(false)
    setLoading(false)
    void refresh()
    const activeRequests = requests.current
    return () => {
      lifetime.current += 1
      generation.current += 1
      catalogGeneration.current += 1
      activeRequests.forEach((controller) => controller.abort())
      activeRequests.clear()
    }
  }, [scope, refresh])

  const mutate = useCallback(async (path, body, relist = false) => {
    if (!available || current.current !== scope) return null
    const life = lifetime.current
    setBusy(true)
    setError(null)
    try {
      const result = await request(path, 'POST', body)
      if (current.current !== scope || life !== lifetime.current) return null
      if (relist) await refresh()
      return result
    } catch (e) {
      if (current.current === scope && life === lifetime.current) setError(deviceError(e))
      throw e
    } finally { if (current.current === scope && life === lifetime.current) setBusy(false) }
  }, [available, scope, request, refresh])

  const enroll = useCallback(async (label) => {
    const result = await mutate('/api/devices/enrollments', { project_id: projectId, label })
    return result ? { enrollment_id: text(result.enrollment_id), install_command: text(result.install_command), expires_at: text(result.expires_at) } : null
  }, [mutate, projectId])
  const command = useCallback((id, op, args) => mutate(`/api/devices/${encodeURIComponent(id)}/commands`, { op, args }), [mutate])
  const getCommand = useCallback((id, commandId) => request(`/api/devices/${encodeURIComponent(id)}/commands/${encodeURIComponent(commandId)}`), [request])
  const audit = useCallback(async (id) => {
    const result = await request(`/api/devices/${encodeURIComponent(id)}/audit?limit=50`)
    return result ? projectAudit(result.events) : null
  }, [request])
  const revoke = useCallback((id) => mutate(`/api/devices/${encodeURIComponent(id)}/revoke`, undefined, true), [mutate])
  const relayToken = useCallback(async (id, label) => {
    const result = await mutate(`/api/devices/${encodeURIComponent(id)}/relay-tokens`, { package: 'homeassistant', label })
    return result ? { relay_token: text(result.relay_token), relay_origin: text(result.relay_origin), path_prefix: text(result.path_prefix) } : null
  }, [mutate])

  return {
    devices: available && loadedScope === scope ? devices : [],
    loading: available && loading, busy: available && busy,
    error: available && loadedScope === scope ? (listError ? { kind: 'list', message: listError, retry: refresh } : error) : null,
    catalog: mock ? demoCatalog : available && loadedScope === scope ? catalog : [],
    catalogLoading: available && loadedScope === scope && catalogLoading,
    catalogError: available && loadedScope === scope ? catalogError : null,
    refresh, loadCatalog, enroll, command, getCommand, audit, revoke, relayToken,
  }
}

// Command queue transport is shared by the drawer and each HA panel. Closing,
// switching projects, or unmounting invalidates even a late mint/poll response.
export function useDeviceCommand({ active = true, onCommand, onGetCommand }) {
  const [pending, setPending] = useState(null)
  const running = useRef(false)
  const generation = useRef(0)
  const live = useRef(active)
  live.current = active
  const sleeper = useRef(null)
  useEffect(() => {
    live.current = active
    setPending(null)
    running.current = false
    return () => {
      generation.current += 1
      live.current = false
      if (sleeper.current) { clearTimeout(sleeper.current.timer); sleeper.current.resolve() }
    }
  }, [active, onCommand, onGetCommand])

  const run = async (deviceId, op, args) => {
    if (!live.current || running.current || !onCommand || !onGetCommand) return null
    const gen = generation.current
    const valid = () => live.current && gen === generation.current
    running.current = true
    setPending(op)
    try {
      const queued = await onCommand(deviceId, op, args)
      if (!valid()) return null
      if (!queued?.command_id) throw new Error('The device command could not be started. Try again.')
      for (let count = 0; count < 600 && valid(); count += 1) {
        const response = await onGetCommand(deviceId, queued.command_id)
        if (!valid()) return null
        if (response?.result) {
          const result = response.result
          if (result.status !== 'ok' || result.error) throw { error: result.error || { code: 'invalid_result' } }
          return result
        }
        await new Promise((resolve) => { sleeper.current = { timer: setTimeout(resolve, 1000), resolve } })
        sleeper.current = null
      }
      if (valid()) throw { code: 'timeout' }
      return null
    } finally {
      if (valid()) { running.current = false; setPending(null) }
    }
  }
  return { run, pending }
}
