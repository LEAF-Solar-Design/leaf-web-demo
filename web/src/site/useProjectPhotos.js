import { useEffect, useMemo, useRef, useState } from 'react'
import { subscribeBeforeLogout } from '../auth.js'
import { readActiveScope } from '../auth/activeScope.js'
import { projectPortfolioPhotos } from './portfolioDocument.js'

export default function useProjectPhotos(projectId, scope) {
  const [photos, setPhotos] = useState([]), [error, setError] = useState(''), [loading, setLoading] = useState(false)
  const operation = useRef(null), lifetime = useRef(null)
  const generation = useMemo(() => ({}), [projectId, scope])
  useEffect(() => {
    lifetime.current = generation
    setPhotos([]); setError('')
    const clear = () => {
      lifetime.current = null; operation.current?.abort(); operation.current = null
      setPhotos([]); setLoading(false)
    }
    const logout = subscribeBeforeLogout(clear)
    return () => { clear(); logout() }
  }, [projectId, scope, generation])
  const loadPhotos = async () => {
    if (!scope || !projectId || lifetime.current !== generation || readActiveScope() !== scope || operation.current) return
    const controller = new AbortController()
    operation.current = controller; setLoading(true); setError('')
    const current = () => lifetime.current === generation && !controller.signal.aborted && readActiveScope() === scope
    try {
      const rows = await projectPortfolioPhotos(projectId, { signal: controller.signal, expectedScope: scope })
      if (current()) setPhotos(rows)
    } catch (failure) { if (current()) setError(failure.message) }
    finally { if (operation.current === controller) { operation.current = null; setLoading(false) } }
  }
  return { photos, error, loading, loadPhotos }
}
