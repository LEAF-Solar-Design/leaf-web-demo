// useIosSurface — ONE point-in-time GET /api/ios-surface/status read per
// (project, revision), kept inert behind the flag. Mirrors useProjectLifecycle's
// enabled-gate + generation-guard discipline: a stale response never overwrites
// a newer one, and a resolved fetch after unmount never sets state.
//
// SCOPE — point-in-time, NOT a live poll. The last contract for the current
// (project, revision) is held in React state; a build's stage advancing (e.g.
// BUILT -> RECEIPT) is NOT reflected until the project/revision changes or the
// panel remounts. The sibling ios_ship lane polls on a timer; this consume-only
// glance deliberately does not. Each individual read IS live (iosSurfaceStatus
// never caches and never serves a prior success once the upstream fails), and
// `enabled` false (flag off, or no project/revision) keeps the hook fully inert:
// no fetch, contract null -> IosSurface renders its dormant placeholder.
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
