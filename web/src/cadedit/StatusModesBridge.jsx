/**
 * Cockpit parity: the status bar sits outside EngineSessionProvider, so it
 * reads drafting modes through cockpit:modes, requests current values with
 * cockpit:modes-request, and writes through cockpit:mode-toggle.
 */
import { useEffect, useRef } from 'react'

import { useEngineSessionContext } from './EngineSessionProvider.jsx'

export default function StatusModesBridge() {
  const { ortho, setOrtho, osnap, setOsnap } = useEngineSessionContext()
  const current = useRef({ ortho, setOrtho, osnap, setOsnap })
  current.current = { ortho, setOrtho, osnap, setOsnap }

  useEffect(() => {
    if (typeof window === 'undefined') return
    window.dispatchEvent(new CustomEvent('cockpit:modes', { detail: { live: true, ortho, osnap } }))
  }, [ortho, osnap])

  useEffect(() => {
    if (typeof window === 'undefined') return undefined
    const onRequest = () => {
      const { ortho, osnap } = current.current
      window.dispatchEvent(new CustomEvent('cockpit:modes', { detail: { live: true, ortho, osnap } }))
    }
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
    window.addEventListener('cockpit:modes-request', onRequest)
    window.addEventListener('cockpit:mode-toggle', onToggle)
    return () => {
      window.removeEventListener('cockpit:modes-request', onRequest)
      window.removeEventListener('cockpit:mode-toggle', onToggle)
      window.dispatchEvent(new CustomEvent('cockpit:modes', { detail: { live: false } }))
    }
  }, [])
  return null
}
