import { useEffect, useRef, useState } from 'react'
import { serviceReauth } from '../auth.js'
import { serviceSession } from '../lib/serviceAuthSession.js'

export default function ServiceSessionRenew({ session = serviceSession(), now = () => Date.now(), onRenew = serviceReauth, windowMs = 5 * 60 * 1000 }) {
  const [state, setState] = useState(() => ({ snapshot: session.snapshot() }))
  const [renewing, setRenewing] = useState(false)
  const clicked = useRef(false)

  useEffect(() => {
    const refresh = () => setState({ snapshot: session.snapshot() })
    const unsubscribe = session.subscribe(refresh)
    refresh()
    const interval = setInterval(refresh, 15000)
    return () => {
      unsubscribe()
      clearInterval(interval)
    }
  }, [session])

  const { snapshot } = state
  const remaining = snapshot.expiresAt - now()
  if (snapshot.mode !== true || !snapshot.identity || !(remaining > 0 && remaining <= windowMs)) return null

  const renew = () => {
    if (clicked.current) return
    clicked.current = true
    setRenewing(true)
    onRenew()
  }

  return (
    <button type="button" className="chip-act tc-service-renew" onClick={renew} disabled={renewing}>Renew session</button>
  )
}
