import { useEffect, useState } from 'react'

export function computeSafeRect(canvasRect, occluders, { padding = 16 } = {}) {
  if (!canvasRect) return null
  const { left: x, top: y, width, height } = canvasRect
  if (![x, y, width, height, padding].every(Number.isFinite) || width <= 0 || height <= 0) return null
  let left = x, top = y, right = x + width, bottom = y + height
  for (const { rect, edge, reserve } of occluders) {
    if (!rect || ![rect.left, rect.top, rect.width, rect.height].every(Number.isFinite)
      || rect.width <= 0 || rect.height <= 0) continue
    const r = rect.left + rect.width, b = rect.top + rect.height
    if (r <= x || b <= y || rect.left >= x + width || rect.top >= y + height) continue
    let side = edge
    if (side === 'nearest') {
      const cx = rect.left + rect.width / 2, cy = rect.top + rect.height / 2
      side = [
        ['top', Math.abs(cy - y)], ['bottom', Math.abs(y + height - cy)],
        ['left', Math.abs(cx - x)], ['right', Math.abs(x + width - cx)],
      ].sort((a, b) => a[1] - b[1])[0][0]
    }
    const extra = Number.isFinite(reserve) && reserve > 0 ? reserve : 0
    if (side === 'top') top = Math.max(top, b + extra)
    if (side === 'bottom') bottom = Math.min(bottom, rect.top - extra)
    if (side === 'left') left = Math.max(left, r + extra)
    if (side === 'right') right = Math.min(right, rect.left - extra)
  }
  left += padding; top += padding; right -= padding; bottom -= padding
  if (right <= left || bottom <= top) return null
  const result = { left: Math.round(left - x), top: Math.round(top - y), width: Math.round(right - left), height: Math.round(bottom - top) }
  return result.width > 0 && result.height > 0 ? result : null
}

export default function useDrawingViewport(root, occluderSpecs) {
  const [safe, setSafe] = useState(null)
  useEffect(() => {
    if (!root) { setSafe(null); return }
    const doc = root.ownerDocument
    const win = doc.defaultView
    let frame = null
    let observed = new Set()
    let scrollTargets = new Set()
    const schedule = () => {
      if (frame === null) frame = win.requestAnimationFrame(measure)
    }
    const observer = new win.ResizeObserver(schedule)
    function measure() {
      frame = null
      const canvas = root.querySelector('.viewer-canvas canvas')
      const elements = occluderSpecs.map(([selector, edge, options]) => ({ element: doc.querySelector(selector), edge, reserve: options?.reserve }))
      const next = computeSafeRect(canvas?.getBoundingClientRect(), elements.map(({ element, edge, reserve }) => ({ rect: element?.getBoundingClientRect(), edge, reserve })))
      setSafe((previous) => previous === next || (previous && next && ['left', 'top', 'width', 'height'].every((key) => previous[key] === next[key])) ? previous : next)
      const targets = new Set([canvas, ...elements.map(({ element }) => element)].filter(Boolean))
      for (const element of observed) if (!targets.has(element)) observer.unobserve(element)
      for (const element of targets) if (!observed.has(element)) observer.observe(element)
      observed = targets
      const nextScroll = new Set(['main.center-scroll', '.studio-shell'].map((selector) => doc.querySelector(selector)).filter(Boolean))
      for (const element of scrollTargets) if (!nextScroll.has(element)) element.removeEventListener('scroll', schedule)
      for (const element of nextScroll) if (!scrollTargets.has(element)) element.addEventListener('scroll', schedule, { passive: true })
      scrollTargets = nextScroll
    }
    // Discover the lazy canvas and chrome that mounts after the first measure.
    const discoverySelector = ['.viewer-canvas', '.viewer-canvas canvas', 'main.center-scroll', '.studio-shell', ...occluderSpecs.map(([selector]) => selector)].join(',')
    const mutations = new win.MutationObserver((records) => {
      if (records.some((record) => [...record.addedNodes, ...record.removedNodes].some((node) =>
        node.nodeType === 1 && (node.matches(discoverySelector) || node.querySelector(discoverySelector))))) schedule()
    })
    mutations.observe(doc.body, { childList: true, subtree: true })
    win.addEventListener('resize', schedule)
    schedule()
    return () => {
      if (frame !== null) win.cancelAnimationFrame(frame)
      observer.disconnect()
      mutations.disconnect()
      win.removeEventListener('resize', schedule)
      for (const element of scrollTargets) element.removeEventListener('scroll', schedule)
    }
  }, [root, occluderSpecs])
  return root ? safe : null
}
