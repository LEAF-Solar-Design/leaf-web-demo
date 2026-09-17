import { useLayoutEffect, useState } from 'react'
import { computeSafeRect } from './useDrawingViewport.js'

// THE WINDOW. On Browser and iOS the product frame (#product-surface-panel)
// is transparent chrome over the ground, like the workspace card over the
// drawing; its head, title, and project line stay at the top and the rest
// of the frame is the window the ground shows through. The ground lays its
// content INTO that window by measuring it — never by guessing the
// console's grid: the project line's height changes with state (a
// drawing-only state adds an explainer, an action, and a reason), the rails
// change width at 1200px, and the center column scrolls. ResizeObserver +
// scroll + resize keep it exact; jsdom and any measurement that yields no
// height fall back to the CSS-variable geometry in landing.css.
const WINDOW_GUTTER = 14
const MIN_WINDOW_HEIGHT = 160

export function measureGroundWindow(doc = document) {
  const panel = doc.getElementById('product-surface-panel')
  if (!panel) return null
  const frame = panel.getBoundingClientRect()
  if (!(frame.height > 0) || !(frame.width > 0)) return null
  // The chrome is everything above the window: the last of the project
  // line, the description, the title, or the head, whichever renders last.
  const chrome = panel.querySelector('.tc-product-project')
    || panel.querySelector('.tc-product-frame h1 + p, .tc-product-morph > p')
    || panel.querySelector('h1')
    || panel.querySelector('.tc-product-frame-head')
  const chromeBottom = chrome ? chrome.getBoundingClientRect().bottom : frame.top
  const top = chromeBottom + WINDOW_GUTTER
  const height = frame.bottom - WINDOW_GUTTER - top
  if (!(height >= MIN_WINDOW_HEIGHT)) return null
  const inset = 21 // the frame's own horizontal padding
  return {
    top: Math.round(top),
    left: Math.round(frame.left + inset),
    width: Math.round(frame.width - inset * 2),
    height: Math.round(height),
  }
}

export function measureContainedWindow(board, doc = document, occluders = []) {
  const origin = board?.getBoundingClientRect()
  if (!(origin?.width > 0) || !(origin?.height > 0)) return null
  return computeSafeRect(origin, occluders.map(([selector, edge, options]) => ({
    rect: doc.querySelector(selector)?.getBoundingClientRect(), edge, reserve: options?.reserve,
  })), { padding: 16 })
}

export const NO_OCCLUDERS = []

export function useGroundWindow(active, contained = false, boardRef, occluders = NO_OCCLUDERS) {
  const [rect, setRect] = useState(null)
  useLayoutEffect(() => {
    if (!active || typeof window === 'undefined') { setRect(null); return undefined }
    let frame = 0
    let observed = new Set()
    let observer = null
    const measure = () => {
      frame = 0
      const next = contained ? measureContainedWindow(boardRef.current, document, occluders) : measureGroundWindow()
      if (contained && observer) {
        const targets = new Set([boardRef.current, ...occluders.map(([selector]) => document.querySelector(selector))].filter(Boolean))
        for (const element of observed) if (!targets.has(element)) observer.unobserve(element)
        for (const element of targets) if (!observed.has(element)) observer.observe(element)
        observed = targets
      }
      setRect((prev) => {
        if (!next) return null
        if (prev && prev.top === next.top && prev.left === next.left
          && prev.width === next.width && prev.height === next.height) return prev
        return next
      })
    }
    const schedule = () => { if (!frame) frame = window.requestAnimationFrame(measure) }
    const panel = contained ? boardRef.current : document.getElementById('product-surface-panel')
    const scroller = document.querySelector('main.center-scroll')
    observer = (typeof ResizeObserver !== 'undefined' && panel) ? new ResizeObserver(schedule) : null
    if (!contained) {
      observer?.observe(panel)
      if (observer && scroller) observer.observe(scroller)
    }
    measure()
    const discovery = contained && typeof MutationObserver !== 'undefined' ? new MutationObserver((records) => {
      if (records.some((record) => [...record.addedNodes, ...record.removedNodes].some((node) =>
        node.nodeType === 1 && occluders.some(([selector]) => node.matches(selector) || node.querySelector(selector))))) schedule()
    }) : null
    discovery?.observe(document.body, { childList: true, subtree: true })
    window.addEventListener('resize', schedule)
    scroller?.addEventListener('scroll', schedule, { passive: true })
    return () => {
      if (frame) window.cancelAnimationFrame(frame)
      observer?.disconnect()
      discovery?.disconnect()
      window.removeEventListener('resize', schedule)
      scroller?.removeEventListener('scroll', schedule)
    }
  }, [active, contained, boardRef, occluders])
  return rect
}

export const windowStyle = (rect) => (rect
  ? { top: rect.top, left: rect.left, width: rect.width, height: rect.height, right: 'auto' }
  : undefined)

export function shortId(value, n = 8) {
  const text = String(value ?? '').trim()
  return text ? text.slice(0, n) : ''
}
