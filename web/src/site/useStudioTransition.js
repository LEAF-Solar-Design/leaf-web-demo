import { useCallback, useEffect, useRef, useState } from 'react'
import { surfaceContract } from './productSurfaces.js'

// Drafting profiles share a canvas and swap its palette without a chrome fade.
export const studioChromeIdentity = (surface) => {
  const slots = surfaceContract(surface)
  return slots.chrome.cockpit ? 'drawing' : slots.toolbar.profile
}

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
  // An unrelated render, including flushSync, must not erase a commit React
  // has not applied yet. The parent's rendered value wins as soon as it changes.
  const committedRef = useRef(committed)
  const lastProp = useRef(committed)
  if (committed !== lastProp.current) {
    lastProp.current = committed
    committedRef.current = committed
  }
  const latest = useRef({ onCommit, isDrafting, reducedMotion })
  latest.current = { onCommit, isDrafting, reducedMotion }
  const exitPending = useCallback(() => pending.current !== null, [])
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
      committedRef.current = target
    }
    setPhase('idle')
  }, [cancel])

  const request = useCallback((target) => {
    cancel()
    pending.current = null
    setPhase('idle')
    const current = latest.current
    if (target === committedRef.current) return
    const reduced = current.reducedMotion ?? prefersReducedMotion()
    if (reduced || studioChromeIdentity(committedRef.current) === studioChromeIdentity(target)) {
      current.onCommit(target)
      committedRef.current = target
    } else if (current.isDrafting(committedRef.current) || !current.isDrafting(target)) {
      pending.current = target
      setPhase('out')
      const entering = !current.isDrafting(committedRef.current)
      timer.current = setTimeout(() => {
        settle()
        if (entering) {
          setPhase('in')
          timer.current = setTimeout(() => { timer.current = null; setPhase('idle') }, STUDIO_MOTION.chromeInMs)
        }
      }, STUDIO_MOTION.chromeOutMs)
    } else {
      current.onCommit(target)
      committedRef.current = target
      setPhase('in')
      timer.current = setTimeout(() => { timer.current = null; setPhase('idle') }, STUDIO_MOTION.chromeInMs)
    }
  }, [cancel, settle])
  return { phase, request, settle, exitPending }
}
