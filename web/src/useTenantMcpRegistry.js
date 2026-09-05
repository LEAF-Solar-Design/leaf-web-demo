import { useCallback, useEffect, useRef, useState } from 'react'
import {
  connectTenantMcpServer,
  healthTenantMcpServer,
  listTenantMcpServers,
  registerTenantMcpServer,
  unlinkTenantMcpServer,
} from './api.js'

// Transport for LinkServiceDrawer.jsx (standardization slice 8c), shared by
// both scenes (App.jsx console, site/ToolCast.jsx stage) so the registry has
// ONE fetch/mutate path rather than two copies that can drift. Mirrors the
// generation-guard discipline of createPlatformTrustController.js (a stale
// in-flight read must never clobber a newer one) at hook scale, since this
// registry has exactly one resource, not four.
//
// `mock` renders the drawer inert (no network at all): the registry has no
// mock backend, the same posture ClaudeAccountPanel takes for the Claude
// grant (`if (mock) return null`).
export default function useTenantMcpRegistry({ mock = false } = {}) {
  const [servers, setServers] = useState([])
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const generation = useRef(0)

  const refresh = useCallback(async () => {
    if (mock) { setServers([]); setLoading(false); return [] }
    const gen = ++generation.current
    setLoading(true)
    setError(null)
    const list = await listTenantMcpServers()
    if (gen !== generation.current) return list
    setServers(list)
    setLoading(false)
    return list
  }, [mock])

  useEffect(() => {
    if (!mock) refresh()
  }, [mock, refresh])

  const register = useCallback(async (url, label) => {
    if (mock) return null
    setBusy(true)
    setError(null)
    try {
      const record = await registerTenantMcpServer(url, label)
      await refresh()
      return record
    } catch (e) {
      setError(String(e?.body?.error?.message || e?.message || e))
      throw e
    } finally {
      setBusy(false)
    }
  }, [mock, refresh])

  // Connect and health each mint their OWN error, keyed by server id, rather
  // than the shared `error` field: a failed health ping on one row must never
  // blank out a registration error on another, or vice-versa.
  const connect = useCallback(async (id) => {
    if (mock) return null
    setBusy(true)
    try {
      const result = await connectTenantMcpServer(id)
      await refresh()
      return result
    } finally {
      setBusy(false)
    }
  }, [mock, refresh])

  const health = useCallback(async (id) => {
    if (mock) return null
    return healthTenantMcpServer(id)
  }, [mock])

  const unlink = useCallback(async (id) => {
    if (mock) return null
    setBusy(true)
    try {
      const result = await unlinkTenantMcpServer(id)
      await refresh()
      return result
    } finally {
      setBusy(false)
    }
  }, [mock, refresh])

  return { servers, loading, busy, error, refresh, register, connect, health, unlink }
}
