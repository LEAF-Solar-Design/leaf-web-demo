// useIosSurface — ONE live GET /api/ios-surface/status read for the open
// project/revision, kept inert behind the flag. Mirrors useProjectLifecycle's
// enabled-gate + generation-guard discipline: a stale response never overwrites
// a newer one, and a resolved fetch after unmount never sets state.
//
// `enabled` false (the flag is off, or no project/revision) keeps the hook
// completely inert: no fetch, contract stays null, and IosSurface renders its
// dormant placeholder. Nothing is cached — a project/revision change refetches
// live, and a failed/absent upstream yields null ("never-configured"), never a
// stale contract.
import { useEffect, useRef, useState } from 'react'

import { fetchIosSurfaceStatus } from './iosSurfaceStatus.js'

export default function useIosSurface(projectId, revision, { enabled = true } = {}) {
  const [contract, setContract] = useState(null)
  const generationRef = useRef(0)
  useEffect(() => () => { generationRef.current += 1 }, [])

  useEffect(() => {
    const generation = ++generationRef.current
    if (!enabled || !projectId || !revision) {
      setContract(null)
      return
    }
    // Clear before the live read so a project/revision switch never shows the
    // previous project's contract while the new one is in flight.
    setContract(null)
    fetchIosSurfaceStatus({ projectId, revision }).then((next) => {
      if (generationRef.current !== generation) return
      setContract(next)
    })
  }, [enabled, projectId, revision])

  return { contract }
}
