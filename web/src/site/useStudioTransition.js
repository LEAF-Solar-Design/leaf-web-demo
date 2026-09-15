import { useCallback, useEffect, useRef, useState } from 'react'

export const STUDIO_MOTION = Object.freeze({ chromeOutMs: 80, chromeInMs: 140, groundMs: 180, easing: 'cubic-bezier(.2,0,0,1)' })

const prefersReducedMotion = () => typeof window !== 'undefined'
  && window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches === true

export function useLeavingGround(ground, { reducedMotion = prefersReducedMotion() } = {}) {
  const [snapshot, setSnapshot] = useState({ ground, leaving: null })
  if (snapshot.ground !== ground) {
    setSnapshot({ ground, leaving: reducedMotion ? null : snapshot.ground })
  }
  useEffect(() => {
    if (reducedMotion) {
      setSnapshot((value) => value.leaving === null ? value : { ...value, leaving: null })
      return undefined
    }
    if (!snapshot.leaving) return undefined
    const timer = setTimeout(() => setSnapshot((value) => ({ ...value, leaving: null })), STUDIO_MOTION.groundMs)
    return () => clearTimeout(timer)
  }, [ground, reducedMotion, snapshot.leaving])
  return reducedMotion ? null : snapshot.leaving
}

export function useStudioTransition({ committed, onCommit, isDrafting, reducedMotion }) {
  const [phase, setPhase] = useState('idle')
  const timer = useRef(null)
  const pending = useRef(null)
  const latest = useRef({ committed, onCommit, isDrafting, reducedMotion })
  latest.current = { committed, onCommit, isDrafting, reducedMotion }
  const cancel = useCallback(() => {
    clearTimeout(timer.current)
    timer.current = null
  }, [])
  useEffect(() => () => { cancel(); pending.current = null }, [cancel])

  const settle = useCallback(() => {
    cancel()
    const target = pending.current
    pending.current = null
    if (target !== null) {
      latest.current.onCommit(target)
      latest.current.committed = target
    }
    setPhase('idle')
  }, [cancel])

  const request = useCallback((target) => {
    cancel()
    pending.current = null
    setPhase('idle')
    const current = latest.current
    if (target === current.committed) return
    const reduced = current.reducedMotion ?? prefersReducedMotion()
    if (reduced || current.isDrafting(current.committed) === current.isDrafting(target)) {
      current.onCommit(target)
      latest.current.committed = target
    } else if (current.isDrafting(current.committed)) {
      pending.current = target
      setPhase('out')
      timer.current = setTimeout(settle, STUDIO_MOTION.chromeOutMs)
    } else {
      current.onCommit(target)
      latest.current.committed = target
      setPhase('in')
      timer.current = setTimeout(() => { timer.current = null; setPhase('idle') }, STUDIO_MOTION.chromeInMs)
    }
  }, [cancel, settle])
  return { phase, request, settle }
}
