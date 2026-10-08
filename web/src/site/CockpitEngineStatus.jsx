import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'

import { useEngineSessionOptional } from '../cadedit/EngineSessionProvider.jsx'
import LiveRegion from '../components/LiveRegion.jsx'

// Consume the workspace's one session; the command dock lives outside its
// provider, so App supplies a slot there rather than reading context itself.
export default function CockpitEngineStatus({ importOpen = false, slotId = null }) {
  const context = useEngineSessionOptional()
  const status = context?.session.status || ''
  const [slot, setSlot] = useState(null)
  const [message, setMessage] = useState('')
  useEffect(() => {
    setSlot(slotId ? document.getElementById(slotId) : null)
  })
  // Let the region exist before inserting its first announcement.
  useEffect(() => {
    setMessage(importOpen || (slotId && !slot) ? '' : status)
  }, [status, importOpen, slotId, slot])

  if (importOpen || !status || (slotId && !slot)) return null
  const line = (
    <LiveRegion
      role="status"
      atomic={true}
      className="cockpit-engine-status"
      data-error={context.session.errorKind ? 'true' : undefined}
      data-testid="cockpit-engine-status"
    >{message}</LiveRegion>
  )
  return slotId ? createPortal(line, slot) : line
}
