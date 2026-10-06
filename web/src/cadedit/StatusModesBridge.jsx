/**
 * Cockpit parity: the status bar sits outside EngineSessionProvider, so it
 * reads drafting modes through cockpit:modes, requests current values with
 * cockpit:modes-request, and writes through cockpit:mode-toggle (the ORTHO
 * and OSNAP masters) and cockpit:osnap-mode-set (B1b: one object snap mode,
 * an absolute { kind, enabled }, validated here before the provider sees it).
 */
import { useEffect, useRef } from 'react'

import { useEngineSessionContext } from './EngineSessionProvider.jsx'
import { snapModeBit } from './snapModes.js'

const publish = ({ ortho, osnap, snapModes, snapLimited }) => {
  window.dispatchEvent(new CustomEvent('cockpit:modes', { detail: { live: true, ortho, osnap, snapModes, snapLimited } }))
}

export default function StatusModesBridge() {
  const { ortho, setOrtho, osnap, setOsnap, snapModes, setSnapMode, snapLimited } = useEngineSessionContext()
  const current = useRef({ ortho, setOrtho, osnap, setOsnap, snapModes, setSnapMode, snapLimited })
  current.current = { ortho, setOrtho, osnap, setOsnap, snapModes, setSnapMode, snapLimited }

  useEffect(() => {
    if (typeof window === 'undefined') return
    publish({ ortho, osnap, snapModes, snapLimited })
  }, [ortho, osnap, snapModes, snapLimited])

  useEffect(() => {
    if (typeof window === 'undefined') return undefined
    const onRequest = () => publish(current.current)
    const onToggle = ({ detail }) => {
      if (!detail || typeof detail !== 'object') return
      const modes = current.current
      if (detail.id === 'ortho') {
        const next = !modes.ortho
        modes.ortho = next
        modes.setOrtho(next)
      } else if (detail.id === 'osnap') {
        const next = !modes.osnap
        modes.osnap = next
        modes.setOsnap(next)
      }
    }
    // Fails closed: anything but a known kind and a boolean is ignored.
    const onModeSet = ({ detail }) => {
      if (!detail || typeof detail !== 'object') return
      const { kind, enabled } = detail
      if (typeof kind !== 'string' || !snapModeBit(kind) || typeof enabled !== 'boolean') return
      current.current.setSnapMode(kind, enabled)
    }
    window.addEventListener('cockpit:modes-request', onRequest)
    window.addEventListener('cockpit:mode-toggle', onToggle)
    window.addEventListener('cockpit:osnap-mode-set', onModeSet)
    return () => {
      window.removeEventListener('cockpit:modes-request', onRequest)
      window.removeEventListener('cockpit:mode-toggle', onToggle)
      window.removeEventListener('cockpit:osnap-mode-set', onModeSet)
      window.dispatchEvent(new CustomEvent('cockpit:modes', { detail: { live: false } }))
    }
  }, [])
  return null
}
