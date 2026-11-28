import { useEffect, useMemo, useRef, useState } from 'react'
import { subscribeBeforeLogout } from '../auth.js'
import { readActiveScope } from '../auth/activeScope.js'
import { listProjectPhotos } from '../projects/api.js'

const EMPTY_PAGE = { photos: [], cursors: [null], nextCursor: null }

export default function useProjectPhotos(projectId, scope) {
  const [page, setPage] = useState(EMPTY_PAGE)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const operation = useRef(null), lifetime = useRef(null)
  const generation = useMemo(() => ({}), [projectId, scope])
  useEffect(() => {
    lifetime.current = generation
    setPage(EMPTY_PAGE); setError('')
    const clear = () => {
      lifetime.current = null; operation.current?.abort(); operation.current = null
      setPage(EMPTY_PAGE); setLoading(false)
    }
    const logout = subscribeBeforeLogout(clear)
    return () => { clear(); logout() }
  }, [projectId, scope, generation])
  const readPage = async cursors => {
    if (!scope || !projectId || lifetime.current !== generation || readActiveScope() !== scope || operation.current) return
    const controller = new AbortController()
    operation.current = controller; setLoading(true); setError('')
    const current = () => lifetime.current === generation && !controller.signal.aborted && readActiveScope() === scope
    try {
      const result = await listProjectPhotos(projectId, {
        cursor: cursors.at(-1), signal: controller.signal, expectedScope: scope,
      })
      if (!current()) return
      if (!Array.isArray(result.materials)) throw new Error('Project photos could not be read. Reload photos.')
      const nextCursor = result.next_cursor
      if (nextCursor && cursors.includes(nextCursor)) throw new Error('Photo pagination failed. Reload photos.')
      setPage({ photos: result.materials, cursors, nextCursor })
    } catch (failure) { if (current()) setError(failure.message) }
    finally { if (operation.current === controller) { operation.current = null; setLoading(false) } }
  }
  return {
    photos: page.photos, error, loading, pageNumber: page.cursors.length, hasNext: Boolean(page.nextCursor),
    loadPhotos: () => readPage(EMPTY_PAGE.cursors),
    previousPage: () => { if (page.cursors.length > 1) return readPage(page.cursors.slice(0, -1)) },
    nextPage: () => { if (page.nextCursor) return readPage([...page.cursors, page.nextCursor]) },
  }
}
